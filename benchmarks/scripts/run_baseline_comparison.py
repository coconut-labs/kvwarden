"""Head-to-head baseline comparison of vLLM vs SGLang.

Runs identical workloads against both engines and produces comparison
metrics. This is the controlled experiment that quantifies the scheduling
overhead gap between the two engines.

Usage:
    python benchmarks/scripts/run_baseline_comparison.py \\
        --vllm-url http://localhost:8000 \\
        --sglang-url http://localhost:8001 \\
        --model meta-llama/Llama-3.1-8B-Instruct \\
        --concurrency 1,10,50,100 \\
        --workload all
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

# Ensure profiling scripts are importable
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROFILING_SCRIPTS = _SCRIPT_DIR.parent.parent / "profiling" / "scripts"
sys.path.insert(0, str(_PROFILING_SCRIPTS))

from profiling_utils import (  # noqa: E402
    AsyncBenchmarkClient,
    GPUMetricsCollector,
    RequestGenerator,
    TimingContext,
    check_engine_health,
    compute_aggregate_stats,
    log_environment,
    prompts_to_requests,
)

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Head-to-head baseline comparison: vLLM vs SGLang",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--vllm-url",
        type=str,
        default="http://localhost:8000",
        help="vLLM server base URL",
    )
    parser.add_argument(
        "--sglang-url",
        type=str,
        default="http://localhost:8001",
        help="SGLang server base URL",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="meta-llama/Llama-3.1-8B-Instruct",
        help="Model name served by both engines",
    )
    parser.add_argument(
        "--concurrency",
        type=str,
        default="1,10,50,100",
        help="Comma-separated concurrency levels to sweep",
    )
    parser.add_argument(
        "--num-requests",
        type=int,
        default=200,
        help="Number of requests per (engine, concurrency) pair",
    )
    parser.add_argument(
        "--workload",
        type=str,
        default="all",
        choices=["sharegpt", "fixed", "mixed", "all"],
        help="Workload type(s) to run",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="benchmarks/results/baseline",
        help="Directory for output files",
    )
    parser.add_argument(
        "--warmup",
        type=int,
        default=10,
        help="Number of warmup requests before each run",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for reproducibility",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="Number of times to repeat each benchmark for statistical significance",
    )
    return parser.parse_args()


def generate_workload(
    workload_type: str, num_requests: int, seed: int
) -> list[dict[str, Any]]:
    """Generate benchmark requests for a given workload type.

    Args:
        workload_type: One of 'sharegpt', 'fixed', 'mixed'.
        num_requests: Number of requests to generate.
        seed: Random seed.

    Returns:
        List of request dicts with 'prompt' and 'max_tokens'.
    """
    gen = RequestGenerator(seed=seed)

    if workload_type == "sharegpt":
        prompts = gen.generate_sharegpt_batch(num_requests)
        return prompts_to_requests(prompts, max_tokens=256)
    elif workload_type == "fixed":
        return gen.generate_fixed_length(num_requests, input_len=512, output_len=256)
    elif workload_type == "mixed":
        # Cap at reasonable lengths for 8B model
        distribution = {1024: 0.4, 4096: 0.3, 8192: 0.2, 16384: 0.1}
        return gen.generate_mixed_length(num_requests, distribution)
    else:
        raise ValueError(f"Unknown workload type: {workload_type}")


async def warmup_engine(base_url: str, model: str, num_warmup: int) -> None:
    """Send warmup requests to an engine to stabilize performance.

    Args:
        base_url: Engine base URL.
        model: Model name.
        num_warmup: Number of warmup requests.
    """
    logger.info("Warming up engine at %s with %d requests...", base_url, num_warmup)
    client = AsyncBenchmarkClient(
        base_url=base_url,
        model_name=model,
        concurrency_level=min(num_warmup, 8),
        timeout_s=120,
    )
    warmup_requests = [
        {"prompt": f"Hello, this is warmup request {i}.", "max_tokens": 32}
        for i in range(num_warmup)
    ]
    results = await client.run(warmup_requests)
    successful = sum(1 for m in results.per_request_metrics if m.error is None)
    logger.info("Warmup complete: %d/%d successful", successful, num_warmup)


async def benchmark_engine(
    engine_name: str,
    base_url: str,
    model: str,
    concurrency: int,
    requests: list[dict[str, Any]],
    num_warmup: int,
    output_dir: Path,
    repeats: int = 1,
) -> dict[str, Any]:
    """Run a benchmark against a single engine at a specific concurrency.

    Args:
        engine_name: Human-readable engine name (e.g., 'vllm', 'sglang').
        base_url: Engine API URL.
        model: Model name.
        concurrency: Concurrency level.
        requests: Pre-generated request dicts.
        num_warmup: Number of warmup requests.
        output_dir: Directory for output files.
        repeats: Number of times to repeat the benchmark.

    Returns:
        Summary metrics dictionary.
    """
    logger.info(
        "--- Benchmarking %s at concurrency=%d (%d requests) ---",
        engine_name,
        concurrency,
        len(requests),
    )

    # Warmup
    await warmup_engine(base_url, model, num_warmup)

    run_results = []
    for run_idx in range(repeats):
        if repeats > 1:
            logger.info(
                "Run %d/%d at concurrency %d", run_idx + 1, repeats, concurrency
            )

        # Benchmark
        gpu_collector = GPUMetricsCollector(interval_ms=100)
        client = AsyncBenchmarkClient(
            base_url=base_url,
            model_name=model,
            concurrency_level=concurrency,
            timeout_s=300,
        )

        with TimingContext(f"{engine_name}_c{concurrency}_run{run_idx + 1}") as timer:
            results = await client.run(requests, gpu_collector=gpu_collector)

        file_suffix = (
            f"c{concurrency}" if repeats == 1 else f"c{concurrency}_run{run_idx + 1}"
        )

        # Save results
        csv_path = output_dir / f"{engine_name}_{file_suffix}.csv"
        results.to_csv(csv_path)

        if results.gpu_metrics_df is not None and not results.gpu_metrics_df.empty:
            gpu_path = output_dir / f"{engine_name}_{file_suffix}_gpu.csv"
            results.gpu_metrics_df.to_csv(gpu_path, index=False)

        summary = results.summary()
        summary["engine"] = engine_name
        summary["concurrency"] = concurrency
        summary["wall_time_ms"] = timer.elapsed_ms
        summary["model_id"] = model
        summary["model_short_name"] = model.split("/")[-1]

        if repeats > 1:
            summary_path = output_dir / f"{engine_name}_{file_suffix}_summary.json"
            with open(summary_path, "w") as f:
                json.dump(summary, f, indent=2)

        run_results.append(summary)

    agg_summary = compute_aggregate_stats(run_results)

    if repeats > 1:
        agg_path = output_dir / f"{engine_name}_c{concurrency}_stats.json"
        with open(agg_path, "w") as f:
            json.dump(agg_summary, f, indent=2)

    return agg_summary


def compute_comparison(
    vllm_summaries: list[dict[str, Any]],
    sglang_summaries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Compute head-to-head comparison metrics.

    Args:
        vllm_summaries: List of vLLM benchmark summaries.
        sglang_summaries: List of SGLang benchmark summaries.

    Returns:
        List of comparison dicts with gap percentages.
    """
    comparisons = []

    # Index SGLang summaries by concurrency
    sglang_by_conc: dict[int, dict[str, Any]] = {}
    for s in sglang_summaries:
        sglang_by_conc[s["concurrency"]] = s

    for vllm_s in vllm_summaries:
        conc = vllm_s["concurrency"]
        sglang_s = sglang_by_conc.get(conc)
        if sglang_s is None:
            continue

        vllm_tp = vllm_s.get("throughput_tok_per_sec", 0)
        sglang_tp = sglang_s.get("throughput_tok_per_sec", 0)

        throughput_gap_pct = 0.0
        if sglang_tp > 0:
            throughput_gap_pct = ((sglang_tp - vllm_tp) / sglang_tp) * 100

        comparisons.append(
            {
                "concurrency": conc,
                "vllm_throughput_tok_s": vllm_tp,
                "sglang_throughput_tok_s": sglang_tp,
                "throughput_gap_pct": throughput_gap_pct,
                "vllm_ttft_p50_ms": vllm_s.get("ttft_p50_ms", 0),
                "sglang_ttft_p50_ms": sglang_s.get("ttft_p50_ms", 0),
                "vllm_ttft_p99_ms": vllm_s.get("ttft_p99_ms", 0),
                "sglang_ttft_p99_ms": sglang_s.get("ttft_p99_ms", 0),
                "vllm_tpot_p50_ms": vllm_s.get("tpot_p50_ms", 0),
                "sglang_tpot_p50_ms": sglang_s.get("tpot_p50_ms", 0),
                "vllm_gpu_util_mean": vllm_s.get("gpu_utilization_mean", 0),
                "sglang_gpu_util_mean": sglang_s.get("gpu_utilization_mean", 0),
            }
        )

    return comparisons


