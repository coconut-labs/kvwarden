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
python profiling/scripts/profile_vllm_scheduler.py --model meta-llama/Llama-3.1-8B-Instruct
```

## Benchmarks

```bash
docker compose -f docker/docker-compose.yml up vllm-server
python benchmarks/scripts/run_benchmark.py --config benchmarks/configs/sharegpt_baseline.yaml
```

## License

MIT
