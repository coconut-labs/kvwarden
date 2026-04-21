"""KVWarden CLI.

The entry point for ``pip install kvwarden``. Commands:

* ``kvwarden serve --config configs/quickstart_fairness.yaml``
* ``kvwarden status``
* ``kvwarden models``
* ``kvwarden doctor``
* ``kvwarden bench reproduce-hero``
* ``kvwarden man [topic]``
* ``kvwarden --version``

Run ``kvwarden man`` with no topic for the overview; ``kvwarden man
<command>`` for a specific page.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Sequence

import aiohttp
from aiohttp import web
from rich.console import Console
from rich.markdown import Markdown
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from kvwarden import __version__, _telemetry
from kvwarden._manpages import get_page, list_topics
from kvwarden.cache.manager import CacheManager
from kvwarden.common.config import KVWardenConfig
from kvwarden.common.metrics import MetricsCollector
from kvwarden.router.router import WorkloadRouter
from kvwarden.tenant.manager import TenantManager

logger = logging.getLogger("kvwarden")
_console = Console()
_err_console = Console(stderr=True)


# ─────────────────────────────── arg parser ───────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the KVWarden CLI."""
    parser = argparse.ArgumentParser(
        prog="kvwarden",
        description=(
            "Tenant-fair LLM inference orchestration on a single GPU. "
            "No Kubernetes. Middleware on top of vLLM / SGLang."
        ),
        epilog=(
            "Run `kvwarden man` for expanded help. "
            "Issues: https://github.com/coconut-labs/kvwarden/issues"
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"kvwarden {__version__}",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # ── serve ──
    serve_parser = sub.add_parser(
        "serve",
        help="Start the KVWarden API server",
        description=(
            "Start the API server. Prefer `--config PATH` for anything "
            "non-trivial; CLI flags are fine for a quick sanity check."
        ),
    )
    serve_parser.add_argument(
        "models",
        nargs="*",
        help="HuggingFace model IDs (ignored when --config is given)",
    )
    serve_parser.add_argument(
        "--gpu-budget",
        type=str,
        default="80%",
        help="Fraction of GPU memory KVWarden may use (e.g. '80%%' or '0.8')",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="HTTP port (default: 8080)",
    )
    serve_parser.add_argument(
        "--engine",
        choices=["vllm", "sglang"],
        default="vllm",
        help="Default engine backend (default: vllm)",
    )
    serve_parser.add_argument(
        "--max-concurrent",
        type=int,
        default=128,
        help=(
            "Coarse engine-side concurrent-request cap. NOT the per-tenant "
            "fairness lever -- those live in the YAML. (default: 128)"
        ),
    )
    serve_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="YAML config file (overrides other flags)",
    )
    serve_parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log level (default: INFO)",
    )
    serve_parser.add_argument(
        "--no-interactive",
        action="store_true",
        help="Fail fast instead of prompting when neither MODELS nor --config is given",
    )

    # ── status ──
    status_parser = sub.add_parser(
        "status",
        help="Snapshot loaded models + cache + tenant budgets",
    )
    status_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="API server port to query (default: 8080)",
    )
    status_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit raw JSON instead of a rendered table",
    )

    # ── models ──
    models_parser = sub.add_parser("models", help="List available/loaded models")
    models_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="API server port to query (default: 8080)",
    )
    models_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit raw JSON instead of a rendered table",
    )

    # ── doctor ──
    sub.add_parser(
        "doctor",
        help="Local environment + prerequisite check",
        description=(
            "Verify Python version, GPU visibility, engine availability, "
            "port availability, and whether a newer KVWarden is on PyPI."
        ),
    )

    # ── bench ──
    bench_parser = sub.add_parser(
        "bench", help="Benchmark helpers (reproduce the hero number, etc.)",
        description="Benchmark helpers. See `kvwarden bench reproduce-hero --help`.",
    )
    bench_sub = bench_parser.add_subparsers(dest="bench_cmd", help="bench subcommand")
    repro = bench_sub.add_parser(
        "reproduce-hero",
        help="Replicate the launch-post hero number against a running server",
        description="Run the published noisy-neighbor bench and print a "
                    "side-by-side table vs docs/launch/gate0_launch_post.md.",
    )
    repro.add_argument("--flavor", choices=["2tenant", "n6", "n8"], default="2tenant",
                       help="Bench flavor (default: 2tenant — the hero).")
    repro.add_argument("--duration-s", type=float, default=None,
                       help="Bench wall time (default: 300 s).")
    repro.add_argument("--base-url", default="http://localhost:8000",
                       help="KVWarden server URL (default: http://localhost:8000).")
    repro.add_argument("--pod", action="store_true",
                       help="Provision a 1x A100 pod via RunPod and run against it.")
    repro.add_argument("--no-delete", action="store_true",
                       help="With --pod: keep the pod alive after the run.")

    # ── man ──
    man_parser = sub.add_parser(
        "man",
        help="Open a built-in help page in the terminal",
        description=(
            "Render one of the bundled help topics. Run with no topic "
            "for the overview; 'kvwarden man topics' lists all pages."
        ),
    )
    man_parser.add_argument(
        "topic",
        nargs="?",
        default="overview",
        help="Help topic (default: overview). Use 'topics' to list all.",
    )

    # ── telemetry ──
    telem_parser = sub.add_parser(
        "telemetry",
        help="Inspect or change the opt-in telemetry setting",
        description=(
            "Opt-in anonymous install/usage telemetry. See the README's "
            "'Telemetry' section and docs/privacy/telemetry.md."
        ),
    )
    telem_parser.add_argument(
        "action",
        choices=["on", "off", "status"],
        help="'on' enables, 'off' disables, 'status' prints the current state",
    )

    return parser


