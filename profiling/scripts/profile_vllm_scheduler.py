"""Profile vLLM scheduling overhead.

Reproduces WukLab findings that vLLM scheduling consumes >50% of inference
time on fast models. Measures CPU scheduling time vs GPU execution time
at multiple concurrency levels.

Two profiling levels:
  Level A — External (black-box): Send requests via OpenAI-compatible API,
            collect throughput, TTFT, TPOT, GPU utilization.
  Level B — Internal (white-box): py-spy flame graphs + cProfile of
            scheduler hot paths.

Usage:
    python profiling/scripts/profile_vllm_scheduler.py \\
        --base-url http://localhost:8000 \\
        --model meta-llama/Llama-3.1-8B-Instruct \\
        --concurrency 1,8,32,64,128 \\
        --num-requests 200 \\
        --workload sharegpt
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# Ensure project root is importable
_SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPT_DIR))

from profiling_utils import (
    AsyncBenchmarkClient,
    BenchmarkResults,
    GPUMetricsCollector,
    RequestGenerator,
    TimingContext,
    check_engine_health,
    log_environment,
    prompts_to_requests,
    compute_aggregate_stats,
)

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Profile vLLM scheduling overhead",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--base-url",
        type=str,
        default="http://localhost:8000",
        help="vLLM server base URL",
    )
    parser.add_argument(
        "--model",
        type=str,
        default="meta-llama/Llama-3.1-8B-Instruct",
        help="Model name served by the vLLM instance",
    )
    parser.add_argument(
        "--concurrency",
        type=str,
        default="1,8,32,64,128",
        help="Comma-separated concurrency levels to sweep",
    )
    parser.add_argument(
        "--num-requests",
        type=int,
        default=200,
        help="Number of requests per concurrency level",
    )
    parser.add_argument(
        "--workload",
        type=str,
        default="sharegpt",
        choices=["sharegpt", "fixed", "mixed"],
        help="Workload type to profile",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="profiling/results/vllm",
        help="Directory for output files",
    )
    parser.add_argument(
        "--profile-internal",
        action="store_true",
        help="Enable internal profiling (py-spy flame graphs + cProfile)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=60,
        help="Duration in seconds for py-spy recording",
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
    """Generate benchmark requests based on workload type.

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
        distribution = {128: 0.4, 1024: 0.3, 4096: 0.2, 8192: 0.1}
        return gen.generate_mixed_length(num_requests, distribution)
    else:
        raise ValueError(f"Unknown workload type: {workload_type}")


