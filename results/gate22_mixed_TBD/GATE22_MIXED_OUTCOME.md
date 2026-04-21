# Gate 2.2 (Mixed Prompt-Length) — Fairness Under Realistic Long-Tail Prompts

**Date:** <TBD_DATE>
**Hardware:** 1× <TBD_GPU_MODEL> on RunPod (SECURE on-demand, ~$<TBD_HOURLY>/hr) — same pod as Gate 2.1 post-completion — see Caveats re: GPU substitution from A100-SXM4 80GB.
**Total cost:** ~$<TBD_COST_ACTUAL> actual (ceiling $<TBD_COST_CEILING>)
**main tip at start:** `<TBD_SHA>` (PR #78 `--prompt-length-dist` landed cbee679).
**Model:** `meta-llama/Llama-3.1-8B-Instruct`, bfloat16, max_model_len=8192, gpu_memory_utilization=0.85
**Workload:** 1 flooder + 7 quiet tenants, 300s sustained, 128-token outputs, `--prompt-length-dist "64:0.4,512:0.3,2048:0.2,8192:0.1"`. Drawn from the same distribution for all 8 tenants.
**Status:** **<TBD_STATUS>.** <TBD_STATUS_ONELINER>

**Hypothesis going in:** the per-tenant bucket fairness observed under uniform 128-token prompts (Gate 2-FAIRNESS, Gate 2.1 N=8) survives a realistic long-tail prompt distribution — specifically, that the DRR quantum accounting tracks tokens (or at least accounts for prefill-time variance proportionally), so an 8192-token quiet prompt doesn't get starved by a flood of 64-token flooder prompts, and a flooder spamming 8192-token prompts doesn't exhaust its quota more slowly than expected.

Null: fairness is prompt-length-dependent. Long prompts either (a) exhaust DRR quantum faster than RPM rate-limit accounts for, starving same-tenant follow-up requests, or (b) cluster at the vLLM prefill batcher in a way that the admission layer can't see, reintroducing quiet-tenant tail latency that N=8 uniform hid.

---

## Headline numbers

| | Arm 0 solo baseline (mixed) | Arm 1 FIFO contended (mixed) | Arm 5 DRR + rate_limit (mixed) |
|---|---:|---:|---:|
| `quiet_user.ttft_p50` (median across 7 quiets) | **<TBD> ms** | <TBD> ms | **<TBD> ms** |
| `quiet_user.ttft_p99` (worst of 7 quiets, p99) | <TBD> ms | <TBD> ms | <TBD> ms |
| `quiet_user.ttft_p50`, **8192-token bucket only** | **<TBD> ms** | <TBD> ms | **<TBD> ms** |
| `quiet_user.ttft_p99`, **8192-token bucket only** | <TBD> ms | <TBD> ms | <TBD> ms |
| `flooder.count_ok` | (n/a) | <TBD> | <TBD> |
| `flooder.count_err` (429) | (n/a) | <TBD> | **<TBD>** |

**Per-bucket fairness ratio (Arm 5 quiet_p50 / Arm 0 quiet_p50), by prompt length:**

| Prompt length | N | Arm 0 p50 | Arm 5 p50 | Ratio |
|---|---:|---:|---:|---:|
| 64 tok (40%) | <TBD> | <TBD> ms | <TBD> ms | **<TBD>×** |
| 512 tok (30%) | <TBD> | <TBD> ms | <TBD> ms | **<TBD>×** |
| 2048 tok (20%) | <TBD> | <TBD> ms | <TBD> ms | **<TBD>×** |
| 8192 tok (10%) | <TBD> | <TBD> ms | <TBD> ms | **<TBD>×** |

If all 4 buckets stay within ~1.5× of solo baseline, per-bucket fairness holds. If the 8192-tok bucket blows up to 5-10× while the 64-tok bucket is near baseline, we've learned that DRR quantum accounts for request count but not token-weighted cost — a known limitation to footnote, not a launch-blocker.

---

## Pre-committed criteria (from advisor + runbook)

| Criterion | Value | Result |
|---|---|---|
| Arm 5 overall quiet_p99 ≤ 2× Arm 0 overall quiet_p99 | <TBD> ms vs <TBD> ms | **<TBD>** |
| Arm 5 per-bucket quiet_p50 ≤ 1.5× Arm 0 per-bucket p50 (all 4 buckets) | see table | **<TBD>** |
| Arm 5 flooder gets 429'd (rate-limit fires under mixed load) | tenant_rejected=<TBD> | **<TBD>** |
| Arm 5 no quiet tenant starved on any specific bucket | worst-bucket quiet count_ok > 0 for all tenants | **<TBD>** |

The "per-bucket" criterion is the real novel test here. Uniform-prompt fairness is straightforward; the question is whether mixed traffic breaks the abstraction.

---

## All arms — full data table

| Arm | Config | quiet_p50 (median) | quiet_p99 (worst) | flood_p50 | flood_p99 | flood_err | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| **0** | gate2_fairness_fifo + flooder_rps=0 (mixed, solo) | **<TBD>** | **<TBD>** | n/a | n/a | n/a | 7 quiets on mixed dist |
| **1** | gate2_fairness_fifo + flooder_rps=32 (mixed, no fairness) | **<TBD>** | **<TBD>** | <TBD> | <TBD> | <TBD> | expected: bucket-dependent starvation |
| **5** | gate2_fairness_drr_ratelimit (mixed, rate_limit=600) | **<TBD>** | **<TBD>** | <TBD> | <TBD> | **<TBD>** | expected: per-bucket near baseline |

---

## What the data falsifies and confirms

### Confirmed
- <TBD>

### Falsified
- <TBD>

### Diagnosed
- <TBD>

---

## Token-weighted cost breakdown (Arm 5)

| Bucket | total prefill tokens (all tenants) | total prefill tokens (flooder) | total prefill tokens (quiets sum) | flooder share | quiet share per-quiet |
|---|---:|---:|---:|---:|---:|
| 64 tok | <TBD> | <TBD> | <TBD> | <TBD>% | <TBD>% |
| 512 tok | <TBD> | <TBD> | <TBD> | <TBD>% | <TBD>% |
| 2048 tok | <TBD> | <TBD> | <TBD> | <TBD>% | <TBD>% |
| 8192 tok | <TBD> | <TBD> | <TBD> | <TBD>% | <TBD>% |

If flooder's aggregate prefill-token share exceeds its RPM-implied fair share (1/8 = 12.5% per-tenant), the rate-limit is not token-weighted and long prompts break fairness by volume. If flooder's aggregate share tracks its RPM budget, fairness holds even on total-compute terms.

---

## Operational notes

- <TBD: resumed same pod as Gate 2.1 / fresh spin — whichever the runbook executed>
- <TBD: any OOM or prefill-memory incidents at 8192 max_model_len>
- <TBD: cost actual vs ceiling>

---

## Implications for the launch post

<TBD: 1-4 bullets. If CONFIRM → the mixed-prompt fairness story is the strongest version of the hero: "works on realistic traffic, not just synthetic uniform prompts." If per-bucket FAIL on 8192 → honest footnote: "DRR quantum is count-based; token-weighted scheduling is roadmap." If overall FAIL → diagnose and escalate.>

**Hero number for launch post:** <TBD>

---

## Rsync'd artifacts

- `<TBD_arm0_mixed_dir>/` — 300s solo baseline (FIFO config, flooder_rps=0, mixed dist, 7 quiets)
- `<TBD_arm1_mixed_dir>/` — FIFO contended (no fairness, mixed dist, 1 flooder + 7 quiets)
- `<TBD_arm5_mixed_dir>/` — **DRR + rate_limit_rpm=600 (the fix, mixed dist, 1 flooder + 7 quiets)**

Each contains tarball + extracted: `benchmarks/{tenant_flooder.csv, tenant_quiet_{1..7}.csv, summary.json, prompt_length_histogram.csv}`, `engine_logs/`, `prometheus_dump.txt`, `gpu_trace.csv`, `server.log`, `phase_*.ts`, `status_{before,after}.json`, `pip_freeze.txt`, `metadata.json` (records actual GPU model — see Caveats).

---

## Caveats not yet investigated

1. **GPU substitution.** A100-SXM4 80GB availability collapsed during scheduling; Gate 2.1 pivoted to **H100 SXM 80GB HBM3** and Gate 2.2 may follow. Actual pod GPU recorded in the `metadata.json` tarball; absolute numbers may differ from the A100 spec but the per-tenant fairness ratio should preserve since fairness is a property of the admission+scheduling layer, not the compute substrate.
2. Distribution is drawn iid for all 8 tenants — a realistic "enterprise vs hobbyist" scenario would assign different distributions per tenant (e.g., flooder = all 64-tok, quiet_7 = all 8192-tok). Deferred.
3. Output length is still fixed at 128 tokens. Variable output lengths would interact with DRR quantum differently (decode dominates compute at long outputs).
4. The 8192-tok bucket is only 10% of traffic by count but dominates compute by volume (8192/64 = 128× cheaper per prefill token means the 10% bucket is ~73% of total prefill cost). Whether DRR should be token-weighted or count-weighted is a design question this bench surfaces.

These are enhancements to consider post-launch, not blockers for the launch story.

---

## Decision tree from here

<TBD: conditional on outcome. If CONFIRM overall + per-bucket → launch post can carry the "works on realistic traffic" claim at full strength. If per-bucket FAIL on 8192 → footnote + defer token-weighted DRR to post-launch roadmap. If overall FAIL → falls back to N=8 uniform as the scaling-claim ceiling.>

This OUTCOME is interpretation-friendly per the runbook contract: raw numbers + diagnostic decomposition + the user calls the shot on framing.