def _parse_gpu_budget(raw: str) -> float:
    """Parse a GPU budget string to a float fraction in [0, 1]."""
    raw = raw.strip()
    if raw.endswith("%"):
        return float(raw[:-1]) / 100.0
    val = float(raw)
    if val > 1.0:
        return val / 100.0
    return val


# ─────────────────────────────── serve ───────────────────────────────


async def _run_server(config: KVWardenConfig) -> None:
    """Start the aiohttp server with the WorkloadRouter."""
    metrics = MetricsCollector()
    cache_manager = CacheManager()
    # Wire config.tenant_defaults → TenantManager.default_budget. Without
    # this, the manager always falls back to its internal 64-concurrent
    # default and rejects high-concurrency traffic with 429 even when the
    # config lifts the cap.
    from kvwarden.tenant.manager import TenantBudget

    td = config.tenant_defaults
    default_budget = TenantBudget(
        max_concurrent_requests=td.max_concurrent_requests,
        rate_limit_rpm=td.rate_limit_rpm,
        rate_limit_burst=td.rate_limit_burst,
        max_gpu_memory_gb=td.max_gpu_memory_gb,
        priority=td.priority,
    )
    tenant_manager = TenantManager(default_budget=default_budget)

    router = WorkloadRouter(
        config=config,
        metrics=metrics,
        cache_manager=cache_manager,
        tenant_manager=tenant_manager,
    )

    await router.start()

    app = web.Application()
    app.router.add_post("/v1/chat/completions", router.handle_request)
    app.router.add_post("/v1/completions", router.handle_request)
    app.router.add_get("/v1/models", router.handle_models)
    app.router.add_get("/health", router.handle_health)

    async def handle_status(request: web.Request) -> web.Response:
        return web.json_response(router.snapshot())

    async def handle_metrics(request: web.Request) -> web.Response:
        return web.Response(
            body=metrics.prometheus_output(),
            content_type="text/plain",
        )

    app.router.add_get("/kvwarden/status", handle_status)
    app.router.add_get("/metrics", handle_metrics)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.host, config.port)
    await site.start()

    _console.print(
        f"[bold green]✓[/bold green] KVWarden serving on "
        f"[cyan]http://{config.host}:{config.port}[/cyan] "
        f"with [bold]{len(config.models)}[/bold] model(s)"
    )
    _console.print(
        f"  [dim]Models:[/dim] {', '.join(m.model_id for m in config.models)}"
    )
    _console.print(f"  [dim]GPU budget:[/dim] {config.gpu_budget_fraction * 100:.0f}%")
    _console.print(
        f"  [dim]/health ready check:[/dim] curl -fs localhost:{config.port}/health"
    )

    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await router.stop()
        await runner.cleanup()


