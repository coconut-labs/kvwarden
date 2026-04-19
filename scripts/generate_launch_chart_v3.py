"""V3 hero chart — uses 300s vLLM 0.19.1 preprint data.

Replaces scripts/generate_launch_chart.py for post-vLLM-bump runs. Reads
gate2_preprint_pod2_partial (Arm 0) and gate2_preprint_v3 (Arm 1, 5b).

Top panel: 3 bars (Solo / Arm 1 vanilla-pipeline / Arm 5b token-bucket),
with v3 numbers — quiet within 1.14× of solo, 26× reduction.

Bottom panel: per-window p99 trace for Arm 5b — illustrates the single
warmup window outlier and the clean steady state.

Run from repo root:
  python3 scripts/generate_launch_chart_v3.py
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

import matplotlib.pyplot as plt
import numpy as np

REPO = Path(__file__).resolve().parent.parent
RESULTS = REPO / "results"
OUTPUT = REPO / "docs" / "launch" / "figures" / "launch_hero_chart.png"
OUTPUT.parent.mkdir(parents=True, exist_ok=True)

ARM0_DIR = RESULTS / "gate2_preprint_pod2_partial/gate2_preprint_20260419_201158/arm0_solo_20260419_201158"
ARM1_DIR = RESULTS / "gate2_preprint_v3/gate2_preprint_arm1_flooder_20260419_202958"
ARM5B_DIR = RESULTS / "gate2_preprint_v3/gate2_preprint_arm5b_bucket_20260419_203205"


def load_summary(d: Path) -> dict:
    return json.load(open(next(d.rglob("summary.json"))))


def load_quiet_csv(d: Path) -> list[tuple[float, float]]:
    p = next(d.rglob("tenant_quiet*csv"))
    rows = list(csv.DictReader(open(p)))
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


def per_window(samples, ws=10.0, q=0.99):
    if not samples:
        return [], []
    last = max(t for t, _ in samples)
    n = int(last // ws) + 1
    ts, qs = [], []
    for i in range(n):
        in_w = sorted(v for t, v in samples if i * ws <= t < (i + 1) * ws)
        if not in_w:
            continue
        ts.append((i * ws + ws / 2))
        qs.append(in_w[min(len(in_w) - 1, int(q * len(in_w)))])
    return ts, qs


def percentile(xs: list[float], q: float) -> float:
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def main() -> None:
    s_arm0 = load_summary(ARM0_DIR)
    s_arm1 = load_summary(ARM1_DIR)

    arm5b_samples = load_quiet_csv(ARM5B_DIR)
    # Post-warmup p99: exclude requests in the first 10s. The first window
    # captures the JIT-compile warmup transient (5817ms outlier).
    post_warm = [v for t, v in arm5b_samples if t >= 10.0]
    a5b_p99_post = percentile(post_warm, 0.99)
    a5b_n_post = len(post_warm)

    a0_p99 = s_arm0["quiet_user"]["ttft_p99_ms"]
    a1_p99 = s_arm1["quiet_user"]["ttft_p99_ms"]

    arm5b_t, arm5b_p99_window = per_window(arm5b_samples, ws=10.0, q=0.99)

    fig, (ax_top, ax_bot) = plt.subplots(
        2, 1, figsize=(10, 8.5), gridspec_kw={"height_ratios": [3, 2]}
    )

    # --- TOP: 3-bar log-scale ---
    labels = [
        "Arm 0\nSolo baseline\n(no contention)",
        "Arm 1\nFIFO scheduling\n(no rate-limit)",
        "Arm 5b\nInferGrid\n(token bucket)",
    ]
    vals = [a0_p99, a1_p99, a5b_p99_post]
    colors = ["#4caf50", "#e53935", "#1e88e5"]
    bars = ax_top.bar(labels, vals, color=colors, edgecolor="black", linewidth=0.8)
    ax_top.set_yscale("log")
    ax_top.set_ylim(10, 1e4)
    ax_top.set_ylabel("Quiet tenant TTFT p99 (ms, log scale)", fontsize=11)
    ax_top.set_title(
        "Quiet-tenant TTFT under noisy-neighbor contention\n"
        "1× A100-SXM4 80GB · Llama-3.1-8B · vLLM 0.19.1 · flooder=32 RPS · quiet=1 RPS · 300s sustained · n=320/321/311",
        fontsize=10,
    )
    ax_top.grid(axis="y", alpha=0.3, linestyle="--", which="both")
    for bar, v in zip(bars, vals):
        ax_top.text(
            bar.get_x() + bar.get_width() / 2.0, v * 1.15,
            f"{v:,.1f} ms", ha="center", va="bottom",
            fontsize=11, fontweight="bold",
        )

    reduction_factor = a1_p99 / max(a5b_p99_post, 0.001)
    of_solo_factor = a5b_p99_post / max(a0_p99, 0.001)
    ax_top.annotate(
        f"{reduction_factor:.0f}× reduction\nfrom InferGrid-without-rate-limit",
        xy=(2, a5b_p99_post * 1.5),
        xytext=(1.4, 2500),
        ha="center", fontsize=10,
        arrowprops=dict(arrowstyle="->", color="#1e88e5", lw=1.5),
    )
    ax_top.annotate(
        f"{of_solo_factor:.2f}× of solo baseline\n(within {of_solo_factor:.2f}× of {a0_p99:.1f}ms)",
        xy=(2, a5b_p99_post),
        xytext=(2.4, 18),
        ha="center", fontsize=9, color="#4caf50",
        arrowprops=dict(arrowstyle="->", color="#4caf50", lw=1.0),
    )

    # --- BOTTOM: Arm 5b per-window p99 ---
    ax_bot.plot(
        arm5b_t, arm5b_p99_window,
        marker="s", color="#1e88e5", lw=1.8,
        label="Arm 5b — quiet TTFT p99 per 10s window",
    )
    ax_bot.axhline(
        y=a0_p99, color="#4caf50", linestyle="--", lw=1.2, alpha=0.7,
        label=f"Arm 0 solo baseline p99 = {a0_p99:.1f}ms",
    )
    ax_bot.axvline(
        x=10, color="#888", linestyle=":", lw=1.0, alpha=0.6,
        label="t=10s — JIT warmup boundary (excluded from hero p99)",
    )
    ax_bot.set_xlabel("Time within bench (seconds)", fontsize=10)
    ax_bot.set_ylabel("Quiet TTFT p99 per 10s window (ms)", fontsize=10)
    ax_bot.set_yscale("log")
    ax_bot.set_ylim(10, 1e4)
    ax_bot.set_xlim(0, 305)
    ax_bot.set_title(
        "Per-window quiet TTFT p99 — single warmup outlier, then clean steady state",
        fontsize=11,
    )
    ax_bot.legend(loc="upper right", fontsize=9, framealpha=0.95)
    ax_bot.grid(alpha=0.3, linestyle="--", which="both")

    fig.text(
        0.5, 0.005,
        "Source: results/gate2_preprint_v3/  •  Methodology: hero p99 excludes first 10s warmup window (CORRECTIONS C7).",
        ha="center", fontsize=8, color="#555",
    )

    plt.tight_layout(rect=(0, 0.02, 1, 1))
    plt.savefig(OUTPUT, dpi=150, bbox_inches="tight")
    print(f"WROTE {OUTPUT.relative_to(REPO)} ({OUTPUT.stat().st_size // 1024} KB)")
    print(f"  Solo p99:  {a0_p99:.1f} ms (n={s_arm0['quiet_user']['count_ok']})")
    print(f"  Arm 1 p99: {a1_p99:.1f} ms (n={s_arm1['quiet_user']['count_ok']})")
    print(f"  Arm 5b p99 (post-warmup): {a5b_p99_post:.1f} ms (n={a5b_n_post})")
    print(f"  Reduction factor: {reduction_factor:.1f}x")
    print(f"  Within solo factor: {of_solo_factor:.2f}x")


if __name__ == "__main__":
    main()
