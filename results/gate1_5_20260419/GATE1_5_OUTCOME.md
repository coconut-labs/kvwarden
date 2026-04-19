# Gate 1.5 — Powered admission-control TTFT validation on H100 SXM5

**Date:** 2026-04-19
**Hardware:** 1× NVIDIA H100 SXM5 80GB HBM3 on RunPod (SECURE on-demand, $2.99/hr)
**Pod ID:** `2yb6zm2aw60azx` (created 13:52 UTC, terminated 14:18 UTC; ~26 min billable wall ≈ ~$1.30 actual spend, well under the $15 ceiling)
**Main at pod clone:** `0f4f113` — PRs #28-40 all active
**Model:** `meta-llama/Llama-3.1-8B-Instruct`, bfloat16, max_model_len=4096, gpu_memory_utilization=0.85
**Arms:** `configs/gate1_admission.yaml` (Arm A, cap=128) and `configs/gate1_admission_off.yaml` (Arm B, cap=1024)
**Bench args:** `--workload concurrent --concurrency 64,128,192,256 --num-requests 4000 --seed 42`
**Status:** **Hypothesis ROBUSTLY DISCONFIRMED.** Plumbing PASS. Sustained cap pressure achieved (Arm A: 16001 admits, 6444 queued ~40%, 18628 total wait-seconds). Result is independent of bench length — Gate 1's 1.04× ratio reproduced exactly under 16× the sample size.

---

## Raw numbers

### Arm A — `configs/gate1_admission.yaml` (max_concurrent=128)

| Metric | c=64 | c=128 | c=192 | c=256 |
|---|---:|---:|---:|---:|
| num_successful | 4000 | 4000 | 4000 | 4000 |
| num_failed | 0 | 0 | 0 | 0 |
| ttft_p50_ms | 368.4 | 569.2 | 3029.0 | 5450.1 |
| ttft_p95_ms | 416.1 | 3180.9 | 3486.7 | 5700.1 |
| ttft_p99_ms | **502.5** | **3271.8** | **5690.9** | **5877.5** |
| throughput_tok_per_sec | 4002.1 | 4990.5 | 5016.9 | 5038.7 |
| wall_time_ms | 127867 | 102833 | 102413 | 101905 |

**Arm A admission histogram** (Prometheus, all 4 cells + 1 smoke combined):
- Total admits: **16001**
- ≤1ms (fast path, no queue): **9557** (60%)
- 1-5s (queued): **5884** (37%)
- 5-10s (queued longer): **502** (3%)
- **Total wait across 16001 admits: 18628 seconds (avg 1.16s per admit; ~5h cumulative wait time).**

### Arm B — `configs/gate1_admission_off.yaml` (max_concurrent=1024)

| Metric | c=64 | c=128 | c=192 | c=256 |
|---|---:|---:|---:|---:|
| num_successful | 4000 | 4000 | 4000 | 4000 |
| num_failed | 0 | 0 | 0 | 0 |
| ttft_p50_ms | 364.1 | 595.8 | 3068.2 | 4659.2 |
| ttft_p95_ms | 431.2 | 3192.3 | 3311.0 | 5874.9 |
| ttft_p99_ms | **624.4** | **3357.3** | **3502.9** | **6100.2** |
| throughput_tok_per_sec | 3994.1 | 5013.5 | 4954.6 | 4934.6 |
| wall_time_ms | 128094 | 102318 | 103560 | 104061 |

**Arm B admission histogram:**
- Total admits: **16001**
- **All 16001 admits ≤1ms** (fast path; cap=1024 never reached at 256 offered concurrency).
- **Total wait across 16001 admits: 0.022 seconds (avg 1.4 µs per admit).**
- Admission control did not engage. Matches experimental design — Arm B is the control.

---

## Plumbing sanity — BEFORE reading the hypothesis

Per `docs/launch/gate1_runbook.md` "Plumbing sanity" section + the new "Gate 1.5 Re-Run" addendum:

