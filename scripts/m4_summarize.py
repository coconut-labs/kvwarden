#!/usr/bin/env python3
"""
T2 M4 Path C summarizer. Consumes per-cell bundles under
results/m4_path_c_probe_20260502/m4_arm{1,2}_seed{0,1,2}/ and emits
post_warmup_summary.json + a markdown table the OUTCOME draws from.

Per cell, the bench harness drops `summary.json` with
`quiet_per_tenant.quiet_N.ttft_p99_ms`. The bootstrap also drops
`gauge_trace.csv` (epoch_s,gauge) at 250 ms cadence, captured on
localhost:8001/metrics. We compute gauge p50/p95/p99 per cell from
gauge_trace.csv and per-quiet-tenant TTFT p99 medians per cell from
summary.json. Aggregate is median across 7 quiets, then median across
3 seeds, per arm.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent / "results" / "m4_path_c_probe_20260502"


def percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    lo, hi = math.floor(k), math.ceil(k)
    if lo == hi:
        return s[int(k)]
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def parse_gauge_trace(path: Path) -> dict[str, Any]:
    """Return {p50, p95, p99, count, max, min, na_count} from gauge_trace.csv."""
    if not path.exists():
        return {"p50": None, "p95": None, "p99": None, "count": 0, "na_count": 0,
                "min": None, "max": None}
    vals: list[float] = []
    na = 0
    try:
        with open(path) as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                v = (row.get("gauge") or "").strip()
                if v in ("", "NA"):
                    na += 1
                    continue
                try:
                    vals.append(float(v))
                except ValueError:
                    na += 1
    except Exception as exc:
        return {"error": str(exc), "count": 0, "na_count": 0}
    if not vals:
        return {"p50": None, "p95": None, "p99": None, "count": 0,
                "na_count": na, "min": None, "max": None}
    return {
        "p50": round(percentile(vals, 0.50), 4),
        "p95": round(percentile(vals, 0.95), 4),
        "p99": round(percentile(vals, 0.99), 4),
        "count": len(vals),
        "na_count": na,
        "min": round(min(vals), 4),
        "max": round(max(vals), 4),
    }


def parse_summary(path: Path) -> dict[str, Any]:
    """Pull per-tenant TTFT p99/p50, count_ok/err from summary.json."""
    if not path.exists():
        return {"present": False}
    try:
        with open(path) as fh:
            data = json.load(fh)
    except Exception as exc:
        return {"present": True, "parse_error": str(exc)}
    quiet_per = data.get("quiet_per_tenant", {})
    quiet_p99 = []
    quiet_p50 = []
    quiet_count_ok = 0
    quiet_count_err = 0
    for name, t in quiet_per.items():
        p99 = t.get("ttft_p99_ms")
        p50 = t.get("ttft_p50_ms")
        if p99 is not None and p99 >= 0:
            quiet_p99.append(p99)
        if p50 is not None and p50 >= 0:
            quiet_p50.append(p50)
        quiet_count_ok += t.get("count_ok", 0)
        quiet_count_err += t.get("count_err", 0)
    flooder = data.get("flooder", {})
    return {
        "present": True,
        "quiet_p99_per_tenant": quiet_p99,
        "quiet_p50_per_tenant": quiet_p50,
        "quiet_p99_median_across_7": (
            round(statistics.median(quiet_p99), 1) if quiet_p99 else None
        ),
        "quiet_p99_max_across_7": max(quiet_p99) if quiet_p99 else None,
        "quiet_p99_min_across_7": min(quiet_p99) if quiet_p99 else None,
        "quiet_p50_median_across_7": (
            round(statistics.median(quiet_p50), 1) if quiet_p50 else None
        ),
        "quiet_count_ok_total": quiet_count_ok,
        "quiet_count_err_total": quiet_count_err,
        "flooder_count_ok": flooder.get("count_ok", 0),
        "flooder_count_err": flooder.get("count_err", 0),
        "flooder_ttft_p99_ms": flooder.get("ttft_p99_ms"),
        "wall_time_s": data.get("wall_time_s") or data.get("duration_s"),
    }


def find_cell_dir(arm: str, seed: int) -> Path:
    return ROOT / f"m4_{arm}_seed{seed}"


def find_summary_json(cell_dir: Path) -> Path | None:
    """The bench writes summary.json under benchmarks/.../ inside the
    bootstrap RDIR. The orchestrator untars the tarball into cell_dir;
    look at any depth."""
    for p in cell_dir.rglob("summary.json"):
        return p
    return None


def find_gauge_trace(cell_dir: Path) -> Path | None:
    for p in cell_dir.rglob("gauge_trace.csv"):
        return p
    return None


def main() -> int:
    cells_summary = []
    arms = ["arm1", "arm2"]
    seeds = [0, 1, 2]

    for arm in arms:
        for seed in seeds:
            cell_dir = find_cell_dir(arm, seed)
            sjson = find_summary_json(cell_dir)
            gtrace = find_gauge_trace(cell_dir)
            cell = {
                "arm": arm,
                "seed": seed,
                "cell_dir": str(cell_dir.relative_to(ROOT.parent.parent)),
                "summary_json_path": str(sjson) if sjson else None,
                "gauge_trace_path": str(gtrace) if gtrace else None,
                "gauge": parse_gauge_trace(gtrace) if gtrace else {},
                "bench": parse_summary(sjson) if sjson else {"present": False},
            }
            cells_summary.append(cell)

    # Aggregate per arm: median across seeds of the per-cell quiet_p99_median.
    per_arm = {}
    for arm in arms:
        seeds_p99 = [
            c["bench"].get("quiet_p99_median_across_7")
            for c in cells_summary
            if c["arm"] == arm and c["bench"].get("quiet_p99_median_across_7") is not None
        ]
        seeds_p50 = [
            c["bench"].get("quiet_p50_median_across_7")
            for c in cells_summary
            if c["arm"] == arm and c["bench"].get("quiet_p50_median_across_7") is not None
        ]
        gauge_p99s = [
            c["gauge"].get("p99")
            for c in cells_summary
            if c["arm"] == arm and c["gauge"].get("p99") is not None
        ]
        gauge_p95s = [
            c["gauge"].get("p95")
            for c in cells_summary
            if c["arm"] == arm and c["gauge"].get("p95") is not None
        ]
        gauge_p50s = [
            c["gauge"].get("p50")
            for c in cells_summary
            if c["arm"] == arm and c["gauge"].get("p50") is not None
        ]
        flood_err = sum(
            c["bench"].get("flooder_count_err", 0) or 0
            for c in cells_summary if c["arm"] == arm
        )
        quiet_err = sum(
            c["bench"].get("quiet_count_err_total", 0) or 0
            for c in cells_summary if c["arm"] == arm
        )
        per_arm[arm] = {
            "n_cells": sum(1 for c in cells_summary if c["arm"] == arm),
            "quiet_p99_med_across_seeds": (
                round(statistics.median(seeds_p99), 1) if seeds_p99 else None
            ),
            "quiet_p50_med_across_seeds": (
                round(statistics.median(seeds_p50), 1) if seeds_p50 else None
            ),
            "quiet_p99_per_seed": seeds_p99,
            "quiet_p50_per_seed": seeds_p50,
            "gauge_p50_med": (
                round(statistics.median(gauge_p50s), 4) if gauge_p50s else None
            ),
            "gauge_p95_med": (
                round(statistics.median(gauge_p95s), 4) if gauge_p95s else None
            ),
            "gauge_p99_med": (
                round(statistics.median(gauge_p99s), 4) if gauge_p99s else None
            ),
            "gauge_p99_max_across_seeds": (
                round(max(gauge_p99s), 4) if gauge_p99s else None
            ),
            "flooder_count_err_total": flood_err,
            "quiet_count_err_total": quiet_err,
        }

    # Compute the headline ratio.
    a1 = per_arm.get("arm1", {}).get("quiet_p99_med_across_seeds")
    a2 = per_arm.get("arm2", {}).get("quiet_p99_med_across_seeds")
    if a1 and a2 and a1 > 0:
        ratio = round(a1 / a2, 3)
    else:
        ratio = None

    # Kill criterion check: any arm with gauge p99 >= 0.7?
    pressured_arms = [
        arm for arm, p in per_arm.items()
        if (p.get("gauge_p99_max_across_seeds") or 0) >= 0.7
    ]
    regime_pressured = bool(pressured_arms)

    # Verdict.
    has_any_gauge = any(
        per_arm[arm].get("gauge_p99_max_across_seeds") is not None for arm in arms
    )
    has_any_bench = any(
        per_arm[arm].get("quiet_p99_med_across_seeds") is not None for arm in arms
    )
    if not has_any_gauge and not has_any_bench:
        verdict = "incomplete"
        verdict_rationale = (
            "No bench data and no gauge trace recovered. Cells did not complete; "
            "see cell_results.json for status per cell."
        )
    elif not regime_pressured:
        verdict = "regime-broken"
        verdict_rationale = (
            "Gauge p99 < 0.7 across all arms — workload doesn't pressure cache; "
            "no cache-pressure-conditioned admission policy can help. Ship (c) "
            "disconfirm; queue (b) LMCache for v0.3."
        )
    elif ratio is None:
        verdict = "incomplete"
        verdict_rationale = "Insufficient cells to compute Arm 2 vs Arm 1 ratio."
    elif a2 > a1:
        verdict = "abandon-(a)"
        verdict_rationale = (
            f"Arm 2 quiet p99 ({a2} ms) is worse than Arm 1 ({a1} ms). "
            f"Saturation bias adds latency without signal. Abandon (a); "
            f"ship (c); queue (b) LMCache for v0.3."
        )
    elif ratio >= 1.5:
        verdict = "green"
        verdict_rationale = (
            f"Arm 1/Arm 2 quiet p99 ratio = {ratio}× ≥ 1.5×. (a) GA-track, "
            f"default-on. M5a build is funded."
        )
    elif ratio >= 1.2:
        verdict = "yellow"
        verdict_rationale = (
            f"Arm 1/Arm 2 quiet p99 ratio = {ratio}× in [1.2×, 1.5×). "
            f"(a) experimental, flag-gated. M5a ships behind a config flag."
        )
    else:
        verdict = "red"
        verdict_rationale = (
            f"Arm 1/Arm 2 quiet p99 ratio = {ratio}× < 1.2×. DRR-only already "
            f"captures the gap. Ship (c) disconfirm; queue (b) LMCache for v0.3."
        )

    output = {
        "cells": cells_summary,
        "per_arm": per_arm,
        "headline": {
            "arm1_quiet_p99_med_ms": a1,
            "arm2_quiet_p99_med_ms": a2,
            "ratio_arm1_over_arm2": ratio,
            "regime_pressured": regime_pressured,
            "pressured_arms": pressured_arms,
            "verdict": verdict,
            "rationale": verdict_rationale,
        },
    }

    out_path = ROOT / "post_warmup_summary.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(output, fh, indent=2)
    print(f"wrote {out_path}")
    print(json.dumps(output["headline"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
