# Research Roadmap: InferGrid

## PAPER 1: Adaptive Inference Orchestration — Closing the 81% Efficiency Gap in LLM Serving

### The Problem

Current LLM inference systems waste compute at three distinct layers:

**Layer 1 — Scheduling overhead.** vLLM's CPU-side scheduling consumes >50% of total
inference time on fast models (WukLab, 2024). Even with identical compute kernels
(FlashInfer), vLLM trails SGLang by 29% purely due to orchestration overhead.

**Layer 2 — KV cache mismanagement.** A 70B model at 128K context requires ~40GB of KV
cache per request. No system provides unified compression, eviction, offloading across
GPU/CPU/SSD, and cross-request sharing as a single managed resource.

**Layer 3 — Static allocation.** GPU clusters allocate whole devices to single workloads.
Kubernetes treats GPUs as indivisible integers. An inference task needing 4GB gets an
80GB A100 (95% waste per allocation).

**The compound waste:** 81.6% total efficiency loss from physical GPU to effective compute.

### System Design

Three components, one runtime:

1. **WorkloadRouter** — Length-aware, model-aware, cost-aware request scheduler
2. **CacheManager** — Tiered KV cache (GPU HBM → CPU DRAM → NVMe SSD)
3. **TenantManager** — Safe multi-tenancy with software-level resource budgets

### Experiments

- Benchmark 1: ShareGPT (real conversation workloads)
- Benchmark 2: Length-heterogeneous synthetic workload
- Benchmark 3: Multi-tenant isolation
- Benchmark 4: KV cache efficiency

### Timeline

| Phase | Activity | Duration |
|-------|----------|----------|
| Phase 1 | Deep-dive vLLM/SGLang, reproduce baselines | Weeks 1-3 |
| Phase 2 | Build WorkloadRouter | Weeks 4-7 |
| Phase 3 | Build CacheManager | Weeks 8-11 |
| Phase 4 | Build TenantManager | Weeks 12-14 |
| Phase 5 | Integration, ablation, benchmarks | Weeks 15-17 |
| Phase 6 | Paper writing, polish, submit | Weeks 18-19 |

### Target Venue

Primary: MLSys 2026. Fallback: arXiv preprint + open-source release.

### Baselines to Beat

- Vanilla vLLM (latest stable)
- Vanilla SGLang (latest stable)
- TensorRT-LLM
- CascadeInfer (length-aware scheduling only)

### Key References

- WukLab scheduling overhead study
- vLLM v0.6.0 performance blog
- CascadeInfer paper (arXiv:2512.19179)
- SGLang paper
- PagedAttention paper
- GPU Efficiency Funnel (81.6% waste)