def _interactive_serve_wizard() -> argparse.Namespace:
    """Prompt the user for the minimum inputs to start `serve`.

    Returns a Namespace compatible with what argparse would produce.
    """
    _console.print(
        "[bold]No config or model given.[/bold] Quick wizard "
        "(Ctrl-C to abort; pass --config to skip this).\n"
    )
    model = Prompt.ask(
        "HuggingFace [cyan]model ID[/cyan]",
        default="meta-llama/Llama-3.1-8B-Instruct",
    )
    engine = Prompt.ask(
        "Engine",
        choices=["vllm", "sglang"],
        default="vllm",
    )
    gpu_budget = Prompt.ask(
        "GPU memory budget (e.g. '80%%' or '0.8')",
        default="80%",
    )
    port = Prompt.ask("HTTP port", default="8080")

    return argparse.Namespace(
        command="serve",
        models=[model],
        gpu_budget=gpu_budget,
        port=int(port),
        engine=engine,
        max_concurrent=128,
        config=None,
        log_level="INFO",
        no_interactive=False,
    )


def _cmd_serve(args: argparse.Namespace) -> None:
    """Handle the 'serve' command."""
    if not args.config and not args.models:
        if args.no_interactive or not sys.stdin.isatty():
            raise SystemExit(
                "kvwarden serve: at least one MODEL or --config is required "
                "(--no-interactive set and stdin is not a tty)"
            )
        args = _interactive_serve_wizard()

    if args.config:
        config = KVWardenConfig.from_yaml(args.config)
    else:
        gpu_budget = _parse_gpu_budget(args.gpu_budget)
        config = KVWardenConfig.from_cli_args(
            model_ids=args.models,
            gpu_budget=gpu_budget,
            port=args.port,
            engine=args.engine,
            max_concurrent=args.max_concurrent,
        )
    config.log_level = args.log_level

    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    asyncio.run(_run_server(config))


# ─────────────────────────────── status / models ───────────────────────────────


async def _fetch_json(url: str) -> dict:
    """Fetch JSON from a URL."""
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            return await resp.json()


def _cmd_status(args: argparse.Namespace) -> None:
    """Handle the 'status' command."""
    url = f"http://localhost:{args.port}/kvwarden/status"
    try:
        data = asyncio.run(_fetch_json(url))
    except Exception as e:
        _err_console.print(
            f"[red]✗[/red] Could not reach KVWarden on port {args.port}: {e}"
        )
        _err_console.print(
            f"  Start the server with [cyan]kvwarden serve --port {args.port}[/cyan]"
        )
        sys.exit(1)

    if args.json:
        print(json.dumps(data, indent=2))
        return

    _render_status(data)


def _render_status(data: dict) -> None:
    """Render a status snapshot as a rich table."""
    models = data.get("models", [])
    tenants = data.get("tenants", [])
    cache = data.get("cache", {})

    _console.print(
        f"[bold]KVWarden status[/bold]  "
        f"[dim]({len(models)} models · {len(tenants)} tenants)[/dim]"
    )

    if models:
        t = Table(title="Models", header_style="bold", box=None, padding=(0, 1))
        t.add_column("ID", style="cyan")
        t.add_column("Engine")
        t.add_column("State", style="green")
        t.add_column("Last used", style="dim")
        for m in models:
            state = m.get("state", "?")
            t.add_row(
                m.get("id") or m.get("model_id", "?"),
                m.get("engine", "?"),
                state,
                str(m.get("last_used_at", "—")),
            )
        _console.print(t)

    if tenants:
        t = Table(title="Tenants", header_style="bold", box=None, padding=(0, 1))
        t.add_column("ID", style="cyan")
        t.add_column("In-flight", justify="right")
        t.add_column("Bucket tokens", justify="right")
        t.add_column("Rate limit (RPM)", justify="right")
        for tt in tenants:
            t.add_row(
                tt.get("id", "?"),
                str(tt.get("in_flight", 0)),
                f"{tt.get('tokens', 0):.1f}",
                str(tt.get("rate_limit_rpm", "—")),
            )
        _console.print(t)

    if cache:
        t = Table(title="Cache", header_style="bold", box=None, padding=(0, 1))
        t.add_column("Tier", style="cyan")
        t.add_column("Blocks", justify="right")
        t.add_column("Evictions", justify="right")
        for tier, stats in cache.items():
            if isinstance(stats, dict):
                t.add_row(
                    tier,
                    str(stats.get("blocks", 0)),
                    str(stats.get("evictions", 0)),
                )
        _console.print(t)


