#!/usr/bin/env python3
"""Summarize h100_adversarial_sweep_20260421 run using post-10s-warmup CSVs.

Each cell has tenant_*.csv. We exclude the first 10s from submit_time (so
the transient where all Poisson loops launch simultaneously doesn't poison
the tail).

Emits JSON keyed by phase then cell name. Each cell has:
  flooder: {n, p50, p95, p99, max}
  quiet_aggregate: {n, p50, p95, p99, max}          # concat across quiets
  quiet_per_tenant: {quiet_i: {n, p50, p95, p99, max}}
  worst_quiet_p99_ms
  num_quiet
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.cwd()
WARMUP_S = 10.0


def percentiles(ttfts: list[float]) -> dict:
    ttfts = sorted(ttfts)
    n = len(ttfts)
    if n == 0:
        return {"n": 0, "p50_ms": -1, "p95_ms": -1, "p99_ms": -1, "max_ms": -1}

    def p(q):
        idx = min(n - 1, max(0, int(q * n)))
        return ttfts[idx]

    return {
        "n": n,
        "p50_ms": round(p(0.50), 1),
        "p95_ms": round(p(0.95), 1),
        "p99_ms": round(p(0.99), 1),
        "max_ms": round(ttfts[-1], 1),
    }


def load_cell(cell_dir: Path) -> dict | None:
    flooder = cell_dir / "tenant_flooder.csv"
    if not flooder.exists():
        return None
    # find global earliest submit across all tenants
    t0 = None
    tenant_rows: dict[str, list[dict]] = {}
    for csv_f in cell_dir.glob("tenant_*.csv"):
        tenant = csv_f.stem.replace("tenant_", "")
        with open(csv_f) as fh:
            rows = [r for r in csv.DictReader(fh)]
        tenant_rows[tenant] = rows
        for r in rows:
            t = float(r.get("submit_time", 0) or 0)
            if t > 0 and (t0 is None or t < t0):
                t0 = t
    if t0 is None:
        return None

    flooder_ttfts: list[float] = []
    flooder_errs = 0
    quiet_all: list[float] = []
    quiet_per_tenant: dict = {}
    num_quiet = 0
    for tenant, rows in sorted(tenant_rows.items()):
        ok_ttfts = []
        err_n = 0
        for r in rows:
            if r.get("error"):
                err_n += 1
                continue
            ttft = float(r.get("ttft_ms", 0) or 0)
            if ttft <= 0:
                continue
            submit = float(r.get("submit_time", 0) or 0)
            if submit - t0 < WARMUP_S:
                continue
            ok_ttfts.append(ttft)
        if tenant == "flooder":
            flooder_ttfts = ok_ttfts
            flooder_errs = err_n
        else:
            num_quiet += 1
            quiet_per_tenant[tenant] = {**percentiles(ok_ttfts), "errs": err_n}
            quiet_all.extend(ok_ttfts)

    quiet_p99s = [s["p99_ms"] for s in quiet_per_tenant.values() if s["p99_ms"] > 0]
    return {
        "flooder": {**percentiles(flooder_ttfts), "errs": flooder_errs},
        "quiet_aggregate": percentiles(quiet_all),
        "quiet_per_tenant": quiet_per_tenant,
        "worst_quiet_p99_ms": round(max(quiet_p99s), 1) if quiet_p99s else None,
        "num_quiet": num_quiet,
    }


def collect(section: str, root: Path) -> dict:
    out: dict = {}
    sec = root / section
    if not sec.is_dir():
        return out
    for sub in sorted(sec.iterdir()):
        if sub.is_dir():
            cell = load_cell(sub)
            if cell:
                out[sub.name] = cell
    return out


def main() -> None:
    summary = {
        "overhead": collect("overhead", ROOT),
        "rps_sweep": collect("rps_sweep", ROOT),
        "n_sweep": collect("n_sweep", ROOT),
        "fifo_anchor": collect("fifo_anchor", ROOT),
    }
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
