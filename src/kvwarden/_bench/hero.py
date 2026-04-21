"""``kvwarden bench reproduce-hero`` — one-liner hero-number replication.

Drives ``benchmarks/scripts/benchmark_n_tenant_single_model.py`` as an
in-process import (NOT a subprocess) and renders a side-by-side
comparison against the published Gate 2-FAIRNESS preprint v3 numbers.

Reference numbers live in :mod:`kvwarden._bench.compare` — do NOT
reload them from YAML or results/ at runtime; they're published
constants.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import shutil
import signal
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
)

from kvwarden._bench.compare import REFERENCES, render_comparison

_HERO_MODEL = "meta-llama/Llama-3.1-8B-Instruct"


# ─────────────────────────────── flavor config ───────────────────────────────


@dataclass(frozen=True)
class FlavorSpec:
    """Bench-script args for one flavor (2tenant / n6 / n8)."""

    name: str
    num_quiet: int
    flooder_rps: float
    quiet_rps: float
    default_duration_s: float
    config_hint: str  # shipped config YAML the user should serve against.


FLAVORS: dict[str, FlavorSpec] = {
    "2tenant": FlavorSpec(
        name="2tenant",
        num_quiet=1,
        flooder_rps=32.0,
        quiet_rps=1.0,
        default_duration_s=300.0,
        config_hint="configs/gate2_fairness_token_bucket.yaml",
    ),
    "n6": FlavorSpec(
        name="n6",
        num_quiet=5,
        flooder_rps=32.0,
        quiet_rps=1.0,
        default_duration_s=300.0,
        config_hint="configs/gate2_fairness_token_bucket_n6.yaml",
    ),
    "n8": FlavorSpec(
        name="n8",
        num_quiet=7,
        flooder_rps=32.0,
        quiet_rps=1.0,
        default_duration_s=300.0,
        config_hint="configs/gate21_fairness_n8.yaml",
    ),
}


# ─────────────────────────────── pre-flight ───────────────────────────────


def _detect_gpu() -> str | None:
    """Best-effort one-line GPU description from nvidia-smi, else None."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        line = (out.stdout or "").strip().splitlines()
        return line[0] if line else None
    except Exception:
        return None


async def _preflight_server(
    base_url: str, model: str, console: Console
) -> tuple[bool, str | None]:
    """Check that the target server is healthy and has the hero model loaded.

    Returns ``(ok, hint)``. On failure, ``hint`` is a human-readable
    remediation string. ``ok=True`` means both ``/health`` and
    ``/v1/models`` passed and the model appears in the model list.
    """
    timeout = aiohttp.ClientTimeout(total=5)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(f"{base_url}/health") as resp:
                if resp.status != 200:
                    body = (await resp.text())[:200]
                    return False, (
                        f"{base_url}/health returned HTTP {resp.status} "
                        f"({body!r}). Wait for engines to finish loading — "
                        "the first call is 503 until vLLM JIT-compiles "
                        "(typically 30-90s for an 8B model on A100)."
                    )
            async with session.get(f"{base_url}/v1/models") as resp:
                if resp.status != 200:
                    return False, (
                        f"{base_url}/v1/models returned HTTP {resp.status}. "
                        "Is this an KVWarden server? (OpenAI /v1/models endpoint is required.)"
                    )
                payload = await resp.json()
    except aiohttp.ClientConnectorError:
        return False, (
            f"Could not connect to {base_url}. Start the server in "
            "another terminal with:\n"
            "  kvwarden serve --config configs/gate2_fairness_token_bucket.yaml\n"
            "then wait for `curl -fs $BASE/health` to return 200."
        )
    except asyncio.TimeoutError:
        return False, f"{base_url} timed out after 5s. Is the server overloaded?"
    except Exception as exc:
        return False, f"Unexpected error probing {base_url}: {exc}"

    ids = {m.get("id") for m in payload.get("data", [])}
    if model not in ids and not any(model.split("/")[-1] in str(i) for i in ids):
        return False, (
            f"Model {model!r} not found in /v1/models (got {sorted(ids)!r}). "
            "Point `kvwarden serve --config <path>` at a config that includes "
            f"{model!r} — e.g. `configs/gate2_fairness_token_bucket.yaml`."
        )
    return True, None


def _split_host_port(base_url: str) -> tuple[str, int]:
    """Parse ``http://host:port`` into ``(host, port)`` with sane defaults."""
    stripped = base_url.split("://", 1)[-1].rstrip("/")
    if ":" in stripped:
        host, port_s = stripped.rsplit(":", 1)
        try:
            return host, int(port_s)
        except ValueError:
            return stripped, 8000
    return stripped, 8000


def _port_listening(host: str, port: int) -> bool:
    """Cheap liveness probe that doesn't require the HTTP layer to work."""
    try:
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


# ─────────────────────────────── bench runner ───────────────────────────────