def print_comparison_table(comparisons: list[dict[str, Any]]) -> None:
    """Print a formatted comparison table to the console.

    Args:
        comparisons: List of comparison dicts from compute_comparison.
    """
    header = (
        f"{'Conc':>6} | "
        f"{'vLLM tp':>10} | {'SGLang tp':>10} | {'Gap':>7} | "
        f"{'vLLM TTFT':>10} | {'SGL TTFT':>10} | "
        f"{'vLLM TPOT':>10} | {'SGL TPOT':>10} | "
        f"{'vLLM GPU':>9} | {'SGL GPU':>9}"
    )
    separator = "-" * len(header)

    logger.info("\n%s", separator)
    logger.info("Head-to-Head Comparison: vLLM vs SGLang")
    logger.info("%s", separator)
    logger.info("%s", header)
    logger.info(
        "%6s | %10s | %10s | %7s | %10s | %10s | %10s | %10s | %9s | %9s",
        "",
        "tok/s",
        "tok/s",
        "%",
        "p50 ms",
        "p50 ms",
        "p50 ms",
        "p50 ms",
        "%",
        "%",
    )
    logger.info("%s", separator)

    for comp in sorted(comparisons, key=lambda c: c["concurrency"]):
        logger.info(
            "%6d | %10.1f | %10.1f | %+6.1f%% | %10.1f | %10.1f | "
            "%10.1f | %10.1f | %8.1f%% | %8.1f%%",
            comp["concurrency"],
            comp["vllm_throughput_tok_s"],
            comp["sglang_throughput_tok_s"],
            comp["throughput_gap_pct"],
            comp["vllm_ttft_p50_ms"],
            comp["sglang_ttft_p50_ms"],
            comp["vllm_tpot_p50_ms"],
            comp["sglang_tpot_p50_ms"],
            comp["vllm_gpu_util_mean"],
            comp["sglang_gpu_util_mean"],
        )

    logger.info("%s", separator)


