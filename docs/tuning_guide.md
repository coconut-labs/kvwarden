# KVWarden Tuning Guide — When to Use Which Lever

**Audience:** operators running KVWarden in front of vLLM/SGLang on a single GPU or small cluster.
**Status:** based on empirical results from Gates 0/0.5/0.6/1/1.5/2-FAIRNESS (2026-04-19). Each recommendation links to the experiment that produced the evidence.

KVWarden exposes three orthogonal scheduling levers. Most production tuning regrets come from reaching for the wrong one. This document walks through what each lever actually does — measured, not theorized — and gives a decision tree by workload shape.

---

## TL;DR — the wrong-lever mistakes

| Lever | Don't use it for... | Why |
|---|---|---|
| `max_concurrent` (admission cap) | Single-tenant tail-latency reduction | Modern vLLM continuous batching matches a coarse upstream cap. At cap > offered load, no effect. At cap < offered load, the queue *adds* tail latency without removing engine-side latency. (Gate 1.5: B/A=1.04× at c=256; cap=128 actively hurt at c=192 by 69% at p99.9.) |
| `tenant_defaults.scheduling: drr` | Rescuing a starved tenant on a saturated engine, alone | DRR reorders KVWarden's admission queue. vLLM's internal scheduler is tenant-blind and runs *after* admission. Reordering above a saturated engine doesn't propagate. (Gate 2-FAIRNESS Arms 3 + 4 both DISCONFIRM-DRR-no-help.) |
| `tenant_defaults.rate_limit_rpm` (without `rate_limit_burst`) | Fairness-critical multi-tenant from t=0 | The default 60-second sliding-window-equivalent capacity allows ~`rate_limit_rpm` requests through before any 429s fire. A 32-RPS flooder drains a 600-RPM bucket in ~19 seconds, during which the engine saturates and the quiet tenant inherits the saturation. (Gate 2-FAIRNESS Arm 5: full-bench p99 = 5378ms despite ~30ms steady state.) |

**Use the right lever for the right job. Reach for `rate_limit_burst` first when the answer is "fairness."**

---

## The three levers in one paragraph each

### 1. `max_concurrent` (admission cap)

The number of in-flight requests KVWarden forwards to the engine. Above the cap, requests queue at the KVWarden layer; the queue is ordered by `tenant_defaults.scheduling`. **Useful for:** preventing engine OOM under burst (the original "scheduling cliff" intuition), back-pressuring runaway clients before they reach the engine, gating slow models that can't keep up. **Not useful for:** TTFT optimization on modern continuous-batching engines (vLLM ≥ 0.6, SGLang ≥ 0.4) at concurrency where the engine has not yet OOM'd.

### 2. `tenant_defaults.scheduling` (`fifo` | `drr`)

Discipline for the admission queue. `fifo` (default): requests dequeue in arrival order, with length-bucket priority breaking ties (short prompts before long). `drr` (deficit round-robin): tenants with more in-flight requests wait behind quieter tenants (`TenantRecord.priority_score = active_requests * 10 + budget.priority`). **Useful for:** giving short bursts of small requests preference within a single tenant; modest cross-tenant fairness when the admission cap actually binds AND there is meaningful queue depth. **Not useful for:** rescuing a starved tenant on its own — vLLM's internal batching is tenant-blind and runs after admission.

### 3. `tenant_defaults.rate_limit_rpm` + `rate_limit_burst` (token-bucket rate limit)

Per-tenant request rate cap. Refill rate = `rate_limit_rpm / 60` tokens/sec. Capacity = `rate_limit_burst` (defaults to `rate_limit_rpm` for sliding-window-equivalent backward compat). Above-cap requests get `429 Too Many Requests` at the budget gate, BEFORE any admission queueing. **Useful for:** the actual mechanism that delivers per-tenant fairness. By 429-rejecting a flooder above its quota, KVWarden prevents flooder requests from saturating vLLM's internal batch queue — the layer where starvation actually lives. **The single most under-used lever.** Set `rate_limit_burst` to ~1 second of capacity (`rate_limit_rpm / 60`) for fairness-critical workloads.

---

## Decision tree by workload shape

### A) Single-tenant, single-model
You don't need most of KVWarden's machinery. Run with a wide cap and let vLLM do its job.

```yaml
max_concurrent: 1024            # effectively off
admission_queue_size: 2048
tenant_defaults:
  max_concurrent_requests: 1024
  scheduling: fifo
```

**Evidence:** Gate 1.5 robust DISCONFIRM. Single-model admission cap delivers no TTFT benefit and actively hurts at c=192. Save KVWarden's value for the multi-tenant case.

### B) Multi-tenant, single-model, fairness matters (the canonical KVWarden case)
You have N tenants on a shared model and a noisy-neighbor risk. This is where KVWarden wins.

```yaml
max_concurrent: 256             # generous; let the engine do its scheduling
admission_queue_size: 2048
tenant_defaults:
  max_concurrent_requests: 512
  rate_limit_rpm: 600           # 10 RPS sustained per tenant
  rate_limit_burst: 10          # 1 second of burst capacity — engages from t=0
  scheduling: drr               # belt-and-suspenders; rate-limit does the heavy lifting
```

