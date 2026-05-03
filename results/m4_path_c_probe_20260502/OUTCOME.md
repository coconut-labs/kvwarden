# M4 Path C probe — ABORTED at provisioning (2026-05-02 / 03)

**Status:** ABORTED. No bench data captured.
**Spend:** $5.33 across two pod attempts (no useful runs).
**Verdict on T2:** unchanged. M4 stays scheduled for the original post-Show-HN window (2026-05-13 → 2026-05-19) per the strategic plan.

## What happened

Two pod attempts, both stuck-provisioning, no SSH ever reachable.

| Attempt | Pod ID | SKU | Created | Killed | Wall | Cost | Outcome |
|---|---|---|---|---|---|---|---|
| 1 | `ia9cicl9p4v7ti` | A100 SXM4 80GB SECURE | 2026-05-02 22:11:41Z | 22:20:32Z | 9 min | $0.22 | `desiredStatus=RUNNING` but no `publicIp` / SSH port for 9 min — known SECURE stuck-provisioning per Gate 1.5 memory note. Killed manually. |
| 2 | `phmp43fhifnf0q` | A100 80GB COMMUNITY | 2026-05-02 22:44:37Z | 2026-05-03 05:12:00Z | **6h 28m** | $5.11 | Same pattern. Pod entered `RUNNING` state, never populated `publicIp`. Orchestrator's `wait_for_ssh` deadline didn't fire because the outer poll-loop kept ticking on `desiredStatus` instead of gating on `publicIp != ""`. Killed manually after credit-burn audit. |

## Operational lessons (capture for future M4 retry)

1. **`desiredStatus=RUNNING` is necessary but not sufficient.** RunPod pods can hold the RUNNING state for hours without ever getting `publicIp` or port mappings. Future pod-readiness logic must gate on `publicIp != "" AND portMappings != null`, not on `desiredStatus` alone. The orchestrator at `scripts/m4_path_c_orchestrator.py:160` polls `desiredStatus`; that's the bug.
2. **MAX_POD_SECS in-pod self-destruct does not fire** if the pod never actually starts user-space (no SSH = no shell to run the timer). The local-side wall-clock cap is the only real ceiling.
3. **Both SECURE and COMMUNITY A100 stalled today.** Memory's "use SECURE for stability" advice is a snapshot, not a guarantee. Validate slot health day-of with a 2-minute spin-and-tear-down probe before committing to a multi-hour bench.
4. **Polling reflex from the agent runtime.** The agent kept firing monitor-tick "completed" notifications back to the parent session for hours. Future orchestrators should use one bg poller + one deadline alarm (per P4's `PROVISIONING_LOG.md` recommendation), not free-running polls.

## Next M4 attempt — preconditions

Before re-spending against M4, fix:

- **Orchestrator poll fix** (~10 LOC) — gate readiness on `publicIp != ""`, not `desiredStatus`. Single-line condition change at `scripts/m4_path_c_orchestrator.py:160`.
- **Two-minute spin-and-tear probe first** — before launching the full M4 orchestrator, probe the chosen SKU with a tiny no-op pod. If publicIp populates within 90s, proceed. If not, pivot SKU. Saves multi-hour stuck-provisioning waits.
- **Calendar alignment** — original plan locks M4 at 2026-05-13 → 2026-05-19 (post-Show-HN). Today's pre-launch attempt was an over-extension; returning to plan.

## Reusable infra shipped on this branch

- `scripts/m4_path_c_orchestrator.py` (534 LOC) — full RunPod orchestrator with cost guards, SKU fallback (SECURE → COMMUNITY/spot), deferred SCP, per-cell smoke-then-bench, tear-down in `finally{}`. The poll bug is fixable without re-architecting; the rest is solid foundation for the M4 retry.
- `scripts/m4_summarize.py` — post-bench summarizer; computes per-cell quiet p99, gauge p99, Arm-vs-Arm ratio, applies the locked threshold table.
- `scripts/gate_pod_bootstrap.sh` extension — adds 250 ms `vllm:kv_cache_usage_perc` gauge scraper that writes `gauge_trace.csv` per cell, plus PID tracking + cleanup of the gauge process. This is the in-pod side of the M4 measurement; works as designed when the pod's actually reachable.

## What this changes for v0.2.0

Nothing material. M4 was always post-launch in the locked plan. The pre-launch over-extension cost $5.33 to learn that A100 SECURE + COMMUNITY both have stuck-provisioning days, which is captured here for the M4 retry runbook update. The reusable orchestrator + bootstrap extension are net wins regardless.

## Cost reconciliation

- Session pod spend total: **$7.21** ($0.24 P1 gauge pre-flight + $5.33 M4 abort + $1.64 Gate 2.1b H100 abort)
- T2 chapter ceiling: $20
- Remaining headroom: $12.79
- M4 retry budget intact: ~$8 expected on a healthy SKU.
