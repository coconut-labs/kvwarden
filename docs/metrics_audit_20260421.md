# Prometheus Metrics Audit — KVWarden (2026-04-21, pre-launch)

**Scope:** `src/kvwarden/common/metrics.py` (defined), `router/router.py`,
`router/admission.py`, `cache/manager.py`, `tenant/manager.py`,
`engines/base.py` (fired from). Cross-checked against a live
`prometheus_dump.txt` from `results/gate2_preprint_v3/gate2_preprint_arm1_flooder_20260419_202958/`
to see what actually ships on `GET /metrics`.

**Audit by:** Shrey Patel. **Intent:** report only; no code touched this PR.

---

## 1. Top-line verdict: **B−**

The core fairness story — the product's central claim — is instrumented
well: `kvwarden_tenant_ttft_seconds{model,tenant}` is a textbook hero
metric, the bundled `dashboards/kvwarden-fairness.json` wires it up,
admission-controller internals are properly exported (queue depth,
in-flight, wait histogram, reject reasons). An SRE can build the "p99
TTFT per tenant per model" alert in 10 minutes.

Why not A:

- **Four of the declared metrics are dead on arrival** and the live
  `/metrics` dump confirms they emit nothing useful:
  `kvwarden_cache_hits_total`, `kvwarden_cache_memory_bytes`,
  `kvwarden_gpu_memory_used_bytes` never have `.inc()`/`.set()` called
  from any production call site; `kvwarden_cache_misses_total` is
  declared but also never fired. Shipping a `/metrics` surface where
  four entries are permanently empty is a Day-1 "is this thing
  plugged in?" question from the first ops-minded user — precisely the
  audience whose confidence we cannot afford to lose at launch.
- **`kvwarden_requests_total` is missing an `engine` label**, so the
  dashboard question "5xx by engine kind (vllm vs sglang)" cannot be
  answered. For a tool whose whole premise is "middleware sitting
  between you and the engine," this is the first label an SRE will
  ask for.
- **No engine up/down gauge, no cold-start histogram, no
  stream-disconnect counter.** These are standard for an inference
  service and would take <1 hour each to add.

Fix the dead metrics + add the `engine` label and this jumps to A−.

---

## 2. Completeness matrix

| # | SRE question                                                                | Status     | Metric(s)                                                         | Remediation                                                                                            |
|---|------------------------------------------------------------------------------|------------|-------------------------------------------------------------------|--------------------------------------------------------------------------------------------------------|
| 1 | Requests per second per tenant?                                             | **Present** | `kvwarden_requests_total{tenant,model,status}`                    | None. `rate(...)[1m]` works.                                                                           |
| 2 | 50/90/99 p-ile TTFT per tenant per model?                                   | **Present** | `kvwarden_tenant_ttft_seconds{model,tenant}` histogram            | Hero metric. Buckets fine for >25ms; widen to 10ms floor for solo baseline visibility (minor).         |
| 3 | How full is the admission queue right now?                                  | **Partial** | `kvwarden_admission_queue_depth` (global, unlabeled)              | Global only. AdmissionController is process-global so "per model" isn't meaningful today — document it. |
| 4 | How many tenants currently have active requests?                            | **Missing** | —                                                                  | Add `kvwarden_tenants_active` gauge (count of tenants with `active_requests > 0`).                     |
| 5 | Requests rate-limited at tenant-budget gate? Per tenant?                    | **Partial** | `kvwarden_tenant_requests_rejected_total{tenant,reason}`          | Only `reason="budget_exceeded"` is fired. Add `"rate_limit"` when token bucket is dry (see §4).         |
| 6 | How many admitted to the engine? Per model?                                 | **Partial** | `kvwarden_admission_admitted_total` (unlabeled)                   | No `model` label. For a single-engine deployment this is fine; for multi-model, add label.              |
| 7 | 5xx / engine errors? Per model, per engine kind (vllm/sglang)?              | **Partial** | `kvwarden_requests_total{...,status="error"}`                     | No `engine` label. **Critical gap.** Add `engine` label to `request_count`.                             |
| 8 | KV cache block utilization? Per model?                                      | **Missing** | `kvwarden_cache_memory_bytes` declared but never `.set()`         | Wire `CacheManager.tier_stats()` into gauge, e.g. periodic `publish_cache_stats()` hook.                |
| 9 | GPU memory pressure?                                                        | **Missing** | `kvwarden_gpu_memory_used_bytes` declared but never `.set()`      | Add `nvidia-smi` sampler task (best-effort) or wire vLLM's `vllm:gpu_cache_usage_perc` passthrough.     |
| 10| Engine subprocess up/down? Per model?                                       | **Missing** | —                                                                  | Add `kvwarden_engine_up{model,engine}` gauge. `state.adapter.is_healthy` already tracks it.           |
| 11| Cold-start time histogram per model?                                        | **Missing** | —                                                                  | Add `kvwarden_engine_cold_start_seconds{model,engine}`. Measure around `adapter.start()` in `load_model`. |
| 12| HTTP SSE stream duration / disconnect rate?                                 | **Missing** | —                                                                  | Add `kvwarden_stream_duration_seconds{model}` histogram + `kvwarden_stream_disconnects_total{reason}`. `_stream_with_admission` already distinguishes `status="ok"/"error"/"timeout"` — plumb into a dedicated counter. |