async def main() -> None:
    """Main entry point for baseline comparison benchmark."""
    args = parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info("=" * 70)
    logger.info("KVWarden Phase 1: Baseline Comparison — vLLM vs SGLang")
    logger.info("=" * 70)

    # Log environment
    env_info = log_environment(seed=args.seed)
    logger.info("Configuration:")
    logger.info("  vllm_url: %s", args.vllm_url)
    logger.info("  sglang_url: %s", args.sglang_url)
    logger.info("  model: %s", args.model)
    logger.info("  concurrency: %s", args.concurrency)
    logger.info("  num_requests: %d", args.num_requests)
    logger.info("  workload: %s", args.workload)
    logger.info("  warmup: %d", args.warmup)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save run configuration
    config = {
        "vllm_url": args.vllm_url,
        "sglang_url": args.sglang_url,
        "model": args.model,
        "concurrency": args.concurrency,
        "num_requests": args.num_requests,
        "workload": args.workload,
        "warmup": args.warmup,
        "seed": args.seed,
        "environment": env_info,
    }
    with open(output_dir / "run_config.json", "w") as f:
        json.dump(config, f, indent=2)

    # Check engine connectivity
    engines: dict[str, str] = {
        "vllm": args.vllm_url,
        "sglang": args.sglang_url,
    }

    available_engines: dict[str, str] = {}
    for name, url in engines.items():
        logger.info("Checking %s server at %s...", name, url)
        healthy = await check_engine_health(url)
        if healthy:
            available_engines[name] = url
        else:
            logger.warning(
                "%s server at %s is not reachable — will be skipped", name, url
            )

    if not available_engines:
        logger.error(
            "No engines are reachable. Start at least one engine:\n"
            "  docker compose -f docker/docker-compose.yml up"
        )
        sys.exit(1)

    logger.info("Available engines: %s", list(available_engines.keys()))

    # Parse concurrency levels
    concurrency_levels = [int(c.strip()) for c in args.concurrency.split(",")]

    # Determine workloads to run
    if args.workload == "all":
        workload_types = ["sharegpt", "fixed", "mixed"]
    else:
        workload_types = [args.workload]

    # Run benchmarks
    all_results: dict[str, dict[str, list[dict[str, Any]]]] = {}

    for workload_type in workload_types:
        logger.info(f"\n{'=' * 60}")
        logger.info("Workload: %s", workload_type)
        logger.info("=" * 60)

        # Generate workload (same for both engines)
        requests = generate_workload(workload_type, args.num_requests, args.seed)
        logger.info("Generated %d %s requests", len(requests), workload_type)

        workload_dir = output_dir / workload_type
        workload_dir.mkdir(parents=True, exist_ok=True)

        workload_results: dict[str, list[dict[str, Any]]] = {}

        for engine_name, engine_url in available_engines.items():
            engine_summaries: list[dict[str, Any]] = []

            for concurrency in concurrency_levels:
                try:
                    summary = await benchmark_engine(
                        engine_name=engine_name,
                        base_url=engine_url,
                        model=args.model,
                        concurrency=concurrency,
                        requests=requests,
                        num_warmup=args.warmup,
                        output_dir=workload_dir,
                        repeats=args.repeats,
                    )
                    summary["workload"] = workload_type
                    engine_summaries.append(summary)
                except Exception as exc:
                    logger.error(
                        "Benchmark failed for %s at concurrency=%d: %s",
                        engine_name,
                        concurrency,
                        exc,
                    )
                    engine_summaries.append(
                        {
                            "engine": engine_name,
                            "concurrency": concurrency,
                            "workload": workload_type,
                            "error": str(exc),
                        }
                    )

            workload_results[engine_name] = engine_summaries

        all_results[workload_type] = workload_results

        # Compute comparison if both engines available
        if "vllm" in workload_results and "sglang" in workload_results:
            comparisons = compute_comparison(
                workload_results["vllm"],
                workload_results["sglang"],
            )
            print_comparison_table(comparisons)

            # Save workload-level comparison
            comp_path = workload_dir / "comparison.json"
            with open(comp_path, "w") as f:
                json.dump(comparisons, f, indent=2)

    # Save overall comparison summary
    summary_path = output_dir / "comparison_summary.json"
    with open(summary_path, "w") as f:
        json.dump(all_results, f, indent=2, default=str)
    logger.info("Overall comparison summary saved to %s", summary_path)

    logger.info("\n" + "=" * 70)
    logger.info("Baseline comparison complete. Results in %s", output_dir)
    logger.info("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
