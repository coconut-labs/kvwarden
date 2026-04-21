# Research Roadmap: KVWarden

> **Status update (April 21, 2026):** the scheduling-cliff thesis has been *robustly DISCONFIRMED* (Gate 1.5, H100 SXM5, 16k requests/arm — see [`results/gate1_5_20260419/GATE1_5_OUTCOME.md`](results/gate1_5_20260419/GATE1_5_OUTCOME.md)). The project has pivoted to a **tenant-fairness hero**: quiet-tenant TTFT p99 under a 32-RPS flooder drops from 1,585 ms (FIFO) to 61.5 ms (token-bucket rate-limit), within 1.14× of the 53.9 ms solo baseline — CONFIRMED at N=2 (Gate 2-FAIRNESS v3) and N=6 (Track C). See the [launch post](docs/launch/gate0_launch_post.md) for the full story.
>
> **Post-launch gate ladder** (queued for execution at ~$63 of a $72 RunPod budget, configs already on `main`): Gate 2.1 (N=8 scaling), Gate 2.4 (Mixtral-8×7B MoE, TP=2), Gate 2.3 (Llama-3.1-70B, TP=4). Gate 2.2 (mixed prompt-length distribution) is blocked on a prerequisite harness PR. Gate 2.5 (32K long-context + memory-pressure fairness) defers to v0.3 when [LMCache](https://github.com/LMCache/LMCache) lands in the hot path. See the [post-launch tracking issue](https://github.com/coconut-labs/kvwarden/issues/69) for the full backlog.
>
> The sections below are **pre-pivot** (April 19) and are kept for provenance. Read the launch post and the tracking issue first; use this file only for the historical claim-ledger.

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

Independent of the cliff question, no lightweight tool provides intelligent multi-model lifecycle management (load, evict, hot-swap based on traffic patterns) on bare metal without Kubernetes. Gate 2-lite tests this — the actual KVWarden differentiator that has never been benchmarked.

Separately, no lightweight tool provides intelligent multi-model lifecycle management (load, evict, hot-swap based on traffic patterns) on bare metal without Kubernetes.

### System Design (unchanged)

Three components, one runtime:

1. **WorkloadRouter** — Admission control + frequency-based model lifecycle (not LRU)
2. **CacheManager** — KV cache lifecycle tracking (metadata layer; planned LMCache integration for cross-tier offloading)
3. **TenantManager** — Per-tenant resource budgets with request isolation

### Revised Experiments

- Benchmark 1: Admission control ON vs OFF at scheduling cliff (c=128, c=256)
- Benchmark 2: Multi-model serving (KVWarden vs manual 2x vLLM vs Ollama)
- Benchmark 3: Model switch latency and eviction policy effectiveness
- Benchmark 4: KVWarden proxy overhead measurement

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
| Gate 2-lite | Multi-model contention (3-arm: KVWarden vs uvicorn vs round-robin) | Wk 2 ($8) |
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
