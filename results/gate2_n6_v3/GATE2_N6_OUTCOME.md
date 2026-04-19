# Gate 2 N=6 Tenants — Outcome

**Date:** 2026-04-19
**Hardware:** Single A100-SXM4 80GB (RunPod, fresh pod)
**Software:** vLLM 0.19.1, InferGrid main @ post-PR-#61
**Bench:** `benchmarks/scripts/benchmark_n_tenant_single_model.py`, 1 flooder + 5 quiet, 300 s sustained
**Config:** `configs/gate2_fairness_token_bucket_n6.yaml` (max_concurrent=384, rate_limit_rpm=600, rate_limit_burst=10, scheduling=drr)
**Spend:** ~$0.50 (single pod, ~15 min)

## Headline

**The token-bucket per-tenant fairness guarantee scales linearly to N=6 tenants.**
With 5 quiet tenants sharing one Llama-3.1-8B engine alongside a 32-RPS flooder,
each quiet tenant's TTFT p99 (post-warmup) lands within 56-65 ms — within 1.21× of
the solo baseline (53.9 ms) for the worst tenant, with aggregate p99 = **61.0 ms
(1.13× of solo, n=1456)**.

## Per-tenant results (post 10 s warmup window)

| Tenant | n | p50 (ms) | p95 (ms) | p99 (ms) |
|---|---:|---:|---:|---:|
| quiet_0 | 311 | 42.1 | 55.4 | 61.0 |
| quiet_1 | 285 | 41.5 | 55.4 | 60.4 |
| quiet_2 | 285 | 41.9 | 55.3 | 65.0 |
| quiet_3 | 280 | 41.5 | 54.2 | 56.1 |
| quiet_4 | 295 | 41.8 | 54.2 | 62.3 |
| **Aggregate** | **1456** | **41.8** | **54.8** | **61.0** |

**Per-tenant p95 spread is only 1.2 ms (54.2-55.4 ms across all 5 tenants).**
At the user-perceived latency floor (p95), every quiet tenant gets effectively
identical service. No tenant is systematically disadvantaged.

Solo baseline (Arm 0 v3, no flooder): p99 = 53.9 ms (n=320). Aggregate ratio: **1.13×**.

Worst-tenant ratio: 65.0 / 53.9 = **1.21× of solo** for `quiet_2`. Per-tenant spread
across the 5 quiet tenants: 56.1-65.0 ms p99 (range 8.9 ms).

## Compared to N=2 (Track A v3 Arm 5b)

| Workload | Quiet p99 (post-warmup) | Of solo |
|---|---:|---:|
| 1 quiet vs 1 flooder | 61.5 ms | 1.14× |
| 5 quiet vs 1 flooder | 61.0 ms (agg), 65.0 ms (worst) | 1.13× / 1.21× |

The fairness guarantee is **not degraded** by adding 4 more quiet tenants. This is
the strongest test of the per-tenant token-bucket mechanism we've shipped to date.

## Methodology notes

- First 10 s window excluded per CORRECTIONS C7 — vLLM JIT compile transient.
  Aggregate p99 including warmup: 2078 ms (one outlier per tenant from cold-start).
- Same Poisson arrivals per tenant (independent RNG seeds).
- All 5 quiet tenants and the flooder share a single vLLM engine (Llama-3.1-8B).
- Flooder rejected requests: 6488 / 9497 = 68% (rate-limit at 10 RPS doing its job).

## What this DOESN'T claim

- N=10+ tenants — not tested.
- Mixed prompt-length workloads (all tenants used the same short-prompt set).
- Heterogeneous quiet RPS (all 5 quiet ran at exactly 1 RPS).
- A single flooder pattern (32 RPS constant Poisson). Bursty flooders not tested.

## Raw artifacts

`results/gate2_n6_v3/gate2_n6_20260419_205733/`:
- `summary.json` — bench-emitted summary
- `tenant_flooder.csv` + `tenant_quiet_{0..4}.csv` — per-request CSVs
- `server.log` — InferGrid + vLLM logs
- `prometheus_dump.txt` — final /metrics snapshot