**Scoreboard:** 2 fully present, 4 partial, 6 missing. For a tool whose
launch narrative is "operate multi-tenant LLM serving with real
visibility," the missing-six list is the honest reason for B− not B+.

---

## 3. Naming findings

### Critical

None. Names are lowercase-underscore throughout, counters correctly end
in `_total`, durations correctly use `_seconds`. The Prometheus-style
convention is followed cleanly.

### Minor

- **`kvwarden_admission_queue_depth` / `kvwarden_admission_in_flight`**
  are gauges without unit suffix. They're counts of requests, which is
  dimensionless, so this is fine per Prometheus guidance — but a
  `_requests` suffix (e.g. `kvwarden_admission_queue_requests`) would
  be pedantically correct. Leave as-is.
- **`kvwarden_tenant_ttft_seconds` TTFT buckets start at 25ms.** The
  task brief asks for 10ms floor. Our solo vLLM TTFT on H100 was
  ~50ms (see gate2 dumps) so 25ms isn't catastrophic, but add `0.01`
  and `0.015` buckets for a clean percentile near the noise floor.
- **`kvwarden_request_latency_seconds` has no `tenant` label** while
  `kvwarden_tenant_ttft_seconds` does. End-to-end latency per tenant
  is a reasonable question; if you plan to surface it, add the label.
  If you don't, document that TTFT is the tenant-scoped latency and
  end-to-end isn't.
