"""Built-in help pages rendered by `kvwarden man [topic]`.

Each entry is a Markdown string. Keep them tight — this is terminal-rendered,
not the LP. Content here is the canonical in-CLI explanation of what each
command does + how to think about the tuning knobs.
"""

from __future__ import annotations

PAGES: dict[str, str] = {}


PAGES["overview"] = """
# KVWarden

**Tenant-fair LLM inference orchestration on a single GPU. No Kubernetes.**

Middleware that sits on top of vLLM / SGLang and keeps a quiet user's TTFT
predictable even when a noisy neighbor is hammering the same shared engine.

## The flow

```
client → router → tenant-manager → admission → engine (vLLM / SGLang)
                  (token bucket)    (queue)
```

Every request carries an `X-Tenant-ID` header. The tenant manager checks the
tenant's token bucket before the request reaches the engine; over-budget
requests get a 429, under-budget requests pass through. The quiet tenant
stays near solo TTFT even with a flooder hitting the same engine.

## Commands

- `kvwarden serve`  -- start the API server
- `kvwarden status` -- show loaded models, cache, tenant stats
- `kvwarden models` -- list available/loaded models
- `kvwarden doctor` -- environment + prerequisite check
- `kvwarden man`    -- open a help page in the terminal
- `kvwarden --version`

Run `kvwarden man <command>` for a detailed page on a specific command.

## Further reading

- Quickstart: `configs/quickstart_fairness.yaml`
- Tuning: https://github.com/coconut-labs/kvwarden/blob/main/docs/tuning_guide.md
- Empirical results: https://github.com/coconut-labs/kvwarden/tree/main/results
- Bug reports: https://github.com/coconut-labs/kvwarden/issues
"""


PAGES["serve"] = """
# kvwarden serve

Start the KVWarden API server. Listens on port 8080 by default, serves an
OpenAI-compatible HTTP surface, and spawns the configured engine
subprocess(es) (vLLM or SGLang).

## Two ways to configure

**YAML (recommended for anything non-trivial):**

```bash
kvwarden serve --config configs/quickstart_fairness.yaml
```

The YAML is where tenant budgets, multi-model lists, rate-limit knobs, and
engine-specific flags live. A sample config is shipped with the package
under `configs/`.

**CLI flags (quick sanity check with one model, symmetric tenants):**

```bash
kvwarden serve meta-llama/Llama-3.1-8B-Instruct --gpu-budget 80%
```

`--config` wins when both are supplied.

## Flags

- `MODELS...`         HuggingFace model IDs (repeatable). Ignored when `--config` is set.
- `--config PATH`     YAML config file. Overrides all other flags.
- `--gpu-budget PCT`  Fraction of GPU memory KVWarden may use. Default `80%`.
- `--engine {vllm|sglang}`  Default engine backend. Default `vllm`.
- `--port N`          HTTP port. Default `8080`.
- `--max-concurrent N`  Engine-side concurrent-request cap. Default `128`.
                        This is NOT the per-tenant fairness lever -- it's a
                        coarse upper bound upstream of the engine's own
                        scheduler. Per-tenant rate limits go in the YAML.
- `--log-level LEVEL` DEBUG | INFO | WARNING | ERROR. Default `INFO`.

## What happens on startup

1. Parse config (YAML or CLI flags).
2. Start the engine subprocess(es). First model takes ~30-90s on an 8B-class
   model to load weights.
3. `/health` returns 503 with `{"missing_models": [...]}` until all configured
   engines finish loading. Poll `/health` before sending traffic:

   ```bash
   until curl -fs localhost:8080/health > /dev/null; do sleep 2; done
   ```

4. Serve requests. Send `X-Tenant-ID: your-tenant-id` on each call.

## OpenAI-compatible routes

- `POST /v1/chat/completions`
- `POST /v1/completions`
- `GET  /v1/models`

## KVWarden-specific routes

- `GET /health`              -- 200 when all engines are loaded, 503 otherwise
- `GET /metrics`             -- Prometheus text exposition
- `GET /kvwarden/status`    -- JSON snapshot (what `kvwarden status` reads)
"""


PAGES["status"] = """
# kvwarden status

Print a JSON snapshot of the running server's internal state: loaded models,
cache usage per tier, per-tenant budgets + current utilization, engine health,
admission-queue depths.

```bash
kvwarden status
kvwarden status --port 8080
```

Use this for debugging; use `/metrics` for continuous monitoring.

## What's in the snapshot

- `models[]`       -- each model, its engine, load state, last-used timestamp
- `cache`          -- block counts per tier (GPU / CPU / SSD) + eviction stats
- `tenants[]`      -- each tenant, budget, tokens-in-bucket, requests-in-flight
- `admission`      -- queue depth per length bucket
- `engines[]`      -- health, circuit-breaker state, recent failure count

A non-200 response means the server isn't running on the port queried.
"""


