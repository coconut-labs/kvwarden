# M4 Path C probe — 2026-05-17 (third consecutive capacity-out, no-go)

**Verdict:** NO-GO. RunPod inventory depleted across 5 SKU/cloud combos. Retry overnight US-East morning (2026-05-18 12-15Z).

## SKU sweep at 18:34Z

| GPU | Cloud | Interruptible | Status |
|---|---|---|---|
| A100-SXM4 80GB | SECURE | no | 500 no instances available |
| A100 80GB PCIe | SECURE | no | 500 no instances available |
| H100 80GB HBM3 | SECURE | no | 500 no instances available |
| H100 PCIe | SECURE | no | 500 no instances available |
| A100-SXM4 80GB | COMMUNITY | yes (spot) | 500 no longer available |

Earlier same-day 03:38Z attempt also no-go: A100-SXM4 SECURE pod `n66rx9ih3wudm9` created at $1.49/hr but never published `publicIp` within the 90s deadline; torn down at t=93.7s. Fallback A100 PCIe / A100-SXM4 community-spot both 500'd at create time. Full log: `orchestrator.log`.

## Pattern across three attempts

| Date | Result | Notes |
|---|---|---|
| 2026-05-15 | No-go | A100-SXM4 / A100 PCIe / H100 SXM5 / H100 PCIe all 500 at create |
| 2026-05-17 03:38Z | No-go | A100-SXM4 SECURE created but stalled with no publicIp (90s deadline); fallbacks 500 |
| 2026-05-17 18:34Z | No-go | All 5 SKU/cloud combos 500 at create |

Structural, not transient. RunPod's A100/H100 supply has been thin for three days running.

## No spend

Zero pod-hours billed across all three attempts. Orchestrator's `--probe-only` mode + 90s/120s deadlines + immediate teardown on no-publicIp prevented the kind of $5.33 burn that triggered PR #135's hardening.

## Next attempt

Retry overnight US-East morning (Mon 2026-05-18, ~12-15Z = 8-11 ET = 5-8 PT) when both US-East and Asia inventory tend to be fresher. If three consecutive morning attempts also no-go, escalate to provider switch (Lambda Labs A100 SXM4 has been consistently available; orchestrator port estimated 4-6h of work).

Standalone M4 retry command (probe-only, requires `RUNPOD_API_KEY` + dummy `HF_TOKEN`):

```sh
python3 scripts/m4_path_c_orchestrator.py --probe-only --probe-timeout 120 --cloud-type SECURE
```

If probe returns 0, kick the full bench (drops `--probe-only`, runs 6 cells, ~$8, needs real `HF_TOKEN`).

## Decision-gate state unchanged

Locked threshold table (per `project_kvwarden_t2.md` and issue #103):

| Arm 2 vs Arm 1 quiet-p99-TTFT delta | v0.2.0 ship |
|---|---|
| ≥ 1.5× | GA — default-on, README hero |
| 1.2 – 1.5× | Experimental — flag-gated |
| < 1.2× | Disconfirm + publish null — ship LRU as default |

v0.2.0 ships W7-W8 (mid-June). 4-week runway → no panic on the M4 slip.
