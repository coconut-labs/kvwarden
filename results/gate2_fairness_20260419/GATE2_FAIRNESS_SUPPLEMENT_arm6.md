# Gate 2-FAIRNESS — Arm 6 Supplement: DRR-Isolation Control

**Date:** 2026-04-19
**Hardware:** 1× NVIDIA A100-SXM4 80GB on RunPod (SECURE on-demand, fresh pod)
**Cost:** ~$0.30 (~10 min billable wall × $1.89/hr)
**Code tip:** `c608763` (PR #50 — Arm 6 config). PR #47's token-bucket from Arm 5b unchanged.
**Config:** `configs/gate2_fairness_fifo_token_bucket.yaml` (IDENTICAL to Arm 5b's `gate2_fairness_token_bucket.yaml` except `tenant_defaults.scheduling: fifo`).
**Status:** **DRR contribution is NOT material on this workload.** Arm 6 (FIFO + token bucket) ≈ Arm 5b (DRR + token bucket) on every percentile. The token bucket is doing 100% of the fairness work; the DRR wiring shipped in PR #42 is ornamental for this workload shape.

---

## The hypothesis

The Arm 5b launch-hero config combines two mechanisms (`scheduling: drr` + `rate_limit_burst: 10`). Arm 6 isolates which one is doing the work by setting `scheduling: fifo` and otherwise keeping the config identical.

- If Arm 6 ≈ Arm 5b → token bucket is the load-bearing mechanism; DRR contributes nothing here.
- If Arm 6 materially worse than Arm 5b → DRR earns its keep.

Pre-committed threshold (from `configs/gate2_fairness_fifo_token_bucket.yaml` header): "DRR contribution material" if Arm 6.quiet_p99 > 1.5 × Arm 5b.quiet_p99 (~110ms given 5b's 74ms).

---

## Result

| Metric | Arm 5b (DRR + token bucket) | Arm 6 (FIFO + token bucket) | Δ |
|---|---:|---:|---:|
| quiet ttft_p50_ms | 33.0 | 37.5 | +4.5 (+14%) |
| quiet ttft_p95_ms | 59.1 | 59.4 | +0.3 (~0%) |
| quiet ttft_p99_ms | **74.2** | **73.4** | **−0.8 (−1%)** |
| quiet ttft_max_ms | 74.5 | 95.2 | +20.7 (+28%) |
| quiet count_ok | 113 | 113 | — |
| quiet count_err | 0 | 0 | — |
| flooder ttft_p50_ms | 32.2 | 32.6 | +0.4 |
| flooder ttft_p99_ms | 66.3 | 75.6 | +9.3 |
| flooder count_ok | 1208 | 1209 | +1 |
| flooder count_err (rate-limited) | 2588 | 2587 | −1 |

**Pre-committed threshold:** material if quiet p99 > 110ms. Arm 6 quiet p99 = **73.4ms** → **NOT MATERIAL.** Conclusion: token bucket is doing the work.

---

## Interpretation

Both arms achieve essentially identical fairness for the quiet tenant under flooder contention. The pinpoint differences:

- Quiet **p50** is 14% higher in Arm 6 (37.5ms vs 33.0ms). On 113 samples, this is statistically thin and could be either DRR providing modest median improvement or sample noise.
- Quiet **p99** is essentially identical (74.2 vs 73.4) — within sample noise.
- Quiet **max** is 28% higher in Arm 6 (95.2ms vs 74.5ms). One sample. Could be a single bad batch alignment in FIFO that DRR would have reordered around. Or sample noise.

The headline finding: **on this workload, the token-bucket rate limit at the budget gate gets the quiet tenant to baseline fairness with or without DRR scheduling above it.** PR #42's DRR implementation is correct (its 7 unit tests verify priority semantics), but doesn't load-bear in this single-engine, two-tenant scenario.

---

## Implications

### For the launch post
Don't lead with DRR. The launch-post draft (`docs/launch/gate0_launch_post.md` v2 on `draft/gate0-launch-post`) correctly attributes the win to the token bucket, with DRR only mentioned as "belt-and-suspenders" in the YAML diff. That framing now has explicit empirical support.

### For the codebase
PR #42's DRR wiring stays shipped. Two arguments for keeping it:
1. It may matter for workload shapes we haven't tested (e.g., >2 tenants with mixed priorities, or workloads where the admission cap actually binds).
2. The cost of keeping it is ~24 LOC + 7 tests. The risk of removing it is "discover later that some workload shape needed it."

If a future experiment shows DRR doesn't load-bear in any shape we care about, deprecate then. Until then, default to "more correct, more flexible, low-overhead" beats "remove for theoretical purity."

### For the tuning guide
`docs/tuning_guide.md` already correctly de-prioritizes DRR — section "When NOT to use" lists `scheduling: drr` as not-load-bearing for tenant-fairness rescue. Arm 6 confirms; no doc edit needed.

---

## Sample-size honesty

Same caveats as Arm 5b apply: 113 quiet requests at 1 RPS over 120s. p99 has ~1 sample below it; comparisons to Arm 5b at the percentile level are statistically thin. The aggregate finding (both arms cluster around quiet p99 ≈ 74ms; no order-of-magnitude difference) is robust. Per-percentile micro-deltas (the 14% p50 gap, the 28% max gap) are not.

---

## Artifacts

- `gate2f_arm6_20260419_165802/gate2f_arm6_*_results.tar.gz` (240 KB tarball)
- Extracted: tenant CSVs, summary.json, server.log, engine_logs/, prometheus_dump.txt, gpu_trace.csv, status_before/after, phase_*.ts.

---

## Total session compute spend on Gate 2-FAIRNESS

| Run | Cost | Notes |
|---|---:|---|
| Arm 0 (solo baseline, 240s) | ~$0.30 | first pod |
| Arm 1 (FIFO contended) | ~$0.30 | second pod |
| Arm 3 (DRR cap=256) | ~$0.30 | third pod |
| Arm 4 (DRR cap=16) | ~$0.30 | fourth pod (+ one failed retry) |
| Arm 5 (sliding-window rate limit) | ~$0.30 | fifth pod |
| Arm 5b (token-bucket rate limit) | ~$0.30 | sixth pod |
| **Arm 6 (FIFO + token bucket)** | **~$0.30** | **seventh pod** |
| **Total** | **~$2.10** | well under the $8 ceiling |
