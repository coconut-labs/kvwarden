# kvwarden — standing brief

Tenant-fair LLM inference orchestration in front of a single GPU. kvwarden spawns
and proxies a vLLM or SGLang subprocess; the engine never learns which tenant sent
what. All policy lives one layer above the engine boundary.

## Commands

```
pytest tests/unit/ -q        # 273 pass + 10 xfail, ~13s, no GPU
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
