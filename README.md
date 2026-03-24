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

## GPU profiling setup (RunPod / Lambda Labs)

Always use a virtual environment on cloud GPU instances to avoid dependency conflicts:

```bash
git clone https://github.com/coconut-labs/infergrid.git
cd infergrid
export HF_TOKEN="your_token"
source scripts/setup_venv.sh
bash scripts/setup_gpu_env.sh --model-config configs/models/llama4_scout.yaml
nohup bash scripts/run_all_baselines.sh --model-config configs/models/llama4_scout.yaml --repeats 2 > run.log 2>&1 &
```

## Automated Baseline Collection (Recommended)

The fastest way to reproduce all results on a GPU instance:

```bash
# 1. Set up the GPU environment (idempotent, safe to re-run)
export HF_TOKEN=hf_...
bash scripts/setup_gpu_env.sh --model-config configs/models/llama31_8b.yaml

# 2. Run all phases (~70-100 min on A100)
bash scripts/run_all_baselines.sh --model-config configs/models/llama31_8b.yaml

# Or preview the plan first:
bash scripts/run_all_baselines.sh --model-config configs/models/llama31_8b.yaml --dry-run

# ---------------------------------------------------------------------------------
# For Llama 4 Scout MoE (Requires 4x A100 80GB):
# ---------------------------------------------------------------------------------
bash scripts/setup_gpu_env.sh --model-config configs/models/llama4_scout.yaml
bash scripts/run_all_baselines.sh --model-config configs/models/llama4_scout.yaml
```

This runs 5 phases: vLLM profiling → SGLang profiling → head-to-head comparison → py-spy flame graphs → package results tarball.

Use `--resume` to continue from the last checkpoint if interrupted:

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
    --concurrency 1,8,32,64,128,256 \
    --num-requests 200 \
    --workload sharegpt
```

Add `--profile-internal` to generate py-spy flame graphs (requires py-spy installed).

### 3. Profile SGLang scheduling overhead

```bash
python profiling/scripts/profile_sglang_scheduler.py \
    --base-url http://localhost:8001 \
    --model meta-llama/Llama-3.1-8B-Instruct \
    --concurrency 1,8,32,64,128,256 \
    --num-requests 200 \
    --workload sharegpt
```

### 4. Run head-to-head comparison

```bash
python benchmarks/scripts/run_baseline_comparison.py \
    --vllm-url http://localhost:8000 \
    --sglang-url http://localhost:8001 \
    --concurrency 1,8,32,64,128,256 \
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
