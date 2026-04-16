"""InferGrid CLI.

Commands:
    infergrid serve model1 model2 --gpu-budget 80 --port 8080
    infergrid status
    infergrid models
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Sequence

import aiohttp
from aiohttp import web

from infergrid.cache.manager import CacheManager
from infergrid.common.config import InferGridConfig, ModelConfig
from infergrid.common.metrics import MetricsCollector
from infergrid.router.router import WorkloadRouter
from infergrid.tenant.manager import TenantManager

logger = logging.getLogger("infergrid")


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the InferGrid CLI.

    Returns:
        Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        prog="infergrid",
        description="InferGrid -- adaptive inference orchestration for LLM serving",
    )
    sub = parser.add_subparsers(dest="command", help="Available commands")

    # ── serve ──
    serve_parser = sub.add_parser(
        "serve", help="Start the InferGrid API server"
    )
    serve_parser.add_argument(
        "models",
        nargs="+",
        help="HuggingFace model IDs to serve",
    )
    serve_parser.add_argument(
        "--gpu-budget",
        type=str,
        default="80%",
        help="GPU memory budget as a percentage (e.g. '80%%') or fraction (e.g. '0.8')",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="API server port (default: 8080)",
    )
    serve_parser.add_argument(
        "--engine",
        choices=["vllm", "sglang"],
        default="vllm",
        help="Default engine backend (default: vllm)",
    )
    serve_parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to a YAML configuration file (overrides other flags)",
    )
    serve_parser.add_argument(
        "--log-level",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        default="INFO",
        help="Log level (default: INFO)",
    )

    # ── status ──
    status_parser = sub.add_parser(
        "status", help="Show loaded models, cache usage, tenant stats"
    )
    status_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="API server port to query (default: 8080)",
    )

    # ── models ──
    models_parser = sub.add_parser(
        "models", help="List available/loaded models"
    )
    models_parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="API server port to query (default: 8080)",
    )

    return parser


def _parse_gpu_budget(raw: str) -> float:
    """Parse a GPU budget string to a float fraction.

    Args:
        raw: Budget string like "80%" or "0.8".

    Returns:
        Float between 0.0 and 1.0.
    """
    raw = raw.strip()
    if raw.endswith("%"):
        return float(raw[:-1]) / 100.0
    val = float(raw)
    if val > 1.0:
        return val / 100.0
    return val


async def _run_server(config: InferGridConfig) -> None:
    """Start the aiohttp server with the WorkloadRouter.

    Args:
        config: InferGrid configuration.
    """
    metrics = MetricsCollector()
    cache_manager = CacheManager()
    tenant_manager = TenantManager()

    router = WorkloadRouter(
        config=config,
        metrics=metrics,
        cache_manager=cache_manager,
        tenant_manager=tenant_manager,
    )

    await router.start()

    app = web.Application()

    # OpenAI-compatible endpoints
    app.router.add_post("/v1/chat/completions", router.handle_request)
    app.router.add_post("/v1/completions", router.handle_request)
    app.router.add_get("/v1/models", router.handle_models)
    app.router.add_get("/health", router.handle_health)

    # InferGrid status endpoints
    async def handle_status(request: web.Request) -> web.Response:
        return web.json_response(router.snapshot())

    async def handle_metrics(request: web.Request) -> web.Response:
        return web.Response(
            body=metrics.prometheus_output(),
            content_type="text/plain",
        )

    app.router.add_get("/infergrid/status", handle_status)
    app.router.add_get("/metrics", handle_metrics)

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.host, config.port)
    await site.start()

    logger.info(
        "InferGrid serving on http://%s:%d with %d model(s)",
        config.host, config.port, len(config.models),
    )
    logger.info("  Models: %s", [m.model_id for m in config.models])
    logger.info("  GPU budget: %.0f%%", config.gpu_budget_fraction * 100)

    # Run until interrupted
    try:
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await router.stop()
        await runner.cleanup()


def _cmd_serve(args: argparse.Namespace) -> None:
    """Handle the 'serve' command."""
    if args.config:
        config = InferGridConfig.from_yaml(args.config)
    else:
        gpu_budget = _parse_gpu_budget(args.gpu_budget)
        config = InferGridConfig.from_cli_args(
            model_ids=args.models,
            gpu_budget=gpu_budget,
            port=args.port,
            engine=args.engine,
        )
    config.log_level = args.log_level

    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    asyncio.run(_run_server(config))


async def _fetch_json(url: str) -> dict:
    """Fetch JSON from a URL.

    Args:
        url: HTTP URL.

    Returns:
        Parsed JSON as a dict.
    """
    timeout = aiohttp.ClientTimeout(total=5)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as resp:
            return await resp.json()


def _cmd_status(args: argparse.Namespace) -> None:
    """Handle the 'status' command."""
    url = f"http://localhost:{args.port}/infergrid/status"
    try:
        data = asyncio.run(_fetch_json(url))
    except Exception as exc:
        print(f"Error connecting to InferGrid at port {args.port}: {exc}")
        sys.exit(1)

    print(json.dumps(data, indent=2))


def _cmd_models(args: argparse.Namespace) -> None:
    """Handle the 'models' command."""
    url = f"http://localhost:{args.port}/v1/models"
    try:
        data = asyncio.run(_fetch_json(url))
    except Exception as exc:
        print(f"Error connecting to InferGrid at port {args.port}: {exc}")
        sys.exit(1)

    models = data.get("data", [])
    if not models:
        print("No models loaded.")
        return

    print(f"{'Model ID':<50} {'Engine':<10} {'Healthy':<10}")
    print("-" * 70)
    for m in models:
        print(f"{m['id']:<50} {m.get('engine', '?'):<10} {m.get('healthy', '?'):<10}")


def main(argv: Sequence[str] | None = None) -> None:
    """CLI entry point.

    Args:
        argv: Command-line arguments (defaults to sys.argv).
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "serve":
        _cmd_serve(args)
    elif args.command == "status":
        _cmd_status(args)
    elif args.command == "models":
        _cmd_models(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
