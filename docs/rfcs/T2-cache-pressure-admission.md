# T2 — Cache-pressure admission

**Status:** Draft, opened 2026-04-29. Supersedes the eviction RFC at `docs/rfcs/T2-tenant-aware-eviction.md` ([PR #116](https://github.com/coconut-labs/kvwarden/pull/116)). Reason for supersession: the prior RFC's lever was a shadow ledger. `CacheManager` does not couple to engine cache decisions today, so tenant-aware eviction ordering inside it changes nothing observable downstream. The reframe moves the lever to admission, where a real per-request decision already runs in the hot path.

**Tracking:** [#103](https://github.com/coconut-labs/kvwarden/issues/103). Signature stub: [#115](https://github.com/coconut-labs/kvwarden/pull/115). Discussion: [#117](https://github.com/coconut-labs/kvwarden/discussions/117). Persisted plan (REVISION 2026-04-28T+1): `~/Personal Projects/.claude/agent-memory-local/god-planner/project_kvwarden_t2_scope_apr28.md`.

**Author:** Shrey Patel.

---

## Motivation

The product is named kvwarden. The name promises KV-cache wardening. v0.1 is admission-only — Gate 2-FAIRNESS hero (1× A100, Llama-3.1-8B, 32 RPS flooder vs 1 RPS quiet) shows per-tenant token-bucket admission cuts p99 quiet-tenant TTFT from 28,716 ms to 74 ms (1.35× solo). That win is real. It is upstream of the KV cache by every meaningful definition.

The earlier RFC (#116) proposed extending fairness into eviction: tenant-aware `reuse_score` ordering inside `CacheManager._evict_from_tier`. Three greps on 2026-04-28 invalidated the foundational assumption underneath that plan:

- `grep -rnE 'cache|block_id|allocate|access_block' src/kvwarden/engines/ --include="*.py"` → 0 matches. Engine adapters never tell `CacheManager` about a request, never receive eviction guidance back.
- `grep -rnE 'cache|cache_manager' src/kvwarden/router/admission.py` → 0 matches. `AdmissionController` reads tenant rate limits and concurrency caps; cache pressure is invisible to it.
- `grep -nE '_lmcache\.' src/kvwarden/cache/manager.py` → 0 method calls. The LMCache integration imports the package and logs but never calls a method.

`CacheManager` self-documents at the top of the file as the orchestration layer's bookkeeper. It is. Tenant-aware eviction order inside `_evict_from_tier` would change shadow-ledger output that no decision in kvwarden or any engine reads. Path A1 cannot move TTFT regardless of harness or workload.

The lever exists at admission, not eviction. `AdmissionController.acquire(priority: int, timeout: float)` runs per request and already shapes who waits and who proceeds. What it does not see today is engine cache pressure. vLLM exposes that signal as a Prometheus gauge. T2 v0.2 wires the gauge into priority composition so admission becomes cache-pressure-aware.

---

## Goals

- Couple engine KV cache pressure to admission decisions. Admission gains a signal it does not have today.
- Ship v0.2.0 with cache-aware admission gating: a poller in `cache_manager` (or sidecar in `router`) scrapes vLLM's `/metrics`, surfaces the gauge in `cache_manager.snapshot()`, and `AdmissionController` composes it with per-tenant deficit when computing priority.
- Pre-committed validation gate. Gate 3 on 1× A100, N=8, 70% prefix overlap, 32/1 RPS, 3 seeds. Two-stage thresholds in §8.
- Honest scope: ~200 LOC core (~30 poller + ~10 snapshot surface + ~30 admission scaling + ~20 priority composition + ~80 tests). Not a tracker. The substrate is vLLM's; we bridge to it.

## Non-goals

- vLLM `BlockManager` patch (Path A2). 1,500-2,500 LOC, breaks on every vLLM minor release. Out of 90-day scope.
- LMCache substrate replacement (Path B). 4-6 week slip past the locked Show HN. Queued for v0.3 explicitly.
- Cross-tenant prefix-cache sharing. Separate RFC, depends on per-tenant block tagging that v0.2 does not gain.
- Preemption guards. Admission is the lever. If admission fails open, preemption is an admission-layer bug.
- Per-tenant cache attribution. The vLLM gauge is global. v0.2 does not claim tenant-level cache visibility — see §6 risk and Open question (c).

---

## Design

### The bridge metric

vLLM exposes `vllm:kv_cache_usage_perc` as a Prometheus `Gauge` on the engine's `/metrics` endpoint. Documented at `docs.vllm.ai/en/latest/design/metrics/`: "Fraction of used KV cache blocks (0–1)". Verified primary source on 2026-04-28T+1.

Two properties of the gauge are load-bearing:

1. **Instance-level.** The only label is `model_name`. There is no per-tenant, per-request, or per-block label. Cache pressure as kvwarden sees it is global to the engine instance.
2. **Range [0.0, 1.0].** Composes cleanly with a unitless scaling function. No magnitude calibration across hardware.

The first property means tenant fairness in v0.2 comes from kvwarden's existing per-tenant signal (DRR deficit, tenant_manager state) composed with the engine's global pressure. We do not claim per-tenant cache visibility. The composition is honest: cache pressure is engine state, tenant attribution is orchestration state, and admission combines them.

Adjacent histograms exist (`vllm:kv_block_lifetime_seconds`, `vllm:kv_block_idle_before_evict_seconds`, `vllm:kv_block_reuse_gap_seconds`) for v0.3+ adaptive thresholding. Out of scope for v0.2.

Refresh tempo is a verifiable property of the gauge, not a documented one. M1 pre-flight scrapes `/metrics` against the existing test vLLM container to measure the update cadence and confirm the gauge is reachable without auth. Per R5 in the persisted plan, if vLLM updates the gauge slower than ~1 Hz, the poller caches the last value with a TTL bounded by the observed update rate; admission still composes against a stale-but-bounded reading rather than blocking on a fresh poll.

### Architecture

A poller lives in `cache_manager` (or a sidecar inside `router`; placement is an implementation detail not locked here). The poller:

- Scrapes `/metrics` every N ms. Target cadence ~250 ms; tunable via config. See Open question (b).
- Caches the latest gauge value in memory. Async, off the request hot path.
- Surfaces it as `cache_manager.snapshot()["kv_cache_pressure"]: float in [0.0, 1.0]`. Parallel to today's `model_blocks` key in `snapshot()`.
- Falls back to `0.0` (no pressure signal) if the engine's `/metrics` is unreachable. Soft degradation — admission falls back to today's behavior, not error.

Engine adapter responsibility: at startup, advertise the engine's Prometheus endpoint URL into the engine record `cache_manager` already keeps. One-line change per adapter, no new abstraction.

Multi-instance behavior is decided here for clarity: when kvwarden routes across multiple engine instances of the same model, the snapshot's `kv_cache_pressure` is the **max across instances** that hold the candidate model. Composition against per-tenant deficit therefore reflects worst-case engine pressure for the route, not an average that could mask one saturated engine. Cross-model pressure is reported per `model_name` as today's `model_blocks` already is. Engine restart drops the cached value; soft degrade applies for one poll cycle until the post-restart endpoint responds.

### Admission integration

`AdmissionController.acquire(priority: int, timeout: float)` stays an `int` priority. The composition happens upstream — at the router, where today's DRR ordering at `router.py:463-467` builds the priority integer. The W4 change extends that composition.

Today (paraphrased):

```
priority = bucket_priority + tenant_deficit_score
```

T2 extends to:

```
pressure = cache_manager.snapshot().get("kv_cache_pressure", 0.0)
scale = cache_load_scaling(pressure)        # ≥ 1.0
priority = (tenant_deficit_score * scale) + bucket_priority
```

The scaling function shape: piecewise linear, identity below a soft threshold, ramp under hard pressure, saturate. Sketch:

```
cache_load_scaling(p):
    if p < 0.5:   return 1.0
    if p < 0.9:   return 1.0 + 6.0 * (p - 0.5)        # ramps 1.0 → 3.4
    return 4.0                                          # saturates
```

The exact knee, slope, and saturation ceiling are tuned in M5 against the M4 probe distribution. Sigmoid and hand-tuned tables are alternatives — see Open question (a). The shape is locked to "monotone non-decreasing in pressure, identity at zero pressure, bounded above" so that v0.1 behavior is recovered when the engine cache is cold and DRR dominates harder when the cache saturates.

Two invariants the scaling function preserves across any tuning pass:

- **Identity at zero pressure.** `cache_load_scaling(0.0) == 1.0`. v0.1 admission behavior is recovered byte-for-byte when the engine cache is cold. The reframe does not regress quiet-tenant TTFT in the no-pressure regime.
- **Bounded above.** The ceiling caps the deficit amplification so a single saturated engine cannot starve a tenant whose deficit happens to be small. Saturation ceiling at 4.0 in the sketch above; tuned against M4.

### Reused surface from PR #115

PR #115 landed `TenantPolicy` and `tenant_id` on `CacheBlock`. Both stay. The semantics of `TenantPolicy.tenant_weights` shift from "eviction weight" → "admission cost weight": a per-tenant multiplier on `tenant_deficit_score` under cache pressure. A flooder configured at weight 0.25 contributes 0.25× as much to its own priority under saturation, evicting it from the admit gate faster than its DRR deficit alone would.

`tenant_id` on `CacheBlock` has no semantic role in T2 v0.2. It stays because v0.3 trace-replay tooling (the Path A1 carry-over, queued explicitly) needs per-block tenant attribution to replay captured traces against alternative policies offline.

### Why this composition is honest

The engine owns the cache substrate. We do not patch its eviction. We do not claim to outperform vLLM's `BlockManager`. We claim the orchestration layer composes a signal — `cache_pressure × tenant_deficit` — that the engine does not compose on its own. Engines admission-throttle by their own queue depth, not by the joint distribution of their cache state and a tenant fairness ledger that lives outside them. That joint signal is the lever kvwarden adds.

The launch framing follows from the mechanism: v0.1 ships admission-layer fairness (DRR), v0.2 makes admission cache-pressure-aware. The cache eviction policy itself remains the engine's responsibility. We do not claim to outperform vLLM's `BlockManager`; we claim that admission has more information than it had before. This is genuinely defensible. It does not assert a per-block lever we do not have. It does not assert a per-tenant cache controller. It asserts a feedback loop the engine doesn't currently provide.

---

## Alternatives considered

**A1 — tenant-aware eviction in `CacheManager._evict_from_tier`.** Rejected. The shadow-ledger gap (greps above) means changes to eviction ordering inside `_evict_from_tier` change output that no caller reads. The earlier RFC at `docs/rfcs/T2-tenant-aware-eviction.md` (PR #116) scoped this work; the supersession audit trail there preserves the rejection.

**A2 — vLLM `BlockManager` patch.** Rejected. 1,500-2,500 LOC. vLLM minor-version cadence (0.19.x → 0.20.x) breaks any fork inside the 90-day window. Upstream review timelines we don't control. v0.3 carry-over only if a design partner asks for production-grade real eviction.

**B — LMCache substrate replacement, delete `CacheManager`.** Rejected. 4-6 week slip past the locked Show HN at 2026-05-12. The substrate change supports going less ambitious post-launch, not more. Queued for v0.3 explicitly: if v0.2 ships GA, v0.3 reframes as "LMCache + cache-pressure admission as unified control plane."

**C — drop the perf framing entirely.** Rejected for v0.2 default. Held as M4 fallback if the probe disconfirms. Forces a wholesale launch-narrative rewrite at T-14 days otherwise, for a chapter that is post-launch anyway.

---

## Risks

| Risk | Mitigation |
|---|---|
| vLLM `vllm:kv_cache_usage_perc` label set or gauge name changes between minor releases. | Pin `vllm>=0.19.0,<0.20.0` in `requirements-gpu.txt` for v0.2.0. Smoke-check at engine startup: scrape `/metrics`, fail fast if the gauge is absent. Re-validate on each vLLM minor bump pre-release. |
| Global gauge means cache pressure cannot distinguish per-tenant cache state. A flooder pressuring the cache pulls all tenants' priorities up together. | Tenant fairness comes from the per-tenant `tenant_deficit_score` and `tenant_weights` multiplier, not from per-tenant cache state. Document the limitation in v0.2 docs. Flag as "v0.2 limitation, v0.3 if a design partner asks." See Open question (c). |
| Poller adds `/metrics` scrape latency, blocks the request hot path. | 250 ms async cadence. Cached in memory. Hot path reads the cached value, never blocks on HTTP. Soft degrade to `0.0` on poller failure — admission keeps working. |
| Workload regime: if `kv_cache_usage_perc` p99 doesn't exceed ~0.7 in any probe arm, the lever exists but is never exercised. The bench would return Arm 2 ≈ Arm 1 by construction. | M4 runbook explicitly captures the gauge p50/p95/p99 distribution per arm as a blocker check. If max p99 < 0.7 across the regime, rescale the workload (longer prompts, more tenants, sustained flooder) and re-probe. If still < 0.7 after rescale, ship (c) disconfirm and queue (b). See §8 kill criterion. |
| Reference framing read as "yet another priority queue" by HN. | Lead with: engines admission-throttle by their own queue depth, not by the joint distribution of cache state and a tenant fairness ledger that lives outside them. The orchestration layer composes the joint signal. Honest > clever. |

---

## Testing plan

A new file replaces the eviction-semantic test surface from PR #118. The signature-acceptance tests stay live; semantic xfail tests get docstring updates to point at admission semantics.

- `tests/unit/test_cache_pressure_admission.py` — 8-10 tests, all `@pytest.mark.xfail(strict=True, reason="W4 wiring")`. Concrete list:
  - `test_scaling_function_identity_below_soft_threshold` — `cache_load_scaling(p)` returns 1.0 for `p < 0.5`.
  - `test_scaling_function_monotone_non_decreasing` — pairwise property: `p1 < p2 ⟹ scaling(p1) ≤ scaling(p2)`.
  - `test_scaling_function_saturation_ceiling` — `scaling(p ≥ 0.9)` saturates at the configured ceiling, no further amplification.
  - `test_priority_composition_uses_snapshot_pressure` — `AdmissionController` priority reflects `cache_manager.snapshot()["kv_cache_pressure"]` end-to-end.
  - `test_tenant_weights_multiplier_under_saturation` — flooder configured at weight 0.25 contributes 0.25× under high pressure; baseline tenant unaffected.
  - `test_missing_tenant_falls_back_to_default_weight` — request with no `tenant_id` admits with weight 1.0 (today's behavior).
  - `test_metrics_endpoint_unreachable_soft_degrades` — poller failure surfaces `kv_cache_pressure: 0.0` in snapshot, admission falls back to v0.1 priority composition.
  - `test_gauge_value_out_of_range_clamps` — gauge >1.0 clamps to 1.0, <0.0 clamps to 0.0 (defensive against vLLM bugs).
  - `test_zero_pressure_is_byte_for_byte_v01_behavior` — at `p == 0.0`, priority composition matches the v0.1 code path exactly.
  - Optional 10th: `test_multi_instance_pressure_is_max_across_instances` — when two engine instances host the same model, the snapshot reflects the max of the two gauges.

  Strict xfail flips to xpass-fail when W4 lands; no assertion edits needed.
- `tests/unit/test_tenant_policy.py` — kept as live tests. Construction, `tenant_weights` validation, default empty dict, two-tenant ordering, YAML round-trip. Docstrings update to "admission cost weight" semantics; behavior unchanged.
- `tests/unit/test_tenant_aware_reuse_score.py` — deleted by sibling agent A3 in M1. The eviction-semantic xfails it carries are no longer reachable.
- `tests/integration/test_cache_manager_hot_path.py` — reframed (or renamed to `test_cache_pressure_admission_hot_path.py`; final path locked in T-agent's M1 PR). Stubs: `cache_manager.snapshot()` returns `kv_cache_pressure` key; `AdmissionController.acquire` priority composition reflects gauge value end-to-end; engine adapter advertises `/metrics` URL on startup; soft degrade on engine `/metrics` 503.

CI already runs `pytest tests/integration/` after PR #115. Strict xfail markers gate the implementation: a failing test that needs a new code path not covered by xfail is the canary that scope is creeping.

---

## Validation plan

Sibling agent A4 owns the bench config rewrite at PR #119. Reference for visibility:

- 1× A100 SXM4 80GB on RunPod (matches Gate 2 hero).
- vLLM 0.19.1, Llama-3.1-8B-Instruct.
- N=8 tenants. 70% shared prompt prefix (RAG-style: long doc + short tenant-specific suffix).
- Flooder tenant 32 RPS for 5 min. 7 quiet tenants at 1 RPS each for 5 min.
- 3 seeds per arm.

Three arms:

- **Arm 1** — DRR-only (status quo, current code). Baseline.
- **Arm 2** — DRR + simulated saturation bias. Priority injection that pretends the gauge reads >0.9, scales `tenant_deficit_score` by the saturation ceiling. Measures the scaling function in isolation, no `/metrics` polling. Synthetic upper bound for what cache-pressure admission can do at this regime.
- **Arm 3** — Real `/metrics` polling + cache-pressure admission. The W4-W6 implementation end-to-end.

Path C probe (M4, ~$8): runs Arm 1 + Arm 2 only. Two-stage thresholds:

**Stage 1 — M4 probe, Arm 2 vs Arm 1:**

| Result | Action |
|---|---|
| p99 quiet-TTFT improvement ≥ **1.5×** | (a) GA-track: build the real implementation. |
| **1.2× – 1.5×** | (a) Experimental-track: ship flag-gated, regime documented. |
| **< 1.2×** | (c) disconfirm: DRR-admission is already capturing the gap. Ship null result. v0.2.0 ships harness + replay tooling + writeup, no perf claim. |
| Arm 2 *worse* than Arm 1 | Abandon (a) mid-flight. Ship (c). Queue (b) for v0.3. |

**Stage 2 — M6 full Gate 3, Arm 3 vs Arm 1:**

Same 1.5× / 1.2-1.5× / <1.2× thresholds against the real implementation. Additional constraint: Arm 3 must track Arm 2 within ±15%. If Arm 3 underperforms Arm 2 by more than 15%, the implementation is sub-optimal vs the simulated upper bound and needs a tuning pass before v0.2.0 ships.

Metric: p99 quiet-TTFT, matches Gate 2 hero. Hit-rate, evictions/min, prefix-cache hit-rate collected as supporting evidence, not gating.

**Single discriminating fact / kill criterion:** does `vllm:kv_cache_usage_perc` p99 exceed ~0.7 in any arm of M4? The runbook captures this distribution explicitly as a blocker check before the threshold gates are evaluated. If the answer is NO under the locked regime, rescale once: longer prompts, more tenants, sustained flooder over a longer duration. Re-probe (~$8, 3 days). If still NO after rescale, ship (c) disconfirm and queue (b) for v0.3 with rationale "admission-side levers exhausted, cache substrate replacement is the next coherent step." This protects the chapter from disconfirm-by-construction: a workload that never pressures the cache cannot disconfirm a cache-pressure lever, and the runbook must distinguish the two failure modes.

Hard cap: $20. Path C probe ~$8, full Gate 3 ~$8.

---

## Rollout

- **Disconfirm:** ship LRU + DRR-only as v0.2.0 default. Disconfirm post in `docs/launch/gate3_results.md`. README hero unchanged. CHANGELOG entry: "Gate 3 disconfirm — admission-DRR captures the gap. Cache-pressure admission queued for v0.3 with substrate replacement."
- **Experimental:** feature-flag `cache_pressure_admission.enabled: false` default in `configs/`. Regime documented in README ("wins under high-prefix-overlap + cache-saturating flooder"). CHANGELOG: "Cache-pressure admission (experimental, flag-gated)."
- **GA:** default-on. README hero updated with Gate 3 numbers. CHANGELOG: "Cache-pressure admission GA. p99 quiet-TTFT improvement on N=8 / 70% overlap / 32 RPS flooder: X×."

Discussion [#117](https://github.com/coconut-labs/kvwarden/discussions/117) is appended with a revision pointer to this RFC. Stays open through v0.2.0.

---

## Timeline

| # | Window | What |
|---|---|---|
| M1 | 04-29 → 05-04 | This RFC + sig-stub edit + tests rewrite + bench config rewrite. Landing in parallel today across 4 sibling agents (R/T/B + this one). |
| M2 | 05-05 → 05-11 | RFC review window. CI green across the reframed surface. PROGRESS.md anchor pointing at v0.2 chapter. |
| M3 | 2026-05-12 | **Show HN — v0.1.0 ships.** RFC + skeletons + bench config visible in repo. No T2 code wired. |
| M4 | 05-13 → 05-19 | **Gate 3 Path C probe.** Arm 1 + Arm 2 on 1× A100. Decision gate per §8. |
| M5a | 05-20 → 06-02 | ~200 LOC implementation if M4 says go. Poller + snapshot surface + admission scaling + priority composition + tests. Unwind T-agent xfail markers. |
| M5b | 05-20 → 06-02 | (c) disconfirm publication if M4 says no-go. Park to v0.3. Chapter ships as null result + replay tooling. |
| M6 | 06-03 → 06-09 | Gate 3 Arm 3 full bench. 3 seeds, bootstrap CIs. Skipped on M5b path. |
| M7 | 06-10 → 06-16 | Bench analysis + writeup. Gate against Stage 2 thresholds. |
| M8 | 06-17 → 06-23 | **v0.2.0 release.** PyPI publish, CHANGELOG, README, Discussion close. |

Pre-launch deliverable through M2: this RFC + amended skeletons + reframed bench config. No wired code before Show HN.

---

## Open questions

These are unresolved at draft time and should be settled in review:

1. **Scaling function shape — piecewise linear vs sigmoid vs hand-tuned table.** The piecewise sketch in §Design is a starting point. A sigmoid is smoother and has one fewer knee parameter to tune. A hand-tuned table is the most flexible but loses analytical tractability. The right answer depends on the M4 probe distribution: if the gauge is bimodal (cache mostly cold or mostly saturated), piecewise is fine; if continuous, sigmoid wins; if the response is non-monotone in a way I don't predict, table. Default proposal: piecewise linear for M5, revisit after M6 with the real distribution in hand. Tune knee + slope in M5, lock for v0.2.0 release.

2. **Poller cadence — 250 ms default vs adaptive.** A fixed 250 ms cadence is simple, predictable, and bounds the admission decision's staleness at 250 ms worst case. An adaptive cadence (poll faster when pressure is changing, slow down when stable) reduces overhead under steady state but adds state and a tuning surface. Pro fixed: implementation simplicity, easier to reason about under load. Pro adaptive: lower overhead at low pressure, finer resolution at the threshold knees. Default proposal: fixed 250 ms for v0.2.0; adaptive as a v0.3 RFC if M6 shows the cadence is the bottleneck.

3. **How to surface the global-gauge limitation in v0.2 docs without overclaiming per-tenant cache fairness.** The gauge is global. v0.2 does not give kvwarden per-tenant cache visibility. The fairness story is "tenant attribution is orchestration state, cache pressure is engine state, admission composes them" — true, but easy to read as "kvwarden makes the cache itself tenant-fair." README and launch post need a one-paragraph honest framing that says what v0.2 is (admission-side joint signal) and what it isn't (per-tenant cache controller). Resolve before M3 ships, since the launch narrative locks at Show HN. Default proposal: a short "v0.2 limitations" paragraph in the README under the Gate 3 hero, plus an FAQ entry tying the limitation to the v0.3 LMCache carry-over.

Bonus, lower priority: SGLang adapter parity. SGLang exposes a comparable cache-pressure metric under a different name; v0.2 ships vLLM-only. If a design partner needs SGLang under the same regime, a thin per-engine metric-name mapping keeps the abstraction. Default proposal: vLLM-only for v0.2, file SGLang as a v0.3 issue if asked.

---
