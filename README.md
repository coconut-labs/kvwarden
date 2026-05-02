# kvwarden

[![PyPI](https://img.shields.io/pypi/v/kvwarden.svg)](https://pypi.org/project/kvwarden/)
[![Python](https://img.shields.io/pypi/pyversions/kvwarden.svg)](https://pypi.org/project/kvwarden/)
[![CI](https://github.com/coconut-labs/kvwarden/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/coconut-labs/kvwarden/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Tenant-fair LLM inference on one GPU. Sits in front of vLLM/SGLang, rate-limits per tenant at admission, and keeps a quiet user fast while a noisy neighbor floods the same engine.

![Quiet-tenant TTFT under noisy-neighbor contention — solo 53.9 ms vs FIFO 1,585 ms vs token-bucket 61.5 ms on A100 + Llama-3.1-8B + vLLM 0.19.1](https://raw.githubusercontent.com/coconut-labs/kvwarden/main/docs/launch/figures/launch_hero_chart.png)

**Hero number.** A100-SXM4, Llama-3.1-8B, vLLM 0.19.1, two tenants sharing one engine, 300 s sustained:

| | Quiet user TTFT p99 |
|---|---:|
| Solo (no contention) | **53.9 ms** |
| FIFO under flooder (no rate-limit) | **1,585 ms** (29x starvation) |
| kvwarden token-bucket under flooder | **61.5 ms** (1.14x solo) |

Ten lines of YAML. No application code change. Raw artifacts: [`results/gate2_preprint_v3/`](results/gate2_preprint_v3/).

```bash
pip install kvwarden
```

## Quickstart

kvwarden does not bundle vLLM. Install an engine separately, then let kvwarden spawn and proxy it.

```bash
# 1. Install kvwarden + the engine.
pip install kvwarden
pip install vllm  # needs a GPU box; see vLLM docs for your CUDA stack

# 2. Start kvwarden. It launches vLLM as a subprocess per the model list
#    in the config and exposes one OpenAI-compatible endpoint.
kvwarden serve --config configs/quickstart_fairness.yaml

# 3. In another shell, wait for /health and send two tenants at the same model.
until curl -fs localhost:8000/health > /dev/null; do sleep 2; done

curl localhost:8000/v1/completions -H "X-Tenant-ID: noisy" \
  -d '{"model":"llama31-8b","prompt":"Hello","max_tokens":64,"stream":true}'
curl localhost:8000/v1/completions -H "X-Tenant-ID: quiet" \
  -d '{"model":"llama31-8b","prompt":"Hello","max_tokens":64,"stream":true}'

# 4. Watch the token bucket fire and the engine queue stay composed.
curl localhost:8000/metrics | grep -E "tenant_rejected|admission_queue_depth"
```

First call returns `503` until vLLM finishes loading (30-90 s on A100 for an 8B). The config at [`configs/quickstart_fairness.yaml`](configs/quickstart_fairness.yaml) is heavily commented; every knob traces back to a specific experiment.

## Docker Compose

A two-service compose bundle brings up vLLM + kvwarden with one command. Requires a Linux GPU host with the [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html); CPU-only and Apple Silicon hosts cannot run this end-to-end (vLLM needs CUDA).

```bash
export HF_TOKEN=hf_...                      # gated Llama-3.1-8B model
docker compose -f docker/docker-compose.yml up
# in another shell, once both services report healthy (~3 min cold):
curl localhost:8000/v1/completions -H 'X-Tenant-ID: quiet' \
  -d '{"model":"llama31-8b","prompt":"Hello","max_tokens":32}'
```

Compose pins `vllm/vllm-openai:v0.19.1` (the image the hero number was measured against) and serves the bundled [`docker/quickstart-compose.yaml`](docker/quickstart-compose.yaml) — same two-tenant token-bucket shape as `configs/quickstart_fairness.yaml`, port-pinned for the bundle.

## When it breaks

| Symptom | Likely cause | Fix |
|---|---|---|
| `kvwarden doctor` reports "SSL trust unavailable" | macOS stock Python venv missing CA roots | `pip install --upgrade certifi`, then re-run |
| `/health` returns 503 right after `kvwarden serve` | vLLM JIT-compiling the model (30-90 s for an 8B on A100) | Wait, then `until curl -fs localhost:8000/health; do sleep 2; done` |
| `/v1/completions` returns 401 | HuggingFace gated-model auth missing | `huggingface-cli login`, or `export HF_TOKEN=...` before `kvwarden serve` |
| Quiet tenant still starved under flooder | `rate_limit_rpm` too high in config | Drop the per-tenant `rate_limit_rpm` in your config; the hero uses 600 with `rate_limit_burst: 10` |
| All requests return 429 immediately | `rate_limit_burst` set tighter than your client's burst | Raise `rate_limit_burst` or unset it (default = `rate_limit_rpm`) |
| vLLM subprocess crashes with CUDA OOM | `gpu_memory_utilization` too high for the loaded model + KV cache | Drop the model's `gpu_memory_utilization` to 0.40 (hero default) and retry |
| `Address already in use` on port 8000 | Stale daemon or another local service | `kvwarden serve --port 8001`, or `lsof -i :8000` to find the holder |
| `ModuleNotFoundError: kvwarden` after `pip install kvwarden` | venv mismatch — installed into the wrong interpreter | `python -c "import kvwarden; print(kvwarden.__version__)"` to confirm; reinstall in the venv that runs `kvwarden` |
| Engine pre-load takes >5 min | First-time model download from HuggingFace | Pre-warm the cache: `huggingface-cli download <model_id>` before `kvwarden serve` |

If your symptom is not in the table, file an issue with the output of `kvwarden doctor` and `kvwarden serve --log-level DEBUG`.

## Is this for me?

| Tool | Orchestration | Tenant fairness | Engines | Target scale |
|---|---|---|---|---|
| NVIDIA Dynamo | Kubernetes | No | Multi | Datacenter |
| llm-d | Kubernetes | No | vLLM | Datacenter |
| Ollama | None | No | llama.cpp | Single-user |
| **kvwarden** | **None** | **Yes** | **vLLM + SGLang** | **Single-node, 2-8 tenants** |

kvwarden is the "shared GPU, a handful of tenants, no cluster" cell. If you already run Kubernetes or you're a single user on your own box, you probably want one of the others.

## How it works

A thin orchestration layer (~3,500 LOC src) sits between your app and vLLM/SGLang. On request arrival, a per-tenant token bucket decides admit-or-429 before the request reaches the engine queue. Admitted requests flow through a length-bucketed admission controller and a DRR priority scheduler, then out to the engine subprocess kvwarden manages. Engines never see tenant identity; kvwarden does, and that's the entire trick. Multi-model lifecycle (freq+recency eviction, hot-swap) lives at the same layer. The HTTP API is OpenAI-compatible so your client code doesn't change.

Deeper read and the component diagram live in [`docs/architecture/overview.md`](docs/architecture/overview.md).

## Reproduce the hero number

```bash
# Terminal A — start kvwarden on the hero config (vLLM 0.19.1, Llama-3.1-8B).
kvwarden serve --config configs/gate2_fairness_token_bucket.yaml --port 8000

# Terminal B — wait for /health, then run the 300-second bench.
until curl -fs localhost:8000/health > /dev/null; do sleep 5; done
kvwarden bench reproduce-hero --flavor=2tenant
```

Needs an A100-SXM4 80GB (or equivalent — 1xH100 works) with vLLM 0.19.1 installed. Runtime is about 300 s for the bench plus ~30 s preflight. Output goes to `./kvwarden-reproduce-<timestamp>/report.json` with your numbers side-by-side against the published reference so you can file an issue with concrete data if they diverge. Other flavors: `--flavor=n6`, `--flavor=n8`. Full doc: [`docs/reproduce_hero.md`](docs/reproduce_hero.md).

## Frontier coverage

The same admission mechanism holds on larger models. Gate 2.3 (70B dense, TP=4 on 4x H100) and Gate 2.4 (Mixtral-8x7B MoE, TP=2 on 2x H100) both land quiet-to-solo p50 ratios between 1.07x and 1.94x — the mechanism lives before the engine boundary, so sharding topology, expert routing, and attention shape don't change the fairness picture. Matrix, caveats, and raw bench pointers in [`docs/launch/frontier_coverage.md`](docs/launch/frontier_coverage.md). The 8B A100 run is the only one with a single-command reproduce path today; 70B and Mixtral wrappers are a roadmap item.

## About the name

kvwarden ships tenant-fair admission today. Tenant-aware KV cache eviction — the feature the name implies — is a scaffold in [`src/kvwarden/cache/manager.py`](src/kvwarden/cache/manager.py), not a shipping feature. The path to making the name true is tracked in [#103](https://github.com/coconut-labs/kvwarden/issues/103) and targets the 0.2 release. If you pip install 0.1.3 expecting KV-cache isolation today, you will not get it; you get admission-gate fairness, which is what the hero number measures.

## What's next

- [#102 T1: Distribution](https://github.com/coconut-labs/kvwarden/issues/102) — 10 onboarding installs in week 1
- [#103 T2: Name-truth](https://github.com/coconut-labs/kvwarden/issues/103) — tenant-aware KV eviction for 0.2
- [#104 T3: Moat](https://github.com/coconut-labs/kvwarden/issues/104) — vllm-project/production-stack router + LiteLLM adapter
- [#105 W1: Launch blockers](https://github.com/coconut-labs/kvwarden/issues/105) — pre-launch QA + day-0 ops

## Telemetry

kvwarden ships opt-in, anonymous install/usage telemetry. First interactive run prompts once; default is no; answer `n` or hit Enter and nothing is ever transmitted. Opt in and each command sends seven fields: a locally-minted uuid4 install ID, kvwarden version, Python major.minor, OS, bucketed GPU class, command name, and a unix timestamp. No prompts, model names, tenant IDs, or receiver-side IP capture. Toggle with `kvwarden telemetry off/on/status`; hard-disable with `export KVWARDEN_TELEMETRY=0`. Non-interactive sessions auto-opt-out. Worker source: [`telemetry-worker/`](telemetry-worker/). Full policy: [`docs/privacy/telemetry.md`](docs/privacy/telemetry.md).

## Tests

```bash
pytest tests/unit/        # 153 tests, no GPU needed, ~10 s
ruff check src/ tests/
ruff format --check src/ tests/
```

CI runs this matrix on Python 3.11 and 3.12; a red PR cannot merge.

## Honesty log

Every metric we under-counted and the fix is in [`results/CORRECTIONS.md`](results/CORRECTIONS.md). TTFT measurement was rebuilt mid-project after a shadow review caught the original harness timing SSE first-frame RTT instead of first non-empty token (C2/C5). The 8B hero numbers exclude a 10 s JIT warmup window per C7; all 29 post-warmup windows sit between 36 ms and 65 ms.

## Getting help

- File a bug: [GitHub Issues](https://github.com/coconut-labs/kvwarden/issues/new/choose). A `prometheus_dump.txt` plus `server.log` is worth more than a star.
- Questions + launch ops context: [`docs/ops/onboarding_playbook.md`](docs/ops/onboarding_playbook.md).
- Contributing: [`CONTRIBUTING.md`](CONTRIBUTING.md). Start with a [good first issue](https://github.com/coconut-labs/kvwarden/issues?q=is%3Aissue+is%3Aopen+label%3A%22good+first+issue%22).

## License

MIT. See [LICENSE](LICENSE).

## Cite as

```bibtex
@software{kvwarden_2026,
  title  = {kvwarden: tenant-fair LLM inference on a single GPU},
  author = {Patel, Shrey and {Coconut Labs contributors}},
  year   = {2026},
  version = {0.1.3},
  url    = {https://github.com/coconut-labs/kvwarden}
}
```
