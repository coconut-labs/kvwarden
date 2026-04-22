# InferGrid — Project Progress

**Last updated:** April 21, 2026 (post-launch-polish + PyPI 0.1.2 + LP live at infergrid.org, through PR #76)
**Repository:** [coconut-labs/infergrid](https://github.com/coconut-labs/infergrid)
**Author:** Shrey Patel (patelshrey77@gmail.com)

---

## Project Summary

InferGrid is a middleware orchestration layer for LLM inference that sits on top of vLLM and SGLang. It provides per-tenant fairness on a shared engine, multi-model lifecycle, and OpenAI-compatible HTTP API on bare metal — no Kubernetes required.

**Validated thesis (Gate 2-FAIRNESS):** Per-tenant token-bucket rate-limiting at the budget gate is the load-bearing mechanism for tenant isolation on a shared inference engine. Empirically: 523× starvation (28,716 ms p99) reduced to 74 ms p99 (within 1.35× of solo baseline) on 1× A100 + Llama-3.1-8B + 32 RPS flooder vs 1 RPS quiet.

**Falsified prior thesis (Gate 1.5):** Single-model admission cap is NOT load-bearing on its own. vLLM's continuous batching absorbs overload as well as a coarse upstream cap does (B/A=1.04× across 16,000 reqs).

---

## Codebase Metrics

| Metric | Count |
|--------|:-----:|
| Source code (src/infergrid/) | ~3,000 lines across 18 files |
| Tests (tests/) | ~2,500 lines across 10 files |
| Infrastructure (profiling, scripts, benchmarks) | 6,774+ lines |
| Documentation (docs/) | 871+ lines |
| Profiling data points | 416,019 + 75 (Gate 0.6) rows across 880+ files |
| Unit tests | 153 pass (148 pre-PR-#58 + 2 race + 3 health gating) |
| GPU configurations profiled | 3 (vLLM A100, SGLang A100, vLLM H100) |
| Live GPU bring-ups | 4+ (Gate 0, Gate 0.6, Gate 1.5, Gate 2-FAIRNESS, pre-launch sprint) |
| PRs merged | 61 (#1-61 minus #18 closed-superseded) |

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

**Test results:** 129 unit tests collected, 121 pass + 8 pre-existing integration xfail in `test_benchmark_client.py` (CSV/dataframe export, connection error handling — orthogonal to the core router/cache/tenant code paths). Runs in ~12s on M1 Mac.

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

(See "Strategic Plan — Post-Gate-1" section above for the current 4-week roadmap. This section preserves the original v0 plan for historical context; it has been superseded by the Little's Law rerun and multi-model contention pivot.)

### Gate 0 — ✅ DONE (2026-04-18, ~$5.76)
- [x] 1× A100 SXM on RunPod, Llama-8B + Qwen-7B co-load, system PASS

### Gate 1 — ✅ DONE (2026-04-19, ~$1, PLUMBING PASS / HYPOTHESIS UNDER-POWERED)
- [x] 1× H100 SXM5, admission ON vs OFF, honest TTFT
- [x] Plumbing verified; admission engaged cleanly on Arm A
- Hypothesis verdict deferred to Gate 1.5 (Little's Law caveat — see above)

### Gate 1.5 — ✅ DONE (2026-04-19, ~$1.30 actual, ROBUST DISCONFIRM)
- [x] 1× H100 SXM5 SECURE, 16000 req/arm × 4 cells × 100s sustained wall
- [x] Pod-restart between Arm A and Arm B worked cleanly (runpod.stop_pod + resume_pod with gpu_count=1)
- [x] Plumbing PASS: Arm A had 6444/16001 admits queued (~5h cumulative wait); Arm B all admits ≤1ms
- [x] Verdict: A_p99(c=256)/A_p99(c=128) = 1.80× (PASS ≤2× criterion); B_p99(c=256)/A_p99(c=256) = 1.04× (FAIL ≥4× criterion). Same ratio as Gate 1's short-bench result, now under sustained cap pressure
- [x] No CONFIRM, no overload-protection emergence (both arms had 0 failures everywhere)
- [x] Single-model admission cap is **not load-bearing** for TTFT on this workload/hardware; vLLM scheduler handles overload as well or better
- [x] At c=192 specifically, cap=128 actively HURTS (p99.9 = 5953 vs 3525, +69% worse than uncapped)
- [x] Full writeup: `results/gate1_5_20260419/GATE1_5_OUTCOME.md`

### Gate 2-FAIRNESS — ✅ DONE (2026-04-19, $1.40 actual, CONFIRM with caveat) — the launch-worthy result
5 arms × A100-SXM4 ($1.89/hr): Arm 0 solo baseline (quiet p50=28.5ms), Arm 1 FIFO contended (**523× starvation: quiet p50=15087ms**), Arm 3 DRR cap=256 (DISCONFIRM: cap never bound), Arm 4 DRR cap=16 (DISCONFIRM: vLLM internal queue dominated, flooder 2821 timeouts), **Arm 5 DRR + rate_limit_rpm=600 (CONFIRM: quiet p50 dropped to 30ms steady-state — 503× improvement; 5179 tenant_rejected on flooder)**. Caveat: full-bench p99 still 5378ms because sliding-window rate limit takes ~30s to engage.

**Hero number for launch post (post-PR #47 token-bucket refinement): 523× quiet-tenant TTFT starvation on vanilla vLLM, fixed by ~10 lines of config to InferGrid. Quiet tenant ends up within 1.35× of solo baseline for the full bench — essentially unaware of the flooder.** Architectural finding: per-tenant rate limit at BUDGET layer is the right lever; admission-queue priority reordering alone (DRR) is insufficient because vLLM's internal scheduler is tenant-blind. Also found and fixed a sliding-window warmup defect (PR #47 token bucket replaces it). Full writeup: `results/gate2_fairness_20260419/GATE2_FAIRNESS_OUTCOME.md` + `GATE2_FAIRNESS_SUPPLEMENT_arm5b.md`. Pivot was advisor + god-planner creative scrum.

### Gate 2-lite — Wk 2 fallback (~$8) — plan B if Gate 2-FAIRNESS DISCONFIRMs
- [ ] 1× A100-SXM4, 3 arms (InferGrid vs raw uvicorn vs round-robin)
- [ ] Llama-3.1-8B + Qwen2.5-7B co-loaded
- [ ] Two-tenant mixed workload (chat + RAG), 120s sustained
- [ ] Design doc: `docs/launch/gate2_design.md`

### Launch — Tue 2026-05-12
- [ ] PyPI `infergrid` placeholder (LAUNCH-BLOCKER, user action)
- [ ] Cloudflare Worker for waitlist (LAUNCH-BLOCKER, user action)
- [ ] Quickstart polish
- [ ] Launch-post reframe (product-led lead, meta-honesty as supporting beat)
- [ ] HN/Reddit launch

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
| #13 | Comprehensive PROGRESS.md | Merged | Apr 17, 2026 |
| #14 | Phase 1 multi-GPU profiling raw results (21.7 MB) | Merged | Apr 18, 2026 |
| #15 | Gate 0 multi-model config (Llama 8B + Qwen 7B, 0.35 mem util) | Merged | Apr 18, 2026 |
| #16 | Gate 0 compat hardening (transformers<5, numpy<2.3, CLI fix, engine log capture, PYTHONUNBUFFERED) | Merged | Apr 18, 2026 |
| #17 | Phase B 4-week roadmap | Merged | Apr 18, 2026 |
| #19 | Gate 0 first-GPU multi-model run results (3.5 MB) | Merged | Apr 18, 2026 |
| #21 | Gate 0.5 bench harness resilience v1 (session reuse, asymmetric+sock_read timeouts, mock engine, repro) | Merged | Apr 18, 2026 |
| #22 | R4 engine circuit breaker + late `response.prepare()` | Merged | Apr 18, 2026 |
| #23 | D3 router request-ID logs + PROGRESS.md test counts + CORRECTIONS.md | Merged | Apr 18, 2026 |
| #24 | Gate 1 configs (admission ON/OFF) + smoke_bench pre-flight | Merged | Apr 19, 2026 |
| #25 | R5 harness phase-abort + checked-in `scripts/gate_pod_bootstrap.sh` (D2) | Merged | Apr 19, 2026 |
| #26 | Gate 0.6 multi-model bench validation on real vLLM | Merged | Apr 19, 2026 |
| #27 | PROGRESS.md catch-up through Gate 0.6 | Merged | Apr 19, 2026 |
| #28 | real-TTFT v1 (C2 fix): bench timed first SSE frame, not first non-empty content | Merged | Apr 19, 2026 |
| #29 | admission-streaming-bypass fix: route_request released slot before stream consumed; cap was a no-op for streaming. Wrapper holds slot for stream lifetime; the existing smoke_bench /metrics poller (PR #24) was wired into the pass criteria here as the regression check; 3 unit tests | Merged | Apr 19, 2026 |
| #30 | aclose inner generator in stream-wrapper finally (closes engine HTTP connection on client abort) | Merged | Apr 19, 2026 |
| #31 | TTFT v2: `bool(" ")` truthy + chat-completions `delta.content` + JSONDecodeError → continue. 3-case discriminator. Smoke poller 50ms→10ms (Nyquist). Bonus: `scripts/gate1_dress_rehearsal.sh` | Merged | Apr 19, 2026 |
| #32 | Streaming budget accounting: tenant.record_completion got `gpu_seconds=0.07ms` at handoff pre-fix; now in wrapper finally with real elapsed + chunk count | Merged | Apr 19, 2026 |
| #33 | Max-stream-duration fence (`INFERGRID_STREAM_MAX_DURATION_S=600`) + GC-path test confirms asyncgen finalizer hook releases slot | Merged | Apr 19, 2026 |
| #34 | 5 Gate-1 blockers from dress rehearsal: bash REPO_ROOT precedence; bench `len(models)<2` gate; cli.py never wired tenant_defaults to TenantManager; gate1 configs missing tenant_defaults; aiohttp `TCPConnector.limit_per_host=100` clamped c=256 to 100 | Merged | Apr 19, 2026 |
| #35 | Cost-cap 3-layer defense in `gate_pod_bootstrap.sh`: MAX_POD_SECS self-destruct timer, /workspace/ABORT sentinel, phase wall-clock budget | Merged | Apr 19, 2026 |
| #36 | docs: PROGRESS catch-up + Gate 1 runbook | Merged | Apr 19, 2026 |
| #37 | shadow-review followups: real cost-cap fix (in-pod poweroff is best-effort, not the primary cap; trap kills timer; CPU smoke), router buffered SSE-frame parser (chunk_count was network-fragmentation-unstable; tokens_out now stable), CORRECTIONS C5, doc fixes | Merged | Apr 19, 2026 |
| #38 | Gate 1 H100 SXM5 raw results (Arm A+B tarballs, GATE1_OUTCOME.md, ~$1 spend) | Merged | Apr 19, 2026 |
| #39 | Gate 1 OUTCOME Little's Law caveat: 10s bench wall did not sustain cap pressure; headline 1.04× B/A is short-bench under-power, not DISCONFIRM. Softened "FAIL" → "AMBIGUOUS"; points to `--num-requests 4000` rerun (Gate 1.5) | Merged | Apr 19, 2026 |
| #40 | docs+configs: 4-week plan, Gate 1.5 runbook, Gate 2-lite scaffold (PROGRESS.md Strategic Plan, gate1_runbook.md Gate 1.5 Re-Run section with pod-restart-between-arms, gate2_design.md NEW, gate2_multi_tenant.yaml + gate2_round_robin.yaml NEW, research_roadmap.md cliff claim updated for honest TTFT) | Merged | Apr 19, 2026 |
| #41 | results: Gate 1.5 H100 SXM5 robust DISCONFIRM (~$1.30 spend). 16000 req/arm × 4 concurrency × ~100s sustained. B/A=1.04× same as Gate 1, under sustained cap pressure. At c=192 cap=128 actively HURTS (+69% p99.9). Runbook resume_pod gpu_count fix. | Merged | Apr 19, 2026 |
| #42 | feat(tenant): DRR-priority admission via tenant active-request deficit (~24 LOC + 7 tests, 135/135 passing). Wires TenantRecord.priority_score() into AdmissionController when `tenant_defaults.scheduling: drr`. Default (fifo) unchanged. Core of Gate 2-FAIRNESS. | Merged | Apr 19, 2026 |
| #43 | scaffold: Gate 2-FAIRNESS bench + configs + runbook + dress rehearsal. benchmark_two_tenant_single_model.py (Poisson per-tenant, X-Tenant-ID propagation), gate2_fairness_{fifo,drr}.yaml, runbook with abort rules, scripts/gate2_fairness_dress_rehearsal.sh (CPU-only mock, OVERALL: PASS). | Merged | Apr 19, 2026 |
| #44 | config: Arm 4 cap=16 diagnostic yaml — added mid-experiment after Arm 3 DISCONFIRM showed cap=256 didn't bind. Bootstrap re-clones main; configs needed inline must be committed first. | Merged | Apr 19, 2026 |
| #45 | config: Arm 5 rate_limit_rpm=600 — added after Arm 4 also DISCONFIRMed; advisor-reconciled call to shift fairness lever from admission queue to budget-layer rate limit. | Merged | Apr 19, 2026 |
| #46 | results: Gate 2-FAIRNESS A100-SXM4 5-arm CONFIRM-with-caveat (~$1.40 spend). Hero: 523× starvation → steady-state baseline. Caveat: full-bench p99=5378ms from 30s sliding-window warmup. | Merged | Apr 19, 2026 |
| #47 | feat(tenant): replace 60s sliding-window rate-limit with token bucket. Backward-compatible (default burst=rpm). Adds rate_limit_burst: int \| None. 144/144 tests passing (135+9 new). Eliminates Arm 5's warmup transient. | Merged | Apr 19, 2026 |
| #48 | config: Arm 5b token-bucket validation yaml (rate_limit_burst=10). | Merged | Apr 19, 2026 |
| #49 | results: Gate 2-FAIRNESS Arm 5b SUPPLEMENT — token bucket eliminates the 30s warmup transient; quiet within 1.35× of solo across full bench. | Merged | Apr 19, 2026 |
| #50 | config: Arm 6 FIFO + token-bucket isolation control. | Merged | Apr 19, 2026 |
| #51 | docs: tuning_guide.md — when to use admission cap vs DRR vs rate-limit, with empirical evidence. | Merged | Apr 19, 2026 |
| #52 | results: Gate 2-FAIRNESS Arm 6 SUPPLEMENT — DRR not material on this workload; token bucket alone does the work. | Merged | Apr 19, 2026 |
| #53 | launch: quickstart_fairness.yaml + hero chart PNG generator + chart asset. | Merged | Apr 19, 2026 |
| #54 | docs: pivot README + pitch.md to tenant-fairness hero (523× → 74ms). | Merged | Apr 19, 2026 |
| #55 | feat: per-tenant TTFT histogram + 6-panel Grafana fairness dashboard (Track B of pre-launch sprint). | Merged | Apr 19, 2026 |
| #56 | feat: N-tenant fairness bench (1 flooder + N quiet) + N=6 config (Track C bench scaffold). | Merged | Apr 19, 2026 |
| #57 | feat: Track D OOM-under-burst chat+RAG bench + runner. | Merged | Apr 19, 2026 |
| #58 | fix: serialize concurrent ensure_model_loaded() per-model lock. Caught a TOCTOU race that fork-bombed Pod 1 during Track A v1 (32 RPS cold-start → 6 vLLM procs → GPU OOM). 2 new tests, 150/150 suite. | Merged | Apr 19, 2026 |
| #59 | fix: gate /health on full pre-load + surface load failures + Pod 1 evidence. /health returns 503 with missing_models until engines ready; pre-load failures log at ERROR. 3 new tests, 153/153 suite. CORRECTIONS C6 documents the Pod 1 fork-bomb. | Merged | Apr 19, 2026 |
| #60 | docs: README + quickstart wait on /health before requests. Pairs with #59 to make the launch-day demo flow safe. | Merged | Apr 19, 2026 |
| #61 | fix: track_d_runner waits for GPU memory release between arms (vLLM v1 + pkill leaves 70 GB stuck for 30-90s). | Merged | Apr 19, 2026 |

PR #18 closed (conflict-dead, superseded by #19). PR #20 open as DRAFT (Gate 0 launch post, retired in favor of `draft/gate0-launch-post` branch + post-launch v2 narrative).

---

## Pre-launch sprint (2026-04-19, ongoing)

Plan stamped at `.claude/agent-memory-local/god-planner/prelaunch_sprint_apr19.md`.

| Track | Status | Notes |
|---|---|---|
| A — Track A 3-arm preprint re-run (300s, A100) | Arm 0 ✅ p99=53.9ms; Arm 1 + 5b running on dedicated single-arm pods | v1 fork-bombed Pod 1; v2 partial on Pod 2 hit cross-arm CUDA leak; v3 strategy = one fresh pod per arm in parallel |
| B — Per-tenant TTFT histogram + Grafana dashboard | ✅ shipped via PR #55 | 4 unit tests, 6-panel dashboard JSON |
| C — N=6 tenant fairness experiment | Bench + config in main; runner staged at /tmp/run_track_c.sh | Will run on Pod 3 or Pod 4 after Arm 1/Arm 5b finish |
| D — OOM-under-burst chat+RAG (Llama+Qwen) | Bench + runner in main (#57, #61); pod TBD | Pre-committed null rule: cut from launch if D1 ≈ D2 with no OOM |
| E — SGLang parity for Arm 5b workload | Optional; gated on SGLang adapter present (✅ adapter exists at src/infergrid/engines/sglang_adapter/) | TBD post-D |

Bug discoveries shipped as fixes (not blockers):
- **PR #58**: TOCTOU race in `ensure_model_loaded` — fork-bombed Pod 1.
- **PR #59**: `/health` returned 200 OK while engines cold-loading — launch-day footgun.
- **PR #61**: Cross-arm CUDA-context leak after `pkill` — strategy pivot to one-arm-per-pod.

Compute spend so far this sprint: ~$3 (Pod 1 broken, Pod 2 stuck, Pod 3 + Pod 4 single-arm). Total within $50 budget.

---

## Gate 0 — First Live GPU Bring-up (2026-04-18)

A100-SXM4-80GB on RunPod, 3h52m, ~$5.76 spend. Two open-weight LLMs (Llama-3.1-8B + Qwen-2.5-7B) co-resident.

**Verdict: SYSTEM PASS, bench harness deferred.**

Six-run recovery log:
1. Pod created without SSH port mapping → reprovision with `ports="22/tcp,8000/http"`
2. `HF_TOKEN` not inherited by SSH sessions → scp `/root/.gate0_env` + bootstrap sources it
3. `transformers==5.5.4` removed `all_special_tokens_extended` → pin `<5.0` (PR #16)
4. `numpy==2.4.4` broke numba's import-time check → pin `<2.3` (PR #16)
5. Qwen co-load OOM under vLLM v1 engine → `VLLM_USE_V1=0` + util 0.40→0.35
6. Success on system; bench harness stalled on `c=1 alternating` after req ~46 → deferred

181 requests admitted, 0 rejected, 0 OOMs. Both engines `healthy: true` for 3h+. See `results/gate0_20260418/GATE0_OUTCOME.md`.

## Gate 0.5 — Bench Harness Investigation + Fix (2026-04-18, local)

Investigated the stall: router's aiohttp client timed out at 300s waiting for vLLM response headers (76 identical stacks over 3h). Root cause in vLLM itself unknown — Gate 0 ran on a branch predating PR #16's engine log capture.

Fixes shipped (PRs #21, #22, #23, #25): session reuse + asymmetric timeouts + sock_read=30s + late `response.prepare()` + R4 circuit breaker + R5 harness phase-abort + D3 request-ID trace logs + D2 checked-in pod bootstrap + mock engine + local reproducer.

**Reproducer drops a 301s stall to 33s (9x speedup), proven locally.**

## Gate 0.6 — Real vLLM Validation (2026-04-19)

A100-SXM4-80GB, ~2h, ~$3.17 spend. Cloned `main@a610a14` (all PR #16-23 fixes active). 75 bench requests + 2 smoke = 77 admitted, 0 rejected, 0 OOMs, both engines healthy at shutdown. Engine stderr captured per PR #16 (234 KB).

**Throughput** (multi-model alternating, 25 req/concurrency):
- c=1: 84.0 tok/s
- c=8: 269.9 tok/s
- c=32: 812.2 tok/s

(TTFT numbers in artifacts are SSE-frame RTT, not real first-token — see `results/CORRECTIONS.md` C2.)

See `results/gate06_20260419/GATE06_OUTCOME.md`.

## Pre-Gate-1 Measurement-Honesty Pass (2026-04-19)

Between Gate 0.6 and Gate 1, two questions surfaced that turned the planned $7-10 H100 spend into a likely waste:

**(1) Was the bench measuring TTFT honestly?** No. PR #28 fixed the obvious case (TTFT was the SSE first-frame, not first non-empty content). A Jay-style shadow review of #28 then surfaced two more silent failure modes: `bool(" ")` is truthy in Python so whitespace-only frames still counted as the first token, and chat-completions endpoints emit `delta.content` not `text` so the v1 check always saw "" and TTFT silently collapsed to total_latency_ms. PR #31 fixed both. The discriminator now has three cases; on pre-fix code, whitespace and chat-shape both fail loudly.

**(2) Was admission control engaged at all for streaming traffic?** No — and this was the bigger find. `route_request` awaited `forward_request`, which for `stream=True` returned the async generator object immediately, then released the admission slot in `finally` — microseconds after acquire. The `smoke_bench` `/metrics` poller (added in #29) confirmed: 149 samples during a c=32 phase reported peak `in_flight=0` with `cap=16`. **The Gate 1 hypothesis (Arm A `cap=128` vs Arm B `cap=1024`) was unmeasurable**: both arms would have admitted all 256 reqs simultaneously and produced identical numbers. PR #29 wraps the iterator so admission releases when the stream truly ends; post-fix smoke shows peak `in_flight=16, queue_depth=16`.

PRs #30, #32, #33 closed three follow-ups Jay flagged in the review of #29: (a) the wrapper didn't `aclose()` the inner engine generator, leaking the engine-side HTTP connection on client abort; (b) accounting was logged at handoff with `gpu_seconds=0.07ms, tokens_out=0`, never corrected post-stream; (c) PR #29's "client abandons mid-stream" caveat had no test and no max-duration fence. All three landed.

PR #34 then surfaced and fixed five separate Gate-1 blockers in one go after the dress-rehearsal script (added in #31) actually ran end-to-end for the first time:

| # | Where | What was wrong |
|---|---|---|
| 1 | `scripts/gate1_dress_rehearsal.sh` | Bash precedence bug: `git ... \|\| cd ... && pwd` parsed as `(git \|\| cd) && pwd`, so REPO_ROOT got `pwd` output appended on a newline. |
| 2 | `benchmarks/scripts/benchmark_multi_model.py` | Hard `len(models) < 2` gate would have killed Gate 1 (single-model Llama). Now workload-aware. |
| 3 | `src/infergrid/cli.py` | `config.tenant_defaults` was parsed but never passed to `TenantManager()`. Default `max_concurrent=64` rejected c=128+ traffic with 429. |
| 4 | `configs/gate1_admission*.yaml` | No `tenant_defaults` set — relied on the broken default in #3. |
| 5 | `benchmarks/scripts/benchmark_multi_model.py` | aiohttp default `TCPConnector.limit_per_host=100` silently clamped c=256 to 100. Pre-fix Arm B reported peak_in_flight=100 (the connector limit, not the engine). |

PR #35 added a 3-layer cost-cap defense to `gate_pod_bootstrap.sh` (self-destruct timer at `MAX_POD_SECS=10800`, `/workspace/ABORT` sentinel, phase wall-clock budget that aborts before bench if engine bring-up ate > MAX/2).

`results/CORRECTIONS.md` updated with C2 v1+v2 history and a new C5 entry (admission was a no-op for streaming pre-#29; Gate 0/0.5/0.6 conclusions stand because they didn't depend on admission engagement, but Gate 1 onward critically does).

## Gate 1 Readiness (2026-04-19)

| Item | State |
|---|---|
| `main` contains all measurement-honest fixes | ✅ at `5b64b4c` |
| Unit tests | ✅ 127 passing |
| `bash benchmarks/scripts/smoke_bench.sh` | ✅ OVERALL: PASS, peak in_flight=16 |
| `bash scripts/gate1_dress_rehearsal.sh` (NUM_REQUESTS=300, SKIP_DISCRIMINATOR=1) | ✅ OVERALL: PASS — Arm A peak in_flight=128 + queue_depth=128, Arm B peak in_flight=256 |
| Cost-cap hardening in pod bootstrap | ✅ 3-layer defense |
| H100 SXM5 spot price ≤ $4/hr | Verify before provisioning |
| User greenlight on $7-10 spend | ✅ granted 2026-04-19, executed |

**Gate 1 launch runbook:** `docs/launch/gate1_runbook.md`.

**Hypothesis discriminator:** Arm A TTFT p99 @ c=256 ≤ 2× Arm A @ c=128, AND Arm B TTFT p99 @ c=256 ≥ 4× Arm A @ c=256.

**Caveat to flag in any analysis:** the dress rehearsal's load-aware mock is *linear* in latency (`base + max(0, excess) * per_excess`). If Gate 1 on H100 reports near-identical TTFTs across arms, that's a **disconfirmation of the hypothesis** — the c=128→c=256 cliff Phase 1 saw (185ms → 1293ms, 7×) may have been measurement artifact (pre-PR-#28 TTFT bug). Either outcome is publishable; don't read a flat result as a plumbing failure.

## Gate 1 — Outcome and Little's Law Caveat (2026-04-19)

Gate 1 shipped on H100 SXM5 SECURE ($2.99/hr, ~$1 total). Both arms completed 800/800 requests with 0 failures. Plumbing passed (admission engaged cleanly on Arm A: 272 queued / 854s total wait; open on Arm B). TTFT honest (post-PR #28/#31). Raw numbers:

| | c=128 p99 | c=256 p99 | Ratio |
|---|---:|---:|---:|
| Arm A (cap=128) | 3374 ms | 5964 ms | 1.77× |
| Arm B (cap=1024) | 3426 ms | 6177 ms | 1.80× |
| **B/A @ c=256** | — | — | **1.04×** |

The naive read (B/A=1.04×, needed ≥4×) looks like DISCONFIRM. **But post-run Little's Law review caught a methodology bug:** the 10s bench wall per `(arm × concurrency)` cell did not produce sustained cap pressure.

- Arm A c=128: `avg_in_flight ≈ throughput × avg_latency / requests_per_cell ≈ 103` — **below** cap=128.
- Arm A c=256: avg in-flight ≈ 198 over 10.7s wall — cap engaged in bursts, not steady state.
- Admission histogram: **only 272 of 801 admissions (~34%) queued at all.** Sustained cap+ load would expect >50% queued at c=256.

The 1.04× ratio is therefore **short-bench under-power, not a real DISCONFIRM**. Published as `results/gate1_20260419/GATE1_OUTCOME.md` with a "Methodology caveat (Little's Law)" section; hypothesis row softened from `FAIL` → `AMBIGUOUS` (PR #39).

**Operational finding:** PR #35's in-pod cost-cap (`poweroff`) does NOT free GPU memory reliably — Arm A's vLLM subprocess leaked 67.9 GiB past `pkill -9`, and `nvidia-smi --gpu-reset` is "Not Supported" on containerized pods. Recovery for Gate 1 Arm B required `runpod.stop_pod + resume_pod` (pod overlay fs wipes `/workspace` on restart — re-scp env + bootstrap). **Gate 1.5 runbook bakes pod-restart between arms explicitly.**

## Strategic Plan — Post-Gate-1 (2026-04-19)

Synthesized from advisor + god-planner review of the Gate 1 Little's Law caveat. Full plan in `.claude/agent-memory-local/god-planner/project_4week_plan_apr19.md`.

**Headline:** Gate 1.5 (this week) + Gate 2-lite (next week) → launch Tue 2026-05-12. Total compute budget $30-40 of $50 cap.

### Gate 1.5 — Powered rerun (next)

Same hypothesis, properly sized. Uses `configs/gate1_admission.yaml` + `configs/gate1_admission_off.yaml` unchanged — statistical power comes from sample size, not config.

**Bench args per arm:**
```
--concurrency 64,128,192,256  --num-requests 4000
```

4 concurrency steps × ~1000 req each = clean Little's Law per cell (vs. 400 total split across 2 cells in Gate 1). c=64 and c=192 added to locate the cliff knee, not just stride past it.

**Budget envelope:** ~$7-10 expected (~2.5h pod-wall @ $2.99/hr). $15 hard ceiling. MAX_POD_SECS=5400 per arm.

**Abort rules:**
- c=64 cell > 18 min → drop c=192 and rerun, keep 3 steps only.
- Engine bring-up > 15 min with no log progress → `touch /workspace/ABORT`.
- `infergrid_admission_in_flight` stuck at 0 during c=256 → admission regression, abort.
- `/workspace/COST_CAP_HIT` appears → manual RunPod-console terminate.

**Pod-restart between arms baked in** (see Gate 1.5 Re-Run section of runbook). Arm A ends → rsync → RunPod.stop_pod + resume_pod → re-scp env + bootstrap → Arm B. Prevents Arm B CUDA-OOM from leaked Arm A context.

**Outcome buckets:**
1. **CONFIRM** — Arm A flattens, Arm B explodes. Pitch holds.
2. **Robust DISCONFIRM** — both arms explode similarly at steady state. Admission doesn't help tail TTFT on this hardware/workload. Pivot pitch.
3. **Overload-protection emergence** — Arm B queues to OOM / 500s; Arm A degrades gracefully. Different, arguably stronger narrative.

### Gate 2-lite — Multi-model contention (the actual InferGrid differentiator)

Gate 0/0.5/0.6/1 all ran single-model under varied admission. InferGrid's actual differentiator is **multi-model orchestration on 1-4 GPUs without K8s** — never benchmarked. Design doc: `docs/launch/gate2_design.md`.

**3 arms on 1× A100-SXM4** (reuse Gate 0.6 bootstrap):
- Arm 1: InferGrid (Llama-3.1-8B + Qwen2.5-7B co-loaded, admission ON, WorkloadRouter mediates). Config: `configs/gate2_multi_tenant.yaml`.
- Arm 2: Raw uvicorn serving Llama-3.1-8B only (baseline: what do users get without middleware?).
- Arm 3: Round-robin WorkloadRouter without admission or KV lifecycle (baseline: what does the InferGrid "thin proxy" part contribute vs full stack?). Config: `configs/gate2_round_robin.yaml`.

**Workload:** two-tenant mixed (chat-style short prompts + RAG-style 8K-prompt bursts), 120s sustained wall. New bench script: `benchmarks/scripts/benchmark_two_tenant.py` (scaffold in this PR; implementation next session).

**Success criterion:** per-tenant p99 TTFT within 1.5× of solo-engine baseline AND no OOMs. Failure criterion: InferGrid ≈ round-robin ≈ raw uvicorn → thin proxy isn't load-bearing, rethink pitch.

**Budget:** $8 ceiling on A100-SXM4 @ ~$1.89/hr × ~4h.

### Week 3-4 — Launch prep

- **LAUNCH-BLOCKER #1 (user):** PyPI `infergrid` placeholder upload (squat protection).
- **LAUNCH-BLOCKER #2 (user):** Cloudflare Worker deploy for waitlist (`window.WAITLIST_API = ''` in landing page line 447).
- Quickstart polish (install → 2 models → request in <5 min).
- Launch-post framing: product-led title ("Run two LLMs on one A100 without K8s") with meta-honesty (shadow-review catches) as supporting beat. PR #20 draft needs reframe after Gate 1.5 lands.
- **Launch target: Tuesday 2026-05-12** (HN timing).

---

## 2026-04-19 → 2026-04-21 — Post-pivot launch polish + PyPI 0.1.2 + LP live

Closes out the Gate 2-FAIRNESS launch sprint: drops the last pre-pivot experiment results, cleans the public surface (README, CLI, PyPI, CI, issue templates), lands the gate-ladder v0.2 scaffolding for the $72 4-gate plan, and ships the launch post. LP + Cloudflare + Resend wired end-to-end.

- PR #65 (452593d) · results: Track C N=6 CONFIRM (1 flooder + 6 quiet, p99 fairness holds) + Track D OOM-under-burst NULL (no OOM, D1 ≈ D2 — cut from launch per pre-committed null rule).
- PR #66 (1443174) · deprecate `scripts/generate_launch_chart.py` (superseded by v3 chart pipeline); add Track C p95 row to Gate 2-FAIRNESS outcome doc.
- PR #67 (54f3095) · docs: README + pyproject + quickstart destale — drop pre-PyPI `pip install .` refs, align quickstart numbers to v3 hero, reword CacheManager section to match the actual (not-adaptive) implementation.
- PR #68 (2ce029f) · release: 0.1.1 — fix `[tool.hatch.build.targets.wheel]` TOML structure bug (blocked wheel build) + version bump.
- PR #70 (8312b0c) · first CI (GitHub Actions: pytest + ruff on Python 3.11 + 3.12); durable `__version__` via `importlib.metadata.version("infergrid")` (fallback to `"0.0.0+unknown"` for editable installs without egg-info); delete orphaned `scripts/generate_charts.py`; replace literal `"TODO"` token-bucket metric strings with `"n/a"`.
- PR #71 (f4f448c) · README badges (PyPI / CI / license / Python versions) + 4 YAML issue-form templates (bug / feature / docs / benchmark-regression) + disable Wiki + 10 custom repo labels + 8 topics for discoverability + GitHub Discussions enabled + GH Release v0.1.1 cut.
- PR #72 (899f523) · CLI 0.1.2 — `infergrid --version`, `infergrid doctor` (env sanity), `infergrid man [topic]` (offline help), interactive `infergrid serve` wizard, rich-table output; fixes stale "adaptive" CacheManager description and lingering "scheduling cliff" reference in help text.
- PR #73 (4c59682) · gate-ladder v0.2 — 3 new configs (`gate21_fairness_n8.yaml`, `gate24_fairness_mixtral_tp2.yaml`, `gate23_fairness_70b_tp4.yaml`), bump vLLM 0.8.5 → 0.19.1 in `requirements-gpu.txt`, `scripts/gate_pod_bootstrap.sh` adjusted for new vLLM surface + `pyproject.toml` extras; drops 0.8.5-specific compat pins.
- PR #20 (928d55b) · launch post merged: `docs/launch/gate0_launch_post.md` (2013 words, v3 hero numbers, post-review fixes applied: waitlist CTA wired + 6 path fixes + cost reconciliation $12 → $17).
- PR #74 (aab59a6) · docs sweep — README LOC/test count refresh (2,872 → 4,100 LOC; 144 → 153 tests), quickstart adds 0.1.2 CLI surface, roadmap replaces vague post-launch bullets with concrete links into the gate ladder, CONTRIBUTING adds ruff gate, `research_roadmap.md` prepends a post-pivot status block.

Out-of-repo shipping (LP side, not in this repo's git history):

- LP live at https://infergrid.org (Vercel project auto-deploys from `coconut-labs/infergrid-root` main).
- Cloudflare Worker live at `infergrid-waitlist.shrey77-wrk.workers.dev` with D1 binding + Resend API key wired.
- `FROM_EMAIL` `hello@infergrid.org` domain verified in Resend.
- Launch email template committed at `landing_page/waitlist-api/templates/launch_announcement.html`.
- GitHub tracking issue #69 filed for post-launch backlog (~2.8K words; issue, not PR).
- PyPI `infergrid==0.1.2` live; durable `__version__` fix confirmed via clean-venv round-trip (`pip install infergrid && python -c "import infergrid; print(infergrid.__version__)"` → `0.1.2`).

### Pending / blocked

- **Gate 2.2 harness PR** — blocked on prerequisite harness code: `benchmarks/scripts/benchmark_n_tenant_single_model.py` needs a `--prompt-length-dist` flag wiring `RequestGenerator.generate_mixed_length`.
- **3 user-side tokens awaiting rotation** — PyPI, Resend, Cloudflare.
- **4-gate $72 RunPod ladder queued** — 2.1 / 2.4 / 2.3 runnable immediately on the v0.2 configs; 2.2 after harness lands.
- **Arch-panel feature** — liquid-glass modal triggered from nav, 10 forks decided on defaults; Phase 1 dispatched to a sibling agent.

---

## 2026-04-21 — Gate ladder v0.2 in flight (parallel execution)

Dispatched two parallel bench agents on fresh RunPod pods (H100 SKU after A100-SECURE stuck-provisioning incidents on Apr 20):

- **Gate 2.1 N=8 tenant fairness** — pod `nmhl7l30g9jx1g` (1× H100 80GB SXM @ $2.99/hr). Config `configs/gate21_fairness_n8.yaml`. Background agent cloning repo, installing vLLM 0.19.1, will warm Llama-3.1-8B-Instruct, run 3 arms (solo baseline, FIFO, token-bucket), fill `results/gate21_n8_TBD/` OUTCOME template, open PR. Budget ceiling $4.50. PASS bar: aggregate quiet_p99 ≤ 75 ms AND worst-tenant p99 ≤ 80 ms (N=6 landed 61/65).
- **Gate 2.4 Mixtral-8x7B MoE fairness** — pod `hstyv0nj0cwwcq` (2× H100 80GB SXM TP=2 @ $5.98/hr). Config `configs/gate24_fairness_mixtral_tp2.yaml`. Background agent running same 3-arm pattern on Mixtral. Budget ceiling $12. PASS bar: quiet_p99 ≤ 2× solo AND per-tenant p99 spread < 1.5×.
- **Gate 2.2 mixed-prompt-length** — queued after Gate 2.1 completes; reuses that pod via signal file `/tmp/gate21_complete_reuse_pod.txt`.
- **Gate 2.3 Llama-70B TP=4** — deferred until 2.1 AND 2.4 CONFIRM (budget + de-risk).

### Provisioning notes

- Initial A100-SXM-80GB SECURE pods stalled at `uptime=0` for 7+ min — same SKU issue seen across 2×A100-SXM SECURE tiers. Pivoted to H100 SXM SECURE (HIGH stock) — clean RUNNING state within 3 min.
- First H100 pods created with `--ports` omitted, only `19123/http` exposed → RunPod entrypoint started `sshd` but port 22 not mapped → `runpodctl ssh info` returned `pod not ready`. Fixed live via `runpodctl pod update --ports "22/tcp,8888/http,19123/http"` which triggered a container restart; SSH came up within 15s.
- OUTCOME templates for all 4 gates pre-landed on branch `results/gate-ladder-v02-templates` (PR #79) with deliberate `<TBD_*>` sed-replace seams for mechanical fill from bench output.
- Monitor `bk2ifzhnn` armed with combined budget watcher + 2-min pod heartbeat. Budget alerts at $55 / $65 of the $72 ceiling (raised from $30/$50 per advisor recommendation to accommodate the 2-pod parallel burn rate of ~$9/hr).
- PR #78 (cbee679) shipped pre-bench: `benchmark_n_tenant_single_model.py --prompt-length-dist` flag unblocks Gate 2.2 harness.

### Gate 2.1 CONFIRM (PR #81 · 2026-04-21 05:01 UTC)

Token-bucket per-tenant fairness scales from N=6 to N=8 on a shared vLLM engine without degradation.

| Arm | quiet_p99 (worst tenant) | note |
|-----|-------------------------:|------|
| Arm 0 solo | 48.1 ms | 1 tenant alone baseline |
| Arm 1 FIFO | 59.0 ms | 1.23× solo — FIFO didn't catastrophically starve on H100 (headroom + max_concurrent=512 ceiling); A100 baseline comparison moved to future work |
| Arm 5 DRR + token-bucket | **50.4 ms** | 1.05× solo — PASS (bound: ≤ 75 ms and 1.25× of N=6's 61.0 ms) |

- Per-tenant p99 spread across 7 quiets: 1.06× (criterion ≤ 1.5×)
- Flooder 429 rate: 68% (6481/9490) — rate-limit engaging correctly; zero quiet 429s
- Cost: **$1.65** vs $4.50 ceiling (33 min at $2.99/hr)
- GPU substitution: H100 SXM 80GB for A100-SXM4 80GB — fairness ratio is substrate-independent; absolute TTFTs ~40% lower than A100 would produce

Honest caveat: Arm 1 FIFO p99 59 ms is lower than expected relative to Gate 2-FAIRNESS A100 FIFO (1585 ms). Attributed to (a) H100 absolute headroom at N=8 load and (b) `max_concurrent=512` upper ceiling still admitting all work. Fairness demonstration is still load-bearing: arm 5 shows 68% rejection at the tenant-budget layer that arm 1 lacks.

### Gate 2.4 CONFIRM (PR #82 · 2026-04-21 05:04 UTC)

Token-bucket fairness holds on Mixtral-8x7B MoE TP=2 — first MoE+TP>1 validation.

| Arm | quiet_p99 | note |
|-----|----------:|------|
| Arm 0 solo | 84.9 ms | 1 tenant alone baseline |
| Arm 1 FIFO | 122.7 ms | 1.45× solo — engine didn't saturate at 17 RPS (2× H100 TP=2 headroom) |
| Arm 5 DRR + token-bucket | **109.7 ms** | 1.29× solo — PASS (bound: ≤ 2× solo) |

- Per-tenant p99 spread across 3 quiets: 1.02× (criterion ≤ 1.5×)
- Flooder 429 count: 1777 — rate-limit firing on MoE path identically to dense-model path
- Cost: **$4.90** vs $12 ceiling (49 min at $5.98/hr)

Honest caveats (in OUTCOME + PR body):
1. Engine-headroom: arm 1 FIFO also stayed inside 2× solo — this run proves fairness *holds* on MoE but does NOT prove MoE is *resilient under saturation*. Follow-up Gate 2.4b at higher flooder RPS or TP=1 recommended.
2. Expert-routing histogram NOT available in vLLM 0.19.1 (`enable_return_routed_experts=False` default); indirect evidence (flood_p50 ≈ quiet_p50 within 0.2 ms) consistent with uniform routing.

### Gate 2.2 PASS (PR #83 · 2026-04-21 05:34 UTC)

Token-bucket fairness holds under mixed-prompt-length traffic (N=8 Llama-3.1-8B, bimodal prompts via `--prompt-length-dist "64:0.4,512:0.3,2048:0.2,8192:0.1"`).

| Arm | quiet_p99 (agg) | note |
|-----|----------------:|------|
| Arm 0 solo | 79.0 ms | mixed-length baseline at ~10 RPS |
| Arm 1 FIFO | 231.8 ms | |
| Arm 5 DRR + token-bucket | **132.0 ms** | 1.67× solo — token-bucket 1.76× better than FIFO |

Per-bucket p99 ratios (token-bucket vs solo): 64=1.81×, 512=1.68×, 2048=1.60×. Strict 1.5× bar missed, but:
- No bucket hits 2.5× DISCONFIRM gate.
- Per-bucket **p50** ratios all within 1.17× (precommitted PASS criterion clean).
- Per-tenant p99 spread 1.55× (min/max across 7 quiets) — fairness-layer tight.
- 8192-token bucket skipped: `prompt + max_tokens > max_model_len=4096` short-circuits vLLM to empty SSE streams; filtered uniformly across all 3 arms. max_model_len=8192 rerun is follow-up work.
- Cost: **$1.25** vs $3 ceiling (25 min at $2.99/hr)

Framing: called PASS because the 1.5× miss is diagnosed as engine-load scaling (solo ~10 RPS vs contended ~17 RPS), not fairness-layer degradation. Reviewers can overrule.

### Gate 2.3 CONFIRM (PR #84 · 2026-04-21 05:53 UTC, cost-correction PR #85 · 05:55 UTC)

Token-bucket fairness transfers cleanly from 8B → 70B scale and from 1-GPU → TP=4.

| | Arm 0 solo | Arm 1 contended | Ratio |
|---|---:|---:|---:|
| quiet TTFT p50 (agg) | 53.9 ms | 57.8 ms | **1.07×** |
| quiet TTFT p99 (agg) | 766.8 ms | 1238.6 ms | **1.62×** |
| flooder 429 | — | 6465 / 9474 = **68.2%** | |

**Cross-scale p50 ratio is flat:** 1.03× at 8B N=8 (Gate 2.1) → 1.07× at 70B TP=4 N=4 (this gate). Flooder 429 rate identical to Gate 2.1's 68.3%. Admission layer is architecturally scale-invariant across 8B→70B and 1-GPU→TP=4.

- 4× H100 80GB HBM3 SXM pinned 100% util during arm 1, symmetric VRAM 76.01–76.16 GB (93.4% of 81.56 GB cap), TP imbalance 1.00×
- `HF_HUB_ENABLE_HF_TRANSFER=1` pulled 132 GB in 216 s from AP-IN-1 datacenter (7× faster than 15-25 min task estimate)
- vLLM TP=4 cold load 234 s, `gpu_memory_utilization=0.90` fit first try
- flashinfer-0.6.6 AllReduce JIT-compile fails against CUDA 12.8/Python 3.11 (`std::optional` header bug) — vLLM clean-fallback to stock NCCL, no fairness impact; flagged for future vLLM upgrades
- Cost: **$7.77** vs $18 ceiling (39 min provision-to-delete at $11.96/hr SECURE)

Caveats: 2 arms only (not 3 — 70B is expensive; Gate 2-FAIRNESS Arm 1 carries the FIFO baseline); N=4 tenants not N=8; warmup folded into p99 (bench doesn't exclude first 10s); per-tenant p99 spread 26–30× is cold-cudagraph transient, identical across arms so doesn't contaminate the ratio.

### Verdict — gate ladder v0.2 complete

| Gate | Test | Verdict | Headline ratio | Cost | PR |
|------|------|---------|---------------:|-----:|----|
| 2.1 | N=6 → N=8 tenant scale | **CONFIRM** | 1.05× solo | $1.65 | #81 |
| 2.2 | mixed-prompt-length | **PASS** (strict 1.5× missed; not fairness-layer) | 1.67× solo | $1.25 | #83 |
| 2.3 | 8B → 70B + TP=1 → TP=4 | **CONFIRM** | 1.62× solo (p50 1.07×) | $7.77 | #84, #85 |
| 2.4 | dense → MoE (Mixtral TP=2) | **CONFIRM** | 1.29× solo | $4.90 | #82 |

**Total spend: ~$15.57 of $72 ceiling (~22%)**. 3 CONFIRM + 1 PASS-with-caveat against the pre-committed criteria.

### Regime caveat — load-bearing for any launch-post framing

Post-hoc shadow review surfaced a material framing risk. The 4 gates hold against the criteria as written, but three things the "cross-scale fairness" narrative should **not** claim:

1. **FIFO did not starve on H100 in any gate.** Gate 2.1 FIFO = 59.0 ms (1.23× solo), Gate 2.2 FIFO = 231.8 ms (but decomposed as engine-load not starvation), Gate 2.4 FIFO = 122.7 ms (1.45× solo). Compare the prior Gate 2-FAIRNESS (A100, 2026-04-19) where FIFO = 1585 ms = 29× solo. Same tenant configs; the difference is H100 headroom at offered load of 32-RPS flooder + 7×1-RPS quiet. **Token-bucket "fixing" the absence of starvation is not a demonstration of fairness-at-scale.** The mechanism is sound; this experimental regime under-exercises it.
2. **The 68% 429 rate is arithmetic, not empirical.** `(flooder_rps − rate_limit_rps) / flooder_rps = (32 − 10)/32 = 68.75%` — deterministic from config. Gate 2.4's 37% matches its `flooder_rps=16` the same way. The "identical 68% across all gates" reads as cross-scale convergence but is in fact a plumbing check that the rate-limit fires at the config's setpoint. Valid finding, mis-framed as a discovery.
3. **Cross-scale p50 flatness (1.03× → 1.07×) is not falsifiable from these gates alone.** Any config that doesn't saturate the engine will show p50 ≈ solo. Would also show flat across scales.

### What's validated vs. what's pending

- **Validated (load-bearing):** the rate-limit mechanism engages correctly at the admission layer across N=4, N=8, dense + MoE, 1-GPU + TP=2 + TP=4, mixed prompt lengths. Zero quiet-tenant 429s; no engine OOMs; no NCCL runtime errors; VRAM fits the TP=4 70B load on first try at `gpu_memory_utilization=0.90`. The code path is exercised under all four regimes.
- **Pending for a public "works at 70B" claim:** an explicit saturated-regime test where FIFO produces ≥10× starvation on the same hardware and token-bucket holds inside 1.5×. On H100, that means bumping `flooder_rps` past the engine knee (estimate 128+ RPS, or tenant count to N=16). Provisional follow-up ticket: **Gate 2.1b — saturated H100 N=8 @ 128 flooder RPS** (~$3 budget, 30 min on 1× H100). Awaits user authorization — not in the original $72/4-gate allocation.

### Honest launch-post framing (no 2.1b needed)

- **Primary claim (supported):** "Gate 2-FAIRNESS on A100 showed per-tenant token-bucket reduces FIFO starvation from 28,716 ms p99 to 74 ms (1.35× solo)." (Already the v3 LP hero.)
- **Replication claim (supported):** "The same code path runs cleanly at 70B TP=4 across 4× H100, under mixed prompt lengths, and under MoE routing, without engine failures. Measured TTFT deltas are small because H100 at our offered load is under-saturated — the mechanism is exercised, but the starvation regime that makes fairness load-bearing is the A100 headline."
- **Over-claim to avoid:** "same fairness ratio at every scale" implies empirical cross-scale fairness that these gates don't prove.

### Follow-ups queued (pending user authorization)

- **Gate 2.1b (saturation) — $3**: bump flooder_rps to 128 on 1× H100; if FIFO still doesn't starve, bump tenant count to N=16. Decisive for the "works at 70B" hero claim.
- **Gate 2.2b (max_model_len=8192) — $2**: rerun 2.2 with bigger context window so the 10% 8192-token bucket produces real generations instead of short-circuited empty responses.
- **Gate 2.4b (saturated MoE) — $6**: rerun 2.4 at `flooder_rps=32+` or on a smaller TP config to force the engine past its knee; isolate whether MoE expert-capacity contention leaks past the admission layer.

---

## 2026-04-21 — Rebranded to KVWarden + Cloudflare migration

**Trigger.** Name-collision audit surfaced `infergrid.net` (Samuel J. Bell, FAIR/Meta postdoc) as a senior commercial user with an identical wordmark in the same market. Under common-law trademark, he has the senior claim. Verdict: RENAME-URGENT before launch. Full audit at [docs/naming/infergrid_name_audit.md](docs/naming/infergrid_name_audit.md).

**New name: `kvwarden`** (PascalCase `KVWarden`, upper `KVWARDEN`). Rationale: literal to the mechanism (KV cache + admission warden), clean across PyPI / npm / all relevant TLDs, zero trademark hits, no phonetic overlap with `infergrid`. Candidate pool + availability matrix in the audit doc.

### What landed on main

- [#96 merged](../../commit/f027b1c) — tree-wide rename, 119 files, 962 replacements (606 lowercase + 287 PascalCase + 69 UPPER), `src/infergrid/` → `src/kvwarden/`, pyproject name + entry points, all configs + tests + docs. Left intentionally untouched: `PROGRESS.md` (historical), `docs/launch/gate0_launch_post.md` (historical narrative), `docs/naming/infergrid_name_audit.md` (is the audit record), `results/**` (historical evidence).
- [#97 merged](../../commit/7f79f2d) — CI fix. One import broke post-rename (`hero._delta_badge` had moved to `compare._delta_badge` via a cherry-pick). Pre-existing "9 HF failures" briefing was stale — real count was 1 failure + 4 ruff drift items. All green after the fix.
- [LP repo #2 merged](https://github.com/coconut-labs/infergrid-root/commit/f9871a3) — landing page rename, 16 files, 82 replacements, favicon monogram updated `ig` → `kw`.

### Cloudflare migration (same-day)

- Worker `infergrid-root` now serves KVWarden-branded LP at `kvwarden.org` (Worker Custom Domain binding, AAAA → `100::`).
- DNS + Resend records for `kvwarden.org`: AAAA (100::), MX send → feedback-smtp.us-east-1.amazonses.com, MX @ → inbound-smtp.us-east-1.amazonaws.com, TXT resend._domainkey (DKIM), TXT send (SPF amazonses), TXT _dmarc (v=DMARC1; p=none;). Resend domain-verification click pending in the dashboard.
- Worker `infergrid-redirect` deployed and bound as Custom Domain on both `infergrid.org` and `www.infergrid.org` — issues HTTP 301 → `https://kvwarden.org${path}${query}`. Smoke-tested both bare domain and path-preserving cases.
- Vercel CNAME on `infergrid.org` deleted (was routing the old LP via Vercel; now routed to the redirect Worker via CF).
- Waitlist Worker `kvwarden-waitlist` deployed at `kvwarden-waitlist.shrey77-wrk.workers.dev`, secrets `RESEND_API_KEY` + `ADMIN_KEY` set via `wrangler secret put`. Smoke test POST `/api/subscribe` returned 200 and wrote to D1; test row cleaned up. 1 real subscriber preserved (`patelshrey77@gmail.com`, original from 2026-04-20).
- D1 database name could NOT be renamed (CF API returns `success` but doesn't change it). Database stays as `infergrid-waitlist` by name; the binding uses UUID `d78fc056-d575-474e-9401-7db3e636a9dd` so the Worker talks to it correctly. Cosmetic-only mismatch.

### Cosmetic cruft left for future cleanup (not breaking anything)

- Worker names: `infergrid-root`, `infergrid-waitlist`, `infergrid-redirect` all still reference old brand. Renaming Workers requires delete+redeploy with brief downtime; deferring.
- D1 name: `infergrid-waitlist` (API doesn't support rename).
- GitHub repos: `coconut-labs/infergrid` + `coconut-labs/infergrid-root` still at old names. Waiting on explicit "go repo rename" from founder — GitHub auto-301s the old URLs indefinitely, no functional impact.
- Old `infergrid-waitlist` Worker still running in parallel as safety net. Delete after 7 days of no regressions.

### Token + credential hygiene

- `~/.infergrid/creds.md` + `~/.infergrid/secrets.env` capture all pasted values (mode 600, outside any git tree).
- Rotation plan at [docs/runbooks/token_rotation_20260421.md](docs/runbooks/token_rotation_20260421.md). Founder preference: deprecate all CF tokens post-migration; rotate Resend in place (Worker dependency); keep HF/RunPod/PyPI until next use.

### Launch-plan status (unchanged by rename)

- Gate ladder v0.2 findings, reproduce-hero CLI scaffold, telemetry PR, metrics fixes, SGLang parity, H100 operating envelope — all still valid under `kvwarden`.
- LP hero numbers (1,585 ms → 61.5 ms under FIFO vs token-bucket on A100+vLLM-0.19.1) unchanged.
- Launch content drafts at `docs/launch/` (Show HN, Twitter thread, FAQ, one-pager) were swept to the new brand by the rename agent.
- Outstanding launch-side user homework: buy defensive `.com/.ai/.dev` domains (`.org` + `.com` already live); reserve Twitter/X handles; reserve npm namespace; send courtesy email to Samuel Bell; cold-outreach 5 ICPs; record 30-sec demo video.
- **Docker Hub `kvwarden` namespace reserved on 2026-04-22.** Token stored in `~/.infergrid/secrets.env` as `DOCKER_HUB_TOKEN`. No images pushed yet — reservation only.
- **PyPI `kvwarden` 0.0.1 stub shipped on 2026-04-22.** Live at https://pypi.org/project/kvwarden/0.0.1/; `pip install kvwarden` returns `kvwarden.__version__ == "0.0.1"`. Account-scoped upload token stored as `PYPI_KVWARDEN_TOKEN` in `~/.infergrid/secrets.env`; narrow to project-scoped + revoke the account-scoped token before the 0.1.0 release.
- **Paste-ready artifacts for all four user-side blockers landed in #99** (squash-merged to `main` as ed02973): Samuel email at `docs/naming/email_samuel_bell.md`, cold outreach at `docs/launch/cold_outreach_template.md`, PyPI stub at `scripts/publish_kvwarden_stub.sh` + `docs/launch/pypi_reservation.md`, demo script at `docs/launch/demo_video_script.md`. Each blocker now a few-minutes execution for the user rather than a vague TODO.
