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
| Profiling data points | 416,019 + 75 (Gate 0.6) rows across 880+ files |
| Unit tests | 129 collected, 121 pass, 8 xfail (pre-existing in `test_benchmark_client`) |
| GPU configurations profiled | 3 (vLLM A100, SGLang A100, vLLM H100) |
| Live GPU bring-ups | 2 (Gate 0, Gate 0.6 — both on A100-SXM4) |
| PRs merged | 25 |

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
| #29 | admission-streaming-bypass fix: route_request released slot before stream consumed; cap was a no-op for streaming. Wrapper holds slot for stream lifetime; smoke_bench /metrics poller; 3 unit tests | Merged | Apr 19, 2026 |
| #30 | aclose inner generator in stream-wrapper finally (closes engine HTTP connection on client abort) | Merged | Apr 19, 2026 |
| #31 | TTFT v2: `bool(" ")` truthy + chat-completions `delta.content` + JSONDecodeError → continue. 3-case discriminator. Smoke poller 50ms→10ms (Nyquist). Bonus: `scripts/gate1_dress_rehearsal.sh` | Merged | Apr 19, 2026 |
| #32 | Streaming budget accounting: tenant.record_completion got `gpu_seconds=0.07ms` at handoff pre-fix; now in wrapper finally with real elapsed + chunk count | Merged | Apr 19, 2026 |
| #33 | Max-stream-duration fence (`INFERGRID_STREAM_MAX_DURATION_S=600`) + GC-path test confirms asyncgen finalizer hook releases slot | Merged | Apr 19, 2026 |
| #34 | 5 Gate-1 blockers from dress rehearsal: bash REPO_ROOT precedence; bench `len(models)<2` gate; cli.py never wired tenant_defaults to TenantManager; gate1 configs missing tenant_defaults; aiohttp `TCPConnector.limit_per_host=100` clamped c=256 to 100 | Merged | Apr 19, 2026 |
| #35 | Cost-cap 3-layer defense in `gate_pod_bootstrap.sh`: MAX_POD_SECS self-destruct timer, /workspace/ABORT sentinel, phase wall-clock budget | Merged | Apr 19, 2026 |

PR #18 closed (conflict-dead, superseded by #19). PR #20 open as DRAFT (Gate 0 launch post, ship-gated).

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
| User greenlight on $7-10 spend | Pending |

**Gate 1 launch runbook:** `docs/launch/gate1_runbook.md`.

**Hypothesis discriminator:** Arm A TTFT p99 @ c=256 ≤ 2× Arm A @ c=128, AND Arm B TTFT p99 @ c=256 ≥ 4× Arm A @ c=256.

**Caveat to flag in any analysis:** the dress rehearsal's load-aware mock is *linear* in latency (`base + max(0, excess) * per_excess`). If Gate 1 on H100 reports near-identical TTFTs across arms, that's a **disconfirmation of the hypothesis** — the c=128→c=256 cliff Phase 1 saw (185ms → 1293ms, 7×) may have been measurement artifact (pre-PR-#28 TTFT bug). Either outcome is publishable; don't read a flat result as a plumbing failure.
