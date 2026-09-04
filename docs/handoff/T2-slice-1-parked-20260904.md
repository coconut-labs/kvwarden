# T2 Slice 1 — parked 2026-09-04

Cache-pressure admission, shadow mode. Code-complete, gate-green, and
**not merge-ready**: two blockers and a design defect are open. Parked
here deliberately rather than fixed in a hurry.

- Reviewed code: `feat/cache-pressure-shadow` @ `a6468f3` (also pushed).
- This branch is that plus this note. Nothing else differs.
- Gate at park time: `pytest tests/unit/ -q` 325 passed, `ruff check` and
  `ruff format --check` clean. `tests/integration/test_cache_pressure_admission_hot_path.py`
  1 passed, 3 skipped.

## What shipped

Three commits, each marked `[unstable]` in its body.

| Commit | What |
|---|---|
| `04b8ede` | `cache_load_scaling()` + `compose_priority()` in `router/admission.py`. Pure, no callers. Cleared the 10 strict-xfail markers from #115/#118, added 5 property tests. |
| `8964516` | `cache/pressure.py` — `CachePressurePoller` scrapes `vllm:kv_cache_usage_perc`, records into `CacheManager`. `EngineAdapter.metrics_url`. Inert; nothing constructs it. |
| `a6468f3` | `router/shadow.py` + the `cache_pressure_admission` config block, off by default. Poller started from router lifecycle; shadow recording at `router.py:519-527`. |

Contract: shadow only. The priority handed to `AdmissionController.acquire`
is unchanged. Flag off registers no Prometheus series and starts no task.

## Open before this can merge

### B1 — blocker. `record()` raises into the request path and leaks a tenant slot

`router.py:519-527` sits between `try_acquire_for_tenant()` (already
incremented `active_requests`) and `acquire()`. Nothing in that window
releases the slot. `compose_priority` / `cache_load_scaling` `TypeError`
on a non-numeric config value, and `from_yaml` has no validator, so
`tenant_weights: {flooder: "0.25"}` — a quoted number — is enough.

Reproduced: `active_requests` climbs 1, 2, 3, 4 across four failed
requests and never comes back. Past `max_concurrent_requests` the tenant
is 429'd until process restart, and `priority_score()` drift corrupts DRR
ordering for every other tenant. `handle_request`'s catch-all turns it
into a 500, so it's invisible.

Fix: wrap `record()` in try/except and log. Four lines. The plan doc's own
Global Constraint said "never raise into the request path" — it was
implemented on the poller and not on the recorder.

### B2 — blocker, needs a decision not a patch

`manager.py:545-546` adds `kv_cache_pressure` and
`kv_cache_pressure_last_poll_ts` to `snapshot()` unconditionally, so
`/status` changes with the flag off.

This is a contract conflict, not an oversight. The pinned integration test
`test_snapshot_exposes_pressure_key_and_metadata` asserts a bare
`CacheManager()` exposes both keys on a cold start — that intent predates
this work. "Flag-off `/status` byte-identical" and that test cannot both
hold. Pick one, then fix the CHANGELOG entry and
`configs/t2_cache_pressure_shadow.yaml:11`, which both describe the keys
as appearing only when enabled.

### C1 — design defect in the approved blend

`compose_priority` goes non-monotone in pressure for any
`tenant_weights` value > 1.0, and the delta goes negative:

```
weight=0.25   deltas at p=.5/.6/.7/.8/.9: [0, 2062, 5250, 9562, 15000]
weight=1.0                                [0,  750, 1500,  2250,  3000]
weight=2.0                                [0,  531,  875,  1031,  1000]
weight=4.0                                [0,  422,  563,   422,      0]
weight=10.0                               [0,  356,  375,    56,   -600]
```

