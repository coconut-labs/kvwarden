# InferGrid

**Adaptive inference orchestration for LLM serving — closing the 81% efficiency gap.**

InferGrid is a unified orchestration layer that jointly optimizes scheduling, KV cache management, and GPU multi-tenancy for LLM inference. It sits on top of existing engines (vLLM, SGLang) as middleware.

## The Problem

Current LLM serving wastes compute at three layers:
- **Scheduling:** CPU-side overhead consumes >50% of inference time on fast models
- **KV Cache:** No unified compression + eviction + offloading + sharing
- **Allocation:** GPUs allocated as indivisible units (95% waste per allocation)

Compound loss: **81.6%** from physical GPU to effective compute.

## Architecture

```
                    ┌─────────────────────┐
                    │   WorkloadRouter    │ ← request profiling, SLO-aware routing
                    └────────┬────────────┘
                             │
              ┌──────────────┼──────────────┐
              │              │              │
     ┌────────▼───────┐ ┌───▼────┐ ┌───────▼────────┐
     │  CacheManager  │ │ Shared │ │ TenantManager  │
     │ GPU→CPU→SSD    │ │ State  │ │ Resource budgets│
     └────────────────┘ └────────┘ └────────────────┘
              │                            │
     ┌────────▼────────────────────────────▼────────┐
     │          vLLM / SGLang Engine                │
     └──────────────────────────────────────────────┘
```

## Status

Phase 1: Profiling & Baselines — Reproducing scheduling overhead findings

## Quick Start

```bash
pip install -e ".[dev,profiling]"
```

## Reproducing Baselines

### 1. Start inference engines

```bash
docker compose -f docker/docker-compose.yml up
```

This starts both vLLM (port 8000) and SGLang (port 8001) servers.

### 2. Profile vLLM scheduling overhead

```bash
python profiling/scripts/profile_vllm_scheduler.py \
    --base-url http://localhost:8000 \
    --model meta-llama/Llama-3.1-8B-Instruct \
    --concurrency 1,8,32,64,128 \
    --num-requests 200 \
    --workload sharegpt
```

Add `--profile-internal` to generate py-spy flame graphs (requires py-spy installed).

### 3. Profile SGLang scheduling overhead

```bash
python profiling/scripts/profile_sglang_scheduler.py \
    --base-url http://localhost:8001 \
    --model meta-llama/Llama-3.1-8B-Instruct \
    --concurrency 1,8,32,64,128 \
    --num-requests 200 \
    --workload sharegpt
```

### 4. Run head-to-head comparison

```bash
python benchmarks/scripts/run_baseline_comparison.py \
    --vllm-url http://localhost:8000 \
    --sglang-url http://localhost:8001 \
    --concurrency 1,10,50,100 \
    --workload all
```

Results are saved to `benchmarks/results/baseline/`.

### 5. View analysis

```bash
jupyter notebook profiling/analysis/scheduling_overhead_analysis.ipynb
```

### Running tests

```bash
# Unit tests (no GPU/server required)
pytest tests/unit/ -v

# Integration tests (requires aiohttp)
pytest tests/integration/ -v
```

## License

MIT
