# Gate 2.3 (Llama-3.1-70B, TP=4) — Fairness at Frontier Model Scale

**Date:** <TBD_DATE>
**Hardware:** 4× <TBD_GPU_MODEL> on RunPod (SECURE on-demand, ~$<TBD_HOURLY>/hr, TP=4) — see Caveats re: GPU substitution from A100-SXM4 80GB.
**Total cost:** ~$<TBD_COST_ACTUAL> actual (ceiling $<TBD_COST_CEILING>)
**main tip at start:** `<TBD_SHA>` (<TBD_PR_POINTER>).
**Model:** `meta-llama/Llama-3.1-70B-Instruct`, bfloat16, tensor_parallel_size=4, max_model_len=4096, gpu_memory_utilization=0.90
**Workload:** Two arms — **Arm 0 solo baseline** (1 quiet alone, 300s) + **Arm 5 contended** (1 flooder + 1 quiet, DRR + rate_limit, 300s), 128-token outputs. Frontier-credibility gate — the single experiment that determines whether the launch post can claim "works at 70B" or caps at "demonstrated at 8B."
**Status:** **<TBD_STATUS>.** <TBD_STATUS_ONELINER>

**Hypothesis going in:** the per-tenant fairness property transfers from Llama-3.1-8B on a single GPU to Llama-3.1-70B on 4× GPUs via TP=4. Fairness is a property of the admission + budget layer — it doesn't know and shouldn't care about tensor-parallelism, model size, or attention head count. Null: 70B at TP=4 exposes a new failure mode (NCCL stall propagating head-of-line blocking across tenants; larger per-request compute collapsing batching efficiency; VRAM pressure on 4× 80GB forcing gpu_memory_utilization down and narrowing the batch window enough that fairness degrades).

Two arms instead of three because: (a) 70B at TP=4 is expensive (~$<TBD>/hr vs ~$<TBD>/hr for single-GPU 8B), (b) the FIFO-contended arm was already demonstrated at 8B — the 70B question is specifically "does the fix still work at scale," not "do we re-prove starvation at scale." If Arm 5 holds, the launch post can reference the 8B Arm 1 for the "starvation exists" baseline and this gate for "fix works at frontier scale."

---

## Headline numbers

| | Arm 0 solo baseline (70B) | Arm 5 DRR + rate_limit (70B) |
|---|---:|---:|
| `quiet_user.ttft_p50` | **<TBD> ms** | **<TBD> ms** |
| `quiet_user.ttft_p99` | <TBD> ms | <TBD> ms |
| `quiet_user.count_ok` | <TBD> / 300s | <TBD> / 300s |
| `flooder.count_ok` | (n/a) | <TBD> |
| `flooder.count_err` (429) | (n/a) | **<TBD>** |

**Fairness ratio (Arm 5 quiet_p50 / Arm 0 quiet_p50): <TBD>×**

**Cross-scale fairness-ratio comparison:**

| Model / scale | Arm 0 p50 | Arm 5 p50 | Ratio | Notes |
|---|---:|---:|---:|---|
| Llama-3.1-8B, 1 GPU (Gate 2-FAIRNESS) | 28.5 ms | ~30 ms (steady) | 1.05× | dense, 2 tenants, single GPU |
| Llama-3.1-70B, 4 GPU TP=4 (this gate) | <TBD> | <TBD> | **<TBD>×** | dense, 2 tenants, TP=4 |

If the 70B fairness ratio lands ≤1.5× (same ballpark as 8B), fairness is scale-invariant and the launch post can carry "works at frontier scale." If it blows up to 3-5×, we've identified a scale-dependent mechanism and the launch claim caps at 8B with 70B as roadmap.

---

## Pre-committed criteria (from advisor + runbook)

| Criterion | Value | Result |
|---|---|---|
| Arm 5 quiet_p99 ≤ 2× Arm 0 solo baseline | <TBD> ms vs <TBD> ms | **<TBD>** |
| Arm 5 quiet_p50 ≤ 1.5× Arm 0 solo baseline | <TBD> ms vs <TBD> ms | **<TBD>** |
| Arm 5 flooder gets 429'd (rate-limit fires at 70B scale) | tenant_rejected=<TBD>; flooder count_err=<TBD> | **<TBD>** |
| Arm 5 engine stays healthy under TP=4 | no NCCL timeout, no OOM, no sharding-broken output | **<TBD>** |
| Absolute Arm 0 p50 consistent with published 70B TP=4 TTFT benchmarks | <TBD> ms (lit range ~<TBD> ms) | **<TBD>** |

The fifth criterion is the "frontier credibility" check: if our Arm 0 solo-baseline TTFT at 70B TP=4 is wildly off from public benchmarks (e.g., 10× slower), the pod config is wrong and the fairness result is invalid regardless of what Arm 5 shows.

---

## All arms — full data table

| Arm | Config | quiet_p50 | quiet_p99 | flood_p50 | flood_p99 | flood_err | Notes |
|---|---|---:|---:|---:|---:|---:|---|
| **0** | gate2_fairness_fifo_70b_tp4 + flooder_rps=0 (solo baseline) | **<TBD>** | **<TBD>** | n/a | n/a | n/a | <TBD> req |
| **5** | gate2_fairness_drr_ratelimit_70b_tp4 (rate_limit=<TBD> RPM) | **<TBD>** | **<TBD>** | <TBD> | <TBD> | **<TBD>** | frontier fairness test |

