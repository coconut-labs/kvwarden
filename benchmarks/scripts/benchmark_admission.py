#!/usr/bin/env python3
"""Benchmark: admission control vs. direct engine access.

Simulates the scheduling cliff scenario observed in profiling:
    - vLLM A100: c=128->c=256 yields +0.4% throughput but +1434% TTFT
    - SGLang A100: c=128->c=256 yields -1.2% throughput but +928% TTFT

Sends 256 concurrent requests through two paths:
    1. Via KVWarden with admission control (max_concurrent=128)
    2. Directly to the engine (no admission control)

Then compares TTFT distributions to quantify the value of admission control.

Usage:
    python benchmarks/scripts/benchmark_admission.py \\
        --engine-url http://localhost:8000 \\
        --kvwarden-url http://localhost:8080 \\
        --model meta-llama/Llama-3.1-8B-Instruct \\
        --total-requests 256 \\
        --max-concurrent 128 \\
        --output-dir results/admission_benchmark
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

# Add project root to path for imports
_project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_project_root))
sys.path.insert(0, str(_project_root / "profiling" / "scripts"))

from profiling_utils import (  # noqa: E402
    AsyncBenchmarkClient,
    BenchmarkResults,
    GPUMetricsCollector,
    RequestGenerator,
)

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        Parsed argument namespace.
    """
    parser = argparse.ArgumentParser(
        description="Benchmark admission control vs direct engine access",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--engine-url",
        type=str,
        required=True,
        help="Direct engine URL (e.g. http://localhost:8000)",
    )
    parser.add_argument(
        "--kvwarden-url",
        type=str,
        required=True,
        help="KVWarden proxy URL (e.g. http://localhost:8080)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="meta-llama/Llama-3.1-8B-Instruct",
        help="Model name for requests",
    )
    parser.add_argument(
        "--total-requests",
        type=int,
        default=256,
        help="Total number of concurrent requests to send (default: 256)",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=128,
        help="Max concurrent requests for KVWarden admission control (default: 128)",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=128,
        help="Max output tokens per request (default: 128)",
    )
    parser.add_argument(
        "--input-tokens",
        type=int,
        default=256,
        help="Approximate input token count per request (default: 256)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="results/admission_benchmark",
        help="Directory for output files",
    )
    parser.add_argument(
        "--gpu-monitor",
        action="store_true",
        default=False,
        help="Enable GPU metrics collection",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducibility (default: 42)",
    )
    return parser.parse_args()


async def run_benchmark(
    base_url: str,
    model: str,
    requests: list[dict[str, Any]],
    concurrency: int,
    label: str,
    gpu_collector: GPUMetricsCollector | None = None,
) -> BenchmarkResults:
    """Run a benchmark against a single endpoint.

    Args:
        base_url: Target server URL.
        model: Model name.
        requests: List of request dicts.
        concurrency: Max concurrent requests.
        label: Human-readable label for logging.
        gpu_collector: Optional GPU metrics collector.

    Returns:
        BenchmarkResults with per-request timing data.
    """
    logger.info(
        "Starting %s benchmark: %d requests, concurrency=%d, target=%s",
        label,
        len(requests),
        concurrency,
        base_url,
    )

    client = AsyncBenchmarkClient(
        base_url=base_url,
        model_name=model,
        concurrency_level=concurrency,
        timeout_s=120,
    )

    start = time.time()
    results = await client.run(requests, gpu_collector=gpu_collector)
    elapsed = time.time() - start

    summary = results.summary()
    logger.info("%s complete in %.1fs:", label, elapsed)
    logger.info(
        "  Successful: %d / %d", summary.get("num_successful", 0), len(requests)
    )
    logger.info("  TTFT p50: %.1fms", summary.get("ttft_p50_ms", 0))
    logger.info("  TTFT p99: %.1fms", summary.get("ttft_p99_ms", 0))
    logger.info(
        "  Throughput: %.1f tok/s",
        summary.get("throughput_tok_per_sec", 0),
    )

    return results


def compare_results(
    direct_results: BenchmarkResults,
    admission_results: BenchmarkResults,
) -> dict[str, Any]:
    """Compare direct vs admission-controlled results.

    Args:
        direct_results: Results from direct engine access.
        admission_results: Results from KVWarden with admission control.

    Returns:
        Comparison summary dictionary.
    """
    direct_summary = direct_results.summary()
    admission_summary = admission_results.summary()

    def safe_pct_change(old: float, new: float) -> float:
        if old == 0:
            return 0.0
        return ((new - old) / old) * 100

    comparison: dict[str, Any] = {
        "direct": direct_summary,
        "admission_controlled": admission_summary,
        "improvements": {},
    }

    # TTFT improvement (lower is better, so negative % = improvement)
    for metric in ["ttft_p50_ms", "ttft_p99_ms"]:
        direct_val = direct_summary.get(metric, 0)
        admission_val = admission_summary.get(metric, 0)
        pct = safe_pct_change(direct_val, admission_val)
        comparison["improvements"][metric] = {
            "direct": round(direct_val, 2),
            "admission": round(admission_val, 2),
            "change_pct": round(pct, 2),
            "improved": pct < 0,
        }

    # Throughput (higher is better)
    direct_tps = direct_summary.get("throughput_tok_per_sec", 0)
    admission_tps = admission_summary.get("throughput_tok_per_sec", 0)
    tps_pct = safe_pct_change(direct_tps, admission_tps)
    comparison["improvements"]["throughput_tok_per_sec"] = {
        "direct": round(direct_tps, 2),
        "admission": round(admission_tps, 2),
        "change_pct": round(tps_pct, 2),
    }

    return comparison