def _import_bench_module():
    """Import the n-tenant bench script from the repo checkout."""
    from pathlib import Path as _P

    script_dir = (
        _P(__file__).resolve().parent.parent.parent.parent / "benchmarks" / "scripts"
    )
    if str(script_dir) not in sys.path:
        sys.path.insert(0, str(script_dir))
    try:
        import benchmark_n_tenant_single_model as bench  # type: ignore[import-not-found]

        return bench
    except ImportError as exc:
        raise RuntimeError(
            "Could not import benchmarks/scripts/benchmark_n_tenant_single_model.py. "
            "`kvwarden bench` requires a checkout of the kvwarden repo. "
            "`pip install kvwarden` alone doesn't bundle the bench scripts; "
            "clone the repo and run from there, or `pip install kvwarden[profiling]` "
            "and copy the script from https://github.com/coconut-labs/kvwarden."
        ) from exc


def _build_bench_namespace(
    flavor: FlavorSpec,
    base_url: str,
    duration_s: float,
    output_dir: Path,
    seed: int,
) -> argparse.Namespace:
    """Namespace shaped exactly like ``benchmark_n_tenant_single_model.main()``."""
    return argparse.Namespace(
        url=base_url,
        model=_HERO_MODEL,
        flooder_rps=flavor.flooder_rps,
        quiet_rps=flavor.quiet_rps,
        num_quiet=flavor.num_quiet,
        duration_s=duration_s,
        max_tokens=64,
        timeout_s=60.0,
        seed=seed,
        output_dir=str(output_dir),
        prompt_length_dist="",
    )


async def _run_bench_with_progress(
    bench_module: Any,
    ns: argparse.Namespace,
    console: Console,
) -> int:
    """Run ``bench_module.main_async(ns)`` while ticking a rich progress bar.

    The bench script runs a fixed wall-clock duration; we just drive a
    single progress bar against ``ns.duration_s`` and let the bench do
    the actual work.
    """
    total = int(ns.duration_s)
    with Progress(
        TextColumn("[cyan]{task.description}[/cyan]"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}s"),
        TimeElapsedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
    ) as progress:
        task_id = progress.add_task(
            f"bench flavor={ns.num_quiet + 1}-tenant", total=total
        )

        bench_task = asyncio.create_task(bench_module.main_async(ns))
        start = time.monotonic()
        while not bench_task.done():
            elapsed = min(total, int(time.monotonic() - start))
            progress.update(task_id, completed=elapsed)
            await asyncio.sleep(1.0)
        progress.update(task_id, completed=total)
        return await bench_task


# ─────────────────────────────── result extraction ───────────────────────────────


def _count_429s(csv_path: Path) -> tuple[int, int]:
    """Return ``(n_429, n_total)`` from a tenant CSV written by the bench.

    The bench writes HTTP errors as ``"HTTP 429: <body>"`` in the
    ``error`` column; a missing file means the tenant never issued a
    request (e.g. num_quiet=0), which we treat as ``(0, 0)`` rather than
    crashing.
    """
    if not csv_path.exists():
        return (0, 0)
    n_429 = 0
    n_total = 0
    with open(csv_path, newline="") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            n_total += 1
            err = row.get("error", "") or ""
            if err.startswith("HTTP 429") or "429" in err.split(":", 1)[0]:
                n_429 += 1
    return (n_429, n_total)


def _load_summary(output_dir: Path) -> dict[str, Any]:
    """Load ``summary.json`` written by the bench script. Raise on missing."""
    path = output_dir / "summary.json"
    if not path.exists():
        raise RuntimeError(
            f"Bench produced no summary.json at {path} — did it fail mid-run? "
            f"Check {output_dir}/tenant_*.csv for partial output."
        )
    with open(path) as fh:
        return json.load(fh)


def _build_report(
    flavor: FlavorSpec,
    summary: dict[str, Any],
    flooder_429_rate: float,
    base_url: str,
    duration_s: float,
    gpu: str | None,
    started_at: str,
    finished_at: str,
) -> dict[str, Any]:
    """Assemble the JSON report emitted alongside the bench artifacts."""
    ref = REFERENCES[flavor.name]
    quiet_p99 = summary.get("quiet_aggregate", {}).get("ttft_p99_ms", -1)
    quiet_count = summary.get("quiet_aggregate", {}).get("count_ok", 0)
    flooder_p99 = summary.get("flooder", {}).get("ttft_p99_ms", -1)
    return {
        "schema_version": 1,
        "flavor": flavor.name,
        "model": _HERO_MODEL,
        "base_url": base_url,
        "duration_s": duration_s,
        "started_at_utc": started_at,
        "finished_at_utc": finished_at,
        "gpu": gpu,
        "bench_args": {
            "flooder_rps": flavor.flooder_rps,
            "quiet_rps": flavor.quiet_rps,
            "num_quiet": flavor.num_quiet,
        },
        "user_result": {
            "quiet_aggregate_p99_ms": quiet_p99,
            "quiet_aggregate_count_ok": quiet_count,
            "flooder_p99_ms": flooder_p99,
            "flooder_429_rate": round(flooder_429_rate, 4),
        },
        "reference": {
            "source": ref.source,
            "solo_p99_ms": ref.solo_p99_ms,
            "fifo_p99_ms": ref.fifo_p99_ms,
            "tokenbucket_p99_ms": ref.tokenbucket_p99_ms,
            "ratio_of_solo": ref.ratio_of_solo,
        },
        "docs": "docs/launch/gate0_launch_post.md",
        "raw_summary": summary,
    }


