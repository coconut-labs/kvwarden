# InferGrid — Adaptive Inference Orchestration for LLM Serving

## Project Overview

InferGrid is a unified inference orchestration layer that addresses the 81.6% efficiency gap in LLM serving by jointly optimizing scheduling, KV cache management, and GPU multi-tenancy. It sits on top of existing engines (vLLM, SGLang) as a middleware layer, not a replacement.

This is a research project targeting an MLSys 2026 submission and arXiv preprint, with an open-source release designed to demonstrate production infrastructure skills.

See `research_roadmap.md` for the full research plan, paper structure, benchmarks, and timeline.

## Architecture

Three components communicating through a shared state object:

1. **WorkloadRouter** — Length-aware, model-aware, cost-aware request scheduler
   - Profiles each request at arrival (estimated output length, resource needs, SLO targets)
   - Routes to optimal execution group
   - Jointly optimizes across length, latency SLO, and available KV cache capacity
   - Key data structures: priority queue with multi-key sorting, request feature vector, execution group state

2. **CacheManager** — Tiered KV cache manager (GPU HBM → CPU DRAM → NVMe SSD)
   - Eviction policy: LRU-variant weighted by request priority, prefix shareability, reuse probability
   - Treats KV cache as managed resource pool (like PostgreSQL shared_buffers)
   - Async transfer between tiers without stalling inference

3. **TenantManager** — Safe multi-tenancy with software-level resource budgets
   - Per-tenant resource envelopes (GPU memory, compute share, KV cache allocation)
   - Dynamic rebalancing when tenants are idle
   - NVIDIA MIG/MPS integration + Kubernetes device plugin

**Closed-loop design:** WorkloadRouter consults CacheManager state. CacheManager consults TenantManager budgets. TenantManager consults WorkloadRouter demand patterns.

## Tech Stack

- **Languages:** Python (orchestration, policy logic), C++/CUDA (hot path, GPU ops)
- **Inference engines:** vLLM and SGLang (compatibility with both via plugin interfaces)
- **GPU tools:** NVIDIA Management Library (NVML), GPU Direct Storage (GDS)
- **Profiling:** NVIDIA Nsight, py-spy, custom instrumentation
- **Benchmarking:** ShareGPT dataset, custom synthetic workloads
- **Testing:** pytest, benchmark reproducibility via Docker Compose

## Current Phase: I_1.0.0.2 — Profiling & Baseline Reproduction

Profiling vLLM and SGLang scheduling overhead. Reproducing the published claim
that scheduling consumes >50% of inference time on fast models and that vLLM
trails SGLang by ~29% due to orchestration overhead.

Key outputs:
- Quantified scheduling overhead (flame graphs, cProfile data)
- Head-to-head benchmark: ShareGPT + synthetic workloads across concurrency sweep
- Identified intervention points for WorkloadRouter design
- Baseline metrics: throughput (tok/s), TTFT, TPOT, GPU utilization
- Analysis notebook with publication-quality visualizations
- Findings document: `docs/phase1_findings.md`

### Profiling scripts
- `profiling/scripts/profiling_utils.py` — shared infrastructure (GPU collector, request generator, async client)
- `profiling/scripts/profile_vllm_scheduler.py` — vLLM external + internal profiling
- `profiling/scripts/profile_sglang_scheduler.py` — SGLang external + internal profiling
- `benchmarks/scripts/run_baseline_comparison.py` — controlled head-to-head comparison

### Previous Phase: I_1.0.0.1 (Completed)
Project scaffolding — directory structure, pyproject.toml, docker-compose for
vLLM + SGLang, benchmark config stubs, package layout under src/infergrid/.

## Key Research Context

### The scheduling problem
- vLLM scheduling overhead >50% of total inference time on fast models (WukLab 2024)
- Even with identical FlashInfer kernels, vLLM trails SGLang by 29%
- Bottleneck shifted from GPU math to CPU scheduling
- CascadeInfer (Dec 2025) addresses length heterogeneity but only single-axis

### The KV cache problem
- 70B model at 128K context needs ~40GB KV cache per request
- PagedAttention (vLLM): eliminates fragmentation, fixed-size blocks waste space
- RadixAttention (SGLang): enables prefix sharing, requires entire tree in GPU memory
- No unified system: compression + eviction + offloading + sharing

### The multi-tenancy problem
- Kubernetes treats GPUs as indivisible integers
- 4GB workload gets 80GB A100 = 95% waste
- MIG/MPS exist but require manual config, no workload adaptation

## Baselines to Beat

- Vanilla vLLM (latest stable)
- Vanilla SGLang (latest stable)
- TensorRT-LLM
- CascadeInfer (length-aware scheduling only)

## Benchmark Suite

1. ShareGPT — real conversation workloads, variable length
2. Length-heterogeneous synthetic — mix of 1K, 8K, 32K, 128K context
3. Multi-tenant isolation — chat SLO vs batch SLO sharing GPUs
4. KV cache efficiency — shared system prompts, cache hit rates

## Code Style and Conventions

- Python: follow vLLM's style (type hints, docstrings, Black formatting)
- C++: follow LLVM style for CUDA extensions
- Every component must have unit tests
- Benchmarks must be reproducible via single Docker Compose command
- All experiments logged with hardware specs, software versions, configs
- Git commits: conventional commits (feat:, fix:, bench:, docs:)

## Paper Connections

This is Paper 1 of 2. Paper 2 (AgentOS) will reuse patterns from this project:
- WorkloadRouter → agent TaskScheduler
- CacheManager → agent MemoryManager (tiered: working/episodic/semantic)
- TenantManager → agent ResourceGovernor (token budgets, recursion limits)
- Observability → agent distributed tracing

Design with this reuse in mind — keep abstractions clean and separable.

## Important References

- WukLab scheduling overhead study: https://mlsys.wuklab.io/posts/scheduling_overhead/
- vLLM v0.6.0 blog (profiling data): https://blog.vllm.ai/2024/09/05/perf-update.html
- CascadeInfer paper: https://arxiv.org/abs/2512.19179
- SGLang paper: "Efficient Execution of Structured Language Model Programs"
- PagedAttention paper: "Efficient Memory Management for Large Language Model Serving with PagedAttention"
- GPU Efficiency Funnel (81.6% waste): https://aijourn.com/the-gpu-efficiency-funnel/