PAGES["models"] = """
# kvwarden models

Show a table of models currently known to the running server.

```bash
kvwarden models
kvwarden models --port 8080
```

Column meaning:

- `ID`      -- HuggingFace model ID (or local path)
- `Engine`  -- which adapter serves it (`vllm`, `sglang`)
- `Healthy` -- last health check result for this model's engine process
"""


PAGES["doctor"] = """
# kvwarden doctor

Run a battery of local environment checks and print what needs fixing.

```bash
kvwarden doctor
```

## What it checks

- **Python version** (>= 3.11 required)
- **KVWarden version** vs. the latest on PyPI
- **Engine presence** -- is `vllm` importable? `sglang`?
- **GPU visibility** -- does `nvidia-smi` report a CUDA device?
- **Port availability** -- is the default 8080 free?
- **Config files** -- are the shipped sample configs reachable from CWD?

Doesn't mutate anything; flags issues and points at the fix.
"""


PAGES["tenants"] = """
# Tenant fairness -- the mental model

This is the mechanism that makes KVWarden different from running vLLM alone.

## The problem

Two tenants share one engine. Tenant A sends 32 requests/sec, Tenant B sends
1. vLLM's continuous-batch scheduler is tenant-blind by design: it sees a
stream of requests and tries to maximize throughput. Under contention,
Tenant B's p99 TTFT collapses from 53.9 ms (solo) to 1,585 ms -- a 29x
degradation. Tenant B experiences "noisy neighbor TTFT roulette."

## The fix

Per-tenant token-bucket rate limiting at the budget gate, **upstream** of
the engine. Each tenant has:

- `rate_limit_rpm`   sustained ceiling (refill rate * 60)
- `rate_limit_burst` how many requests may spike before throttle kicks in

Over-budget requests get 429'd at the gate before they reach the engine.
The engine never saturates; Tenant B stays near solo TTFT.

## The numbers

Single A100-SXM4 80GB, Llama-3.1-8B, vLLM 0.19.1, 300s sustained:

| Arm                              | Quiet p99 TTFT | vs. solo |
|----------------------------------|---------------:|---------:|
| Solo (no contention)             |       53.9 ms  |     1.0x |
| KVWarden FIFO (no rate-limit)   |      1,585 ms  |      29x |
| KVWarden + token bucket         |       61.5 ms  |    1.14x |

## Tuning

See the `tenants:` section of `configs/quickstart_fairness.yaml`. Start
with symmetric quotas; widen once you have a tenant with a legit reason
to burst.

Rule of thumb: `rate_limit_burst = rate_limit_rpm / 60` gives one second
of capacity. Smaller burst = faster throttle, longer burst = more grace.
"""


PAGES["quickstart"] = """
# Quickstart

## Install

```bash
pip install kvwarden
```

Python 3.11+ required. On Linux with an NVIDIA GPU, also:

```bash
pip install 'kvwarden[vllm]'   # or [sglang]
```

## Serve

```bash
kvwarden serve --config configs/quickstart_fairness.yaml
```

Shipped configs live under `configs/` in the source distribution. Copy one
and edit. `configs/quickstart_fairness.yaml` is heavily commented.

## Talk to it

```bash
until curl -fs localhost:8080/health > /dev/null; do sleep 2; done

curl localhost:8080/v1/completions \\
  -H "X-Tenant-ID: noisy" \\
  -d '{"model":"llama31-8b","prompt":"...","max_tokens":64,"stream":true}'

curl localhost:8080/v1/completions \\
  -H "X-Tenant-ID: quiet" \\
  -d '{"model":"llama31-8b","prompt":"...","max_tokens":64,"stream":true}'
```

## Watch the rate limit fire

```bash
curl localhost:8080/metrics | grep -E "tenant_rejected|admission_queue_depth"
```

## Next

Run `kvwarden man tenants` for the fairness mental model, or open
`docs/tuning_guide.md` in the repo for a deep treatment.
"""


def get_page(topic: str) -> str | None:
    """Return the markdown text for a topic, or None if unknown."""
    return PAGES.get(topic)


def list_topics() -> list[str]:
    """Return all available man topic names, sorted."""
    return sorted(PAGES.keys())