**Evidence:** Gate 2-FAIRNESS Arm 5b. Quiet tenant within 1.35× of solo baseline (74ms p99 vs 55ms solo) under a 32-RPS flooder. Per-window trace shows zero transient windows — quiet tenant is essentially unaware of the flooder.

### C) Multi-tenant, single-model, burst-tolerant traffic
If your tenants legitimately need to burst occasionally (batch jobs, periodic sync), set a larger `rate_limit_burst` so they can spike without 429s.

```yaml
tenant_defaults:
  rate_limit_rpm: 600
  rate_limit_burst: 60          # 6 seconds of burst — "burst on Mondays" workloads
  scheduling: drr
```

**Tradeoff:** larger burst → more warmup transient effect on co-tenants. Choose the smallest burst that admits your legitimate spikes.

### D) Single-tenant, multiple models on one GPU
Different problem entirely; admission and rate-limit are red herrings here. The lever is the model lifecycle in `WorkloadRouter` (frequency+recency eviction). Configs deferred to the multi-model gate (in progress).

### E) Engine-OOM-on-burst risk (legacy use case)
The original "scheduling cliff prevention" reason for `max_concurrent`. Still relevant if you're on an older engine version (vLLM < 0.5, no continuous batching) or running models that genuinely OOM under load. Set `max_concurrent` to ~80% of your empirically-observed OOM threshold.

```yaml
max_concurrent: 32              # tight — set just below where the engine falls over
admission_queue_size: 1024
```

**Evidence:** Gate 0 documented vLLM v1 OOM with co-loaded Qwen + Llama at default `gpu_memory_utilization`. The fix was a tighter `gpu_memory_utilization`, not the KVWarden cap — but the cap is a useful safety net.

---

## Reference configs in the repo

| Config | Use case | Evidence |
|---|---|---|
| `configs/gate1_admission.yaml` | Pre-pivot single-model admission test | Gate 1.5 DISCONFIRM (single-model admission doesn't help) |
| `configs/gate2_fairness_token_bucket.yaml` | **Recommended starting point for multi-tenant single-model** | Gate 2-FAIRNESS Arm 5b CLEAN CONFIRM |
| `configs/gate2_fairness_drr_ratelimit.yaml` | Rate limit with default sliding-window-equivalent burst — has the warmup transient documented in OUTCOME | Reference only; prefer the token-bucket config |
| `configs/gate2_fairness_fifo.yaml` | Demonstrates the no-fairness baseline (FIFO + wide cap = vanilla vLLM behavior) | Gate 2-FAIRNESS Arm 1 (the 523× starvation) |

---

## Live observability

Each lever surfaces metrics for in-production tuning. Hit `/metrics` (Prometheus format) and look for:

| Metric | What to watch for |
|---|---|
| `kvwarden_admission_in_flight` (gauge) | If this consistently sits below `max_concurrent`, the admission cap isn't binding and is doing nothing for you. Lower the cap or remove the lever from your decision space. |
| `kvwarden_admission_queue_depth` (gauge) | If this is consistently > 0, the cap is binding. Check whether you actually want it to. |
| `kvwarden_admission_wait_seconds` (histogram) | The le=0.001 bucket ratio tells you the fast-path admission rate. If most admits take >1s, the cap is too tight. |
| `kvwarden_tenant_rejected_total{reason="budget_exceeded"}` (counter) | Should be > 0 for tenants that exceed their `rate_limit_rpm`. If 0 for a tenant you expect to flood, your rate-limit isn't engaging — check `rate_limit_burst` and your sliding-window-vs-token-bucket config. |
| Per-tenant `usage.rate_limit_tokens_remaining` (in `/status`) | Live token bucket level per tenant. If a tenant is at 0 and you're seeing 429s for them, the rate-limit is doing its job. |

---

## When to call your shot

Before reaching for a lever, ask:

1. **Did I measure?** A 60-second smoke bench against your actual model with your actual workload shape will tell you more than 10 hours of intuition. The `benchmark_two_tenant_single_model.py` script in `benchmarks/scripts/` is a fast way to repro the Gate 2-FAIRNESS shape against your config.
2. **Is the engine the bottleneck or am I?** Watch `kvwarden_admission_in_flight` over a load test. If it never approaches `max_concurrent`, the admission layer isn't where the latency lives — don't tune it.
3. **Is fairness or throughput the goal?** They're different problems. Fairness needs `rate_limit_burst`. Throughput needs `gpu_memory_utilization` and `tensor_parallel_size` — engine-level knobs KVWarden doesn't gate.

---

## Empirical reference

All recommendations here trace to a measured experiment. Read the OUTCOME docs for the data:

- `results/gate1_5_20260419/GATE1_5_OUTCOME.md` — single-model admission cap is not load-bearing
- `results/gate2_fairness_20260419/GATE2_FAIRNESS_OUTCOME.md` — full 5-arm tenant-fairness experiment
- `results/gate2_fairness_20260419/GATE2_FAIRNESS_SUPPLEMENT_arm5b.md` — token-bucket clean CONFIRM (the recommended config)

If your workload doesn't match any of the above, the honest recommendation is: **run your own bench first.** KVWarden was built around the Gate 2-FAIRNESS workload shape; other shapes may need other levers.
