"""Generate the launch-post hero chart from the Gate 2-FAIRNESS data.

Two-panel figure:
  Top: 3-bar log-scale comparison (Arm 0 / Arm 1 / Arm 5b) of quiet
       tenant TTFT p99. Annotations: 387× reduction, 1.35× of solo.
  Bottom: per-window TTFT trace (Arm 5 sliding-window vs Arm 5b token
          bucket) showing the warmup transient elimination.

Output: docs/launch/figures/launch_hero_chart.png (referenced by the
launch post v2).

Run from repo root:
  python scripts/generate_launch_chart.py
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

REPO = Path(__file__).resolve().parent.parent
RESULTS = REPO / "results" / "gate2_fairness_20260419"
OUTPUT = REPO / "docs" / "launch" / "figures" / "launch_hero_chart.png"
OUTPUT.parent.mkdir(parents=True, exist_ok=True)


def load_summary(arm_dir_name: str) -> dict:
    base = RESULTS / arm_dir_name
    summary = next(base.rglob("summary.json"))
    return json.load(open(summary))


def load_quiet_csv(arm_dir_name: str) -> list[tuple[float, float]]:
    """Returns [(submit_time_relative_s, ttft_ms), ...] for the quiet tenant,
    filtered to successful requests only."""
    base = RESULTS / arm_dir_name
    csv_path = next(base.rglob("tenant_quiet_user.csv"))
    rows = list(csv.DictReader(open(csv_path)))
    rows.sort(key=lambda r: float(r["submit_time"]))
    if not rows:
        return []
    t0 = float(rows[0]["submit_time"])
    out = []
    for r in rows:
        if r.get("error"):
            continue
        ttft = float(r["ttft_ms"])
        if ttft <= 0:
            continue
        out.append((float(r["submit_time"]) - t0, ttft))
    return out


def per_window_p50(samples: list[tuple[float, float]], window_s: float = 10.0) -> tuple[list[float], list[float]]:
    """Bucket samples into windows and return (window_centers_s, p50_per_window_ms)."""
    if not samples:
        return [], []
    last_t = max(t for t, _ in samples)
    n_windows = int(last_t // window_s) + 1
    centers, p50s = [], []
    for i in range(n_windows):
        lo, hi = i * window_s, (i + 1) * window_s
        in_window = sorted(v for t, v in samples if lo <= t < hi)
        if not in_window:
            continue
        centers.append((lo + hi) / 2.0)
        p50s.append(in_window[len(in_window) // 2])
    return centers, p50s


def main() -> None:
    s_arm0 = load_summary("gate2f_arm0_20260419_145845")
    s_arm1 = load_summary("gate2f_arm1_20260419_151008")
    s_arm5b = load_summary("gate2f_arm5b_20260419_163431")

    a0_p99 = s_arm0["quiet_user"]["ttft_p99_ms"]
    a1_p99 = s_arm1["quiet_user"]["ttft_p99_ms"]
    a5b_p99 = s_arm5b["quiet_user"]["ttft_p99_ms"]

    arm5_samples = load_quiet_csv("gate2f_arm5_20260419_154514")
    arm5b_samples = load_quiet_csv("gate2f_arm5b_20260419_163431")

    arm5_t, arm5_p50 = per_window_p50(arm5_samples)
    arm5b_t, arm5b_p50 = per_window_p50(arm5b_samples)

    # Plot
    fig, (ax_top, ax_bot) = plt.subplots(
        2, 1, figsize=(10, 8), gridspec_kw={"height_ratios": [3, 2]}
    )

    # --- TOP: 3-bar log-scale ---
    labels = [
        "Arm 0\nSolo baseline\n(no contention)",
        "Arm 1\nVanilla vLLM\n(under flooder)",
        "Arm 5b\nInferGrid\n(token bucket)",
    ]
    vals = [a0_p99, a1_p99, a5b_p99]
    colors = ["#4caf50", "#e53935", "#1e88e5"]
    bars = ax_top.bar(labels, vals, color=colors, edgecolor="black", linewidth=0.8)
    ax_top.set_yscale("log")
    ax_top.set_ylim(10, 1e5)
    ax_top.set_ylabel("Quiet tenant TTFT p99 (ms, log scale)", fontsize=11)
    ax_top.set_title(
        "Quiet-tenant TTFT under noisy-neighbor contention\n"
        "(1× A100, Llama-3.1-8B, flooder=32 RPS, quiet=1 RPS, 120s sustained)",
        fontsize=11,
    )
    ax_top.grid(axis="y", alpha=0.3, linestyle="--", which="both")

    # Value labels on bars
    for bar, v in zip(bars, vals):
        ax_top.text(
            bar.get_x() + bar.get_width() / 2.0,
            v * 1.15,
            f"{v:,.1f} ms",
            ha="center",
            va="bottom",
            fontsize=11,
            fontweight="bold",
        )

    # Annotations
    ax_top.annotate(
        "387× reduction\nin p99 starvation",
        xy=(2, a5b_p99 * 1.5),
        xytext=(1.5, 8000),
        ha="center",
        fontsize=10,
        arrowprops=dict(arrowstyle="->", color="#1e88e5", lw=1.5),
    )
    ax_top.annotate(
        f"1.35× of solo\nbaseline ({a0_p99:.1f}ms)",
        xy=(2, a5b_p99),
        xytext=(2.4, 25),
        ha="center",
        fontsize=9,
        color="#4caf50",
        arrowprops=dict(arrowstyle="->", color="#4caf50", lw=1.0),
    )

    # --- BOTTOM: per-window trace ---
    ax_bot.plot(
        arm5_t, arm5_p50,
        marker="o", color="#e53935", lw=1.8,
        label="Arm 5 — sliding-window rate limit (5 transient windows)",
    )
    ax_bot.plot(
        arm5b_t, arm5b_p50,
        marker="s", color="#1e88e5", lw=1.8,
        label="Arm 5b — token-bucket rate limit (zero transients)",
    )
    ax_bot.axhline(
        y=a0_p99, color="#4caf50", linestyle="--", lw=1.2, alpha=0.7,
        label=f"Arm 0 solo baseline p99 = {a0_p99:.1f}ms",
    )
    ax_bot.set_xlabel("Time within bench (seconds)", fontsize=10)
    ax_bot.set_ylabel("Quiet TTFT p50 per 10s window (ms)", fontsize=10)
    ax_bot.set_yscale("log")
    ax_bot.set_ylim(10, 1e4)
    ax_bot.set_xlim(0, 125)
    ax_bot.set_title(
        "Per-window quiet TTFT — Arm 5 (sliding window) vs Arm 5b (token bucket)",
        fontsize=11,
    )
    ax_bot.legend(loc="upper right", fontsize=9, framealpha=0.95)
    ax_bot.grid(alpha=0.3, linestyle="--", which="both")

    fig.text(
        0.5, 0.005,
        "Source: results/gate2_fairness_20260419/  •  Sample sizes: n=113 quiet/contended arm, n=262 solo baseline.",
        ha="center", fontsize=8, color="#555",
    )

    plt.tight_layout(rect=(0, 0.02, 1, 1))
    plt.savefig(OUTPUT, dpi=150, bbox_inches="tight")
    print(f"WROTE {OUTPUT.relative_to(REPO)} ({OUTPUT.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main()
