# kvwarden — standing brief

Tenant-fair LLM inference orchestration in front of a single GPU. kvwarden spawns
and proxies a vLLM or SGLang subprocess; the engine never learns which tenant sent
what. All policy lives one layer above the engine boundary.

## Commands

```
pytest tests/unit/ -q        # 288 pass, ~13s, no GPU
ruff check src/ tests/
ruff format --check src/ tests/
```

Do NOT run `pytest tests/` or `pytest tests/integration/` locally. Eight
integration tests try to pull a gated HF tokenizer and 401 on this machine.
`profiling_utils.count_tokens` catches only `ImportError`, and `transformers`
is installed globally here, so the fallback never fires. CI has no
`transformers`, hits the fallback, and stays green. Known, out of scope.

CI (`.github/workflows/ci.yml`) runs unit + integration on py3.11/3.12/3.13,
`ruff check` + `ruff format --check` pinned to `ruff>=0.16,<0.17`, and `pip-audit`.

## Request path

`POST /v1/completions` → `WorkloadRouter.handle_request` reads `X-Tenant-ID` →
per-tenant token bucket (`TenantRecord.try_acquire`, `tenant/manager.py:73`)
admits or 429s → admission priority is composed (`router/router.py:458-476`)
from the tenant's DRR deficit and the request's length bucket → the
length-bucketed `AdmissionController.acquire(priority, timeout)`
(`router/admission.py:176`) → a per-bucket queue worker → the engine subprocess.

## Map

| Thing | Where |
|---|---|
| HTTP app + serve entry | `cli.py:294` — **aiohttp**, `web.Application`. Not FastAPI. |
| Token-bucket decision | `tenant/manager.py:73` `TenantRecord.try_acquire()`, reached via `TenantManager.try_acquire_for_tenant` (`manager.py:252`) from `router.py:449` |
| DRR priority score | `tenant/manager.py:114` `priority_score()` |
| Priority composition | `router/router.py:458-476` |
| Admission controller | `router/admission.py:58` |
| Background tasks | `WorkloadRouter.start()/stop()` (`router.py:232`/`273`) — asyncio tasks in `self._workers`, cancelled in `stop()`. There is no Starlette-style lifespan. |
| Prometheus registry | `common/metrics.py:36` `MetricsCollector._registry`. One registry: `AdmissionController` is handed `metrics._registry` at `router.py:195`, and `/metrics` serves `metrics.prometheus_output()` (`cli.py:303`). |
| Config | `common/config.py` — plain dataclasses, `KVWardenConfig.from_yaml` is `raw.get()` with defaults. **No validator exists.** Unknown top-level keys are silently dropped; unknown keys inside `models:` raise `TypeError` via `ModelConfig(**m)`. |
| Cache manager | `cache/manager.py`, instantiated at `cli.py:268`, passed to the router, exposes `snapshot()`. |

## Rules

- Never import vLLM or SGLang into the policy layer. The engine is reached over
  HTTP or as a subprocess, never as a library.
- No new runtime dependencies. Runtime deps are exactly: `certifi`, `pyyaml`,
  `prometheus-client`, `aiohttp`, `rich`. **`httpx` is not one of them** — it moved
  to the `bench` extra in v0.1.6. Use `aiohttp.ClientSession` for HTTP.
  `prometheus_client` is available, including its text parser.
- Flag-off behavior never changes. A new feature ships disabled by default and
  the existing code path must be untouched when it is off.
- The policy layer stays pure Python and testable with no GPU and no live engine.
  Tests go in `tests/unit/` with fakes.
- Published numbers are pinned to what they were measured on. Don't restate a
  benchmark claim without the config and engine version behind it.

## Status requests

"Status", "brief status", "where are we" and similar mean **engineering
state only**: what shipped, what's in flight, what's blocked, and known
technical debt in this repo. Answer from the section below plus `git log`
and `CHANGELOG.md`. Keep it under 20 lines.

Out of scope for a status answer: release/account operations, credentials,
CI billing, anything outside this repo's code and design. If one of those
is genuinely blocking engineering work, name the blocker in one line
without detail.

## Current state

**Shipped — v0.1.6, PyPI, 2026-08-08.** v0.1.x is tenant-aware *admission*:
per-tenant token bucket at the budget gate (`tenant/manager.py:73`), DRR
priority composition (`router/router.py:458-476`), length-bucketed
concurrency gate (`router/admission.py`). Measured on A100 + Llama-3.1-8B +
vLLM 0.19.1: quiet-tenant p99 TTFT 53.9 ms solo, 1,585 ms behind a flooder
under FIFO, 61.5 ms post-warmup with the token bucket (1.14× solo).
v0.1.6 itself was dependency hygiene — runtime deps 144 MB/27 packages →
33 MB/18, four advisories cleared, `pip-audit` added to CI.

**In flight — v0.2, RFC T2 cache-pressure admission.** Design is locked in
`docs/rfcs/T2-cache-pressure-admission.md`; no implementation yet. Poll the
engine's `vllm:kv_cache_usage_perc`, smooth it, and let it amplify the DRR
deficit in the priority composition. Identity at zero pressure, so v0.1
behavior is recovered exactly when the cache is cold.

**Blocked on:** an A100 probe run. The M4 attempt burned 512 pod requests
over four days without landing capacity, so the curve's watermarks are
still the RFC's placeholders rather than measured.

**Known technical debt, roughly by cost:**

1. `profiling/scripts/profiling_utils.py` `count_tokens` catches only
   `ImportError`. Any environment with `transformers` installed but no
   access to the gated Llama repo gets an uncaught 401 and 8 failing
   integration tests. The `except` is too narrow.
2. `KVWardenConfig.from_yaml` has no validation — unknown top-level keys
   are silently dropped, so a typo'd knob fails silently and looks like the
   feature doesn't work.
3. `CacheManager` is a shadow ledger; no engine adapter reads it. Known and
   deliberate — it's why T2 was reframed from eviction to admission — but
   the dead surface is still there.
4. No publish workflow. Releases are a manual local build and upload.
5. Branch protection on `main` requires `test (py3.11)`, `test (py3.12)`
   and `lint (ruff)`. The `test (py3.13)` and `audit (pip-audit)` jobs run
   but aren't required, so they can't block a merge.
6. #127 — the engine endpoint is effectively hardcoded to localhost because
   the Compose bundle shares a network namespace. Needs to be a real
   `engine_endpoint` field on `ModelConfig`.
