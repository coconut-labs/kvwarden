# T2 — Tenant-aware KV eviction

**Status:** Draft, opened 2026-04-28. Target close W2 (2026-05-04 → 2026-05-11), pre-Show-HN.

**Tracking:** [#103](https://github.com/coconut-labs/kvwarden/issues/103). Signature stub: [#115](https://github.com/coconut-labs/kvwarden/pull/115). Persisted plan: `~/Personal Projects/.claude/agent-memory-local/god-planner/project_kvwarden_t2_scope_apr28.md`.

**Author:** Shrey Patel.

---

## Motivation

The product is named kvwarden. The name promises KV-cache wardening. The ship today only enforces fairness at the admission layer — Gate 2-FAIRNESS hero (1× A100, Llama-3.1-8B, 32 RPS flooder vs 1 RPS quiet) shows per-tenant token-bucket admission cuts p99 quiet-tenant TTFT from 28,716 ms to 74 ms (1.35× solo). That win is real. It is also entirely upstream of the KV cache.

The cache manager exists. It scores blocks with `freq_weight * freq_score + recency_weight * recency_score`. But the router does not call it on the request path. The only two `cache_manager` callers in `src/kvwarden/router/router.py` are:

- `:346` — `free_blocks_for_model(model_id)` on model unload.
- `:1006` — `snapshot()` for the read-only `kvwarden status` endpoint.

`allocate_block`, `access_block`, `_evict_from_tier` are all dead code at runtime. `reuse_score` ranks nothing observable. There is no per-block tenant identity to weight against.

T2 closes that gap. It extends fairness from admission control into the KV cache, so a flooder tenant cannot evict a quiet tenant's blocks under contention.

The bet: when N=8 tenants share a 70%-prefix-overlap workload (RAG-like), tenant-aware eviction adds a measurable second-order win on top of admission-side DRR. The Gate 3 measure-first probe (W3, $8) tests whether the win exists at all before any code lands.

---

## Goals

- Tenant-aware eviction priority via `reuse_score(*, policy=...)`. Higher tenant weight → higher score → block kept longer.
- Hot-path wiring: router calls `cache_manager.allocate_block(tenant_id=...)` per request and `access_block` on hits. This is the load-bearing missing wiring, not the policy itself.
- A pre-committed validation gate. Gate 3 on 1× A100, N=8, 70% prefix overlap, 32/1 RPS, 3 seeds:
  - p99 quiet-TTFT ≥ 1.5× over Arm 1 baseline → GA, default-on.
  - 1.2× ≤ delta < 1.5× → experimental, flag-gated.
  - delta < 1.2× → publish disconfirm, ship LRU as default.
- LOC budget: ~700-900 honest. 400-500 tracker + wiring, ~100 policy, 200-300 tests. Issue #103's original 300 LOC budget collapses two paths and is wrong by 2-3× for what's actually being built.

## Non-goals

- vLLM engine patches (Path A2). vLLM minor-version cadence breaks any BlockManager fork; out of 90-day scope.
- LMCache substrate replacement (Path B). Substrate is not the differentiator; tenant-aware policy is.
- Cross-tenant prefix-cache sharing. Today's cache is per-tenant by tagging. Sharing requires a separate RFC.
- Preemption guards. If admission control fails open, eviction won't save you — that's an admission-layer bug.
- SGLang parity in Gate 3. v0.3 backlog.

---

## Design

### API surface (locked by #115)

The signature stub already merged the surface so R/T/B agents could run in parallel without conflicts:

```python
@dataclass
class TenantPolicy:
    tenant_weights: dict[str, float] = field(default_factory=dict)

@dataclass
class CacheBlock:
    # ... existing fields ...
    tenant_id: str | None = None

    def reuse_score(
        self,
        now: float,
        freq_weight: float = 0.7,
        recency_weight: float = 0.3,
        decay_half_life_s: float = 300.0,
        *,
        policy: TenantPolicy | None = None,
    ) -> float: ...

class CacheManager:
    def allocate_block(
        self,
        block_id: str,
        model_id: str,
        request_id: str,
        num_tokens: int,
        tier: str = "gpu",
        *,
        tenant_id: str | None = None,
    ) -> CacheBlock | None: ...

    def _evict_from_tier(
        self,
        tier: str,
        needed_gb: float,
        *,
        policy: TenantPolicy | None = None,
    ) -> bool: ...
```

`policy` is `del`'d in the body today. `tenant_id` is plumbed through `allocate_block → _place_block` but never read by the router. This RFC does not change those signatures. It locks what `policy` does in W4.

### Semantics — what `policy` does in W4

The existing score formula stays:

```
base = freq_weight * freq_score(self) + recency_weight * recency_score(self, now)
```

The tenant weight applies multiplicatively to the final score:

```
weight = policy.tenant_weights.get(self.tenant_id, 1.0) if policy is not None else 1.0
score = base * weight
```

Properties:

- `policy is None` → `weight = 1.0`. Identical output to today's code path.
- `self.tenant_id is None` → `weight = 1.0`. Untagged blocks behave like a default tenant. (See Open question 2 — this is not obviously right under contention.)
- Higher `tenant_weights[t]` → higher `reuse_score` → block kept longer. Monotonicity falls out trivially.
- `weight = 0.0` → score collapses to 0, block evicts first within the tier. This is the flooder-throttle knob.
- `weight < 0` is rejected at `TenantPolicy.__post_init__`. Negative scores would invert ordering.

`_evict_from_tier` already passes `policy` through to `reuse_score`. The W4 change is one-line: `score = base * weight`. The 100-LOC budget for "policy" is not in `reuse_score` itself — it's in `TenantPolicy` validation, serialization round-trip for the YAML config surface, and the policy lookup glue between router config and cache manager.

### Hot-path wiring (the load-bearing change)

Today `router.py` calls `cache_manager` only on model unload and status read. W4 wires:

- On request admit (after token-bucket clears): the router computes a `block_id` from `(prompt_hash, request_id, block_index)` and calls `cache_manager.allocate_block(..., tenant_id=request.tenant_id)`. This is wrapper-level — we synthesize logical blocks from prompt boundaries; we do not surface vLLM's actual block IDs. Honest framing: a reference simulator on the captured trace, not a real eviction controller.
- On token streaming: per N-token chunk, call `cache_manager.access_block(block_id)` to bump frequency and recency.
- On request completion: leave blocks resident. Eviction is contention-driven, not request-scoped.
- On model unload: existing `free_blocks_for_model` path stays.

Estimated wiring cost: 400-500 LOC inside `router.py` plus a small `_block_synthesis.py` helper for the prompt-hash → block-id mapping. The bulk of T2's LOC is wiring, not policy.

### Why wrapper-level (A1) and not engine-patch (A2)

A2 surfaces vLLM's real block IDs and accepts a tenant policy hook in the BlockManager. It is the right answer for production-grade real eviction. It is also 1,500-2,500 LOC, breaks on every vLLM minor release, and ships against vLLM upstream review timelines we don't control. A1 is a reference simulator we own end-to-end. If A1 ships GA on Gate 3 and a design partner asks for production-grade eviction, A2 becomes a v0.3 chapter, anchored on A1's measured win.

---

## Alternatives considered

**A2 — vLLM BlockManager patch.** Rejected. 1,500-2,500 LOC. vLLM 0.19.x → 0.20.x cadence will break the fork inside the 90-day window. Upstream PR review is months. v0.3 carry-over.

**B — LMCache integration, delete `manager.py`.** Rejected. LMCache is a cache substrate; T2's value is the tenant-aware policy on top of a substrate, not the substrate itself. LMCache eviction policy may not be tenant-aware-extensible without forking LMCache, which is just A2 one layer down. Re-evaluate post-A1.

**C — Status quo + measure first.** This is what W3 actually does. Gate 3 Arm 1 (current LRU + DRR-admission, no T2 code) and Arm 2 (oracle tenant-aware: pin one tenant's blocks, never evict — synthetic upper bound). If `Arm 2 - Arm 1 < 1.2×` at p99 quiet-TTFT, A1 cannot exceed Arm 2, and we publish disconfirm before writing 700-900 LOC. The disconfirm is shippable: "admission-DRR captures most of the gap; tenant-aware eviction is not the second-order win we expected."

C is not an alternative to A1 — it is the gate that decides whether A1 ships at all.

---

## Risks

| Risk | Mitigation |
|---|---|
| vLLM 0.19.x → 0.20.x changes invalidate wrapper-level block synthesis (e.g., chunked-prefill block boundaries shift). | Pin vLLM 0.19.1 in Gate 3. Re-run on each minor bump pre-release. Pivot to A2 only if Gate 3 stays GA across two consecutive vLLM minors. |
| Bench-regime sensitivity — Gate 3 only validates under high prefix overlap. | Document the regime explicitly in the writeup. README hero claim is "wins under prefix-overlap workloads"; not "wins everywhere." If a design partner reports a no-win regime, that's a v0.3 issue. |
| LOC creep past 800. | Hard rule: anything not in M1-M8 → v0.3 backlog issue. T-agent xfail markers gate the impl scope; a test that needs a new code path that isn't xfail'd is the canary. |
| A1's "reference simulator" framing read as "fake" by HN. | Front-load framing in writeup: "what tenant-aware eviction would do, replayed on captured trace; production patch is v0.3." Honest > clever. |
| Path C shows < 1.2× at Arm 2. | Pre-commit to publishing the null. The disconfirm is the chapter, not the failure mode. Don't grind A1 hoping for noise. |

---

## Testing plan

T agent owns these (parallel deliverable, references for visibility):

- `tests/unit/test_tenant_aware_reuse_score.py` — ~10 tests. Signature acceptance (`policy=` kwarg). Backward-compat (`policy=None` produces identical output to legacy). Semantic tests marked `@pytest.mark.xfail(strict=True, reason="W4 semantics")`: weight monotonicity, missing-tenant defaults to 1.0, weight=0 evicts first, weight<0 rejected at construction.
- `tests/unit/test_tenant_policy.py` — ~6 tests. `TenantPolicy` construction, `tenant_weights` validation, default empty dict, two-tenant ordering invariant, YAML round-trip.
- `tests/integration/test_cache_manager_hot_path.py` — ~4 tests, marked `@pytest.mark.xfail(strict=True, reason="W4 wiring")`. Stubs: route_request triggers `allocate_block` with `tenant_id`; eviction respects policy under contention; `kvwarden status` snapshot reflects per-tenant block counts; model unload still calls `free_blocks_for_model`.

20 tests total. Strict xfail means W4 turns them green by removing the markers, not by editing assertions. CI already runs `pytest tests/integration/` after #115.

---

## Validation plan

B agent owns the bench config + runbook (parallel deliverable):

- Config: `configs/gate3_kv_eviction.yaml`. Modeled on `configs/gate2_fairness_drr.yaml`.
- Runbook: `docs/runbooks/gate3_kv_eviction.md`. Modeled on `docs/launch/gate2_fairness_runbook.md`.

Locked workload:

- 1× A100 SXM4 80GB on RunPod (matches Gate 2 hero hardware).
- vLLM 0.19.1, Llama-3.1-8B-Instruct.
- N=8 tenants. 70% shared prompt prefix (RAG-style: long doc + short tenant-specific suffix).
- Flooder tenant 32 RPS for 5 min. 7 quiet tenants at 1 RPS each for 5 min.
- 3 seeds per arm.

Three arms:

- Arm 1 — Engine LRU (vLLM default) + DRR-admission. Status quo.
- Arm 2 — Oracle tenant-aware: pin one tenant's blocks, never evict. Synthetic upper bound.
- Arm 3 — `reuse_score(policy=...)` tenant-aware via A1.

Path C probe (W3, $8): runs Arm 1 + Arm 2 only. Decision gate at Arm 2 vs Arm 1 quiet-TTFT delta:

| Result | Action |
|---|---|
| Arm 3 vs Arm 1 p99 quiet-TTFT ≥ **1.5×** | GA, default-on, README hero |
| 1.2× ≤ delta < 1.5× | Experimental, flag-gated, regime documented |
| delta < 1.2× | Disconfirm, publish null, ship LRU as default |

Metric is p99 quiet-TTFT to match the Gate 2 hero. Hit-rate is recorded but not the gate — it doesn't translate cleanly to user-perceived latency under contention.

Hard cap: $20. Path C probe ~$8, full Gate 3 ~$8.

---

## Rollout

- Disconfirm: ship LRU as v0.2.0 default. Disconfirm post in `docs/launch/gate3_results.md`. README unchanged. CHANGELOG entry: "Gate 3 disconfirm — admission-DRR captures the gap."
- Experimental: feature-flag `tenant_eviction.enabled: false` default in `configs/`. Documented regime in README. CHANGELOG: "Tenant-aware eviction (experimental, flag-gated)."
- GA: default-on. README hero updated with Gate 3 numbers. CHANGELOG: "Tenant-aware eviction GA. p99 quiet-TTFT improvement on N=8 / 70% overlap / 32 RPS flooder: X×."

GitHub Discussion thread (linked from this RFC) tracks community questions. The thread stays open through v0.2.0.

---

## Timeline

| # | Window | What |
|---|---|---|
| M1 | 04-28 → 05-04 (W1) | This RFC + signature stub (#115) + test skeletons (T agent) + Gate 3 config + runbook (B agent). |
| M2 | 05-05 → 05-11 (W2) | RFC review window. Discussion thread open. PROGRESS.md anchor pointing at T2 chapter. |
| M3 | 05-12 | **Show HN — v0.1.0 ships.** RFC + skeletons + bench config visible in repo. No T2 code wired. |
| M4 | 05-13 → 05-19 (W3) | **Path C probe.** Gate 3 Arm 1 + Arm 2 on 1× A100. Decision gate: Arm 2 - Arm 1 ≥ 1.2× → A1; else → disconfirm. |
| M5a | 05-20 → 06-02 (W4-W5) | A1 implementation if M4 says go. Hot-path wiring + policy semantics + unwind T-agent xfail markers. |
| M5b | 05-20 → 06-02 (W4-W5) | Disconfirm publication if M4 says no-go. Park A1 to v0.3 tracking issue. |
| M6 | 06-03 → 06-09 (W6) | Gate 3 Arm 3 full bench, 3 seeds, bootstrap CIs. (Skipped on M5b path.) |
| M7 | 06-10 → 06-16 (W7) | Bench analysis + writeup. Gate against thresholds. |
| M8 | 06-17 → 06-23 (W8) | **v0.2.0 release.** PyPI publish, CHANGELOG, README, Discussion thread close. |

Pre-launch deliverable through M2: RFC + stub + skeletons + bench config. No wired code before Show HN.

---

## Open questions

These are unresolved at draft time and should be settled in review:

1. **Should `tenant_weights` default to inverse of `TenantBudget` RPS?** Auto-coupling admission and eviction policy means a flooder configured with 32 RPS budget gets weight `1/32 = 0.031` automatically — its blocks evict first. Pro: single config surface, no drift between admission and eviction. Con: couples two layers that may want independent tuning (a flooder might be tolerated at the admission layer but still deserve fair eviction within its admitted requests). Default proposal: keep `tenant_weights` independent in v0.2.0; document the coupling as a v0.3 ergonomics improvement once we have field data.

2. **`tenant_id is None` vs `tenant_id` not in `tenant_weights` — same weight 1.0 today. Should they be?** Today both fall back to weight 1.0. But `None` means "untagged" (e.g., system warmup, internal jobs); "not in dict" means "tenant the operator forgot to configure." Under contention, treating an untagged block as a 1.0-weight default tenant might mask a config bug. Alternative: `None` defaults to 1.0, but unknown configured tenants log a warning. Or: separate `default_weight` field on `TenantPolicy` that applies only to non-None tenant_ids. Resolve before W4 implementation.

3. **Static `tenant_weights` config vs dynamic from DRR deficit counters?** The admission layer already maintains per-tenant deficit state. Feeding deficit into `tenant_weights` at eviction time would auto-couple the two policies and remove a manual config knob. Risk: cross-layer state coupling adds debugging surface; the reference-simulator framing of A1 gets harder if eviction depends on live DRR state. Default proposal: static for v0.2.0, dynamic as a v0.3 RFC if Gate 3 ships GA.

Bonus, lower priority: per-model `TenantPolicy` (cross-model isolation) vs global. `CacheManager` is global today. If a single tenant runs against two models, do they get one weight or two? Default proposal: global per-tenant for v0.2.0, scope to v0.3 if a design partner needs per-model.

---
