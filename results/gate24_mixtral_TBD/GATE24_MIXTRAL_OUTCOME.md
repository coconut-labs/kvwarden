# Gate 2.4 (Mixtral-8×7B MoE) — Fairness on an MoE Model with Expert-Routing Variance

**Date:** <TBD_DATE>
**Hardware:** 2× <TBD_GPU_MODEL> on RunPod (SECURE on-demand, ~$<TBD_HOURLY>/hr, TP=2) — see Caveats re: GPU substitution from A100-SXM4 80GB.
**Total cost:** ~$<TBD_COST_ACTUAL> actual (ceiling $<TBD_COST_CEILING>)
**main tip at start:** `<TBD_SHA>` (<TBD_PR_POINTER>).
**Model:** `mistralai/Mixtral-8x7B-Instruct-v0.1`, bfloat16, tensor_parallel_size=2, max_model_len=4096, gpu_memory_utilization=0.90
**Workload:** 1 flooder + N=<TBD> quiet tenants, 300s sustained, 128-token outputs. **First MoE model test.**
**Status:** **<TBD_STATUS>.** <TBD_STATUS_ONELINER>

**Hypothesis going in:** expert-routing variance in Mixtral (2 of 8 experts activated per token, per-token routing decisions) doesn't leak past the InferGrid budget gate. That is, even though different requests activate different expert subsets — and therefore have different per-token compute/memory bandwidth costs, different GPU utilization patterns, and different batching efficiency inside vLLM — the per-tenant fairness property observed on dense Llama-3.1-8B still holds. The rate-limit gate operates on request arrivals and tenant-identity, and is agnostic to whether the engine internally routes tokens to expert_3 or expert_7.

Null: MoE routing variance creates per-request compute heterogeneity that vLLM's batcher handles in a way that reintroduces head-of-line blocking for quiet tenants. Specifically — if flooder's traffic pattern happens to hit a "hot" expert (sustained routing to one physical GPU in TP=2), that GPU becomes the bottleneck and all tenants on that GPU's share of the batch wait, including quiet's requests that ended up batched with flooder's.

---

## Headline numbers

| | Arm 0 solo baseline (Mixtral) | Arm 1 FIFO contended (Mixtral) | Arm 5 DRR + rate_limit (Mixtral) |
|---|---:|---:|---:|
| `quiet_user.ttft_p50` | **<TBD> ms** | <TBD> ms | **<TBD> ms** |
| `quiet_user.ttft_p99` | <TBD> ms | <TBD> ms | <TBD> ms |
| `quiet_user.count_ok` | <TBD> / 300s | <TBD> / 300s | <TBD> / 300s |
| `flooder.count_ok` | (n/a) | <TBD> | <TBD> |
| `flooder.count_err` (429) | (n/a) | <TBD> | **<TBD>** |

**p50 starvation reduction (Arm 1 → Arm 5): <TBD>× improvement.**
**Cross-model fairness-ratio comparison (Arm 5 quiet_p50 / Arm 0 quiet_p50):**

| Model | Arm 0 p50 | Arm 5 p50 | Ratio | Notes |
|---|---:|---:|---:|---|
| Llama-3.1-8B (dense, Gate 2-FAIRNESS) | 28.5 | ~30 (steady) | 1.05× | dense, single GPU |
| Mixtral-8×7B (MoE, this gate) | <TBD> | <TBD> | **<TBD>×** | 8 experts, TP=2 |

If the Mixtral ratio lands in the same ballpark (≤1.5×) as the Llama ratio, fairness is model-class-agnostic. If Mixtral blows up to 3-5×, we've identified a real interaction between MoE routing and admission-layer fairness that's launch-post-worthy as a footnote or follow-up-gate target.

---

## Pre-committed criteria (from advisor + runbook)

| Criterion | Value | Result |
|---|---|---|
| Arm 5 quiet_p99 ≤ 2× Arm 0 solo baseline | <TBD> ms vs <TBD> ms | **<TBD>** |
| Arm 5 quiet_p50 ≤ 1.5× Arm 0 solo baseline | <TBD> ms vs <TBD> ms | **<TBD>** |
| Arm 5 flooder gets 429'd (rate-limit fires on MoE engine) | tenant_rejected=<TBD> | **<TBD>** |
| Arm 5 engine stays healthy under TP=2 MoE pressure | no OOM, no crash, no degraded sharding | **<TBD>** |

The fourth criterion matters more than on Llama: TP=2 on Mixtral is the first time we're testing InferGrid against a tensor-parallel engine. Rate-limit and admission don't change, but engine-level failure modes (all-reduce stalls, NCCL timeouts, expert-weight loading hiccups) are new.

---

## All arms — full data table

| Arm | Config | quiet_p50 | quiet_p99 | flood_p50 | flood_p99 | flood_err | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| **0** | gate2_fairness_fifo_mixtral + flooder_rps=0 (solo baseline, Mixtral TP=2) | **<TBD>** | **<TBD>** | n/a | n/a | n/a | <TBD> req |
| **1** | gate2_fairness_fifo_mixtral + flooder_rps=<TBD> (no fairness) | **<TBD>** | **<TBD>** | <TBD> | <TBD> | <TBD> | expected: starvation |
| **5** | gate2_fairness_drr_ratelimit_mixtral (rate_limit=<TBD> RPM) | **<TBD>** | **<TBD>** | <TBD> | <TBD> | **<TBD>** | expected: quiet near baseline |