Arm 1 (FIFO-contended at 70B) intentionally omitted — see preamble rationale.

---

## What the data falsifies and confirms

### Confirmed
- <TBD>

### Falsified
- <TBD>

### Diagnosed
- <TBD>

---

## TP=4 engine diagnostics

Per-GPU utilization trace (from `gpu_trace.csv`):

| GPU | Avg util | Peak util | Avg VRAM | Peak VRAM |
|---|---:|---:|---:|---:|
| GPU 0 | <TBD>% | <TBD>% | <TBD> GB | <TBD> GB |
| GPU 1 | <TBD>% | <TBD>% | <TBD> GB | <TBD> GB |
| GPU 2 | <TBD>% | <TBD>% | <TBD> GB | <TBD> GB |
| GPU 3 | <TBD>% | <TBD>% | <TBD> GB | <TBD> GB |
| **TP imbalance (max/min util)** | — | **<TBD>** | — | — |

NCCL / all-reduce timing (if vLLM exposes it in engine_logs):
- Median all-reduce latency: **<TBD> ms**
- p99 all-reduce latency: **<TBD> ms**
- NCCL timeouts during the run: **<TBD>**

If TP imbalance stays near 1.0× and all-reduce p99 is clean, the TP substrate is operating normally and any fairness result is attributable to the admission layer (which is what we want to test). If imbalance is high or NCCL p99 spikes, the fairness signal may be contaminated by engine-level noise.

---

## Operational notes

- <TBD: 70B weight download time (much longer than 8B — ~140GB bf16)>
- <TBD: TP=4 NCCL setup stability — any all-reduce timeouts on pod boot>
- <TBD: VRAM pressure at gpu_memory_utilization=0.90 on 4× 80GB (need ~140GB for weights + KV cache headroom)>
- <TBD: cost actual vs ceiling — this is the most expensive gate of the ladder>

---

## Implications for the launch post

<TBD: 1-4 bullets. If CONFIRM → this is the headline claim-enabler: "InferGrid fairness works from 8B to 70B, single-GPU to TP=4 — the admission-layer architecture is genuinely scale-invariant." If FAIL → the launch post caps at 8B and 70B becomes the first roadmap milestone with honest framing about what scale introduces.>

**Hero number for launch post:** <TBD>

---

## Rsync'd artifacts

- `<TBD_arm0_70b_dir>/` — 300s solo baseline (FIFO, flooder_rps=0, 70B TP=4)
- `<TBD_arm5_70b_dir>/` — **DRR + rate_limit (the fix, 70B TP=4)**

Each contains tarball + extracted: `benchmarks/{tenant_flooder.csv, tenant_quiet_user.csv, summary.json}`, `engine_logs/` (including NCCL/all-reduce timing if available), `prometheus_dump.txt`, `gpu_trace.csv` (all 4 GPUs), `server.log`, `phase_*.ts`, `status_{before,after}.json`, `pip_freeze.txt`, `metadata.json` (records actual GPU model — see Caveats).

---

## Caveats not yet investigated

1. **GPU substitution.** A100-SXM4 80GB availability collapsed during scheduling; Gate 2.1 pivoted to **H100 SXM 80GB HBM3** and Gate 2.3 may follow. Actual pod GPU recorded in the `metadata.json` tarball; absolute numbers may differ from the A100 spec but the per-tenant fairness ratio should preserve since fairness is a property of the admission+scheduling layer, not the compute substrate. Note that on H100 at TP=4 the absolute TTFT will be noticeably better than A100 TP=4 — the ratio, not the absolute, is what carries the launch claim.
2. **Two arms, not three.** No FIFO-contended arm at 70B; the 8B FIFO-contended (Arm 1, 523× starvation) carries the "starvation exists" baseline. If a reviewer insists on a same-scale contended arm, that's a follow-up experiment at ~$<TBD> additional cost.
3. **N=2 tenants only.** Doing N=8 on 70B TP=4 would cost ~4× this gate. The scale-invariance claim is "fairness ratio ≤1.5× at 70B with 2 tenants" — we're not claiming N=8 at 70B.
4. **Fixed prompt + output lengths.** Mixed-prompt (Gate 2.2) at 70B is not scheduled. The long-tail interaction from Gate 2.2 + frontier-scale interaction from this gate may compound in ways neither gate individually tests.
5. **vLLM TP=4 behavior may vary by version.** Pin recorded in `pip_freeze.txt`.

These are enhancements to consider post-launch, not blockers for the launch story.

---

## Decision tree from here

<TBD: conditional on outcome. If CONFIRM → frontier credibility secured; launch post claims "works at 70B TP=4." If FAIL with clean engine → honest scope cap at 8B + 70B roadmap. If FAIL with dirty engine (TP imbalance, NCCL) → separate "fairness signal" from "engine noise," potentially rerun on stable pod before making a launch call. If gate was never completed due to GPU scarcity or OOM → 8B Gate 2.1 + Gate 2.2 results alone have to carry the launch post, and 70B becomes a demo-only stretch goal.>

This OUTCOME is interpretation-friendly per the runbook contract: raw numbers + diagnostic decomposition + the user calls the shot on framing.
