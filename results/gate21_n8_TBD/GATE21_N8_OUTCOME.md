# Gate 2.1 (N=8) — Per-Tenant Fairness at N=8 on a Shared vLLM Engine

**Date:** <TBD_DATE>
**Hardware:** 1× <TBD_GPU_MODEL> on RunPod (SECURE on-demand, ~$<TBD_HOURLY>/hr) — see Caveats re: GPU substitution from A100-SXM4 80GB.
**Total cost:** ~$<TBD_COST_ACTUAL> actual (ceiling $<TBD_COST_CEILING>)
**main tip at start:** `<TBD_SHA>` (<TBD_PR_POINTER>).
**Model:** `meta-llama/Llama-3.1-8B-Instruct`, bfloat16, max_model_len=4096, gpu_memory_utilization=0.85
**Workload:** 1 flooder + 7 quiet tenants, 300s sustained, 128-token outputs. Extends the N=6 CONFIRM (Gate 2-FAIRNESS) to N=8 on a single GPU serving a single engine.
**Status:** **<TBD_STATUS>.** <TBD_STATUS_ONELINER>

**Hypothesis going in:** the fairness property observed at N=2 and N=6 holds at N=8 — that is, per-tenant budget-layer rate-limiting keeps the 7 quiet tenants' TTFT near solo baseline even when a single flooder saturates the shared engine. Null: fairness degrades with N (per-tenant baseline quota shrinks, contention for flooder's own capacity grows, warmup transients stack).

---

## Headline numbers

| | Arm 0 solo baseline | Arm 1 FIFO contended | Arm 5 DRR + rate_limit |
|---|---:|---:|---:|
| `quiet_user.ttft_p50` (median across 7 quiets) | **<TBD> ms** | <TBD> ms | **<TBD> ms** |
| `quiet_user.ttft_p99` (worst of 7 quiets, p99) | <TBD> ms | <TBD> ms | <TBD> ms |
| `quiet_user.count_ok` (sum across 7 quiets) | <TBD> / 300s | <TBD> / 300s | <TBD> / 300s |
| `flooder.count_ok` | (n/a) | <TBD> | <TBD> |
| `flooder.count_err` (429) | (n/a) | <TBD> | **<TBD>** |

**p50 starvation reduction (Arm 1 → Arm 5): <TBD>× improvement.**
**Per-quiet-tenant steady-state p50 spread (max − min across 7 quiets): <TBD> ms.**

---

## Pre-committed criteria (from advisor + runbook)

| Criterion | Value | Result |
|---|---|---|
| Arm 5 quiet_p99 (worst of 7) ≤ 110ms (2× solo baseline) | <TBD> ms | **<TBD>** |
| Arm 5 flooder gets 429'd (rate-limit fires) | tenant_rejected=<TBD>; flooder count_err=<TBD> | **<TBD>** |
| Arm 5 no quiet tenant gets 429'd (plumbing check) | quiet count_err sum=<TBD> | **<TBD>** |
| Arm 5 per-quiet-tenant spread ≤ 1.5× | max/min=<TBD> | **<TBD>** |

The fourth criterion is new at N=8: with 7 quiets, "fairness" isn't just flooder-vs-quiet, it's also quiet-vs-quiet. If DRR is working, the 7 quiets should be within ~1.5× of each other on p50 TTFT.

---

## All arms — full data table

| Arm | Config | quiet_p50 (median/worst) | quiet_p99 (worst) | flood_p50 | flood_p99 | flood_err | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| **0** | gate2_fairness_fifo + flooder_rps=0 (solo baseline) | **<TBD> / <TBD>** | **<TBD>** | n/a | n/a | n/a | 7 quiets alone, <TBD> req each |
| **1** | gate2_fairness_fifo + flooder_rps=32 (no fairness) | **<TBD> / <TBD>** | **<TBD>** | <TBD> | <TBD> | <TBD> | expected: starvation on all 7 |
| **5** | gate2_fairness_drr_ratelimit (rate_limit=600 RPM) | **<TBD> / <TBD>** | **<TBD>** | <TBD> | <TBD> | **<TBD>** | expected: all 7 quiets near baseline |