def _cmd_models(args: argparse.Namespace) -> None:
    """Handle the 'models' command."""
    url = f"http://localhost:{args.port}/v1/models"
    try:
        data = asyncio.run(_fetch_json(url))
    except Exception as e:
        _err_console.print(
            f"[red]✗[/red] Could not reach KVWarden on port {args.port}: {e}"
        )
        sys.exit(1)

    models = data.get("data", [])

    if args.json:
        print(json.dumps(data, indent=2))
        return

    if not models:
        _console.print("[yellow]No models loaded.[/yellow]")
        return

    t = Table(header_style="bold", box=None, padding=(0, 1))
    t.add_column("Model ID", style="cyan")
    t.add_column("Engine")
    t.add_column("Healthy")
    for m in models:
        healthy = m.get("healthy", None)
        mark = Text("✓", style="green") if healthy else Text("✗", style="red")
        if healthy is None:
            mark = Text("?", style="dim")
        t.add_row(str(m.get("id", "?")), str(m.get("engine", "?")), mark)
    _console.print(t)


# ─────────────────────────────── doctor ───────────────────────────────


def _pypi_latest_version() -> str | None:
    """Best-effort fetch of the latest kvwarden version from PyPI."""
    try:
        import urllib.request

        req = urllib.request.Request(
            "https://pypi.org/pypi/kvwarden/json",
            headers={"User-Agent": f"kvwarden-doctor/{__version__}"},
        )
        with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode())
            return payload["info"]["version"]
    except Exception:
        return None


def _port_free(port: int) -> bool:
    """True if TCP ``port`` on localhost is bindable right now."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _nvidia_smi_summary() -> str | None:
    """Return a one-line device summary, or None if nvidia-smi is missing."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version",
                "--format=csv,noheader",
            ],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        line = (out.stdout or "").strip().splitlines()
        return line[0] if line else None
    except Exception:
        return None


def _engine_importable(module_name: str) -> bool:
    """True if ``import module_name`` would succeed."""
    try:
        __import__(module_name)
        return True
    except Exception:
        return False


def _cmd_doctor(_args: argparse.Namespace) -> None:
    """Handle the 'doctor' command."""
    checks: list[tuple[str, str, str | None]] = []

    # Python version
    py = sys.version_info
    py_ok = (py.major, py.minor) >= (3, 11)
    checks.append(
        (
            "ok" if py_ok else "fail",
            "Python",
            f"{py.major}.{py.minor}.{py.micro}" + ("" if py_ok else "  ✗ need >= 3.11"),
        )
    )

    # KVWarden version vs PyPI
    latest = _pypi_latest_version()
    if latest is None:
        checks.append(("warn", "KVWarden", f"{__version__} (PyPI unreachable)"))
    elif latest == __version__:
        checks.append(("ok", "KVWarden", f"{__version__} (latest)"))
    else:
        checks.append(
            (
                "warn",
                "KVWarden",
                f"{__version__} installed · {latest} available  "
                "[dim]pip install --upgrade kvwarden[/dim]",
            )
        )

    # Engines
    for eng in ("vllm", "sglang"):
        present = _engine_importable(eng)
        status = "ok" if present else "warn"
        note = "importable" if present else f"missing  [dim]pip install {eng}[/dim]"
        checks.append((status, f"engine:{eng}", note))

    # GPU
    gpu = _nvidia_smi_summary()
    if gpu is None:
        checks.append(("warn", "GPU", "nvidia-smi not found (CPU-only host?)"))
    else:
        checks.append(("ok", "GPU", gpu))

    # Default port
    port = 8080
    if _port_free(port):
        checks.append(("ok", f"port:{port}", "free"))
    else:
        checks.append(
            (
                "warn",
                f"port:{port}",
                "in use  [dim](pick another via --port)[/dim]",
            )
        )

    # Shipped configs reachable
    cfg_path = os.path.join("configs", "quickstart_fairness.yaml")
    if os.path.exists(cfg_path):
        checks.append(("ok", "configs/quickstart_fairness.yaml", "present in CWD"))
    else:
        checks.append(
            (
                "warn",
                "configs/quickstart_fairness.yaml",
                "not in CWD  [dim](run from a clone of the repo, or pass "
                "--config /abs/path.yaml)[/dim]",
            )
        )

    t = Table(header_style="bold", box=None, padding=(0, 1))
    t.add_column("", width=2)
    t.add_column("Check", style="cyan")
    t.add_column("Result")
    for status, name, note in checks:
        icon = {
            "ok": "[green]✓[/green]",
            "warn": "[yellow]![/yellow]",
            "fail": "[red]✗[/red]",
        }[status]
        t.add_row(icon, name, note or "")
    _console.print("[bold]kvwarden doctor[/bold]")
    _console.print(t)

    if any(s == "fail" for s, _, _ in checks):
        sys.exit(1)


