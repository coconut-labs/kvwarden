#!/usr/bin/env python3
"""Background GPU metrics collector for KVWarden experiments.

Polls GPU metrics via pynvml every 500ms and writes incrementally to CSV.
Designed to run as a background process during profiling runs.

Usage:
    python scripts/gpu_monitor.py --output gpu_metrics.csv
    python scripts/gpu_monitor.py --output gpu_metrics.csv --interval-ms 200 --gpu-ids 0,1

    # Background usage (from run_all_baselines.sh):
    python scripts/gpu_monitor.py --output metrics.csv &
    GPU_MON_PID=$!
    # ... run workload ...
    kill -TERM $GPU_MON_PID
"""

from __future__ import annotations

import argparse
import csv
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Globals for signal handling
# ---------------------------------------------------------------------------

_shutdown_requested = False
_csv_writer: csv.writer | None = None
_csv_file: Any = None
_buffer: list[list[Any]] = []
_flush_interval = 100  # Flush every N rows


def _handle_signal(signum: int, frame: Any) -> None:
    """Handle SIGTERM/SIGINT for graceful shutdown."""
    global _shutdown_requested
    _shutdown_requested = True


# ---------------------------------------------------------------------------
# GPU Metrics Collection
# ---------------------------------------------------------------------------

CSV_HEADERS = [
    "timestamp",
    "gpu_id",
    "utilization_pct",
    "memory_used_mb",
    "memory_total_mb",
    "power_draw_w",
    "sm_clock_mhz",
    "temperature_c",
]


def init_nvml() -> tuple[Any, list[tuple[int, Any]]]:
    """Initialize NVML and get device handles.

    Returns:
        Tuple of (pynvml module, list of (gpu_id, handle) pairs).

    Raises:
        SystemExit: If no GPU hardware is detected.
    """
    try:
        import pynvml
    except ImportError:
        print("ERROR: pynvml (nvidia-ml-py) not installed.", file=sys.stderr)
        print("Install with: pip install nvidia-ml-py>=12.0", file=sys.stderr)
        sys.exit(1)

    try:
        pynvml.nvmlInit()
    except pynvml.NVMLError as exc:
        print(f"ERROR: Cannot initialize NVML: {exc}", file=sys.stderr)
        print("No GPU hardware detected. Exiting cleanly.", file=sys.stderr)
        sys.exit(0)

    return pynvml, []


def get_handles(
    pynvml: Any, gpu_ids: list[int] | None
) -> list[tuple[int, Any]]:
    """Get NVML device handles for specified GPUs.

    Args:
        pynvml: The pynvml module.
        gpu_ids: GPU indices to monitor, or None for all.

    Returns:
        List of (gpu_id, nvml_handle) tuples.
    """
    device_count = pynvml.nvmlDeviceGetCount()
    if gpu_ids is None:
        gpu_ids = list(range(device_count))

    handles = []
    for idx in gpu_ids:
        if idx >= device_count:
            print(
                f"WARNING: GPU {idx} not found (only {device_count} GPUs available)",
                file=sys.stderr,
            )
            continue
        handle = pynvml.nvmlDeviceGetHandleByIndex(idx)
        handles.append((idx, handle))

    return handles


def sample_gpu(pynvml: Any, gpu_id: int, handle: Any) -> list[Any]:
    """Take a single GPU metrics sample.

    Args:
        pynvml: The pynvml module.
        gpu_id: GPU index.
        handle: NVML device handle.

    Returns:
        List of metric values matching CSV_HEADERS order.
    """
    ts = time.time()

    try:
        util = pynvml.nvmlDeviceGetUtilizationRates(handle)
        utilization_pct = float(util.gpu)
    except pynvml.NVMLError:
        utilization_pct = -1.0

    try:
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        mem_used_mb = mem.used / (1024 * 1024)
        mem_total_mb = mem.total / (1024 * 1024)
    except pynvml.NVMLError:
        mem_used_mb = -1.0
        mem_total_mb = -1.0

    try:
        power_mw = pynvml.nvmlDeviceGetPowerUsage(handle)
        power_w = power_mw / 1000.0
    except pynvml.NVMLError:
        power_w = -1.0

    try:
        sm_clock = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_SM)
    except pynvml.NVMLError:
        sm_clock = -1

    try:
        temp = pynvml.nvmlDeviceGetTemperature(
            handle, pynvml.NVML_TEMPERATURE_GPU
        )
    except pynvml.NVMLError:
        temp = -1

    return [
        ts,
        gpu_id,
        utilization_pct,
        round(mem_used_mb, 1),
        round(mem_total_mb, 1),
        round(power_w, 1),
        sm_clock,
        temp,
    ]