async def run_external_profiling(
    base_url: str,
    model: str,
    concurrency_levels: list[int],
    requests: list[dict[str, Any]],
    output_dir: Path,
    repeats: int = 1,
) -> dict[str, Any]:
    """Level A: External black-box profiling via the OpenAI-compatible API.

    Args:
        base_url: vLLM server URL.
        model: Model name.
        concurrency_levels: List of concurrency levels to sweep.
        requests: Pre-generated request dicts.
        output_dir: Directory for output files.
        repeats: Number of times to run each concurrency level.

    Returns:
        Dictionary mapping concurrency level to summary metrics.
    """
    external_dir = output_dir / "external"
    external_dir.mkdir(parents=True, exist_ok=True)

    all_summaries: dict[str, Any] = {}

    for concurrency in concurrency_levels:
        logger.info("=== External profiling: concurrency=%d ===", concurrency)

        run_results = []
        for run_idx in range(repeats):
            if repeats > 1:
                logger.info("Run %d/%d at concurrency %d", run_idx + 1, repeats, concurrency)

            gpu_collector = GPUMetricsCollector(interval_ms=100)
            client = AsyncBenchmarkClient(
                base_url=base_url,
                model_name=model,
                concurrency_level=concurrency,
                timeout_s=300,
            )

            with TimingContext(f"external_c{concurrency}_run{run_idx+1}") as timer:
                results = await client.run(requests, gpu_collector=gpu_collector)

            file_suffix = f"c{concurrency}" if repeats == 1 else f"c{concurrency}_run{run_idx+1}"

            # Save per-request metrics
            results.to_csv(external_dir / f"requests_{file_suffix}.csv")

            # Save GPU metrics
            if results.gpu_metrics_df is not None and not results.gpu_metrics_df.empty:
                gpu_path = external_dir / f"gpu_metrics_{file_suffix}.csv"
                results.gpu_metrics_df.to_csv(gpu_path, index=False)

            summary = results.summary()
            summary["concurrency"] = concurrency
            summary["wall_time_ms"] = timer.elapsed_ms
            summary["model_id"] = model
            summary["model_short_name"] = model.split("/")[-1]
            
            if repeats > 1:
                summary_path = external_dir / f"summary_{file_suffix}.json"
                with open(summary_path, "w") as f:
                    json.dump(summary, f, indent=2)
            
            run_results.append(summary)

        agg_summary = compute_aggregate_stats(run_results)
        
        if repeats > 1:
            agg_path = external_dir / f"summary_c{concurrency}_stats.json"
            with open(agg_path, "w") as f:
                json.dump(agg_summary, f, indent=2)

        all_summaries[str(concurrency)] = agg_summary

        logger.info(
            "Concurrency %d: throughput=%.1f tok/s, TTFT_p50=%.1fms, "
            "TPOT_p50=%.1fms, GPU_util=%.1f%%",
            concurrency,
            agg_summary.get("throughput_tok_per_sec", 0),
            agg_summary.get("ttft_p50_ms", 0),
            agg_summary.get("tpot_p50_ms", 0),
            agg_summary.get("gpu_utilization_mean", 0),
        )

    # Save combined summary
    summary_path = external_dir / "summary.json"
    with open(summary_path, "w") as f:
        json.dump(all_summaries, f, indent=2)
    logger.info("External profiling summary saved to %s", summary_path)

    return all_summaries