- **Cardinality on `{tenant}` everywhere.** Tenants auto-register from
  the `X-Tenant-ID` header with no cap — a caller that puts a UUID in
  the header explodes cardinality. This is a **known design tradeoff**
  for a fairness-first tool (the hero metric requires the label), not a
  bug. Mitigation: document a tenant-count ceiling (e.g. "soft limit:
  <1000 distinct X-Tenant-ID values per hour; beyond that, Prometheus
  storage grows") and consider an auto-collapse-to-"_other" cap at
  N=500 tenants. Mention this in the launch runbook, not as a code
  change.

---

## 4. Correctness findings

- **`record_cache_access()` is defined but never called.**
  `kvwarden_cache_hits_total` has zero samples in the live dump; its
  `# HELP/# TYPE` lines appear with no metric body. `CacheManager`
  tracks `self._hits` / `self._misses` in Python dicts but never writes
  them back to the Prometheus metric. The `CacheManager` is also never
  consulted from the request path in `router.py` — so even if the
  record call existed, there's no site to fire it from. This is
  the biggest correctness finding: it's not a metric, it's a promise.
- **`kvwarden_cache_misses_total` fires on engine-side miss**:
  same root cause — unfired. Live dump shows 0.0 permanently.
- **`kvwarden_cache_memory_bytes{tier}` gauge never `.set()`**. Same as
  above; `CacheManager.tier_stats()` exposes per-tier `used_gb` but no
  code path publishes it.
- **`kvwarden_gpu_memory_used_bytes{gpu_index}` gauge never `.set()`**.
  No `nvidia-smi` sampler is wired. Declared in 2026-03 but not hooked
  up.
- **`tenant_rejected` only fires on `budget_exceeded`**: the token-bucket
  rate-limit path in `TenantRecord.try_acquire` (`self._tokens < 1.0`)
  returns `False`, then `router.route_request` increments only
  `reason="budget_exceeded"`. The `rate_limit` vs `concurrency_cap` vs
  `burst_empty` distinction is collapsed. For a tool that advertises
  both budgets and rate limits as first-class, split the label.
- **`models_loaded` gauge: `.inc()` fires in `load_model`, `.dec()`
  in `unload_model`.** `evict_model()` calls `unload_model` so the
  path is covered. Looks correct; no leak.
- **TTFT histogram is observed from inside an async generator
  (`_stream_with_admission`).** `Histogram.observe()` is synchronous
  and threadsafe — it's being called between `yield` points, no
  `await` interleaves mid-observe. Correct.
- **`record_request` on stream timeout uses `status="timeout"`** but the
  `kvwarden_requests_total` Counter only declares three labels and
  accepts any string value — Prometheus won't reject it, but the label
  set grows. Fine, just document that `status` has four values:
  `ok | error | timeout | budget_exceeded` (the last implicitly via
  the exception in router.py line 527–536).
- **Admission queue depth gauge may overcount**: `AdmissionController`
  comments acknowledge that timed-out waiters leave cancelled entries
  until lazily drained on `release()`. Under pathological bursty
  timeouts the gauge reads high. Not a launch blocker; document.

---

## 5. Recommended fix-list (effort : lift)

### Ship-with-launch (aim for v0.1.2 cutover before public PyPI push)

| Fix                                                                                                             | Effort | Lift | Why before launch                                                                                                     |
|------------------------------------------------------------------------------------------------------------------|--------|------|-----------------------------------------------------------------------------------------------------------------------|
| **Delete dead metrics OR wire them up.** `cache_hits`, `cache_misses`, `cache_memory_bytes`, `gpu_memory_used_bytes`. Minimum: delete declarations + tests. Preferred: wire a 10s periodic sampler publishing `CacheManager.tier_stats()` + `nvidia-smi --query-gpu=memory.used`. | 1-3h   | High | First `curl /metrics` after install shows no dead entries. This is the single highest-trust-impact fix.                |
| **Add `engine` label to `kvwarden_requests_total`.** Populate from `state.config.engine` (`"vllm"` / `"sglang"`). | 30min  | High | Unlocks SRE question #7 (5xx by engine kind) and differentiates KVWarden from a naked vLLM deployment in dashboards. |
| **Add `kvwarden_engine_up{model,engine}` gauge.** Set from `state.adapter.is_healthy` on each health-check tick. | 1h     | High | Answers question #10; trivially builds a "model down" alert.                                                          |
| **Split `tenant_rejected.reason` into `budget_exceeded` / `rate_limit` / `concurrency_cap`.** `TenantRecord.try_acquire` already knows which gate failed. | 1h     | Med  | For a fairness tool, knowing which limit fired is central to tuning.                                                  |
| **Widen TTFT histogram to 10ms floor.** Add `0.01, 0.015` buckets.                                              | 5min   | Low  | Task brief asks for it; solo-baseline visibility.                                                                     |

**Total ship-with-launch estimate: ~6 hours.** Do the first two
(dead-metric purge + engine label); they're the ones a reviewer will
notice in the `/metrics` output.

### Queue for v0.1.3

| Fix                                                                                                           | Effort | Lift |
|-----------------------------------------------------------------------------------------------------------------|--------|------|
| `kvwarden_engine_cold_start_seconds{model,engine}` histogram around `adapter.start()`.                        | 1h     | Med  |
| `kvwarden_stream_duration_seconds{model}` + `kvwarden_stream_disconnects_total{reason}` (timeout / client_disconnect / engine_timeout). | 2h     | Med  |
| `kvwarden_tenants_active` gauge (count of tenants with `active_requests > 0`), updated from `TenantManager` snapshot. | 1h     | Low  |
| Add per-model admission counters (only if we ever split admission controllers per model — currently global).  | —      | —    |
| Add `tenant` label to `kvwarden_request_latency_seconds` OR document that TTFT is the tenant-scoped latency. | 30min  | Low  |
| Tenant-cardinality cap (auto-collapse to `_other` beyond N=500) OR document the ceiling in the runbook.       | 30min  | Low  |

---

## Appendix: evidence pointers

- Metric definitions: `src/kvwarden/common/metrics.py` (lines 41–142).
- Admission metrics: `src/kvwarden/router/admission.py` (lines 100–146).
- Live exposition example: `results/gate2_preprint_v3/gate2_preprint_arm1_flooder_20260419_202958/prometheus_dump.txt`
  (notice lines 39–48: `cache_hits_total`, `cache_memory_bytes`, and
  `gpu_memory_used_bytes` appear as bare `# HELP/# TYPE` with no samples).
- Reference dashboard (hero metric, fairness-focused): `dashboards/kvwarden-fairness.json`.
- New launch-day overview dashboard (3 panels, importable): `docs/grafana/kvwarden-overview.json`.
