# Gate 3 — Cache-Pressure-Aware Admission (Path C Probe)

**Date:** <TBD_DATE>
**Hardware:** 1× <TBD_GPU_MODEL> on RunPod (SECURE on-demand, ~$<TBD_HOURLY>/hr) — see Caveats re: GPU substitution if A100-SXM4 80GB unavailable.
**Total cost:** ~$<TBD_COST_ACTUAL> actual (ceiling $<TBD_COST_CEILING>; T2 hard cap $20).
**main tip at start:** `<TBD_SHA>` (<TBD_PR_POINTER>). For M6 only: M5a /metrics-poller branch SHA `<TBD_M5A_SHA>`.
**Model:** `meta-llama/Llama-3.1-8B-Instruct`, bfloat16, max_model_len=4096, gpu_memory_utilization=0.40 (tight VRAM to push cache pressure).
**Workload:** 1 flooder @ 32 RPS + 7 quiet @ 1 RPS each, 70% prompt-prefix overlap (RAG-style: <TBD_PREFIX_TOKENS>-token shared prefix), 300s sustained, 128-token outputs, 3 seeds per arm.
**Prefix-overlap mechanism:** <TBD_PREFIX_MECHANISM — issue #120 harness flag if landed, else bench-side mock procedure used>.
**Arm 2 saturation-bias mechanism:** <TBD_BIAS_MECHANISM — harness `--bias-flooder-cost` flag if landed (follow-up issue parallel to #120), else bench-side admin-endpoint override procedure used>.
**Phase:** <TBD_PHASE — M4 probe (Arm 1 + Arm 2 only) or M6 full Gate 3 (Arm 1 + Arm 2 + Arm 3)>.
**Status:** **<TBD_STATUS>.** <TBD_STATUS_ONELINER>

## What this measures

T2 reframe (2026-04-28T+1, see strategic-plan REVISION): T2 v0.2.0 lever is **cache-pressure-aware admission**, not tenant-aware eviction. The shadow-ledger grep verified the eviction lever is dead code — router never calls `allocate_block`/`access_block`/`_evict_from_tier` from the hot path. The admission lever is real: vLLM 0.19.1 exposes `vllm:kv_cache_usage_perc` (Gauge, instance-level), kvwarden polls it, AdmissionController.acquire() composes (cache_load × tenant_deficit) into priority. ~200 LOC, lands W4-W6 (M5a) gated on this gate's outcome.

**Hypothesis:** under capacity pressure regimes (gauge p99 ≥ ~0.7), scaling flooder admission cost up at saturation reduces quiet-tenant queueing latency. Arm 2 measures the achievable upper bound via a simulated 4× saturation bias. Arm 3 (M6 only) measures the real /metrics-polling implementation. **Null:** DRR-only already captures the gap; Arm 2 - Arm 1 < 1.2× and the M5a build is unjustified.

---

## Cache pressure regime check

**KILL CRITERION.** If `vllm:kv_cache_usage_perc` p99 < 0.7 across all arms after regime-rescale attempts, the workload doesn't pressure cache; no cache-pressure-conditioned admission policy can help. Ship (c) disconfirm, queue (b) LMCache for v0.3, record this section's distribution as the load-bearing evidence and skip the latency rubric.

| Arm | Gauge p50 | Gauge p95 | Gauge p99 | Pressure exercised? |
|---|---:|---:|---:|---|
| Arm 1 (DRR only) | <TBD_GAUGE_P50_ARM1> | <TBD_GAUGE_P95_ARM1> | <TBD_GAUGE_P99_ARM1> | <TBD: yes if p99 ≥ 0.7> |
| Arm 2 (simulated bias) | <TBD_GAUGE_P50_ARM2> | <TBD_GAUGE_P95_ARM2> | <TBD_GAUGE_P99_ARM2> | <TBD> |
| Arm 3 (real polling, M6) | <TBD_GAUGE_P50_ARM3> | <TBD_GAUGE_P95_ARM3> | <TBD_GAUGE_P99_ARM3> | <TBD> |

**Regime-rescale log:** <TBD: if any arm fell short of p99 ≥ 0.7 in initial run, document the rescale attempts here — gmu drops, prompt-length increases, tenant-count bumps, sustained-flooder duration extensions — and the resulting gauge p99 per attempt>.

**Regime decision:** <TBD: PRESSURED (proceed to latency rubric) / REGIME-BROKEN (ship (c) by kill criterion, skip latency rubric)>.

---

## Headline numbers

Median across 7 quiets of per-tenant p99 TTFT, then median across 3 seeds.

| | Arm 1 DRR only | Arm 2 Simulated saturation bias | Arm 3 Real polling (M6) |
|---|---:|---:|---:|
| `quiet_user.ttft_p50` (median across 7 quiets) | <TBD> ms | <TBD> ms | <TBD> ms |
| **`quiet_user.ttft_p99` (median across 7 quiets)** | **<TBD> ms** | **<TBD> ms** | **<TBD> ms** |
| `quiet_user.ttft_p99` (worst of 7 quiets) | <TBD> ms | <TBD> ms | <TBD> ms |
| `quiet_user.ttft_p99` (best of 7 quiets) | <TBD> ms | <TBD> ms | <TBD> ms |
| `quiet_user.count_ok` (sum across 7, sum across 3 seeds) | <TBD> | <TBD> | <TBD> |
| `flooder.count_ok` | <TBD> | <TBD> | <TBD> |
| `flooder.count_err` (429) | <TBD> | <TBD> | <TBD> |

**Stage-1 delta (M4): Arm 1 / Arm 2 quiet p99 = <TBD>×.**
**Stage-2 delta (M6): Arm 1 / Arm 3 quiet p99 = <TBD>×.**
**Arm 3 vs Arm 2 (must track within ±15%): <TBD>%.**
**Per-quiet-tenant spread (max - min p99 across 7, Arm 2): <TBD> ms.**

---

## Pre-committed criteria (from runbook + strategic-plan REVISION)

| Criterion | Value | Result |
|---|---|---|
| Gauge p99 ≥ 0.7 in any arm (kill-criterion gate) | <TBD> | **<TBD>** |
| Stage 1: Arm 2 / Arm 1 quiet p99 ≥ 1.5× → (a) GA-track | <TBD>× | **<TBD>** |
| Stage 1: Arm 2 / Arm 1 in [1.2×, 1.5×) → (a) experimental | <TBD>× | **<TBD>** |
| Stage 1: Arm 2 / Arm 1 < 1.2× → (c) disconfirm | <TBD>× | **<TBD>** |
| Stage 1: Arm 2 worse than Arm 1 → abandon (a), ship (c) | <TBD> | **<TBD>** |
| Stage 2 (M6 only): Arm 3 / Arm 1 ≥ 1.5× → (a) GA | <TBD>× | **<TBD>** |
| Stage 2: Arm 3 tracks Arm 2 within ±15% | <TBD>% | **<TBD>** |
| Arm 2 flooder gets 429d (rate-limit fires, plumbing OK) | flooder count_err=<TBD> | **<TBD>** |
| Arm 2 no quiet tenant gets 429d (plumbing check) | quiet count_err sum=<TBD> | **<TBD>** |
| Arm 2 per-quiet spread ≤ 1.5× max/min | max/min=<TBD> | **<TBD>** |

---

## All arms — full data table (per seed × per arm)

| Arm | Seed | Config | quiet_p50 (med/worst) | quiet_p99 (med/worst) | flood_p99 | flood_err | gauge_p99 | Notes |
|---|---|---|---:|---:|---:|---:|---:|---|
| **1** | 0 | gate3_kv_eviction.yaml arm1 | <TBD>/<TBD> | <TBD>/<TBD> | <TBD> | <TBD> | <TBD> | DRR only |
| **1** | 1 | same | <TBD>/<TBD> | <TBD>/<TBD> | <TBD> | <TBD> | <TBD> | |
| **1** | 2 | same | <TBD>/<TBD> | <TBD>/<TBD> | <TBD> | <TBD> | <TBD> | |
| **2** | 0 | gate3_kv_eviction.yaml arm2 | <TBD>/<TBD> | <TBD>/<TBD> | <TBD> | <TBD> | <TBD> | Simulated bias |
| **2** | 1 | same | <TBD>/<TBD> | <TBD>/<TBD> | <TBD> | <TBD> | <TBD> | |
| **2** | 2 | same | <TBD>/<TBD> | <TBD>/<TBD> | <TBD> | <TBD> | <TBD> | |
| **3** | 0 | gate3_kv_eviction.yaml arm3 | <TBD>/<TBD> | <TBD>/<TBD> | <TBD> | <TBD> | <TBD> | Real polling (M6) |
| **3** | 1 | same | <TBD>/<TBD> | <TBD>/<TBD> | <TBD> | <TBD> | <TBD> | |
| **3** | 2 | same | <TBD>/<TBD> | <TBD>/<TBD> | <TBD> | <TBD> | <TBD> | |

---

## What the data falsifies and confirms

### Confirmed
- <TBD>

### Falsified
- <TBD>

### Diagnosed
- <TBD>

---

## Per-quiet-tenant breakdown (Arm 2, seed 0)

| Tenant | count_ok | ttft_p50 (ms) | ttft_p99 (ms) | count_err |
|---|---:|---:|---:|---:|
| quiet_0 | <TBD> | <TBD> | <TBD> | <TBD> |
| quiet_1 | <TBD> | <TBD> | <TBD> | <TBD> |
| quiet_2 | <TBD> | <TBD> | <TBD> | <TBD> |
| quiet_3 | <TBD> | <TBD> | <TBD> | <TBD> |
| quiet_4 | <TBD> | <TBD> | <TBD> | <TBD> |
| quiet_5 | <TBD> | <TBD> | <TBD> | <TBD> |
| quiet_6 | <TBD> | <TBD> | <TBD> | <TBD> |
| **spread** | — | **<TBD>** | **<TBD>** | — |

If Arm 2's spread shows the 7 quiets within ~1.5× on p99 with the saturation bias firing, the bias mechanism is functioning. A wider spread under Arm 2 indicates the bias didn't take (the harness extension or admin-endpoint override wasn't applied uniformly).

---

## Operational notes

- <TBD: pod spin count / any resume vs terminate lessons>
- <TBD: prefix-overlap mechanism — harness flag landed pre-M4 OR bench-side mock applied at run time; document the procedure used>
- <TBD: Arm 2 bias mechanism — harness flag landed OR admin-endpoint override path used; document any divergence from the YAML's `flooder_cost_multiplier: 4.0` endpoint>
- <TBD: cost actual vs ceiling, OOM retries, gmu drops if any>

---

## Implications for the v0.2.0 decision gate

<TBD: 1-4 bullets on what Path C says about M5a.

If gauge p99 < 0.7 in all arms: regime-broken. Ship (c) disconfirm. Queue (b) LMCache for v0.3 — admission-side levers exhausted; cache substrate replacement is the next coherent step. Skip M5a entirely.

If GREEN (≥1.5×): the M5a /metrics-poller implementation has clear headroom; the build is funded. Capture the Arm 2 - Arm 1 delta as the implementation target Arm 3 must hit within ±15%.

If YELLOW (1.2-1.5×): M5a ships flag-gated experimental; document the regime where it helps (long shared prefix, high RPS skew, tight VRAM, gauge p99 > 0.7) and where it doesn't.

If RED (<1.2×): admission-side DRR already captures the gap. Draft (c) disconfirm post for `docs/launch/`. Park M5a to v0.3. Re-anchor T2 as "trace + replay tooling + null result." Reference strategic-plan REVISION disconfirm framing.>

**M4 (or M6) decision-gate result:** <TBD: GREEN / YELLOW / RED / REGIME-BROKEN + one-line rationale>.

---

## Rsync'd artifacts

Per cell (`arm{1,2,3}_seed{0,1,2}/`):
- `summary.json` — bench-harness aggregate + per-tenant percentiles (includes warmup; used only for count_ok and wall_time_s)
- `tenant_flooder.csv`, `tenant_quiet_{0..6}.csv` — per-request rows; **source of truth** for post-60s-warmup percentile extraction
- `bench.log` — harness stdout
- `server.log`, `engine_logs/` — kvwarden + vLLM stderr
- `gpu_trace.csv` — 1Hz nvidia-smi sample
- `prometheus_dump.txt` — kvwarden + vLLM `/metrics` at end-of-cell; **source of truth** for the cache-pressure gauge distribution that feeds the kill-criterion check
- `status_{before,after}.json` — kvwarden /kvwarden/status snapshots
- `pip_freeze.txt`, `git_head.txt` — reproducibility pins

Top-level:
- `summarize_gate3.py` — post-warmup percentile re-extractor (first 60s of submit_time excluded per CSV) + Prometheus dump parser pulling `vllm:kv_cache_usage_perc` percentiles per arm
- `post_warmup_summary.json` — output of summarize_gate3.py across all cells
- `per_tenant_ttft_histograms.png` (optional) — CDFs across 7 quiets per arm

---

## Caveats not yet investigated

1. **GPU substitution.** If A100-SXM4 80GB availability collapses during scheduling, document the actual pod GPU here. Absolute latencies will differ; the cache-pressure ratio should preserve since it's a property of the engine + workload regime, not the compute substrate.
2. **Prefix-overlap fidelity.** <TBD: whether the realized prefix overlap matched the requested 70% — bench-side mock measurement variance, harness flag bugs if landed late.>
3. **Gauge-update cadence.** vLLM's gauge update frequency is undocumented; if `kv_cache_usage_perc` updates < 1 Hz, the M5a poller's TTL/poll-interval needs revisiting and Arm 3 may underperform Arm 2 even with correct policy logic.
4. **Bias-mechanism fidelity (Arm 2).** Bench-side bias vs the M5a wired implementation may diverge in edge cases — Arm 2 is a synthetic upper bound, not a behavioral spec for Arm 3.
5. **Gauge label drift.** If a vLLM minor between 0.19.1 and the M6 run shifts the metric label (e.g. adds a per-engine label, renames `model_name`), the parser in `summarize_gate3.py` needs an update; if missed at run time it shows up as an empty gauge column in the table above.

These are caveats on the Path C result, not on the v0.2.0 ship decision.

---

## Decision tree from here

<TBD: conditional on outcome.
- REGIME-BROKEN (gauge p99 < 0.7 across rescale attempts) → ship (c); queue (b) LMCache for v0.3; skip M5a.
- GREEN → M5a starts 05-20; lands W6; full Gate 3 (Arm 3) reruns at $8 (M6).
- YELLOW → M5a starts 05-20 with explicit experimental flag; v0.2.0 ships flag-gated.
- RED → draft (c) disconfirm post; park M5a to v0.3; re-anchor T2 as null-result chapter.>

This OUTCOME is interpretation-friendly per the runbook contract: raw numbers + diagnostic decomposition + the user calls the framing.