`cost = 1/weight` makes `blended` a downward parabola in `scale` once
weight > 1. A tenant weighted at the ceiling gets zero amplification at
full saturation — the regime the RFC's "monotone non-decreasing in
pressure" language exists to exclude. `test_scaling_is_monotone_non_decreasing_in_pressure`
covers `cache_load_scaling` only, not `compose_priority`.

Negative deltas also poison `kvwarden_shadow_priority_delta`:
`prometheus_client` accepts negative observations silently, `_sum` goes
negative, `histogram_quantile` returns garbage.

Cheap fix: clamp weights to <= 1.0 and validate at config load. Real fix:
make the blend monotone for all weights.

## Also open, non-blocking

| | |
|---|---|
| C2 | `poll_interval_ms: 0` or negative → unthrottled scrape loop, measured ~61k rounds/sec. No validation. |
| C3 | `poll_once` doesn't guard `self._endpoints()` or `self._on_reading()`. Either raising kills the loop permanently, `stop()` swallows it, and the failure counter stops incrementing in exactly that mode. `run()`'s docstring claims otherwise. |
| C4 | `poll_once` returns unclamped. `+Inf`/`NaN` reach the Prometheus gauge while `CacheManager` holds a clamped value. Docstring says `[0.0, 1.0]`. |
| C6 | `cache_load_scaling(0.0, soft_threshold=-1.0)` → 2.58, breaking the RFC-locked identity-at-zero invariant. `ceiling=0.5` returns 0.5, below the documented `>= 1.0`. The `span <= 0.0` branch is dead code. |
| C7 | The endpoints callable spans all loaded models, so pressure is max-across-**models**. RFC §Architecture locks max across instances of the same model and reports cross-model per `model_name`. |
| Buckets | `kvwarden_shadow_priority_delta` tops out at 100k. At 50 in-flight — routine for a 32-RPS flooder against `max_concurrent: 256` — the delta is ~751,500, so every flooder sample lands in `+Inf`. The measurement is unusable at its own target regime. |
| Cardinality | That histogram is keyed on the client-supplied `X-Tenant-ID`: ~10 series per distinct tenant against 1 for the existing per-tenant counters, growing forever, observed on every request including at zero pressure. |
| Docstring | `cache/pressure.py` says "Everything here is off the request hot path." True of the scrape, not of the recording. |

## RFC amendments this work implies

1. §"Reused surface from PR #115" says a flooder at weight 0.25 "contributes
   0.25× as much to its own priority." The pinned test forces `1/weight` —
   4× *more* expensive. Prose and pinned test contradict; the code follows
   the test. Amend the prose.
2. §Risks row 1 specifies a startup smoke-check — scrape `/metrics`, fail
   fast if the gauge is absent. Not implemented. The runtime substitute is
   the failure counter, which C3 shows is inert in the mode that matters.

## Deviations from the plan doc

`docs/superpowers/plans/2026-09-03-t2-slice-1-cache-pressure-shadow.md`
(untracked) was written before this work and locked a decisions table.
The implementation diverges in ~17 places. Three are corrections — the
plan's version would have changed `/metrics` with the flag off, contradicts
itself on soft-degrade target, and never feeds the `snapshot()` key its own
Step 8 tests. The rest are discretionary; the curve shape and the weight
blend were approved in-session, the others were not surfaced at the time.

The one with teeth: the config block is `cache_pressure_admission`, the
plan says `cache_pressure`. `from_yaml` drops unknown top-level keys
silently, so anyone copy-pasting the plan doc's YAML gets a config that
parses clean and does nothing.

## Picking this back up

1. Fix B1. It's four lines and it's the only thing here that can hurt a
   running system.
2. Decide B2, then reconcile the CHANGELOG and sample config to match.
3. Decide C1 — clamp, or make the blend monotone. Add a
   `compose_priority` monotonicity test either way; the existing one
   tests the wrong function.
4. The watermarks are still the RFC's placeholders. Nothing here is
   worth tuning until a probe lands A100 capacity; the M4 attempt burned
   512 pod requests over four days without landing any.