def find_vllm_server_pid() -> int | None:
    """Find the PID of a running vLLM server process.

    Returns:
        PID of the vLLM server, or None if not found.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", "vllm.entrypoints"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split("\n")
            pid = int(pids[0])
            logger.info("Found vLLM server process: PID %d", pid)
            return pid
    except Exception as exc:
        logger.debug("pgrep failed: %s", exc)

    # Fallback: check via /proc
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True,
            text=True,
        )
        for line in result.stdout.split("\n"):
            if "vllm" in line and "entrypoints" in line:
                parts = line.split()
                if len(parts) > 1:
                    pid = int(parts[1])
                    logger.info("Found vLLM server via ps: PID %d", pid)
                    return pid
    except Exception as exc:
        logger.debug("ps fallback failed: %s", exc)

    return None


def run_pyspy_profiling(
    pid: int, duration: int, output_dir: Path
) -> Path | None:
    """Run py-spy to generate a flame graph of the target process.

    Args:
        pid: Target process PID.
        duration: Recording duration in seconds.
        output_dir: Directory for output files.

    Returns:
        Path to the generated flame graph, or None on failure.
    """
    if not shutil.which("py-spy"):
        logger.error(
            "py-spy not found in PATH. Install with: pip install py-spy"
        )
        return None

    internal_dir = output_dir / "internal"
    internal_dir.mkdir(parents=True, exist_ok=True)

    svg_path = internal_dir / "vllm_flamegraph.svg"
    speedscope_path = internal_dir / "vllm_flamegraph.speedscope.json"

    # Generate SVG flame graph
    logger.info("Recording py-spy flame graph for %ds (PID=%d)...", duration, pid)
    try:
        subprocess.run(
            [
                "py-spy",
                "record",
                "-o",
                str(svg_path),
                "--pid",
                str(pid),
                "--duration",
                str(duration),
                "--format",
                "flamegraph",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        logger.info("Flame graph (SVG) saved to %s", svg_path)
    except subprocess.CalledProcessError as exc:
        logger.error("py-spy SVG recording failed: %s\n%s", exc, exc.stderr)
        svg_path = None

    # Generate speedscope JSON
    try:
        subprocess.run(
            [
                "py-spy",
                "record",
                "-o",
                str(speedscope_path),
                "--pid",
                str(pid),
                "--duration",
                str(duration),
                "--format",
                "speedscope",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        logger.info("Flame graph (speedscope) saved to %s", speedscope_path)
    except subprocess.CalledProcessError as exc:
        logger.error("py-spy speedscope recording failed: %s\n%s", exc, exc.stderr)

    return svg_path


def run_cprofile_analysis(output_dir: Path) -> None:
    """Profile vLLM scheduler internals using cProfile.

    Attempts to import vLLM and profile key scheduler functions.
    Only works when vLLM is installed locally (not just in Docker).

    Args:
        output_dir: Directory for output files.
    """
    internal_dir = output_dir / "internal"
    internal_dir.mkdir(parents=True, exist_ok=True)

    try:
        import cProfile
        import pstats

        from vllm.core.scheduler import Scheduler  # type: ignore[import-untyped]

        logger.info("vLLM installed locally — running cProfile on scheduler hot path")

        profiler = cProfile.Profile()

        # Profile scheduler methods by wrapping them
        original_schedule = Scheduler.schedule

        call_count = 0
        total_schedule_time_ms = 0.0

        def profiled_schedule(self: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal call_count, total_schedule_time_ms
            start = time.perf_counter()
            profiler.enable()
            result = original_schedule(self, *args, **kwargs)
            profiler.disable()
            elapsed = (time.perf_counter() - start) * 1000
            total_schedule_time_ms += elapsed
            call_count += 1
            return result

        Scheduler.schedule = profiled_schedule  # type: ignore[assignment]

        logger.info(
            "Scheduler.schedule patched for profiling. "
            "Run workload to collect data, then check output."
        )

        # Save profiling results
        stats_path = internal_dir / "scheduler_cprofile.prof"
        profiler.dump_stats(str(stats_path))

        text_path = internal_dir / "scheduler_cprofile.txt"
        with open(text_path, "w") as f:
            stats = pstats.Stats(profiler, stream=f)
            stats.sort_stats("cumulative")
            stats.print_stats(50)

        logger.info("cProfile stats saved to %s", stats_path)
        logger.info("cProfile text saved to %s", text_path)

        # Restore original
        Scheduler.schedule = original_schedule  # type: ignore[assignment]

        # Summary
        cprofile_summary = {
            "schedule_call_count": call_count,
            "total_schedule_time_ms": total_schedule_time_ms,
            "avg_schedule_time_ms": (
                total_schedule_time_ms / call_count if call_count > 0 else 0
            ),
        }
        summary_path = internal_dir / "cprofile_summary.json"
        with open(summary_path, "w") as f:
            json.dump(cprofile_summary, f, indent=2)

    except ImportError:
        logger.info(
            "vLLM not installed locally — skipping cProfile analysis. "
            "This is expected when profiling a remote/Docker vLLM instance."
        )
    except Exception as exc:
        logger.error("cProfile analysis failed: %s", exc)


async def run_internal_profiling(
    base_url: str,
    model: str,
    requests: list[dict[str, Any]],
    output_dir: Path,
    duration: int,
) -> None:
    """Level B: Internal white-box profiling using py-spy and cProfile.

    Starts a load generator in the background, then attaches py-spy to the
    vLLM server process to capture flame graphs.

    Args:
        base_url: vLLM server URL.
        model: Model name.
        requests: Pre-generated request dicts.
        output_dir: Directory for output files.
        duration: py-spy recording duration in seconds.
    """
    logger.info("=== Internal profiling (py-spy + cProfile) ===")

    # Find vLLM server PID
    pid = find_vllm_server_pid()
    if pid is None:
        logger.warning(
            "Could not find vLLM server PID. Skipping py-spy profiling. "
            "Ensure vLLM is running locally (not just in Docker) or provide "
            "the PID manually."
        )
    else:
        # Start load generator in background
        client = AsyncBenchmarkClient(
            base_url=base_url,
            model_name=model,
            concurrency_level=32,
            timeout_s=300,
        )

        # Run py-spy and load gen concurrently
        async def background_load() -> None:
            """Generate load while py-spy is recording."""
            await client.run(requests)

        load_task = asyncio.create_task(background_load())

        # Give load generator a moment to start
        await asyncio.sleep(2)

        # Run py-spy (blocking)
        run_pyspy_profiling(pid, duration, output_dir)

        # Wait for load to finish
        await load_task

    # Attempt cProfile analysis
    run_cprofile_analysis(output_dir)


def print_results_table(summaries: dict[str, Any]) -> None:
    """Print a formatted results table to the console.

    Args:
        summaries: Dictionary mapping concurrency level to summary metrics.
    """
    header = (
        f"{'Concurrency':>12} | {'Throughput':>12} | {'TTFT p50':>10} | "
        f"{'TTFT p99':>10} | {'TPOT p50':>10} | {'GPU Util':>10} | "
        f"{'Success':>8}"
    )
    separator = "-" * len(header)

    logger.info("\n%s", separator)
    logger.info("vLLM Profiling Results")
    logger.info("%s", separator)
    logger.info("%s", header)
    logger.info("%s", separator)

    for conc_str, summary in sorted(summaries.items(), key=lambda x: int(x[0])):
        throughput = summary.get("throughput_tok_per_sec", 0)
        ttft_p50 = summary.get("ttft_p50_ms", 0)
        ttft_p99 = summary.get("ttft_p99_ms", 0)
        tpot_p50 = summary.get("tpot_p50_ms", 0)
        gpu_util = summary.get("gpu_utilization_mean", 0)
        success = summary.get("num_successful", 0)

        logger.info(
            "%12s | %10.1f t/s | %8.1f ms | %8.1f ms | %8.1f ms | "
            "%8.1f %% | %8d",
            conc_str,
            throughput,
            ttft_p50,
            ttft_p99,
            tpot_p50,
            gpu_util,
            success,
        )

    logger.info("%s", separator)


async def main() -> None:
    """Main entry point for vLLM scheduler profiling."""
    args = parse_args()

    # Configure logging
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info("=" * 60)
    logger.info("KVWarden Phase 1: vLLM Scheduler Profiling")
    logger.info("=" * 60)

    # Log environment and config
    env_info = log_environment(seed=args.seed)
    logger.info("Configuration:")
    logger.info("  base_url: %s", args.base_url)
    logger.info("  model: %s", args.model)
    logger.info("  concurrency: %s", args.concurrency)
    logger.info("  num_requests: %d", args.num_requests)
    logger.info("  workload: %s", args.workload)
    logger.info("  output_dir: %s", args.output_dir)
    logger.info("  profile_internal: %s", args.profile_internal)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Save run configuration
    config = {
        "base_url": args.base_url,
        "model": args.model,
        "concurrency": args.concurrency,
        "num_requests": args.num_requests,
        "workload": args.workload,
        "seed": args.seed,
        "environment": env_info,
    }
    with open(output_dir / "run_config.json", "w") as f:
        json.dump(config, f, indent=2)

    # Check engine connectivity
    logger.info("Checking vLLM server connectivity...")
    healthy = await check_engine_health(args.base_url)
    if not healthy:
        logger.error(
            "Cannot reach vLLM server at %s. "
            "Start the server first:\n"
            "  docker compose -f docker/docker-compose.yml up vllm-server\n"
            "  OR: python -m vllm.entrypoints.openai.api_server "
            "--model %s",
            args.base_url,
            args.model,
        )
        sys.exit(1)

    # Parse concurrency levels
    concurrency_levels = [int(c.strip()) for c in args.concurrency.split(",")]

    # Generate workload
    logger.info("Generating %s workload (%d requests)...", args.workload, args.num_requests)
    requests = generate_workload(args.workload, args.num_requests, args.seed)
    logger.info("Generated %d requests", len(requests))

    summaries = await run_external_profiling(
        base_url=args.base_url,
        model=args.model,
        concurrency_levels=concurrency_levels,
        requests=requests,
        output_dir=output_dir,
        repeats=args.repeats,
    )

    # Print results table
    print_results_table(summaries)

    # Level B: Internal profiling (optional)
    if args.profile_internal:
        await run_internal_profiling(
            base_url=args.base_url,
            model=args.model,
            requests=requests,
            output_dir=output_dir,
            duration=args.duration,
        )

    logger.info("=" * 60)
    logger.info("Profiling complete. Results saved to %s", output_dir)
    logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