# ---------------------------------------------------------------------------
# Main collection loop
# ---------------------------------------------------------------------------


def run_collector(
    output_path: Path,
    interval_ms: int,
    gpu_ids: list[int] | None,
) -> None:
    """Main collection loop. Runs until SIGTERM/SIGINT.

    Args:
        output_path: Path to output CSV file.
        interval_ms: Polling interval in milliseconds.
        gpu_ids: GPU indices to monitor, or None for all.
    """
    global _csv_writer, _csv_file, _buffer

    pynvml_mod, _ = init_nvml()
    handles = get_handles(pynvml_mod, gpu_ids)

    if not handles:
        print("ERROR: No valid GPU handles. Exiting.", file=sys.stderr)
        pynvml_mod.nvmlShutdown()
        sys.exit(1)

    # Log what we're monitoring
    for gpu_id, handle in handles:
        name = pynvml_mod.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode("utf-8")
        print(f"Monitoring GPU {gpu_id}: {name}")

    interval_s = interval_ms / 1000.0

    # Open CSV file
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _csv_file = open(output_path, "w", newline="")
    _csv_writer = csv.writer(_csv_file)
    _csv_writer.writerow(CSV_HEADERS)
    _csv_file.flush()

    print(
        f"Collecting GPU metrics every {interval_ms}ms → {output_path}"
    )
    print("Send SIGTERM or Ctrl+C to stop.")

    sample_count = 0

    try:
        while not _shutdown_requested:
            rows = []
            for gpu_id, handle in handles:
                row = sample_gpu(pynvml_mod, gpu_id, handle)
                rows.append(row)
                _buffer.append(row)
                sample_count += 1

            if len(handles) > 1 and rows:
                # Add aggregate row computing sum for memory/power and average for util/temperature
                ts = rows[0][0]
                avg_util = sum(r[2] for r in rows if r[2] >= 0) / len(rows)
                sum_mem_used = sum(r[3] for r in rows if r[3] >= 0)
                sum_mem_tot = sum(r[4] for r in rows if r[4] >= 0)
                sum_pwr = sum(r[5] for r in rows if r[5] >= 0)
                avg_clk = sum(r[6] for r in rows if r[6] >= 0) / len(rows)
                avg_temp = sum(r[7] for r in rows if r[7] >= 0) / len(rows)
                
                agg_row = [ts, "all", round(avg_util, 1), round(sum_mem_used, 1), 
                           round(sum_mem_tot, 1), round(sum_pwr, 1), int(avg_clk), int(avg_temp)]
                _buffer.append(agg_row)
                sample_count += 1

            # Flush buffer periodically
            if len(_buffer) >= _flush_interval:
                _csv_writer.writerows(_buffer)
                _csv_file.flush()
                _buffer.clear()

            time.sleep(interval_s)

    except KeyboardInterrupt:
        pass  # Fall through to cleanup

    # Final flush
    if _buffer and _csv_writer is not None:
        _csv_writer.writerows(_buffer)
        _buffer.clear()

    if _csv_file is not None:
        _csv_file.flush()
        _csv_file.close()

    pynvml_mod.nvmlShutdown()
    print(f"GPU monitor stopped. {sample_count} samples written to {output_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Background GPU metrics collector for KVWarden experiments",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--output",
        type=str,
        default="gpu_metrics.csv",
        help="Output CSV file path",
    )
    parser.add_argument(
        "--interval-ms",
        type=int,
        default=500,
        help="Polling interval in milliseconds",
    )
    parser.add_argument(
        "--gpu-ids",
        type=str,
        default=None,
        help="Comma-separated GPU indices to monitor (default: all)",
    )
    return parser.parse_args()


def main() -> None:
    """Entry point."""
    args = parse_args()

    # Register signal handlers
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    gpu_ids: list[int] | None = None
    if args.gpu_ids is not None:
        gpu_ids = [int(x.strip()) for x in args.gpu_ids.split(",")]

    run_collector(
        output_path=Path(args.output),
        interval_ms=args.interval_ms,
        gpu_ids=gpu_ids,
    )


if __name__ == "__main__":
    main()
