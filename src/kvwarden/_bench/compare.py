"""Compare user-measured hero numbers to the published reference.

Reference numbers come from the Gate 2-FAIRNESS preprint v3 re-run on
vLLM 0.19.1 / A100-SXM4 / Llama-3.1-8B. Do NOT re-measure at CLI
runtime — a user running the 300 s bench already has their own numbers;
this module is only the comparison side.

Source of truth: ``docs/launch/gate0_launch_post.md`` and the per-flavor
OUTCOME files under ``results/``.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.console import Console
from rich.table import Table


@dataclass(frozen=True)
class Reference:
    """Published reference numbers for one flavor.

    ``solo_p99_ms`` is the no-contention baseline (Arm 0). ``fifo_p99_ms``
    is the vanilla vLLM FIFO under flooder (Arm 1); present for the
    2-tenant and n8 flavors where we ran the disconfirm arm, ``None`` for
    n6 where we only measured the rate-limit arm.
    ``tokenbucket_p99_ms`` is the hero number (Arm 5b, post-warmup).
    ``ratio_of_solo`` is ``tokenbucket_p99_ms / solo_p99_ms`` —
    pre-computed so the user sees the same fold-change the launch post
    quotes.
    """

    flavor: str
    num_quiet: int
    solo_p99_ms: float
    fifo_p99_ms: float | None
    tokenbucket_p99_ms: float
    ratio_of_solo: float
    source: str


# Pinned from docs/launch/gate0_launch_post.md + per-flavor OUTCOMEs.
# Updating these requires re-running the corresponding gate.
REFERENCES: dict[str, Reference] = {
    "2tenant": Reference(
        flavor="2tenant",
        num_quiet=1,
        solo_p99_ms=53.9,
        fifo_p99_ms=1585.0,
        tokenbucket_p99_ms=61.5,
        ratio_of_solo=1.14,
        source="results/gate2_preprint_v3/",
    ),
    "n6": Reference(
        flavor="n6",
        num_quiet=5,
        solo_p99_ms=53.9,
        fifo_p99_ms=None,
        tokenbucket_p99_ms=61.0,
        ratio_of_solo=1.13,
        source="results/gate2_n6_v3/",
    ),
    "n8": Reference(
        flavor="n8",
        num_quiet=7,
        solo_p99_ms=48.1,
        fifo_p99_ms=59.0,
        tokenbucket_p99_ms=50.4,
        ratio_of_solo=1.05,
        source="results/gate21_n8_20260421/",
    ),
}


def _fmt_ms(v: float | None) -> str:
    if v is None:
        return "—"
    if v >= 1000:
        return f"{v:,.0f} ms"
    return f"{v:.1f} ms"


def _delta_badge(user: float, reference: float) -> str:
    """Return a rich-markup badge describing how user's result compares.

    Within 15% of reference is green; within 50% is yellow; anything
    beyond is red and worth investigating.
    """
    if reference <= 0:
        return "[dim]n/a[/dim]"
    pct = (user - reference) / reference * 100
    sign = "+" if pct >= 0 else "−"
    mag = abs(pct)
    if mag <= 15:
        color = "green"
    elif mag <= 50:
        color = "yellow"
    else:
        color = "red"
    return f"[{color}]{sign}{mag:.0f}%[/{color}]"


def render_comparison(
    flavor: str,
    user_quiet_p99_ms: float,
    user_flooder_429_rate: float,
    user_solo_p99_ms: float | None,
    console: Console,
) -> None:
    """Print a side-by-side table of user-vs-published numbers.

    ``user_solo_p99_ms`` is optional — the reproduce-hero CLI does not
    run a separate solo arm by default, so we skip that row when it's
    absent rather than fabricate it.
    """
    ref = REFERENCES[flavor]

    table = Table(
        title=f"reproduce-hero · flavor={flavor} (num_quiet={ref.num_quiet})",
        header_style="bold",
        title_justify="left",
    )
    table.add_column("Metric", style="cyan")
    table.add_column("Your box", justify="right")
    table.add_column("Published", justify="right", style="dim")
    table.add_column("Δ", justify="right")

    table.add_row(
        "Quiet p99 TTFT (token-bucket arm)",
        _fmt_ms(user_quiet_p99_ms),
        _fmt_ms(ref.tokenbucket_p99_ms),
        _delta_badge(user_quiet_p99_ms, ref.tokenbucket_p99_ms),
    )
    if ref.fifo_p99_ms is not None:
        table.add_row(
            "FIFO p99 (reference)",
            "[dim]—[/dim]",
            _fmt_ms(ref.fifo_p99_ms),
            "[dim](not measured at runtime)[/dim]",
        )
    if user_solo_p99_ms is not None:
        table.add_row(
            "Solo p99 (baseline)",
            _fmt_ms(user_solo_p99_ms),
            _fmt_ms(ref.solo_p99_ms),
            _delta_badge(user_solo_p99_ms, ref.solo_p99_ms),
        )
    table.add_row(
        "Flooder 429 rate",
        f"{user_flooder_429_rate * 100:.1f}%",
        "[dim]>90%[/dim]",
        "[green]ok[/green]" if user_flooder_429_rate > 0.5 else "[yellow]low[/yellow]",
    )

    console.print(table)
    console.print(
        f"[dim]Published source: {ref.source} · "
        "see docs/launch/gate0_launch_post.md for the methodology.[/dim]"
    )
