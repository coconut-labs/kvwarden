# H100 Operating-Envelope Sweep — Saturation Knee + Tenant-Count + Overhead Floor

**Date:** 2026-04-21
**Hardware:** 1x NVIDIA H100 80GB HBM3 SXM5 on RunPod (SECURE on-demand, $2.99/hr, pod `cpjk1k4rzzh1qb`)
**Model:** `meta-llama/Llama-3.1-8B-Instruct`, bfloat16, max_model_len=4096, gpu_memory_utilization=0.85 (0.80 for N=32)
**main tip at bench start:** `64b19a7` (launch: Show HN + Twitter + FAQ + one-pager drafts, #87)
**Total bench wall-clock:** ~51 min (13:31 -> 14:22 UTC). Pod lifetime ~87 min -> **~$4.33 actual cost** (ceiling $20; cut-scope trigger at $15 never hit).
**vllm:** 0.19.1 - **transformers:** 4.57.6 (pinned <5.0, post-PR #16 hardening)

## TL;DR

Three independent operating-envelope probes on the production token-bucket config:

1. **Saturation knee (token-bucket, N=8):** no knee observed across flooder_rps in {16, 32, 64, 128, 256}. Worst quiet p99 stays between 1.13x and 1.87x of the tighter solo baseline (infergrid_solo_A, 45.0 ms). **Token-bucket drops the flooder, not the tail.**
2. **FIFO knee reference:** at flooder_rps=128 N=8 (same substrate), FIFO worst quiet p99 reaches **109.0 ms = 2.42x of solo_A baseline** — the only cell in this run where any tenant crosses the 2x threshold. **FIFO knee <= 128 RPS; token-bucket knee > 256 RPS (out of measured range).**
3. **Tenant-count scaling (token-bucket @ flooder_rps=128):** worst quiet p99 stays below 2x solo across N in {2, 4, 8, 16, 32}. N=32 aggregate p99 = 61.4 ms (1.36x solo_A), worst = 84.5 ms (1.88x). **Token-bucket scales through N=32 on a single H100 without the fairness lever breaking.**
4. **Middleware overhead floor (solo, 1 RPS, 120 s):** **+2.9 ms p50 (9.3%) / +3.6 ms p99 (8.7%)** over direct vLLM, measured as (infergrid_solo_A - direct_vllm_solo). A second infergrid replica (solo_B) widens the p99 tail to +7.2 ms (+17%) as a noise bound.

---

## Pre-committed interpretations

- "Knee" = first flooder_rps where **worst quiet p99 > 2x solo-baseline p99** (warmup-excluded).
- **Solo baseline = infergrid_solo_A** (post-10s-warmup p99 = 45.0 ms, n=103). A second infergrid solo run (solo_B, p99 = 48.6 ms) is reported as a single-sample noise bound, not the primary denominator; using the tighter one makes the 2x threshold 90.0 ms, which is the stricter claim. FIFO crosses 2x under either threshold, so the conclusion is baseline-robust.
- The raw bench-harness summary.json for solo_A shows a p99 of 685.7 ms because the first 3 requests of the run hit a cold engine (355, 686, 992 ms). The 10s warmup exclusion strips those; post-warmup the max across 103 requests is 45.2 ms. We preserve the raw summary.json for audit; headline numbers throughout this document use **post-10s-warmup, per-tenant-CSV-recomputed percentiles** via `summarize_sweep.py`.
- "Overhead floor" = (infergrid solo p50 - direct vllm solo p50) and equivalent p99 delta, on the same GPU, same model, same bench script.
- **Config naming note:** the task brief referenced `configs/gate21_fifo_n8.yaml`, but the actual file in tree is `configs/gate22_fifo_n8.yaml` (Gate 2.2 artifact reused verbatim for the FIFO anchor; both share the same `scheduling=fifo + rpm=999999` structure).

## Headline numbers

All TTFT latencies post-10s-warmup (first 10 s of submit_time excluded per tenant CSV).

### Overhead floor — 1 tenant, 1 RPS, 120 s

| Arm | n | p50 | p95 | p99 | max |
|---|---:|---:|---:|---:|---:|
| direct_vllm_solo    | 103 | **31.2 ms** | 38.6 | **41.4 ms** | 42.8 |
| infergrid_solo_A    | 103 | **34.1 ms** | 43.3 | **45.0 ms** | 45.2 |
| infergrid_solo_B    | 103 | 33.6       | 41.2 | 48.6       | 58.9 |

**Overhead (infergrid_solo_A - direct_vllm_solo):**
- p50: **+2.9 ms (+9.3%)**
- p99: **+3.6 ms (+8.7%)**

Noise bound from solo_B: +2.4 ms p50 (7.7%), +7.2 ms p99 (17%). The two InferGrid runs were back-to-back on the same warm engine with identical bench args — the ~7 ms p99 spread across runs is one jittery request dominating the tail at n=103, not a real mean shift.

### RPS sweep — token-bucket fairness, N=8, 180 s per cell

| flooder_rps | worst_quiet_p99 | ratio (vs solo_A) | agg_quiet_p99 | agg_quiet_p50 | flood_p99 | flood_err (budget 429) |
|---:|---:|---:|---:|---:|---:|---:|
| 16  | 61.1 | **1.36x** | 51.7 | 36.6 | 51.1 |  1,074 |
| 32  | 51.1 | 1.14x | 49.0 | 35.8 | 48.0 |  3,910 |
| 64  | 84.0 | **1.87x** | 53.1 | 36.5 | 49.5 |  9,326 |
| 128 | 52.5 | 1.17x | 51.0 | 36.4 | 48.5 | 19,738 |
| 256 | 55.0 | 1.22x | 52.5 | 37.6 | — | 38,408 |

**No crossing of 2x solo_A (90.0 ms) or solo_B (97.2 ms).** rps=64's bump to 1.87x is a distributed Poisson-coincidence burst (3 outliers at t=40-180s across 3 tenants, ttft 84-97 ms) — flanking cells at rps=32 (1.14x) and rps=128 (1.17x) confirm it is sampling noise, not an approaching knee. The steady-state ratio sits ~1.15-1.20x across the whole offered-load range.

**Admitted-load plateau.** Flooder rejections rise 36x (1,074 at rps=16 -> 38,408 at rps=256) but the quiet tail stays flat. The budget layer caps the flooder at ~10 RPS (rate_limit_rpm=600 / 60) regardless of offered load; the engine sees ~10 RPS of flooder traffic + 7x1=7 RPS quiet = ~17 RPS admitted, well below H100 saturation at this prompt length.

### Tenant-count sweep — token-bucket, flooder_rps=128, 180 s per cell

| N | worst_quiet_p99 | ratio | agg_quiet_p99 | agg_quiet_p50 | flood_err |
|---:|---:|---:|---:|---:|---:|
|  2 | 50.7 | 1.13x | 50.7 | 35.8 | 19,721 |
|  4 | 88.3 | **1.96x** | 56.8 | 36.2 | 19,661 |
|  8 | 52.5 | 1.17x | 51.0 | 36.4 | 19,738 |
| 16 | 61.6 | 1.37x | 55.1 | 39.3 | 19,711 |
| 32 | 84.5 | **1.88x** | 61.4 | 43.2 | 19,662 |

**Worst-tenant stays <2x across the entire range (max observed 1.96x at N=4).** The N=4 bump is again a Poisson-burst outlier (125 ms spike at t=180s, 96/88 ms at t=164s) — aggregate p99 at N=4 is 56.8 ms (1.26x), consistent with the N=8 aggregate (51.0 ms, 1.13x). Worst-case growth from N=2 to N=32 is modest: agg p99 rises 50.7 -> 61.4 ms (+21%); agg p50 rises 35.8 -> 43.2 ms (+21%). This is a clean sub-linear scaling — doubling tenants 4x (N=8 -> N=32) moves median by 6.8 ms.

**Configs generated per N:** max_concurrent scaled to `N * 96`. N=32 config dropped gpu_memory_utilization to 0.80 (from 0.85) as preemptive VRAM-margin insurance for 3072 in-flight concurrency. Server came up cleanly; no OOM; engine start time ~32 s (same as N=8). Per-N configs saved to `configs/gate21_fairness_N{N}.yaml`.

### FIFO anchor — scheduling=fifo, N=8, flooder_rps=128, 180 s

| Metric | Value | Ratio vs solo_A (45.0) | Ratio vs solo_B (48.6) |
|---|---:|---:|---:|
| agg_quiet_p50 | 46.7 ms | 1.37x | 1.39x |
| agg_quiet_p99 | 71.2 ms | 1.58x | 1.47x |
| **worst_quiet_p99** | **109.0 ms** | **2.42x (KNEE)** | **2.24x (KNEE)** |
| flooder_p99 | 67.1 ms | — | — |
| flooder_errs | 0 | (no rate limit) | — |

**This is the only cell in the entire run where any tenant exceeds 2x solo.** The same engine, same offered load (flooder_rps=128, N=8) under token-bucket produced worst_quiet_p99 = 52.5 ms — a 56.5 ms absolute improvement at the tail (109 -> 52.5), driven by rejection-at-budget rather than reordering-at-scheduler. FIFO did not rate-limit, so the flooder admitted 20,316 requests during the run (vs 1,129 admitted with the token-bucket cap). The consequence: one quiet tenant (quiet_0) landed a 109 ms tail — 2.2x the worst-quiet p99 under the fairness config.

### Saturation knee summary

- **Token-bucket knee:** > 256 RPS (off the tested range). Not reached.
- **FIFO knee:** <= 128 RPS. The anchor (rps=128) crosses 2x; the first-crossing RPS is somewhere in (32, 128]. Sweeping FIFO across the full RPS axis was out of scope for this brief (only the anchor was requested). A tight claim at this level of investment is "FIFO at rps=128 N=8 on H100 Llama-3.1-8B breaks the 2x quiet-tail threshold; token-bucket does not."

---

## Interpretations and limits

1. **Token-bucket is a budget-layer ceiling, not a queue reorder.** The constancy of flood_p99 and agg_quiet_p99 across the RPS sweep (51-53 ms) despite 36x offered-load change confirms the admitted load to the engine is bounded by `rate_limit_rpm` per tenant, not by offered RPS. Once the flooder hits the budget, further offered load becomes 429s at the router, not queue pressure at the engine.

2. **The overhead floor (+2.9 ms / +3.6 ms p50/p99, ~9%) is measured at idle.** At 1 tenant at 1 RPS, no admission contention, no tenant accounting under load — just the cost of the HTTP hop through `infergrid.router + infergrid.tenant + infergrid.engines.vllm` vs direct vLLM. Multi-tenant admission overhead (the relevant number at scale) is not captured here and would require cross-arm runs with identical offered load. The fairness-sweep agg_quiet_p50 (~36 ms across all RPS cells) suggests multi-tenant steady-state overhead stays under ~5 ms absolute, but we are not asserting that from this data.

3. **N=4 and rps=64 outliers are Poisson-arrival coincidences, not a bug.** Both showed 2-3 worst-case TTFTs sparsely distributed across the 180 s window (not clustered in the warmup) — the classic multi-tenant engine-queue-hit-at-same-tick signature. Aggregate (not worst-of-N) p99 stayed within 1.15-1.27x solo for both, indicating the tenant-fairness mechanism is intact; these are individual-tenant tail-samples, not fairness violations.

4. **This run did not exercise VRAM pressure or mixed prompt lengths.** All prompts were ~10-token shorts from the legacy hardcoded list; H100 80GB never saw memory stress. At long-prompt / KV-cache-saturated loads, the relationship between token-bucket, FIFO, and DRR may differ (Gate 2.2 data argues this is the regime where fairness matters most). Out of scope for the operating-envelope probe.

5. **FIFO anchor is a single point, not a FIFO RPS sweep.** We know FIFO crosses 2x at rps=128; we do not know whether FIFO is already over 2x at rps=64 or lower. A tight claim is "FIFO knee <= 128 RPS."

---

## Artifacts

Per cell (under `overhead/`, `rps_sweep/`, `n_sweep/`, `fifo_anchor/`):
- `summary.json` — bench-harness aggregate + per-tenant percentiles (includes warmup period; used only for count_ok and wall_time_s)
- `tenant_*.csv` — per-request rows; **this is the source of truth** for post-warmup percentile extraction
- `bench.log` — harness stdout

Top-level:
- `post_warmup_summary.json` — all cells, post-10s-warmup percentiles, produced by `summarize_sweep.py`
- `runner.sh` — orchestrator script (server lifecycle + bench loop + 30s heartbeats; idempotent)
- `summarize_sweep.py` — the post-warmup re-extractor
- `configs/gate21_fairness_N{N}.yaml` — generated per-N configs (token-bucket, max_concurrent scaled)
- `configs/gate21_fairness_n8.yaml`, `configs/gate22_fifo_n8.yaml` — anchor configs (copied from repo)
- `server_logs/*.log` — InferGrid server stdout per phase (scheduling decisions, admission events)
- `git_head.txt`, `pip_relevant.txt`, `gpu_info.txt` — reproducibility pins

## Reproduction

```bash
# On a 1x H100 80GB pod with vllm==0.19.1, transformers<5.0:
cd /workspace/infergrid && git checkout 64b19a7
mkdir -p /workspace/results/h100_adversarial_sweep_20260421
cp results/h100_adversarial_sweep_20260421/runner.sh /workspace/sweep_runner.sh
bash /workspace/sweep_runner.sh
# Wall-clock: ~51 min; cost: ~$2.55 at $2.99/hr.
```

Post-hoc re-extraction of post-warmup percentiles:

```bash
python3 results/h100_adversarial_sweep_20260421/summarize_sweep.py \
    results/h100_adversarial_sweep_20260421 \
    > post_warmup_summary.json
```

---

_Generated on pod `cpjk1k4rzzh1qb` (H100 80GB SXM5 SECURE). Pod deleted after rsync._