- ✅ Both arms completed all 16000 requests (4000 × 4 concurrency levels) with **0 failures**.
- ✅ Arm A's admission control engaged substantially under sustained pressure: 40% of admits queued, 5h+ cumulative wait time.
- ✅ Arm B's admission stayed open as designed: all 16001 admits ≤1ms.
- ✅ Wall time per cell now **~100s sustained** (vs Gate 1's ~10s) — Little's Law math now actually exercises the hypothesis (see next section).
- ✅ TTFT honest (post-PR #28/#31 path; no measurement bug active).
- ✅ No PLUMBING-REGRESSION conditions hit: throughput is stable across arms (~5000 tok/s at saturation), no engine timeouts, no 5xx, no `INFERGRID_STREAM_MAX_DURATION_S` fence trips.
- ✅ Pod-restart-between-arms protocol worked (no Arm B CUDA-OOM, GPU clean at 0 MiB after `runpod.stop_pod + resume_pod`).

The experiment is wired correctly. The numbers are honest. **Gate 1.5 is the methodologically clean version of Gate 1.**

---

## Little's Law check — sustained cap pressure achieved this time

Arm A's average in-flight (Little's Law: `avg_in_flight ≈ throughput_tok_per_sec × tokens_per_req / requests_per_cell × wall_seconds_per_cell` rearranged to `avg_in_flight = num_requests × avg_request_seconds / wall_seconds`):

| Cell | Throughput | tokens/req | avg_seconds/req | wall_s | avg_in_flight | vs cap=128 |
|---|---:|---:|---:|---:|---:|---|
| c=64 | 4002 | 126 | ~2.01 | 127.9 | **~63** | below cap (cap not engaging) |
| c=128 | 4991 | 126 | ~3.23 | 102.8 | **~126** | at cap (binding) |
| c=192 | 5017 | 126 | ~4.82 | 102.4 | **~188** | above cap (~60 sustained queue) |
| c=256 | 5039 | 126 | ~6.40 | 101.9 | **~251** | above cap (~123 sustained queue) |

vs Gate 1 (10s wall): c=128 only averaged ~103 in-flight (BELOW cap=128, cap not actually binding even at the supposedly-cap-tight concurrency level). **Gate 1.5 actually exercises the hypothesis. Gate 1 did not.**

For Arm B (cap=1024) at c=256: avg_in_flight ≈ 4000 × 6.50 / 104.1 ≈ **~250 in-flight**, well below cap=1024 — admission stayed completely open as designed.

---

## Hypothesis math (pre-committed in `docs/launch/gate1_runbook.md`)

| Predicate | Required | Measured | Result |
|---|---|---|---|
| `A_p99(c=256) ≤ 2 × A_p99(c=128)` | ≤ 2.00× | 5877.5 / 3271.8 = **1.80×** | PASS |
| `B_p99(c=256) ≥ 4 × A_p99(c=256)` | ≥ 4.00× | 6100.2 / 5877.5 = **1.04×** | **FAIL (robust)** |

The 1.04× ratio is **identical to Gate 1's 1.04× ratio** at 16× the sample size and 10× the per-cell wall time. PR #39's Little's Law caveat was a correct methodological hedge — Gate 1's bench *was* under-powered relative to the hypothesis's premise, so we couldn't trust its DISCONFIRM at the time. Gate 1.5 shows the concern's *content* (that sustained pressure might give a different answer) cashed out null: the 10s bench gave the same answer as the 100s bench. The hedge was right to take; the result happened to survive it. This is now **a robust, clean DISCONFIRM of the original cliff-prevention hypothesis**.

---

## What the data actually shows (beyond the pre-committed math)

1. **Arm A's admission cap engaged HARD under sustained pressure.** 6444 of 16001 admits (40%) queued, with cumulative wait time of 5h. The cap was binding, the controller was awake, the plumbing worked.

2. **And it didn't help TTFT p99.** B/A @ c=256 = 1.04× — Arm B's uncapped vLLM scheduler delivered essentially the same p99 as Arm A's capped admission. vLLM's own batching and scheduling absorbs overload as well as a coarse upstream concurrency cap does.

3. **At c=192, Arm A's tail is materially worse than Arm B's, beyond a single percentile bucket.** A_p99(c=192) = 5691 ms vs B_p99(c=192) = 3503 ms (62% worse). The deeper tail confirms the gap is real, not a single outlier:

   | percentile | A_c=192 | B_c=192 | A worse by |
   |---|---:|---:|---:|
   | p95  | 3487 ms | 3311 ms | +5% |
   | p99  | 5691 ms | 3503 ms | +62% |
   | p99.9 | 5953 ms | 3525 ms | +69% |
   | max  | 5956 ms | 3539 ms | +68% |

   The flat A-tail near 5950 ms suggests a queue-time floor created by the cap=128 binding under offered-load=192. B's flat tail near 3525 ms suggests vLLM's continuous batching converges to a steady-state ceiling without the cap-induced wait. Above the cap, admission's queue *adds* tail latency without removing engine-side latency.

4. **No overload-protection emergence either.** Both arms had **0 failures** at every concurrency level. There is no concurrency at which Arm B fell over while Arm A stayed graceful — vLLM did not hit OOM, did not 500, did not refuse traffic. The "Arm A degrades gracefully where Arm B catastrophically fails" runbook scenario did not materialize on this workload/hardware.

5. **Throughput is identical across arms at every concurrency.** Both arms sit at ~5000 tok/s saturated. The cap did not improve goodput; it only changed the queueing distribution.

---

## Reading the result against the runbook's pre-committed buckets

Per `docs/launch/gate1_runbook.md` § "Reading the Gate 1.5 result":

- ❌ **CONFIRM** — `A_p99(c=256) ≤ 2 × A_p99(c=128)` AND `B_p99(c=256) ≥ 4 × A_p99(c=256)`. The first half passes (1.80×); the second fails decisively (1.04×). NOT this bucket.
- ✅ **Robust DISCONFIRM** — both arms produce near-identical TTFTs across all concurrency steps even at sustained pressure. Admission genuinely doesn't help tail TTFT on this workload shape. **This is the bucket we landed in.**
- ❌ **Overload-protection emergence** — Arm B's vLLM degenerates while Arm A stays graceful. NOT this bucket — Arm B had 0 failures and lower p50 at c=256.

Per the runbook: "Pivot pitch to multi-model arbitration (Gate 2-lite) or queue-wait-spike avoidance."

---

## Implications for the launch post (raw observations, not decisions)

**What the data falsifies, definitively:**
- The original "InferGrid keeps inference engines below their scheduling cliff" pitch as applied to **single-model** serving on H100 SXM5 with bursty concurrency. vLLM's scheduler beats a coarse upstream admission cap on this workload.
- The Phase 1 "+1434% TTFT" cliff number stays falsified (PR #28/#31 caveat).
- Any framing that says "InferGrid prevents tail-latency blowup on overloaded engines" is now empirically wrong on this configuration.

**What the data does NOT address:**
- Multi-model contention. Two models co-loaded under bursty mixed traffic is the actual InferGrid differentiator and has never been benchmarked. vLLM's scheduler is per-engine; cross-model fairness needs a layer above. Gate 2-lite (`docs/launch/gate2_design.md`) tests this.
- Per-tenant fairness under contention. Same story — vLLM has no notion of tenants.
- Burst arrivals against a stable sustained background. This bench held concurrency constant per cell; real production traffic spikes.
- Smaller GPUs / lower-quality hardware where vLLM's scheduler may be less robust.
- Engines other than vLLM (SGLang, TensorRT-LLM).

**What the data weakly suggests:**
- The "thin-proxy" implementation of InferGrid (route + budget + observability without admission queueing above cap) might be the right product surface. The full admission-cap layer paid no dividends in this experiment and actively hurt at c=192.

Interpretation is the user's call. This document reports the raw numbers + the pre-committed math. See `docs/launch/gate1_runbook.md` § "Reading the Gate 1.5 result" + `docs/launch/gate2_design.md` for the next experimental step that the runbook itself prescribes.

---

## Rsync'd artifacts

- `armA/gate1_5A_20260419_135250_results.tar.gz` — 8.6 MB, extracted under `armA/results/gate1_5A_20260419_135250/`
- `armB/gate1_5B_20260419_140646_results.tar.gz` — 8.6 MB, extracted under `armB/results/gate1_5B_20260419_140646/`

Each contains:
- `benchmarks/concurrent_c{64,128,192,256}_summary.json` + `.csv` per-request rows
- `server.log` with PR #23 `req_id=... ENTER/EXIT` trace lines
- `engine_logs/infergrid_engine_vllm_*.log` (full vLLM stdout+stderr)
- `prometheus_dump.txt`
- `gpu_trace.csv` (1 Hz)
- `status_before.json` / `status_after.json`
- `phase_*.ts` timestamps

---

## Operational notes (improved over Gate 1)

- **Pod-restart-between-arms protocol from the new "Gate 1.5 Re-Run" runbook section worked exactly as designed.** Arm A → rsync → `runpod.stop_pod + resume_pod` (with `gpu_count=1` arg, see issue below) → `nvidia-smi` confirms 0 MiB used → re-scp env + bootstrap → Arm B launches cleanly. No CUDA-OOM. No 67.9 GiB ghost-process leak (the Gate 1 problem) because the pod was actually restarted, not just `pkill -9`'d.
- **`runpod.resume_pod()` API gotcha (FIXED in this same PR).** The runbook's example bash block originally called `runpod.resume_pod(pod_id)` as a one-arg call, which fails with `TypeError: resume_pod() missing 1 required positional argument: 'gpu_count'` against the current `runpod` Python SDK (1.9.0). The Gate 1.5 Re-Run section in `docs/launch/gate1_runbook.md` is updated in this PR to pass `gpu_count=1` explicitly. The next operator following the runbook will not hit this error.
- **Cost came in at ~$1.30 actual** (not the $7-10 estimate). Two factors: (a) bench wall was tighter than projected (~100s/cell × 4 cells × 2 arms = ~800s of bench wall, vs the conservative budget assuming engine-bring-up overhead per cell), (b) the wheel cache was hot on this image so engine pre-load was ~80s, not the 10-15 min budgeted.
- **No SSH port reuse after resume.** As predicted by the runbook, the public SSH port changed (12179 → 13670). The post-arm-A handoff script captured the new port from the SDK and passed it through.

---

## Decision tree from here

Per the runbook's pre-committed framing, the user owns interpretation. Three concrete next-step tracks the operator (this session) recommends spinning up:

1. **Run Gate 2-lite (`docs/launch/gate2_design.md`)** as already planned. Multi-model contention is the actual InferGrid differentiator and is now the load-bearing experiment for the launch pitch. ~$8 on A100-SXM4. The Gate 1.5 result narrows the hypothesis: if Gate 2-lite ALSO shows InferGrid ≈ baseline, the product needs a fundamental rethink before launch. If Gate 2-lite shows InferGrid wins on per-tenant fairness or multi-model resource arbitration, that becomes the pitch.

2. **Reframe the launch post.** PR #20 draft is anchored on a hypothesis that Gate 1.5 just falsified. Do not ship the current draft. Two viable framings to test once Gate 2-lite lands:
   - "What we shipped, what we measured, what we found out we were wrong about" — meta-honesty as the lead, with Gate 2-lite results as the supporting beat.
   - "Multi-model orchestration on 1 GPU without K8s, here's the working code, here's the concrete benchmark" — product-led, Gate 2-lite results as the hero number.

3. **Consider whether the AdmissionController layer should be made optional/off-by-default.** The Gate 1.5 data suggests admission as currently implemented is not load-bearing for single-model serving. If Gate 2-lite confirms it doesn't help multi-model either, the layer becomes a maintenance liability. If Gate 2-lite shows it helps cross-model fairness, the layer's defaults should be tuned for that case (e.g., per-tenant cap rather than global cap).

These are not mutually exclusive. (1) is required regardless. (2) should wait on (1). (3) is a downstream call.
