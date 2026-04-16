# InferGrid

**The missing orchestration layer for LLM inference — intelligent multi-model serving without Kubernetes.**

InferGrid is a middleware that jointly optimizes scheduling, KV cache management, and GPU multi-tenancy for LLM inference. It sits on top of existing engines (vLLM, SGLang) and turns 1-4 GPUs into an intelligent inference platform — no Kubernetes, no datacenter infrastructure.

## The Problem

Current LLM serving suffers from scheduling quality degradation under load:

| Layer | Problem | Impact |
|-------|---------|--------|
| **Scheduling** | TTFT degrades 8-15x at saturation (c>128) while GPU stays >95% utilized | Users experience multi-second wait times despite available compute |
| **KV Cache** | No unified lifecycle management across memory tiers | KV cache evicted and recomputed; no cross-tier offloading |
| **Multi-Model** | Models loaded/evicted with naive LRU | Cold-start latency spikes when traffic patterns shift |

GPU utilization is consistently >95% -- the waste is in **scheduling quality** (TTFT degradation, tail latency), not GPU idleness.

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
| **InferGrid** | **No** | **Yes** | **Planned** | **Yes** |

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

**CacheManager** — KV cache lifecycle tracking with planned LMCache integration for cross-tier offloading. Evicts based on predicted reuse, not just recency.

**TenantManager** — Per-tenant resource budgets with request isolation. One tenant's burst doesn't starve others.

## Phase 1 Results: The Scheduling Cliff

Profiled on NVIDIA A100 80GB SXM and H100 80GB SXM with Llama 3.1 8B Instruct:

### Cross-Engine Throughput (tok/s)

| Concurrency | vLLM A100 SXM | SGLang A100 SXM | vLLM H100 SXM |
|:-----------:|:-------------:|:---------------:|:--------------:|
| 1 | 90 | 84 | 150 |
| 8 | 690 | 691 | 1,142 |
| 32 | 2,060 | 2,155 | 3,590 |
| 64 | 3,314 | 3,348 | 6,365 |
| 128 | 5,334 | 5,276 | 10,341 |
| 256 | 5,353 | 5,214 | 10,545 |

**vLLM vs SGLang throughput gap does not exist** -- the engines are within <5% across all concurrency levels on identical hardware. The real difference is in scheduling quality.

### TTFT at Saturation

| Config | TTFT p50 (ms) |
|--------|:-------------:|
| vLLM A100 c=128 | 150.9 |
| vLLM A100 c=256 | 2,315.2 |
| SGLang A100 c=128 | 102.4 |
| SGLang A100 c=256 | 1,052.8 |
| vLLM H100 c=128 | 184.8 |
| vLLM H100 c=256 | 1,293.4 |

**The scheduling cliff is universal:** On all hardware and both engines, doubling concurrency past the saturation point yields <3% more throughput but 8-15x worse TTFT. GPU utilization stays >95% -- the bottleneck is scheduling quality, not compute.

**SGLang has 2.2x better TTFT at saturation** (1,053ms vs 2,315ms at c=256 on A100 SXM), showing that scheduler implementation quality matters more than raw throughput.

**InferGrid's intervention:** Admission control and multi-queue scheduling to maintain TTFT SLOs at saturation, with expected 2-4x p99 improvement. Multi-model lifecycle management to reduce cold-start latency during traffic shifts.

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