---

## What the data falsifies and confirms

### Confirmed
- <TBD>

### Falsified
- <TBD>

### Diagnosed
- <TBD>

---

## Per-quiet-tenant breakdown (Arm 5)

| Tenant | count_ok | ttft_p50 (ms) | ttft_p99 (ms) | count_err |
|---|---:|---:|---:|---:|
| quiet_1 | <TBD> | <TBD> | <TBD> | <TBD> |
| quiet_2 | <TBD> | <TBD> | <TBD> | <TBD> |
| quiet_3 | <TBD> | <TBD> | <TBD> | <TBD> |
| quiet_4 | <TBD> | <TBD> | <TBD> | <TBD> |
| quiet_5 | <TBD> | <TBD> | <TBD> | <TBD> |
| quiet_6 | <TBD> | <TBD> | <TBD> | <TBD> |
| quiet_7 | <TBD> | <TBD> | <TBD> | <TBD> |
| **spread** | — | **<TBD>** | **<TBD>** | — |

If the spread column shows the 7 quiets within ~1.5× on p50, DRR is genuinely distributing across tenants. If it shows 3-5× spread, DRR is no-op and quiet-vs-quiet fairness is arrival-order-dependent.

---

## Operational notes

- <TBD: pod spin count / any resume vs terminate lessons>
- <TBD: config availability on main — bootstrap limitation reminder if relevant>
- <TBD: cost actual vs ceiling>

---

## Implications for the launch post

<TBD: 1-4 bullets on what the N=8 result adds to the N=6 CONFIRM story. If CONFIRM, the claim "fairness scales with N" is now defensible to at least N=8 on a single engine. If FAIL, note the mechanism (warmup stacking? DRR quantum exhaustion? flooder share erosion?) and whether it's a launch-blocker or a "known limit" footnote.>

**Hero number for launch post:** <TBD>

---

## Rsync'd artifacts

- `<TBD_arm0_dir>/` — 300s solo baseline (FIFO config, flooder_rps=0, 7 quiets only)
- `<TBD_arm1_dir>/` — FIFO contended (no fairness, 1 flooder + 7 quiets)
- `<TBD_arm5_dir>/` — **DRR + rate_limit_rpm=600 (the fix, 1 flooder + 7 quiets)**

Each contains tarball + extracted: `benchmarks/{tenant_flooder.csv, tenant_quiet_{1..7}.csv, summary.json}`, `engine_logs/`, `prometheus_dump.txt`, `gpu_trace.csv`, `server.log`, `phase_*.ts`, `status_{before,after}.json`, `pip_freeze.txt`, `metadata.json` (records actual GPU model — see Caveats).

---

## Caveats not yet investigated

1. **GPU substitution.** A100-SXM4 80GB availability collapsed during scheduling; Gate 2.1 pivoted to **H100 SXM 80GB HBM3**. Actual pod GPU recorded in the `metadata.json` tarball; absolute numbers (latencies, throughputs) may differ from the A100 spec but the per-tenant fairness ratio should preserve since fairness is a property of the admission+scheduling layer, not the compute substrate.
2. <TBD: any warmup-transient observations specific to N=8>
3. <TBD: whether the 7 quiets share a single TenantBudget RPM allocation or individual ones, and what that implies>
4. <TBD: DRR quantum interaction with 8 tenants on a single engine>

These are enhancements to consider post-launch, not blockers for the launch story.

---

## Decision tree from here

<TBD: conditional on outcome. If CONFIRM → proceed to Gate 2.2 mixed-prompt-length on the same pod. If FAIL → diagnose and decide whether N=6 stands as the cap on the scaling claim in the launch post.>

This OUTCOME is interpretation-friendly per the runbook contract: raw numbers + diagnostic decomposition + the user calls the shot on framing.
