# InferGrid

**The missing orchestration layer for LLM inference — intelligent multi-model serving without Kubernetes.**

InferGrid is a middleware that jointly optimizes scheduling, KV cache management, and GPU multi-tenancy for LLM inference. It sits on top of existing engines (vLLM, SGLang) and turns 1-4 GPUs into an intelligent inference platform — no Kubernetes, no datacenter infrastructure.

## The Problem

Current LLM serving wastes compute at three layers:

| Layer | Waste | Impact |
|-------|-------|--------|
| **Scheduling** | CPU-side overhead consumes >50% of inference time on fast models | Requests wait in queue while GPU sits idle between batches |
| **KV Cache** | No unified compression + eviction + offloading across memory tiers | 40GB of KV cache per request at 128K context — evicted and recomputed constantly |
| **Allocation** | GPUs allocated as indivisible units | A task needing 4GB gets an 80GB A100 (95% waste) |

**Compound loss: 81.6%** from physical GPU to effective compute.

## Why InferGrid

Every existing solution requires either Kubernetes or gives up intelligent scheduling:

| System | K8s Required | Multi-Model | KV Tiering | Intelligent Scheduling |
|--------|:------------:|:-----------:|:----------:|:---------------------:|
| NVIDIA Dynamo v1.0 | Yes | Yes | Yes | Yes |
| llm-d v0.5 (CNCF) | Yes | 1 model/pool | Yes | Yes |
| Mammoth (Modular) | Yes | Yes | Yes | Yes |
| AIBrix v0.6 | Yes | Yes | Yes | Yes |
| Ollama | No | LRU only | No | No |
| LocalAI | No | LRU only | No | No |
| **InferGrid** | **No** | **Yes** | **Yes** | **Yes** |

InferGrid is the only system that provides intelligent multi-model orchestration, KV cache tiering, and per-tenant isolation on bare metal without Kubernetes.

## Architecture

```
                    +---------------------+
                    |   WorkloadRouter    |  <-- request profiling, SLO-aware routing
                    +--------+------------+
                             |
              +--------------+--------------+
              |              |              |
     +--------v--------+ +--v---+ +--------v--------+
     |  CacheManager   | |Shared| | TenantManager   |
     | GPU -> CPU -> SSD| |State | |Resource budgets |
     +-----------------+ +------+ +-----------------+
              |                            |
     +--------v----------------------------v--------+
     |          vLLM / SGLang Engine                 |
     +----------------------------------------------+
```

**WorkloadRouter** — Length-aware, frequency-based model loading and request scheduling. Not LRU — learns from traffic patterns.

**CacheManager** — Tiered KV cache across GPU HBM, CPU RAM, and NVMe SSD. Evicts based on predicted reuse, not just recency.

**TenantManager** — Per-tenant resource budgets with request isolation. One tenant's burst doesn't starve others.

## Phase 1 Results: The Scheduling Cliff

Profiled on NVIDIA A100 80GB SXM and H100 80GB HBM3 with Llama 3.1 8B:

| Concurrency | Throughput (tok/s) | TTFT p50 (ms) | TPOT p50 (ms) | GPU Util % |
|:-----------:|:------------------:|:-------------:|:-------------:|:----------:|
| 1 | 86 | 22 | 11.5 | 99.3% |
| 8 | 663 | 41 | 11.8 | 98.6% |
| 32 | 2,014 | 68 | 13.7 | 95.4% |
| 64 | 3,196 | 96 | 16.6 | 99.3% |
| 128 | 5,014 | 319 | 19.1 | 97.4% |
| 256 | 5,121 | 2,608 | 19.0 | 98.5% |

**The scheduling cliff at c>128:** Doubling concurrency from 128 to 256 yields only **2% more throughput** but **8x worse TTFT** (319ms to 2,608ms). GPU utilization stays >95% — the bottleneck is scheduling, not compute. This is where InferGrid's WorkloadRouter intervenes.

**Tail latency is extreme:** TTFT p99/p50 ratio reaches 25x at c=8, meaning 1% of requests wait 25x longer than the median. Priority scheduling can dramatically reduce this.

## Quick Start

```bash
pip install -e ".[dev,profiling]"
```

## Running Benchmarks

### Cloud GPU (RunPod / Lambda Labs)

```bash
# Clone and set up
git clone https://github.com/coconut-labs/infergrid.git
cd infergrid

# Option A: Universal benchmark runner (one engine per run)
export HF_TOKEN="your_token"
export ENGINE="vllm"  # or "sglang"
export GPU_LABEL="a100-sxm"
bash scripts/cloud_benchmark.sh

# Option B: Full baseline collection
source scripts/setup_venv.sh
bash scripts/setup_gpu_env.sh --model-config configs/models/llama31_8b.yaml
bash scripts/run_all_baselines.sh --model-config configs/models/llama31_8b.yaml
```

### Docker (local multi-GPU)

```bash
docker compose -f docker/docker-compose.yml up
python benchmarks/scripts/run_baseline_comparison.py \
    --vllm-url http://localhost:8000 \
    --sglang-url http://localhost:8001 \
    --concurrency 1,8,32,64,128,256 \
    --workload all
```

### Tests

```bash
pytest tests/unit/ -v        # No GPU required
pytest tests/integration/ -v # Requires aiohttp
```

## Roadmap

| Phase | Status | Description |
|-------|--------|-------------|
| Phase 1 | Done | Profiling vLLM/SGLang scheduling overhead on A100/H100 |
| Phase 2 | In Progress | WorkloadRouter: frequency-based model management, priority scheduling |
| Phase 3 | Planned | CacheManager: GPU/CPU/SSD KV cache tiering |
| Phase 4 | Planned | TenantManager: multi-tenant isolation with resource budgets |
| Phase 5 | Planned | Integration benchmarks, arXiv preprint |

## Research

- [Inference Orchestration Gap Analysis](docs/inference_orchestration_gaps_report.md) — 7 verified gaps in the LLM inference landscape (April 2026)
- [Phase 1 Findings](docs/phase1_findings.md) — Scheduling overhead profiling results
- [Strategic Analysis](docs/strategic_analysis.md) — Paper vs product decision framework

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

MIT