def _flooder_rate(output_dir: Path) -> float:
    """Ratio of HTTP 429 responses over total flooder requests (0.0 if none)."""
    n_429, n_total = _count_429s(output_dir / "tenant_flooder.csv")
    if n_total == 0:
        return 0.0
    return n_429 / n_total


# ─────────────────────────────── top-level orchestration ───────────────────────────────


async def _run(
    flavor: FlavorSpec,
    base_url: str,
    duration_s: float,
    output_dir: Path,
    seed: int,
    console: Console,
) -> dict[str, Any]:
    """Core orchestration: pre-flight, bench, summary, report."""
    bench = _import_bench_module()
    ok, hint = await _preflight_server(base_url, _HERO_MODEL, console)
    if not ok:
        console.print(f"[red]✗[/red] pre-flight failed: {hint}")
        raise SystemExit(2)
    gpu = _detect_gpu()
    if gpu and "A100" not in gpu:
        console.print(
            f"[yellow]![/yellow] Detected GPU [bold]{gpu}[/bold] — reference "
            "numbers were measured on A100-SXM4. Expect some divergence; the "
            "comparison table still runs."
        )
    output_dir.mkdir(parents=True, exist_ok=True)
    ns = _build_bench_namespace(flavor, base_url, duration_s, output_dir, seed)
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rc = await _run_bench_with_progress(bench, ns, console)
    finished_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if rc != 0:
        raise SystemExit(
            f"bench script exited with rc={rc}. See {output_dir}/tenant_*.csv "
            "for partial artifacts."
        )
    summary = _load_summary(output_dir)
    rate = _flooder_rate(output_dir)
    report = _build_report(
        flavor=flavor,
        summary=summary,
        flooder_429_rate=rate,
        base_url=base_url,
        duration_s=duration_s,
        gpu=gpu,
        started_at=started_at,
        finished_at=finished_at,
    )
    with open(output_dir / "report.json", "w") as fh:
        json.dump(report, fh, indent=2)
    return report


def run_reproduce_hero(args: argparse.Namespace) -> None:
    """Entry point wired from ``kvwarden.cli._cmd_bench_reproduce_hero``."""
    console = Console()
    flavor_key: str = args.flavor
    if flavor_key not in FLAVORS:
        console.print(f"[red]✗[/red] unknown flavor: {flavor_key!r}")
        raise SystemExit(2)
    flavor = FLAVORS[flavor_key]
    base_url = args.base_url.rstrip("/")
    duration_s = float(args.duration_s or flavor.default_duration_s)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = Path(f"./kvwarden-reproduce-{stamp}").resolve()

    pod_ctx = None
    if args.pod:
        # Lazy-import keeps the pod path optional — `runpod` is NOT a
        # declared runtime dep (zero-new-deps constraint), so a user
        # without RUNPOD_API_KEY + runpod SDK shouldn't pay for it.
        from kvwarden._bench.pod import ensure_pod, pod_signal_handler

        pod_ctx = ensure_pod(console=console, delete_on_exit=not args.no_delete)
        base_url = pod_ctx.base_url
        # Register the SIGINT handler BEFORE the bench starts so Ctrl-C
        # always reaches the cleanup path.
        signal.signal(signal.SIGINT, pod_signal_handler(pod_ctx))
        signal.signal(signal.SIGTERM, pod_signal_handler(pod_ctx))

    try:
        # Probe the bare socket early so we can fail fast with a clean
        # error rather than a hostname resolution exception.
        host, port = _split_host_port(base_url)
        if not _port_listening(host, port):
            console.print(
                f"[red]✗[/red] nothing listening on {host}:{port}. "
                f"Start a server with: [cyan]kvwarden serve --config "
                f"{flavor.config_hint}[/cyan]"
            )
            raise SystemExit(2)

        report = asyncio.run(
            _run(
                flavor=flavor,
                base_url=base_url,
                duration_s=duration_s,
                output_dir=output_dir,
                seed=42,
                console=console,
            )
        )
    finally:
        if pod_ctx is not None and not args.no_delete:
            pod_ctx.teardown()

    user = report["user_result"]
    render_comparison(
        flavor=flavor.name,
        user_quiet_p99_ms=user["quiet_aggregate_p99_ms"],
        user_flooder_429_rate=user["flooder_429_rate"],
        user_solo_p99_ms=None,
        console=console,
    )
    console.print(
        f"\n[bold]artifacts:[/bold] {output_dir}\n"
        f"  report.json ............ side-by-side vs published\n"
        f"  summary.json ........... raw bench summary\n"
        f"  tenant_*.csv ........... per-request rows (file a bug with these)\n"
        f"[bold]docs:[/bold] docs/launch/gate0_launch_post.md"
    )
