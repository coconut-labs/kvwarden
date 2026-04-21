"""Gate 2.2 metrics: per-bucket + aggregate quiet/flooder p50/p99.

Applies two filters relative to the raw harness summary.json:
  1. tokens_out > 0 (drops 8192-bucket phantom-200s that vLLM emits
     when prompt_tokens + max_tokens > max_model_len=4096)
  2. submit_time >= bench_start + 10s (warmup discard)

Emits metrics.json per arm-directory. Prints a compact report.
"""
from __future__ import annotations

import csv
import json
import math
import sys
from pathlib import Path
from statistics import median


def percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return -1.0
    n = len(sorted_vals)
    idx = min(n - 1, max(0, int(q * n)))
    return sorted_vals[idx]


def load_csv(path: Path) -> list[dict]:
    with open(path) as f:
        return list(csv.DictReader(f))


def filter_rows(rows: list[dict], bench_start: float, warmup_s: float = 10.0) -> list[dict]:
    """Keep OK rows (no error, real tokens out) post-warmup."""
    out = []
    for r in rows:
        if r["error"]:
            continue
        try:
            tokens = int(r["tokens_out"])
        except ValueError:
            continue
        if tokens <= 0:
            continue
        try:
            submit = float(r["submit_time"])
        except ValueError:
            continue
        if submit < bench_start + warmup_s:
            continue
        out.append(r)
    return out


def bench_start_from_rows(rows: list[dict]) -> float:
    """Earliest submit_time across rows (harness starts all tenants simultaneously)."""
    mins = []
    for r in rows:
        try:
            mins.append(float(r["submit_time"]))
        except ValueError:
            continue
    return min(mins) if mins else 0.0


def per_bucket(rows: list[dict], bucket: int) -> dict:
    matching = [r for r in rows if int(r["prompt_tokens"]) == bucket]
    ttfts = sorted(float(r["ttft_ms"]) for r in matching)
    if not ttfts:
        return {"n": 0, "p50": None, "p99": None}
    return {
        "n": len(ttfts),
        "p50": round(percentile(ttfts, 0.50), 1),
        "p99": round(percentile(ttfts, 0.99), 1),
    }


def error_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r["error"]]


def count_429(rows: list[dict]) -> int:
    return sum(1 for r in rows if "429" in r["error"])


def agg_ttft(rows: list[dict]) -> dict:
    ttfts = sorted(float(r["ttft_ms"]) for r in rows)
    if not ttfts:
        return {"n": 0, "p50": None, "p99": None, "max": None}
    return {
        "n": len(ttfts),
        "p50": round(percentile(ttfts, 0.50), 1),
        "p99": round(percentile(ttfts, 0.99), 1),
        "max": round(ttfts[-1], 1),
    }


def process_arm(dir_path: Path, has_quiets: bool) -> dict:
    flooder_raw = load_csv(dir_path / "tenant_flooder.csv")
    flooder_all = flooder_raw  # includes errors; needed for 429 count
    quiet_raws: list[list[dict]] = []
    if has_quiets:
        for i in range(7):
            q_path = dir_path / f"tenant_quiet_{i}.csv"
            if q_path.exists():
                quiet_raws.append(load_csv(q_path))

    # Bench start = earliest submit across any tenant (they all start together)
    all_submits = []
    for r in flooder_all:
        try:
            all_submits.append(float(r["submit_time"]))
        except ValueError:
            continue
    for q in quiet_raws:
        for r in q:
            try:
                all_submits.append(float(r["submit_time"]))
            except ValueError:
                continue
    bench_start = min(all_submits) if all_submits else 0.0

    flooder_ok = filter_rows(flooder_all, bench_start)
    quiet_oks = [filter_rows(q, bench_start) for q in quiet_raws]
    quiet_all = [r for sub in quiet_oks for r in sub]

    buckets = [64, 512, 2048, 8192]
    flooder_buckets = {str(b): per_bucket(flooder_ok, b) for b in buckets}
    quiet_buckets = {str(b): per_bucket(quiet_all, b) for b in buckets}

    # Flooder 429 rate = 429s / total-attempted (not pre-warmup; use whole run
    # because the rate-limit layer doesn't care about warmup)
    flooder_429 = count_429(flooder_all)
    flooder_total = len(flooder_all)
    flooder_429_rate = flooder_429 / flooder_total if flooder_total else 0.0

    return {
        "bench_start_epoch": bench_start,
        "flooder": {
            "aggregate": agg_ttft(flooder_ok),
            "per_bucket": flooder_buckets,
            "count_429": flooder_429,
            "count_total_attempted": flooder_total,
            "rate_429": round(flooder_429_rate, 4),
        },
        "quiet_aggregate": agg_ttft(quiet_all),
        "quiet_per_bucket": quiet_buckets,
        "quiet_per_tenant": [agg_ttft(q) for q in quiet_oks],
    }


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: compute_gate22_metrics.py <results_parent_dir>")
        return 1
    parent = Path(sys.argv[1])
    arm0 = parent / "gate22_solo"
    arm1 = parent / "gate22_fifo"
    arm5 = parent / "gate22_tokenbucket"

    out = {
        "arm0_solo": process_arm(arm0, has_quiets=False),
        "arm1_fifo": process_arm(arm1, has_quiets=True),
        "arm5_tokenbucket": process_arm(arm5, has_quiets=True),
    }

    # Write per-arm + combined
    (parent / "gate22_metrics.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