---

## What the data falsifies and confirms

### Confirmed
- <TBD>

### Falsified
- <TBD>

### Diagnosed
- <TBD>

---

## MoE-specific diagnostics

Expert-routing pattern (from vLLM engine logs `moe_expert_routing_histogram` if available):

| Expert | Activations (flooder) | Activations (quiet) | Relative load |
|---|---:|---:|---:|
| expert_0 | <TBD> | <TBD> | <TBD> |
| expert_1 | <TBD> | <TBD> | <TBD> |
| expert_2 | <TBD> | <TBD> | <TBD> |
| expert_3 | <TBD> | <TBD> | <TBD> |
| expert_4 | <TBD> | <TBD> | <TBD> |
| expert_5 | <TBD> | <TBD> | <TBD> |
| expert_6 | <TBD> | <TBD> | <TBD> |
| expert_7 | <TBD> | <TBD> | <TBD> |

If flooder and quiet expert histograms are statistically similar (both broadly uniform over 8 experts because prompts are diverse), fairness doesn't depend on routing. If flooder concentrates on 1-2 experts and those become bottlenecks, we've found an MoE-specific unfairness mechanism InferGrid can't see from the admission gate alone.

Per-GPU utilization trace (from `gpu_trace.csv`):
- GPU 0 avg util: **<TBD>%**, peak **<TBD>%**
- GPU 1 avg util: **<TBD>%**, peak **<TBD>%**
- TP imbalance ratio (max/min): **<TBD>**

---

## Operational notes

- <TBD: Mixtral weight download time on first pod spin — can be 30+ min, factor into cost>
- <TBD: TP=2 NCCL setup stability>
- <TBD: vLLM MoE-specific flags used>
- <TBD: cost actual vs ceiling>

---

## Implications for the launch post

<TBD: 1-4 bullets. If CONFIRM → the claim "fairness works across model classes, not just dense Llama" is defensible. MoE is a headline-worthy model class because Mixtral + DeepSeek + GPT-oss are the dominant open MoE deployments. If FAIL → honest footnote + clear follow-up: "MoE routing variance is a known interaction; our launch claim is dense-model-only until we add routing-aware admission.">

**Hero number for launch post:** <TBD>

---

## Rsync'd artifacts

- `<TBD_arm0_mixtral_dir>/` — 300s solo baseline (FIFO, flooder_rps=0, Mixtral TP=2)
- `<TBD_arm1_mixtral_dir>/` — FIFO contended (no fairness, Mixtral TP=2)
- `<TBD_arm5_mixtral_dir>/` — **DRR + rate_limit (the fix, Mixtral TP=2)**

Each contains tarball + extracted: `benchmarks/{tenant_flooder.csv, tenant_quiet_user.csv, summary.json}`, `engine_logs/` (including MoE expert-routing histograms if available), `prometheus_dump.txt`, `gpu_trace.csv` (both GPUs), `server.log`, `phase_*.ts`, `status_{before,after}.json`, `pip_freeze.txt`, `metadata.json` (records actual GPU model — see Caveats).

---

## Caveats not yet investigated

1. **GPU substitution.** A100-SXM4 80GB availability collapsed during scheduling; Gate 2.1 pivoted to **H100 SXM 80GB HBM3** and other gates may follow. Actual pod GPU recorded in the `metadata.json` tarball; absolute numbers may differ from the A100 spec but the per-tenant fairness ratio should preserve since fairness is a property of the admission+scheduling layer, not the compute substrate.
2. **Expert routing histograms may not be exposed by the vLLM version we're pinning.** If the engine doesn't surface per-expert activation counts, the MoE-specific diagnostics table above will be incomplete and the "routing-hot-expert" hypothesis is only indirectly testable via per-GPU utilization imbalance.
3. **Single-model MoE class.** Mixtral is one specific MoE architecture (8×7B, top-2 routing). DeepSeek-MoE (16×routing, finer experts) or GPT-oss would stress different routing regimes. Deferred.
4. **TP=2 only.** TP=4 MoE has different all-reduce patterns and may surface interactions the TP=2 baseline hides. Gate 2.3 covers TP=4 on a dense 70B; a TP=4 MoE gate is not scheduled.
5. **Prompt distribution uniform.** Mixed-prompt-length (Gate 2.2) + MoE is a 2×2 untested; doing it well needs both. Deferred.

These are enhancements to consider post-launch, not blockers for the launch story.

---

## Decision tree from here

<TBD: conditional on outcome. If CONFIRM → launch post can claim MoE coverage. If FAIL → frame as a scoped limitation (dense models work, MoE has a known routing-variance interaction, roadmapped). If the engine itself was unstable (NCCL timeouts, OOM) → separate out the "fairness property" from the "engine stability" axis and report on each.>

This OUTCOME is interpretation-friendly per the runbook contract: raw numbers + diagnostic decomposition + the user calls the shot on framing.