def print_comparison(comparison: dict[str, Any]) -> None:
    """Print a formatted comparison table.

    Args:
        comparison: Comparison dictionary from compare_results().
    """
    print("\n" + "=" * 70)
    print("ADMISSION CONTROL BENCHMARK RESULTS")
    print("=" * 70)

    improvements = comparison.get("improvements", {})

    header = f"{'Metric':<30} {'Direct':>12} {'Admission':>12} {'Change':>10}"
    print(header)
    print("-" * 70)

    for metric, data in improvements.items():
        label = (
            metric.replace("_", " ")
            .replace("ms", " (ms)")
            .replace("tok per sec", "(tok/s)")
        )
        direct_val = data.get("direct", 0)
        admission_val = data.get("admission", 0)
        change_pct = data.get("change_pct", 0)
        sign = "+" if change_pct > 0 else ""
        row = f"{label:<30} {direct_val:>12.1f} {admission_val:>12.1f} {sign}{change_pct:>8.1f}%"
        print(row)

    print("=" * 70)

    # Highlight TTFT improvement
    ttft_p99 = improvements.get("ttft_p99_ms", {})
    if ttft_p99.get("improved"):
        print(
            f"\nAdmission control REDUCED p99 TTFT by "
            f"{abs(ttft_p99['change_pct']):.1f}% "
            f"({ttft_p99['direct']:.1f}ms -> {ttft_p99['admission']:.1f}ms)"
        )
    else:
        print(
            f"\nDirect access had LOWER p99 TTFT "
            f"({ttft_p99.get('direct', 0):.1f}ms vs "
            f"{ttft_p99.get('admission', 0):.1f}ms). "
            f"Consider increasing --max-concurrent."
        )


async def main() -> None:
    """Run the admission control benchmark."""
    args = parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Generate requests
    generator = RequestGenerator(seed=args.seed)
    requests = generator.generate_fixed_length(
        n=args.total_requests,
        input_len=args.input_tokens,
        output_len=args.max_tokens,
    )

    logger.info(
        "Generated %d requests (input~%d tokens, output~%d tokens)",
        len(requests),
        args.input_tokens,
        args.max_tokens,
    )

    # Optional GPU monitoring
    gpu_collector = GPUMetricsCollector() if args.gpu_monitor else None

    # Benchmark 1: Direct engine access (no admission control)
    logger.info("--- Phase 1: Direct engine access (no admission control) ---")
    direct_results = await run_benchmark(
        base_url=args.engine_url,
        model=args.model,
        requests=requests,
        concurrency=args.total_requests,  # All at once
        label="Direct",
        gpu_collector=gpu_collector,
    )

    # Brief pause between benchmarks to let the engine settle
    logger.info("Waiting 10s for engine to stabilize between benchmarks...")
    await asyncio.sleep(10)

    # Benchmark 2: Through KVWarden with admission control
    logger.info("--- Phase 2: KVWarden with admission control ---")
    admission_results = await run_benchmark(
        base_url=args.kvwarden_url,
        model=args.model,
        requests=requests,
        concurrency=args.total_requests,  # All at once (KVWarden handles queuing)
        label="Admission-controlled",
        gpu_collector=gpu_collector,
    )

    # Save raw results
    direct_results.to_csv(output_dir / "direct_results.csv")
    admission_results.to_csv(output_dir / "admission_results.csv")

    if gpu_collector is not None:
        gpu_collector.to_csv(output_dir / "gpu_metrics.csv")

    # Compare
    comparison = compare_results(direct_results, admission_results)

    comparison_path = output_dir / "comparison.json"
    with open(comparison_path, "w") as f:
        json.dump(comparison, f, indent=2, default=str)
    logger.info("Comparison saved to %s", comparison_path)

    # Save config
    config = {
        "engine_url": args.engine_url,
        "kvwarden_url": args.kvwarden_url,
        "model": args.model,
        "total_requests": args.total_requests,
        "max_concurrent": args.max_concurrent,
        "max_tokens": args.max_tokens,
        "input_tokens": args.input_tokens,
        "seed": args.seed,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    config_path = output_dir / "benchmark_config.json"
    with open(config_path, "w") as f:
        json.dump(config, f, indent=2)

    print_comparison(comparison)


if __name__ == "__main__":
    asyncio.run(main())
