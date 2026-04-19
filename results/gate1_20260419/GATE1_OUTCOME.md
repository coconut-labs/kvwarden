# Gate 1 — Admission-control TTFT validation on H100 SXM5

**Date:** 2026-04-19
**Hardware:** 1× NVIDIA H100 SXM5 80GB HBM3 on RunPod (SECURE on-demand, $2.99/hr)
**Pod ID:** `6xz1kdtv020f03`
**Duration:** ~20 minutes wall clock across two arms (12:21 → 12:36 UTC)
**Cost:** ~$1.00 (two arms × ~10 min each at $2.99/hr; well under the $6 ceiling and the MAX_POD_SECS=7200 budget)
**Main at pod clone:** `0e3b1bd` — PRs #28-37 all active
**Model:** `meta-llama/Llama-3.1-8B-Instruct`, bfloat16, max_model_len=4096, gpu_memory_utilization=0.85

## Raw numbers

### Arm A — `configs/gate1_admission.yaml` (max_concurrent=128)

| Metric | c=128 | c=256 |
|---|---:|---:|
| num_successful | 400 | 400 |
| num_failed | 0 | 0 |
| ttft_p50_ms | 594.2 | 3302.3 |
| ttft_p95_ms | 3370.7 | 5906.2 |
| ttft_p99_ms | **3374.1** | **5964.2** |
| total_latency_p50_ms | 2787.5 | 5292.2 |
| total_latency_p99_ms | 5361.3 | 7987.7 |
| throughput_tok_per_sec | 4712.8 | 4788.9 |
| wall_time_ms | 10839.5 | 10692.7 |
| gpu_compute_util_mean | 74.0% | 73.5% |

Arm A admission wait histogram (Prometheus, includes both concurrency levels + 1 smoke):
- 529 admissions ≤1ms (fast path, in_flight < cap at arrival)
- 216 admissions 1-5s (queued)
- 56 admissions 5-10s (queued longer)
- **Total wait across 801 admissions: 854.1 seconds (avg 1.07s per admission).**

### Arm B — `configs/gate1_admission_off.yaml` (max_concurrent=1024)

| Metric | c=128 | c=256 |
|---|---:|---:|
| num_successful | 400 | 400 |
| num_failed | 0 | 0 |
| ttft_p50_ms | 612.3 | 3406.0 |
| ttft_p95_ms | 3357.5 | 6142.4 |
| ttft_p99_ms | **3425.6** | **6177.1** |
| total_latency_p50_ms | 2827.8 | 5469.2 |
| total_latency_p99_ms | 5362.1 | 8130.5 |
| throughput_tok_per_sec | 4699.8 | 4646.6 |
| wall_time_ms | 10850.7 | 10986.1 |
| gpu_compute_util_mean | 74.3% | 72.5% |

Arm B admission wait histogram:
- **801 admissions ALL ≤1ms** (fast path; cap=1024 never reached with 256 offered concurrency)
- **Total wait across 801 admissions: 0.001 seconds (avg 1.3 µs per admission).**
- Admission control did not engage. This matches the experimental design — Arm B is the control.

## Plumbing sanity — BEFORE reading the hypothesis

Per `docs/launch/gate1_runbook.md` "Plumbing sanity" section:
- Arm A `prometheus_dump.txt` confirms admission engagement (272 queued admissions with 1-10s waits).
- Arm B `prometheus_dump.txt` confirms admission open (all admissions ≤1ms).
- Both arms completed all 800 requests (400 × 2 concurrency levels) with 0 failures.
- No CORRECTIONS.md-tagged measurement bugs active (this run is post-PR-#28, #29, #31, #32, #33, #37).

The experiment is wired correctly. The numbers are honest (real first-token, not SSE first-frame RTT).

## Hypothesis math (pre-committed in the runbook)

| Predicate | Required | Measured | Result |
|---|---|---|---|
| `A_p99(c=256) ≤ 2 × A_p99(c=128)` | ≤ 2.00× | 5964.2 / 3374.1 = **1.77×** | PASS |
| `B_p99(c=256) ≥ 4 × A_p99(c=256)` | ≥ 4.00× | 6177.1 / 5964.2 = **1.04×** | FAIL |

Arm A does flatten TTFT p99 within 2× going c=128 → c=256. But Arm B does not explode relative to Arm A; the two arms are statistically indistinguishable (p99 differs by 3.5%).

## Rsync'd artifacts

- `armA/gate1A_20260419_122131_results.tar.gz` — 464 KB, extracted under `armA/results/gate1A_20260419_122131/`
- `armB/gate1B_20260419_123226_results.tar.gz` — 463 KB, extracted under `armB/results/gate1B_20260419_123226/`

Each contains:
- `benchmarks/concurrent_c{128,256}_summary.json` + `.csv` per-request rows
- `server.log` with PR #23 `req_id=... ENTER/EXIT` trace lines
- `engine_logs/infergrid_engine_vllm_*.log` (full vLLM stdout+stderr)
- `prometheus_dump.txt`
- `gpu_trace.csv` (1 Hz)
- `status_before.json` / `status_after.json`
- `phase_*.ts` timestamps

## Notes from the run (not interpretations)

- **Arm A run 1 completed cleanly in ~4 min wall** (Phase 1 apt 14s, Phase 2 pip 137s, Phase 3-6 ~66s, Phase 7 bench 23s, Phase 8 capture). Unusual speed vs Gate 0.6 (2h); most plausibly the base image had the wheel cache hot.
- **Arm B run 1 FAILED with CUDA OOM at engine pre-load.** Arm A's vLLM subprocess did not release 67.9 GiB of GPU memory despite the `bundle_and_mark` trap's `pkill -9 -f "vllm.entrypoints"` (the process was in an unkillable state; `nvidia-smi` listed it as `[Not Found]` but still owning memory). `nvidia-smi --gpu-reset` is "Not Supported" on containerized pods.
- **Recovery:** stopped + resumed the pod via `runpod.stop_pod` + `runpod.resume_pod` (~1.5 min). This cleared the GPU to 0 MiB used. The pod's `/workspace` is on the container overlay filesystem — contents are LOST on stop+resume. Re-scp'd env + bootstrap, relaunched Arm B.
- **Arm B retry completed cleanly in ~4 min wall**, same shape as Arm A.
- **The `bundle_and_mark` trap + PR #35 cost-cap layer-1 (poweroff) is insufficient for inter-arm GPU reuse.** Followup: the runbook should explicitly say "restart the pod between arms" or automate it.

## Interpretation

Per the advisor and the runbook's pre-committed rubric: **interpretation is the user's call, not the operator's.** This document reports raw numbers + the pre-committed math. See `docs/launch/gate1_runbook.md` § "Reading the result" for CONFIRM / DISCONFIRM / PLUMBING-REGRESSION decision tree.

The raw numbers land in the DISCONFIRM bucket (B_p99 / A_p99 = 1.04×, needed ≥ 4×). The PLUMBING-REGRESSION bucket is ruled out (both arms completed, admission histogram correct). What this means for the pitch is a question for the launch post, not this OUTCOME doc.
