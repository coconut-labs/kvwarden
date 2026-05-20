# M4 Path C probe — 4-day RunPod retry chain (2026-05-17 → 2026-05-20)

**Verdict:** PROVIDER BLOCKED. RunPod's allocator returned unhealthy hosts on every SKU/cloud combination across 4 calendar days, 512 pod create-attempts, $47.16 in spend, and **zero** `PROBE GO` outcomes. Retry chain stopped 2026-05-20 16:18Z when account balance hit zero (`insufficient funds to bid on this pod`).

## Aggregate stats (from orchestrator.log line-counts across `m4_path_c_probe_2026051{7,8,9}` and `…_20260520`)

| Metric | Value |
|---|---|
| Pods created and torn down | **512** |
| 500 "no instances currently available" responses | 289 |
| Total pod-hours billed | 23.48 |
| **Estimated RunPod spend** | **~$47.16** |
| Pods that ran > 5 min (orchestrator suspension hangs) | 33 |
| Single worst hang | H100 80GB HBM3 `mjbne6krm2i7ax`, ran 61 min, billed $3.03 |
| `PROBE GO` outcomes | **0** |
| `PROBE NO-GO` outcomes | 512 |

## Per-day breakdown

| Date | Pods created | 500s | First / last log entry | Notes |
|---|---|---|---|---|
| 2026-05-17 | 13 | 23 | 19:21Z → 22:46Z | Initial probe + first 6 cron ticks (manual + first cron `19ee4a48`) |
| 2026-05-18 | 67 | 83 | 00:00Z → 23:20Z | Full day on cron `13,43 * * * *`; first multi-minute hangs (48-min, 32-min). Orchestrator hardened mid-day with httpx thread-timeout patch and `caffeinate -is` wrapper. Neither eliminated hangs. |
| 2026-05-19 | **302** | 153 | 00:55Z → 23:59Z | User shifted from cron-only to back-to-back retry chain ("until fixed - keep that retries"). ~12 retries/hour for 23 hours. Worst single hang 61 min on H100 HBM3 ($3.03). |
| 2026-05-20 | 130 | 30 | 00:00Z → 16:18Z | Continued back-to-back chain. Account balance ran out around 16:18Z; final 500 was COMMUNITY-spot `insufficient funds to bid`. Cron `19ee4a48` deleted by user. |
| **Total** | **512** | **289** | | |

## Failure mode (load-bearing finding)

Every successful `create OK` was followed by polls reporting `desiredStatus=RUNNING publicIp= ssh_port=None` until the orchestrator's 60–120s deadline fired. Pods that polled cleanly never published a `publicIp`. This held across:
- A100-SXM4 80GB SECURE (`$1.49/hr`)
- A100 80GB PCIe SECURE (`$1.39/hr`)
- H100 80GB HBM3 SECURE (`$2.99/hr`)
- H100 PCIe SECURE (`$3.29/hr` when available; mostly 500)
- A100-SXM4 80GB COMMUNITY-spot (`$1.39/hr`)

Conclusion: not an SKU-specific capacity issue; RunPod's host orchestration layer is returning hosts that boot the OS but never finalize the network attach. The 60–120s deadline is generous (Gate 2-FAIRNESS 2026-04-19 saw publicIp populate in 15–40s on the same SKU on the same orchestrator).

## Orchestrator behavior under macOS process suspension

The `wait_for_running()` poll loop hung 33 times for 5–61 minutes between `time.sleep(15)` polls. Pattern: poll at t=1.5s, poll at t=17.5s, then a single poll line at t=2887s. Wall-clock advances but the process didn't execute. This is macOS suspending the Python process when the laptop's lid closes / system idles / Claude Code harness throttles. Mitigations attempted:
- **httpx 30s client timeout** — present in original code, did not fire (suspension freezes httpx)
- **ThreadPoolExecutor + outer `fut.result(timeout=N+5)` patch** — landed 2026-05-17 22:30Z, doesn't help (suspension freezes both worker thread and the orchestrator's main thread together)
- **`caffeinate -is` wrapper** — prevents idle and system sleep but doesn't cover all suspension paths the harness uses
Real fix is `signal.alarm()` for deadline enforcement OR a watchdog process outside the suspended Python orchestrator.

## Decision gate state — unchanged

The locked threshold table from issue #103 still applies:

| Arm 2 vs Arm 1 quiet-p99-TTFT delta | v0.2.0 ship |
|---|---|
| ≥ 1.5× | GA — default-on, README hero |
| 1.2 – 1.5× | Experimental — flag-gated |
| < 1.2× | Disconfirm + publish null — ship LRU as default |

Could not be evaluated. No Arm data captured.

## What ships next

The 4-day chain proved RunPod is unusable for M4 at this allocator state. Two paths forward:

- **(A) Port orchestrator to Lambda Labs** — `m4_path_c_orchestrator.py` is ~600 LOC, mostly RunPod REST shape. Estimated 4–6h to port. Lambda Labs A100 SXM4 has been consistently available historically; their on-demand API doesn't have the same allocator-returns-unhealthy-hosts failure mode.
- **(B) Ship v0.2.0 disconfirm-by-default** — treat "could not measure" as equivalent to <1.2× and ship LRU per locked threshold. Faster but weaker public claim and forecloses M5 implementation work.

Provider switch is the cleaner call. Lambda port → resume Path C → if Arm 2 vs Arm 1 ≥ 1.5×, GA-track per locked plan.

## Operational artifacts preserved on this branch

- `results/m4_path_c_probe_20260517/orchestrator.log` (289 lines) — already on PR #136
- `results/m4_path_c_probe_20260517/OUTCOME.md` — initial 3-tick abort writeup (already on PR #136)
- `results/m4_path_c_probe_20260518/orchestrator.log` (1231 lines)
- `results/m4_path_c_probe_20260519/orchestrator.log` (4836 lines) — peak retry day
- `results/m4_path_c_probe_20260520/orchestrator.log` (1909 lines) — including final `insufficient funds` line
- `scripts/m4_path_c_orchestrator.py` — patched mid-chain with ThreadPoolExecutor timeout shim (concurrent.futures-based `api()` wrapper). Doesn't fix the actual suspension hangs but documents the attempted fix.

## RunPod-side cleanup

All 5 visible EXITED pods deleted via REST `DELETE /v1/pods/{id}` on 2026-05-20 ~16:30Z: `9mihjj4sr2956r`, `6vd2l2avzr6zq4`, `u6ehpz6ubowmq1`, `ho7iockhd3tyoh`, `7zg8oaj64rirmy`. No alive pods leaked compute throughout the chain — orchestrator teardown was reliable; only the polling-stage hangs ran up the bill.

## What does NOT change

- `kvwarden==0.1.5` on PyPI, repo public on `coconut-labs/kvwarden`, BibTeX cite-as bumped via PR #134.
- Show HN body at `docs/launch/show_hn.md` still final, still not clicked (8 days past locked 2026-05-12 ship date). Independent of M4.
- mlxd G2 cool-off ended 2026-05-19 — execution unblocked but no work landed this window.

## Pause point

User declared "stop" at 2026-05-20 ~16:30Z after balance exhausted. Cron `19ee4a48` deleted. No retries scheduled. Resuming requires either (a) RunPod top-up + acceptance that the same allocator state will keep burning, or (b) Lambda Labs port. User explicit: "wait until we refresh the budget."
