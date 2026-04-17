# InferGrid — Project Progress

**Last updated:** April 17, 2026
**Repository:** [coconut-labs/infergrid](https://github.com/coconut-labs/infergrid)
**Author:** Shrey Patel (patelshrey77@gmail.com)

---

## Project Summary

InferGrid is a middleware orchestration layer for LLM inference that sits on top of vLLM and SGLang. It provides intelligent multi-model serving, admission control, and per-tenant isolation on bare metal — no Kubernetes required.

**New thesis (validated by data):** "InferGrid keeps inference engines below their scheduling cliff while managing multi-model lifecycle on bare metal — no Kubernetes, no cluster, one pip install."

---

## Codebase Metrics

| Metric | Count |
|--------|:-----:|
| Source code (src/infergrid/) | 2,872 lines across 18 files |
| Tests (tests/) | 2,276 lines across 7 files |
| Infrastructure (profiling, scripts, benchmarks) | 6,774 lines |
| Documentation (docs/) | 871 lines |
| Profiling data points | 416,019 rows across 879 files |
| Unit tests | 121 passing |
| GPU configurations profiled | 3 (vLLM A100, SGLang A100, vLLM H100) |
| PRs merged | 12 |

---

## Git History (Chronological)

### Phase 0: Project Scaffolding (Feb 2026)

| Commit | Description |
|--------|-------------|
| `3a1d62b` | Initial commit |
| `e32009c` | Scaffold InferGrid project structure — directories for router, cache, tenant, engines |
| `fa23f02` | Add profiling harness, benchmark scripts, and analysis for Phase I |
| `8ef797b` | Add .gitignore for Python artifacts |
| `9d7f906` | Merge PR #1: Initial project setup |
| `83b1f4f` | Merge PR #2: Phase 1 profiling infrastructure |

**State after Phase 0:** Profiling scripts (profiling_utils.py 891 LOC, profile_vllm_scheduler.py 645 LOC, profile_sglang_scheduler.py 577 LOC, run_baseline_comparison.py 527 LOC), unit tests, integration tests. Core src/infergrid/ was empty scaffolding (0 LOC).

### Phase 1: Cloud Hardening + GPU Profiling (Mar–Apr 2026)

| Commit | Description |
|--------|-------------|
| `e7c258d` | Pre-GPU fixes: repeats flag, real tokenizer, version pins |
| `f3f26f3` | Multi-model benchmarking support across configs |
| `90c7ca7` | Venv isolation for GPU runs — eliminates dependency conflicts |
| `53404c7` | Harden cloud runner: stale server kill, dep preflight, SGLang graceful skip |

**State after:** Profiling scripts production-ready. Ready to run on RunPod/Lambda Labs.

### Phase 2: Gap Analysis + Strategic Research (Apr 16, 2026)

| Commit | Description |
|--------|-------------|
| `7bcc67b` | Gap analysis report, verification overlay, strategic analysis, RunPod provisioner |

**Deliverables:**
- `docs/inference_orchestration_gaps_report.md` — 7 verified gaps in inference orchestration landscape
- `docs/gap_analysis_verification_april2026.md` — claim-by-claim verification against 30+ live sources (GitHub, NVIDIA docs, CNCF, press releases)
- `docs/strategic_analysis.md` — paper-vs-product decision framework
- `scripts/provision_runpod.py` — automated RunPod A100 provisioning

**Key findings from gap analysis:**
- Gap #2 (lightweight multi-model without K8s) confirmed wide open
- Gimlet Labs further along than expected ($92M, eight-figure revenues)
- Dynamo v1.0 cache pinning still experimental
- vLLM multi-model per GPU (Issue #13633) still unimplemented

### Phase 3: GPU Profiling on RunPod (Apr 16, 2026)

| Commit | Description |
|--------|-------------|
| `557285d` | Fix deprecated huggingface-cli and torch/vLLM ABI mismatch |
| `e0f440c` | Update venv path and unpin vLLM version |
| `01cb525` | Allow running without venv on ephemeral cloud instances |
| `49529e2` | Update vLLM CLI flag for v0.19.0 compatibility |
| `efb0dc4` | **Phase 1 profiling results — vLLM on A100 80GB** |
| `8636a01` | Add universal cloud benchmark runner for any engine container |

**GPU runs completed:**
1. vLLM on A100 SXM 80GB (Llama 3.1 8B) — 6 concurrency levels, 200 req each, 2 repeats
2. SGLang on A100 SXM 80GB (Llama 3.1 8B) — same config, separate pod
3. vLLM on H100 SXM 80GB (Llama 3.1 8B) — cross-generation comparison

**Key profiling results:**

| Engine/GPU | c=1 tok/s | c=128 tok/s | c=256 tok/s | c=256 TTFT |
|------------|:---------:|:-----------:|:-----------:|:----------:|
| vLLM A100 | 90 | 5,334 | 5,353 | 2,315ms |
| SGLang A100 | 84 | 5,276 | 5,214 | 1,053ms |
| vLLM H100 | 150 | 10,341 | 10,545 | 1,293ms |

**Thesis validation:**

| Original Claim | Data Result | Status |
|----------------|-------------|--------|
| vLLM trails SGLang by 29% | <5% throughput gap | **Refuted** — engines converged |
| 81% GPU waste | 95-99% GPU utilization | **Refuted** — GPU is busy, waste is in scheduling quality |
| 50% TTFT reduction via middleware | HTTP proxy cannot modify engine internals | **Refuted** — admission control (2-4x p99) is viable mechanism |
| Scheduling cliff at high concurrency | c=128→c=256: +2% throughput, +1434% TTFT | **Confirmed** |
| Cliff is hardware-independent | Same pattern on A100 and H100 | **Confirmed** |
| SGLang better TTFT at saturation | SGLang 2.2x better at c=256 | **Confirmed (new finding)** |

**RunPod cost:** ~$18 for 3 runs.

### Phase 4: Core Implementation (Apr 16, 2026)

| Commit | Description |
|--------|-------------|
| `9c3e3a2` | **Implement InferGrid core — WorkloadRouter, CacheManager, TenantManager, CLI** (3,453 LOC) |

**Components built:**

| Component | File | LOC | What It Does |
|-----------|------|:---:|-------------|
| WorkloadRouter | `src/infergrid/router/router.py` | 655 | Multi-queue length-bucketed scheduling, frequency+recency model lifecycle, priority request routing, OpenAI-compatible API |
| AdmissionController | `src/infergrid/router/admission.py` | 309 | Concurrency cap, priority queue, Prometheus metrics, <1ms fast-path overhead |
| CacheManager | `src/infergrid/cache/manager.py` | 487 | KV cache block tracking across GPU/CPU/SSD tiers, weighted eviction (not LRU), hit rate tracking |
| TenantManager | `src/infergrid/tenant/manager.py` | 267 | Per-tenant budgets, sliding-window rate limiting, concurrency isolation, usage tracking |
| Engine Adapters | `src/infergrid/engines/` | 277 | Abstract base class + vLLM/SGLang subprocess management, health checks, HTTP proxying |
| CLI | `src/infergrid/cli.py` | 277 | `infergrid serve`, `infergrid status`, `infergrid models` |
| Config | `src/infergrid/common/config.py` | 156 | YAML + CLI configuration, model configs |
| Metrics | `src/infergrid/common/metrics.py` | 207 | Prometheus counters, histograms, gauges |

**Design principles:**
- OpenAI-compatible HTTP API throughout
- Single-node, no Kubernetes
- Engines managed as subprocesses
- Async (asyncio + aiohttp)
- Type hints on all public APIs

### Phase 5: Bug Fixes + Validation (Apr 16, 2026)

| Commit | Description |
|--------|-------------|
| `c3ffc15` | Fix TOCTOU race, deprecated asyncio APIs, streaming session leak, port allocation |
| `dea7839` | Add admission controller with priority queuing (1,257 LOC) |
| `de68313` | Deduplicate engine adapters (453→113 lines, -199 net) |

**Bugs fixed:**
- TOCTOU race in TenantManager semaphore acquisition
- Deprecated `asyncio.get_event_loop()` calls in router and adapters
- Streaming session leak in vLLM/SGLang adapters
- Port allocation without availability check

**Test results:** 121 unit tests, all passing (9 seconds on M1 Mac).

### Phase 6: Documentation + Polish (Apr 16, 2026)

| Commit | Description |
|--------|-------------|
| `b233405` | Community standards: CODE_OF_CONDUCT, CONTRIBUTING, SECURITY, issue/PR templates |
| `709ff20` | Benchmark visualization charts (5 PNGs from profiling data) |
| `0a6aa65` | Investor pitch and 2-minute demo script |
| `32a3350` | Multi-model benchmark harness (1,868 LOC) |
| `a2fc39f` | Update research roadmap — deprecate refuted claims |

**Documents produced:**
- `README.md` — investor-facing with competitive landscape table and profiling results
- `docs/pitch.md` — one-page investor summary with 12-week funding ask
- `docs/demo_script.md` — 2-minute demo screenplay (5 scenes)
- `docs/inference_orchestration_gaps_report.md` — publishable landscape analysis
- `docs/phase1_findings.md` — full profiling results with analysis
- `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`, `SECURITY.md`
- `.github/ISSUE_TEMPLATE/` (bug report + feature request)
- `.github/PULL_REQUEST_TEMPLATE.md`

**Charts generated:**
- `docs/figures/throughput_vs_concurrency.png` — scheduling cliff visualization
- `docs/figures/ttft_vs_concurrency.png` — TTFT degradation at saturation
- `docs/figures/tpot_vs_concurrency.png` — stable decode latency
- `docs/figures/scheduling_cliff_detail.png` — throughput gain vs TTFT cost
- `docs/figures/engine_convergence.png` — vLLM-SGLang parity proof

---

## Code Review Summary

**Audit completed:** April 16, 2026. Zero dead code, zero unused imports, zero AI slop.

| Severity | Issues Found | Status |
|----------|:------------:|--------|
| HIGH | 2 (adapter duplication, silent exceptions) | Fixed (PR #9) |
| MEDIUM | 3 (semaphore access, broad catch, CLI logging) | 2 fixed, 1 remaining |
| LOW | 2 (tautological docstrings, loose type hints) | Noted |

**Branch protection:** Main requires 1 approving review. All PRs reviewed before merge.

---

## Competitive Positioning (Verified April 2026)

| System | K8s | Multi-Model | KV Tiering | Admission Control | Hardware |
|--------|:---:|:-----------:|:----------:|:-----------------:|----------|
| Dynamo v1.0 | Yes | Yes | Yes | No | NVIDIA only |
| llm-d v0.5 | Yes | 1/pool | Yes | No | NVIDIA (+WIP) |
| Mammoth | Yes | Yes | Yes | No | NVIDIA, AMD |
| AIBrix v0.6 | Yes | Yes | Distributed | No | NVIDIA |
| Ollama | No | LRU | No | No | Multi |
| **InferGrid** | **No** | **Frequency+recency** | **Planned** | **Yes** | **Via engines** |

---

## What's Next

### Immediate (Gate 0: ~$4.50)
- [ ] Provision 1x A100 SXM on RunPod
- [ ] First-ever GPU execution of `infergrid serve` with 2 models
- [ ] Verify multi-model demo harness works end-to-end

### Gate 1 (~$15)
- [ ] 1x H100: admission control TTFT reduction benchmark
- [ ] 3-model eviction comparison (frequency vs LRU)
- [ ] Multi-model concurrent throughput on H100

### Gate 2 (~$24)
- [ ] 2x H100: Llama 70B (TP=2, bf16 original quants)
- [ ] Heterogeneous serving: 70B + 8B on same cluster

### Gate 3 (~$16.50 buffer)
- [ ] Ollama comparison benchmark
- [ ] Re-runs if data is noisy
- [ ] Optional: 405B on 4x H100

### After GPU benchmarks
- [ ] arXiv preprint draft (scheduling cliff + admission control + multi-model)
- [ ] Demo video recording
- [ ] HN/Reddit launch
- [ ] Community signal collection → decide: fundraise or academic path

---

## PR History

| PR | Title | Status | Date |
|----|-------|--------|------|
| #1 | Initial project setup | Merged | Feb 27, 2026 |
| #2 | Phase 1 profiling infrastructure | Merged | Feb 27, 2026 |
| #3 | Harden cloud runner | Merged | Mar 24, 2026 |
| #4 | Gap analysis, verification, strategic analysis | Merged | Apr 16, 2026 |
| #5 | Community standards + investor README | Merged | Apr 16, 2026 |
| #6 | Core implementation (WorkloadRouter, CacheManager, TenantManager, CLI) | Merged | Apr 16, 2026 |
| #7 | Admission controller | Merged | Apr 16, 2026 |
| #8 | Bug fixes, test validation, corrected claims | Merged | Apr 16, 2026 |
| #9 | Deduplicate adapters, fix code review items | Merged | Apr 16, 2026 |
| #10 | Investor pitch + demo script | Merged | Apr 16, 2026 |
| #11 | Multi-model benchmark harness | Merged | Apr 16, 2026 |
| #12 | Update research roadmap with Phase 1 findings | Merged | Apr 17, 2026 |