# ─────────────────────────────── man ───────────────────────────────


def _cmd_telemetry(args: argparse.Namespace) -> None:
    """Handle the 'telemetry' subcommand."""
    if args.action == "status":
        st = _telemetry.get_status()
        state = "on" if st["enabled"] else "off"
        _console.print(f"[bold]telemetry:[/bold] {state}")
        _console.print(f"  [dim]configured:[/dim]  {st['configured']}")
        _console.print(f"  [dim]install_id:[/dim]  {st['install_id'] or '—'}")
        _console.print(f"  [dim]env override:[/dim] {st['env_disabled']}")
        _console.print(f"  [dim]endpoint set:[/dim] {st['endpoint_set']}")
        _console.print(f"  [dim]config file:[/dim]  {st['config_path']}")
        return

    new = _telemetry.set_enabled(args.action == "on")
    state = "on" if new["enabled"] else "off"
    _console.print(f"[green]✓[/green] telemetry {state}")


def _cmd_man(args: argparse.Namespace) -> None:
    """Handle the 'man' command."""
    topic = args.topic
    if topic == "topics":
        _console.print("[bold]Available help topics[/bold]")
        for t in list_topics():
            _console.print(f"  [cyan]{t}[/cyan]")
        _console.print("\nUsage: [cyan]kvwarden man <topic>[/cyan]")
        return

    page = get_page(topic)
    if page is None:
        _err_console.print(f"[red]✗[/red] Unknown topic: [cyan]{topic}[/cyan]")
        _err_console.print(
            "Run [cyan]kvwarden man topics[/cyan] to list available pages."
        )
        sys.exit(1)

    _console.print(Markdown(page))


# ─────────────────────────────── bench ───────────────────────────────


def _cmd_bench(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Dispatch ``kvwarden bench <subcommand>``."""
    if getattr(args, "bench_cmd", None) == "reproduce-hero":
        # Lazy import — keeps `kvwarden --help` startup fast.
        from kvwarden._bench.hero import run_reproduce_hero

        run_reproduce_hero(args)
        return
    parser.parse_args(["bench", "--help"])  # prints help and exits


# ─────────────────────────────── main ───────────────────────────────


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry point."""
    parser = build_parser()
    args = parser.parse_args(argv)

    # Pre-check that kvwarden is actually installed as a package when we
    # fetch __version__ via importlib.metadata — this is a belt-and-suspenders
    # check for editable installs where metadata can be absent.
    try:
        _pkg_version("kvwarden")
    except PackageNotFoundError:
        pass  # non-fatal; __init__.py falls back to "0.0.0+unknown"

    # Telemetry hook. Runs before dispatch so first-run prompt lands before
    # any command output. Never raises; silently swallows everything. The
    # `telemetry` subcommand itself is exempt — the user is managing the
    # setting there, we shouldn't send an event as a side effect.
    if args.command != "telemetry":
        event = {
            "serve": "serve_started",
            "doctor": "doctor_ran",
            "bench": "bench_reproduce_hero",
        }.get(args.command or "", "install_first_run")
        try:
            _telemetry.maybe_prompt_and_record_event(event=event)
        except Exception:
            pass

    if args.command == "serve":
        _cmd_serve(args)
    elif args.command == "status":
        _cmd_status(args)
    elif args.command == "models":
        _cmd_models(args)
    elif args.command == "doctor":
        _cmd_doctor(args)
    elif args.command == "man":
        _cmd_man(args)
    elif args.command == "telemetry":
        _cmd_telemetry(args)
    elif args.command == "bench":
        _cmd_bench(args, parser)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
