# Research Roadmap: InferGrid

> **Status update (April 19, 2026):** Phase 1 profiling complete. Gate 0 (system bring-up) and Gate 1 (admission control hypothesis) shipped. **The Phase 1 "scheduling cliff" claim was measured with a broken TTFT path** (PR #28/#31 fixed it: pre-fix bench timed SSE first-frame RTT, not first non-empty token). Gate 1 on H100 SXM5 with honest TTFT did NOT reproduce the 7× cliff at c=128→c=256 — but Gate 1's bench wall (10s/cell) was too short to test the hypothesis under sustained cap pressure (Little's Law, see `results/gate1_20260419/GATE1_OUTCOME.md`). **Gate 1.5 (rerun with `--num-requests 4000`) is the next cliff-test**; Gate 2-lite (multi-model contention) is the next test of the actual InferGrid pitch. See `PROGRESS.md` "Strategic Plan" section for the 4-week roadmap.

## Revised Thesis: Middleware-Level Admission Control and Multi-Model Lifecycle for LLM Inference

### What the data showed (Phase 1, April 2026)

| Original Claim | Measured Reality | Status |
|----------------|-----------------|--------|
| vLLM trails SGLang by 29% | <5% throughput gap (engines converged on throughput; SGLang still 2.2× better TTFT at c=256) | **Refuted on throughput** |
| 81.6% compound GPU waste | GPU utilization 95-99% across all configs | **Refuted** |
| Middleware scheduling reduces TTFT 50-70% | HTTP proxy cannot modify engine-internal scheduling | **Refuted** |
| Scheduling cliff at high concurrency | Phase 1's "+2% throughput, +1434% TTFT" used the pre-#28 broken TTFT (timed SSE first-frame RTT, not first token). Gate 1 with honest TTFT on H100 SXM5: c=128→c=256 ratio = 1.77× (NOT 14.34×). | **Phase 1 cliff number invalid; Gate 1 ran short bench, Gate 1.5 (4000 req) is the next test** |
| SGLang better at saturation | SGLang 2.2× better TTFT at c=256 vs vLLM (Phase 1; not re-tested on honest TTFT yet) | **Confirmed (Phase 1, with TTFT caveat)** |
| Cliff is hardware-independent | Pattern needs re-validation post-Gate-1.5 | **Pending re-test** |

### Updated Problem Statement (post-Gate-1, 2026-04-19)

The Phase 1 "+1434% TTFT" cliff number was a measurement artifact (broken TTFT, fixed in PR #28/#31). Gate 1 with honest TTFT on H100 SXM5 saw a 1.77× ratio at c=128→c=256, not 14×. Whether the cliff is real *under sustained cap pressure* is the open question Gate 1.5 will answer (Gate 1's 10s/cell wall was Little's-Law under-powered; rerun with `--num-requests 4000` is queued).

Independent of the cliff question, no lightweight tool provides intelligent multi-model lifecycle management (load, evict, hot-swap based on traffic patterns) on bare metal without Kubernetes. Gate 2-lite tests this — the actual InferGrid differentiator that has never been benchmarked.

Separately, no lightweight tool provides intelligent multi-model lifecycle management (load, evict, hot-swap based on traffic patterns) on bare metal without Kubernetes.

### System Design (unchanged)

Three components, one runtime:

1. **WorkloadRouter** — Admission control + frequency-based model lifecycle (not LRU)
2. **CacheManager** — KV cache lifecycle tracking (metadata layer; planned LMCache integration for cross-tier offloading)
3. **TenantManager** — Per-tenant resource budgets with request isolation

### Revised Experiments

- Benchmark 1: Admission control ON vs OFF at scheduling cliff (c=128, c=256)
- Benchmark 2: Multi-model serving (InferGrid vs manual 2x vLLM vs Ollama)
- Benchmark 3: Model switch latency and eviction policy effectiveness
- Benchmark 4: InferGrid proxy overhead measurement

### Timeline (revised 2026-04-19)

| Phase | Activity | Status |
|-------|----------|--------|
| Phase 1 | Profile vLLM/SGLang on A100/H100 | **Complete** (TTFT numbers under-counted by ~30ms, see CORRECTIONS C2) |
| Phase 2 | Build WorkloadRouter + admission control + per-tenant budget | **Complete** |
| Gate 0 | First live GPU bring-up (system PASS) | **Complete** ($5.76, 2026-04-18) |
| Gate 0.5 | Bench harness resilience | **Complete** (local) |
| Gate 0.6 | Real-vLLM bench validation | **Complete** ($3.17, 2026-04-19) |
| Gate 1 | Admission ON vs OFF, single model, H100 | **Complete but under-powered** ($1, 2026-04-19, see GATE1_OUTCOME.md) |
| Gate 1.5 | Powered rerun (`--num-requests 4000`) | **Next** ($7-10) |
| Gate 2-lite | Multi-model contention (3-arm: InferGrid vs uvicorn vs round-robin) | Wk 2 ($8) |
| Launch | OSS launch (HN, Reddit) | **Tue 2026-05-12** |

### Target

Primary: arXiv preprint + open-source launch. Fallback: MLSys 2027 submission.

### Baselines to Beat

- Manual multi-instance vLLM (2x vLLM with --gpu-memory-utilization 0.4)
- Ollama multi-model serving (LRU eviction)
- Direct engine (vLLM/SGLang) without admission control

### Key References

- Phase 1 profiling data: `results/` directory
- Gap analysis: `docs/inference_orchestration_gaps_report.md`
- Strategic analysis: `docs/strategic_analysis.md`
