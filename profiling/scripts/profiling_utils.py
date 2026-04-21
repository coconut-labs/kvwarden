"""Shared profiling infrastructure for KVWarden benchmarks.

Provides GPU metrics collection, request generation, async benchmarking,
and timing utilities used by all profiling and benchmark scripts.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import random
import sys
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Generator

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def count_tokens(text: str, model_id: str = "meta-llama/Llama-3.1-8B-Instruct") -> int:
    """Count tokens using the model's actual tokenizer.
    
    Falls back to word-count approximation (×1.3) if tokenizer unavailable.
    Caches the tokenizer after first load.
    """
    try:
        from transformers import AutoTokenizer
        if not hasattr(count_tokens, "_tokenizer"):
            count_tokens._tokenizer = AutoTokenizer.from_pretrained(model_id)
        return len(count_tokens._tokenizer.encode(text))
    except ImportError:
        # transformers not installed — use approximation
        logging.getLogger(__name__).warning(
            "transformers not installed, using word-count approximation for token counting"
        )
        return int(len(text.split()) * 1.3)


def compute_aggregate_stats(run_results: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute aggregate statistics across multiple benchmark runs."""
    if not run_results:
        return {}

    base = run_results[0].copy()
    base["repeats"] = len(run_results)
    
    metrics = [
        "throughput_tok_per_sec",
        "ttft_p50_ms",
        "ttft_p99_ms",
        "tpot_p50_ms",
        "gpu_utilization_mean",
        "num_successful",
    ]

    for metric in metrics:
        vals = [r.get(metric) for r in run_results if r.get(metric) is not None]
        if vals:
            base[f"{metric}_mean"] = float(np.mean(vals))
            base[f"{metric}_std"] = float(np.std(vals))
            # Override the base metric with the mean for compatibility with single-run code
            base[metric] = base[f"{metric}_mean"]

    base["runs"] = run_results
    return base


# ---------------------------------------------------------------------------
# Environment Info
# ---------------------------------------------------------------------------


def get_environment_info() -> dict[str, str]:
    """Collect environment metadata for reproducibility.

    Returns:
        Dictionary with Python version, platform, GPU driver info, etc.
    """
    info: dict[str, str] = {
        "python_version": sys.version,
        "platform": platform.platform(),
        "processor": platform.processor() or "unknown",
    }

    try:
        import pynvml

        pynvml.nvmlInit()
        driver = pynvml.nvmlSystemGetDriverVersion()
        device_count = pynvml.nvmlDeviceGetCount()
        info["nvidia_driver"] = driver
        info["gpu_count"] = str(device_count)
        for i in range(device_count):
            handle = pynvml.nvmlDeviceGetHandleByIndex(i)
            name = pynvml.nvmlDeviceGetName(handle)
            if isinstance(name, bytes):
                name = name.decode("utf-8")
            mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
            info[f"gpu_{i}_name"] = name
            info[f"gpu_{i}_memory_mb"] = str(mem_info.total // (1024 * 1024))
        pynvml.nvmlShutdown()
    except Exception as exc:
        info["gpu_error"] = str(exc)

    return info


def log_environment(seed: int | None = None) -> dict[str, str]:
    """Log environment info and optional random seed.

    Args:
        seed: Random seed used for reproducibility.

    Returns:
        The environment info dictionary.
    """
    env = get_environment_info()
    logger.info("Environment info:")
    for key, value in env.items():
        logger.info("  %s: %s", key, value)
    if seed is not None:
        logger.info("  random_seed: %d", seed)
    return env


# ---------------------------------------------------------------------------
# GPU Metrics Collector
# ---------------------------------------------------------------------------


@dataclass
class GPUSample:
    """A single GPU metrics sample."""

    timestamp: float
    gpu_index: int | str
    utilization_pct: float
    memory_used_mb: float
    memory_total_mb: float
    power_draw_w: float
    sm_clock_mhz: int


class GPUMetricsCollector:
    """Collects GPU metrics in a background thread using pynvml.

    Args:
        gpu_indices: GPU indices to monitor. None means all GPUs.
        interval_ms: Polling interval in milliseconds.
    """

    def __init__(
        self,
        gpu_indices: list[int] | None = None,
        interval_ms: int = 100,
    ) -> None:
        self._interval_s = interval_ms / 1000.0
        self._gpu_indices = gpu_indices
        self._samples: list[GPUSample] = []
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._nvml_available = False

        try:
            import pynvml

            pynvml.nvmlInit()
            self._nvml_available = True
            device_count = pynvml.nvmlDeviceGetCount()
            if self._gpu_indices is None:
                self._gpu_indices = list(range(device_count))
            pynvml.nvmlShutdown()
        except Exception as exc:
            logger.error(
                "pynvml NOT available — GPU metrics WILL BE MISSING from results. "
                "Install with: pip install nvidia-ml-py>=12.0. Error: %s", exc
            )
            self._nvml_available = False
            self._gpu_indices = self._gpu_indices or []

    def start(self) -> None:
        """Start background metrics collection."""
        if self._thread is not None:
            logger.warning("GPUMetricsCollector already running")
            return

        self._stop_event.clear()
        self._samples.clear()
        self._thread = threading.Thread(target=self._collect_loop, daemon=True)
        self._thread.start()
        logger.info("GPU metrics collection started (interval=%dms)", int(self._interval_s * 1000))

    def stop(self) -> None:
        """Stop background metrics collection."""
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=5.0)
        self._thread = None
        logger.info("GPU metrics collection stopped (%d samples)", len(self._samples))

    def _collect_loop(self) -> None:
        """Background loop that polls GPU metrics."""
        if not self._nvml_available:
            return

        import pynvml

        pynvml.nvmlInit()
        handles = []
        for idx in self._gpu_indices:  # type: ignore[union-attr]
            try:
                handles.append((idx, pynvml.nvmlDeviceGetHandleByIndex(idx)))
            except Exception as exc:
                logger.warning("Cannot get handle for GPU %d: %s", idx, exc)

        while not self._stop_event.is_set():
            ts = time.time()
            rows = []
            for gpu_idx, handle in handles:
                try:
                    util = pynvml.nvmlDeviceGetUtilizationRates(handle)
                    mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                    try:
                        power = pynvml.nvmlDeviceGetPowerUsage(handle) / 1000.0  # mW -> W
                    except pynvml.NVMLError:
                        power = 0.0
                    try:
                        clock = pynvml.nvmlDeviceGetClockInfo(handle, pynvml.NVML_CLOCK_SM)
                    except pynvml.NVMLError:
                        clock = 0

                    sample = GPUSample(
                        timestamp=ts,
                        gpu_index=gpu_idx,
                        utilization_pct=float(util.gpu),
                        memory_used_mb=mem.used / (1024 * 1024),
                        memory_total_mb=mem.total / (1024 * 1024),
                        power_draw_w=power,
                        sm_clock_mhz=clock,
                    )
                    self._samples.append(sample)
                    rows.append(sample)
                except Exception as exc:
                    logger.debug("Error sampling GPU %d: %s", gpu_idx, exc)

            if len(handles) > 1 and rows:
                avg_util = sum(r.utilization_pct for r in rows if r.utilization_pct >= 0) / len(rows)
                sum_mem_used = sum(r.memory_used_mb for r in rows if r.memory_used_mb >= 0)
                sum_mem_tot = sum(r.memory_total_mb for r in rows if r.memory_total_mb >= 0)
                sum_pwr = sum(r.power_draw_w for r in rows if r.power_draw_w >= 0)
                avg_clk = sum(r.sm_clock_mhz for r in rows if r.sm_clock_mhz >= 0) / len(rows)
                
                self._samples.append(
                    GPUSample(
                        timestamp=ts,
                        gpu_index="all",
                        utilization_pct=avg_util,
                        memory_used_mb=sum_mem_used,
                        memory_total_mb=sum_mem_tot,
                        power_draw_w=sum_pwr,
                        sm_clock_mhz=int(avg_clk),
                    )
                )

            self._stop_event.wait(self._interval_s)

        pynvml.nvmlShutdown()

    def to_dataframe(self) -> pd.DataFrame:
        """Convert collected samples to a pandas DataFrame.

        Returns:
            DataFrame with columns matching GPUSample fields.
        """
        if not self._samples:
            return pd.DataFrame(
                columns=[
                    "timestamp", "gpu_index", "utilization_pct",
                    "memory_used_mb", "memory_total_mb", "power_draw_w",
                    "sm_clock_mhz",
                ]
            )
        records = [
            {
                "timestamp": s.timestamp,
                "gpu_index": s.gpu_index,
                "utilization_pct": s.utilization_pct,
                "memory_used_mb": s.memory_used_mb,
                "memory_total_mb": s.memory_total_mb,
                "power_draw_w": s.power_draw_w,
                "sm_clock_mhz": s.sm_clock_mhz,
            }
            for s in self._samples
        ]
        return pd.DataFrame(records)

    def to_csv(self, path: str | Path) -> None:
        """Save collected samples to a CSV file.

        Args:
            path: Output file path.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.to_dataframe().to_csv(path, index=False)
        logger.info("GPU metrics saved to %s", path)


# ---------------------------------------------------------------------------
# Request Generator
# ---------------------------------------------------------------------------


class RequestGenerator:
    """Generates benchmark request prompts from ShareGPT or synthetic workloads.

    Args:
        seed: Random seed for reproducibility.
    """

    def __init__(self, seed: int = 42) -> None:
        self._seed = seed
        self._rng = random.Random(seed)
        self._sharegpt_data: list[dict[str, Any]] | None = None

    def _load_sharegpt(self) -> list[dict[str, Any]]:
        """Load and cache the ShareGPT dataset from HuggingFace.

        Returns:
            List of conversation dicts from the dataset.
        """
        if self._sharegpt_data is not None:
            return self._sharegpt_data

        try:
            from datasets import load_dataset

            logger.info("Loading ShareGPT dataset from HuggingFace...")
            ds = load_dataset(
                "anon8231489123/ShareGPT_Vicuna_unfiltered",
                split="train",
            )
            self._sharegpt_data = list(ds)  # type: ignore[arg-type]
            logger.info("Loaded %d ShareGPT conversations", len(self._sharegpt_data))
            return self._sharegpt_data
        except Exception as exc:
            logger.error(
                "Could not load ShareGPT dataset: %s. "
                "USING SYNTHETIC FALLBACK — results will NOT reflect real workload distributions. "
                "Install with: pip install datasets", exc
            )
            # Fallback: generate plausible prompts with variable length
            self._sharegpt_data = [
                {"conversations": [{"from": "human", "value": f"Tell me about topic {i} in detail. " * self._rng.randint(1, 20)}]}
                for i in range(1000)
            ]
            return self._sharegpt_data

    def generate_sharegpt_batch(self, n: int) -> list[str]:
        """Generate a batch of prompts from the ShareGPT dataset.

        Args:
            n: Number of prompts to generate.

        Returns:
            List of prompt strings extracted from human turns.
        """
        data = self._load_sharegpt()
        sampled = self._rng.choices(data, k=n)
        prompts: list[str] = []
        for item in sampled:
            conversations = item.get("conversations", [])
            # Find first human message
            for turn in conversations:
                if turn.get("from") == "human" and turn.get("value"):
                    prompts.append(turn["value"])
                    break
            else:
                prompts.append("Explain the concept of machine learning.")
        return prompts

    def generate_fixed_length(
        self, n: int, input_len: int, output_len: int
    ) -> list[dict[str, Any]]:
        """Generate fixed-length synthetic prompts.

        Args:
            n: Number of prompts.
            input_len: Approximate input token count.
            output_len: Desired max output tokens.

        Returns:
            List of dicts with 'prompt' and 'max_tokens' keys.
        """
        base_words = [
            "the", "a", "of", "to", "and", "in", "is", "for", "that", "with",
            "on", "as", "it", "by", "from", "at", "an", "be", "this", "which",
            "or", "are", "was", "but", "not", "have", "had", "has", "its", "all",
        ]
        requests: list[dict[str, Any]] = []
        # Approximate 1 token ≈ 0.75 words
        word_count = int(input_len * 0.75)
        for _ in range(n):
            words = self._rng.choices(base_words, k=word_count)
            prompt = (
                "Please read the following text and provide a detailed analysis:\n\n"
                + " ".join(words)
                + "\n\nProvide your analysis:"
            )
            requests.append({"prompt": prompt, "max_tokens": output_len})
        return requests

    def generate_mixed_length(
        self, n: int, length_distribution: dict[int, float]
    ) -> list[dict[str, Any]]:
        """Generate prompts with a specified length distribution.

        Args:
            n: Total number of prompts.
            length_distribution: Mapping of input_length -> probability.
                Example: {128: 0.4, 1024: 0.3, 4096: 0.2, 8192: 0.1}

        Returns:
            List of dicts with 'prompt' and 'max_tokens' keys.
        """
        lengths = list(length_distribution.keys())
        weights = list(length_distribution.values())
        # Normalize weights
        total = sum(weights)
        weights = [w / total for w in weights]

        requests: list[dict[str, Any]] = []
        for _ in range(n):
            input_len = self._rng.choices(lengths, weights=weights, k=1)[0]
            # Output length scales roughly with input length, capped at 512
            output_len = min(input_len // 2, 512)
            output_len = max(output_len, 64)
            batch = self.generate_fixed_length(1, input_len, output_len)
            requests.extend(batch)
        return requests


# ---------------------------------------------------------------------------
# Async Benchmark Client
# ---------------------------------------------------------------------------


@dataclass
class RequestMetrics:
    """Per-request timing metrics."""

    request_id: int
    timestamp: float
    ttft_ms: float  # Time to first token
    tpot_ms: float  # Time per output token
    total_latency_ms: float
    tokens_in: int
    tokens_out: int
    error: str | None = None


@dataclass
class BenchmarkResults:
    """Aggregated benchmark results.

    Attributes:
        per_request_metrics: Individual request measurements.
        config: Configuration used for the benchmark run.
        gpu_metrics_df: Optional GPU metrics DataFrame.
    """

    per_request_metrics: list[RequestMetrics]
    config: dict[str, Any] = field(default_factory=dict)
    gpu_metrics_df: pd.DataFrame | None = None

    def to_dataframe(self) -> pd.DataFrame:
        """Convert per-request metrics to a DataFrame.

        Returns:
            DataFrame with one row per request.
        """
        records = []
        for m in self.per_request_metrics:
            records.append(
                {
                    "request_id": m.request_id,
                    "timestamp": m.timestamp,
                    "ttft_ms": m.ttft_ms,
                    "tpot_ms": m.tpot_ms,
                    "total_latency_ms": m.total_latency_ms,
                    "tokens_in": m.tokens_in,
                    "tokens_out": m.tokens_out,
                    "error": m.error,
                }
            )
        return pd.DataFrame(records)

    def to_csv(self, path: str | Path) -> None:
        """Save per-request metrics to CSV.

        Args:
            path: Output file path.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.to_dataframe().to_csv(path, index=False)
        logger.info("Benchmark results saved to %s (%d requests)", path, len(self.per_request_metrics))

    def summary(self) -> dict[str, float]:
        """Compute aggregate statistics.

        Returns:
            Dictionary with p50/p95/p99 for TTFT and TPOT, throughput, etc.
        """
        successful = [m for m in self.per_request_metrics if m.error is None]
        if not successful:
            return {"error": "no successful requests"}

        ttft_vals = np.array([m.ttft_ms for m in successful])
        tpot_vals = np.array([m.tpot_ms for m in successful if m.tpot_ms > 0])
        latency_vals = np.array([m.total_latency_ms for m in successful])
        total_tokens_out = sum(m.tokens_out for m in successful)
        total_duration_s = (
            max(m.timestamp + m.total_latency_ms / 1000.0 for m in successful)
            - min(m.timestamp for m in successful)
        )

        result: dict[str, float] = {
            "num_requests": len(self.per_request_metrics),
            "num_successful": len(successful),
            "num_failed": len(self.per_request_metrics) - len(successful),
            "ttft_p50_ms": float(np.percentile(ttft_vals, 50)),
            "ttft_p95_ms": float(np.percentile(ttft_vals, 95)),
            "ttft_p99_ms": float(np.percentile(ttft_vals, 99)),
            "total_latency_p50_ms": float(np.percentile(latency_vals, 50)),
            "total_latency_p95_ms": float(np.percentile(latency_vals, 95)),
            "total_latency_p99_ms": float(np.percentile(latency_vals, 99)),
        }

        if len(tpot_vals) > 0:
            result["tpot_p50_ms"] = float(np.percentile(tpot_vals, 50))
            result["tpot_p95_ms"] = float(np.percentile(tpot_vals, 95))
            result["tpot_p99_ms"] = float(np.percentile(tpot_vals, 99))

        if total_duration_s > 0:
            result["throughput_tok_per_sec"] = total_tokens_out / total_duration_s

        # GPU utilization from collector if available
        if self.gpu_metrics_df is not None and not self.gpu_metrics_df.empty:
            df_to_use = self.gpu_metrics_df
            if "gpu_index" in df_to_use.columns:
                mask = df_to_use["gpu_index"] == "all"
                if mask.any():
                    df_to_use = df_to_use[mask]
            
            result["gpu_utilization_mean"] = float(
                df_to_use["utilization_pct"].mean()
            )
            result["gpu_utilization_p50"] = float(
                df_to_use["utilization_pct"].median()
            )

        return result


class AsyncBenchmarkClient:
    """Async HTTP client for benchmarking OpenAI-compatible LLM endpoints.

    Sends concurrent requests and collects per-request timing metrics
    including TTFT (time to first token) and TPOT (time per output token).

    Args:
        base_url: Server base URL (e.g., http://localhost:8000).
        model_name: Model identifier for the API.
        concurrency_level: Maximum number of concurrent requests.
        timeout_s: Per-request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        model_name: str,
        concurrency_level: int = 32,
        timeout_s: int = 300,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name
        self.concurrency_level = concurrency_level
        self.timeout_s = timeout_s

    async def _send_request(
        self,
        session: Any,
        request_id: int,
        prompt: str,
        max_tokens: int,
        semaphore: asyncio.Semaphore,
    ) -> RequestMetrics:
        """Send a single streaming request and measure timing.

        Args:
            session: aiohttp ClientSession.
            request_id: Unique request identifier.
            prompt: The prompt string.
            max_tokens: Maximum tokens to generate.
            semaphore: Concurrency limiter.

        Returns:
            RequestMetrics with timing data.
        """
        import aiohttp

        url = f"{self.base_url}/v1/completions"
        payload = {
            "model": self.model_name,
            "prompt": prompt,
            "max_tokens": max_tokens,
            "stream": True,
        }

        async with semaphore:
            start_time = time.time()
            first_token_time: float | None = None
            tokens_out = 0

            try:
                timeout = aiohttp.ClientTimeout(total=self.timeout_s)
                async with session.post(url, json=payload, timeout=timeout) as resp:
                    if resp.status != 200:
                        error_body = await resp.text()
                        return RequestMetrics(
                            request_id=request_id,
                            timestamp=start_time,
                            ttft_ms=0.0,
                            tpot_ms=0.0,
                            total_latency_ms=(time.time() - start_time) * 1000,
                            tokens_in=count_tokens(prompt, self.model_name),
                            tokens_out=0,
                            error=f"HTTP {resp.status}: {error_body[:200]}",
                        )

                    async for line in resp.content:
                        decoded = line.decode("utf-8").strip()
                        if not decoded or not decoded.startswith("data: "):
                            continue
                        data_str = decoded[6:]  # Remove "data: " prefix
                        if data_str == "[DONE]":
                            break

                        if first_token_time is None:
                            first_token_time = time.time()

                        # Count tokens from the streaming response
                        try:
                            import json

                            chunk = json.loads(data_str)
                            choices = chunk.get("choices", [])
                            if choices:
                                text = choices[0].get("text", "")
                                # Approximate token count from text
                                if text:
                                    tokens_out += 1
                        except (json.JSONDecodeError, KeyError):
                            tokens_out += 1

            except asyncio.TimeoutError:
                return RequestMetrics(
                    request_id=request_id,
                    timestamp=start_time,
                    ttft_ms=0.0,
                    tpot_ms=0.0,
                    total_latency_ms=(time.time() - start_time) * 1000,
                    tokens_in=count_tokens(prompt, self.model_name),
                    tokens_out=0,
                    error="timeout",
                )
            except Exception as exc:
                return RequestMetrics(
                    request_id=request_id,
                    timestamp=start_time,
                    ttft_ms=0.0,
                    tpot_ms=0.0,
                    total_latency_ms=(time.time() - start_time) * 1000,
                    tokens_in=count_tokens(prompt, self.model_name),
                    tokens_out=0,
                    error=str(exc),
                )

            end_time = time.time()
            total_latency_ms = (end_time - start_time) * 1000

            if first_token_time is not None:
                ttft_ms = (first_token_time - start_time) * 1000
            else:
                ttft_ms = total_latency_ms

            if tokens_out > 1 and first_token_time is not None:
                generation_time_ms = (end_time - first_token_time) * 1000
                tpot_ms = generation_time_ms / (tokens_out - 1)
            else:
                tpot_ms = 0.0

            return RequestMetrics(
                request_id=request_id,
                timestamp=start_time,
                ttft_ms=ttft_ms,
                tpot_ms=tpot_ms,
                total_latency_ms=total_latency_ms,
                tokens_in=count_tokens(prompt, self.model_name),
                tokens_out=tokens_out,
            )

    async def run(
        self,
        requests: list[dict[str, Any]],
        gpu_collector: GPUMetricsCollector | None = None,
    ) -> BenchmarkResults:
        """Execute all benchmark requests concurrently.

        Args:
            requests: List of dicts, each with 'prompt' and optionally 'max_tokens'.
            gpu_collector: Optional GPU metrics collector to run during benchmark.

        Returns:
            BenchmarkResults with per-request and aggregate data.
        """
        import aiohttp

        semaphore = asyncio.Semaphore(self.concurrency_level)

        if gpu_collector is not None:
            gpu_collector.start()

        logger.info(
            "Starting benchmark: %d requests, concurrency=%d, target=%s",
            len(requests),
            self.concurrency_level,
            self.base_url,
        )

        async with aiohttp.ClientSession() as session:
            tasks = []
            for i, req in enumerate(requests):
                prompt = req.get("prompt", req) if isinstance(req, dict) else str(req)
                max_tokens = req.get("max_tokens", 256) if isinstance(req, dict) else 256
                tasks.append(
                    self._send_request(session, i, prompt, max_tokens, semaphore)
                )
            metrics = await asyncio.gather(*tasks)

        if gpu_collector is not None:
            gpu_collector.stop()

        gpu_df = gpu_collector.to_dataframe() if gpu_collector is not None else None

        results = BenchmarkResults(
            per_request_metrics=list(metrics),
            config={
                "base_url": self.base_url,
                "model_name": self.model_name,
                "concurrency_level": self.concurrency_level,
                "num_requests": len(requests),
            },
            gpu_metrics_df=gpu_df,
        )

        summary = results.summary()
        logger.info("Benchmark complete. Summary: %s", summary)
        return results


# ---------------------------------------------------------------------------
# Timing Context Manager
# ---------------------------------------------------------------------------


class TimingContext:
    """Simple wall-clock timer context manager.

    Supports nesting for hierarchical timing.

    Args:
        label: Human-readable label for this timing block.

    Example:
        >>> with TimingContext("scheduling") as t:
        ...     do_work()
        >>> print(f"{t.label}: {t.elapsed_ms:.1f}ms")
    """

    _stack: list[TimingContext] = []

    def __init__(self, label: str) -> None:
        self.label = label
        self.elapsed_ms: float = 0.0
        self._start_time: float = 0.0
        self.parent: TimingContext | None = None
        self.children: list[TimingContext] = []

    def __enter__(self) -> TimingContext:
        if TimingContext._stack:
            self.parent = TimingContext._stack[-1]
            self.parent.children.append(self)
        TimingContext._stack.append(self)
        self._start_time = time.perf_counter()
        return self

    def __exit__(self, *args: Any) -> None:
        self.elapsed_ms = (time.perf_counter() - self._start_time) * 1000.0
        TimingContext._stack.pop()

    def __repr__(self) -> str:
        return f"TimingContext({self.label!r}, elapsed_ms={self.elapsed_ms:.2f})"

    def summary(self, indent: int = 0) -> str:
        """Format a hierarchical timing summary.

        Args:
            indent: Indentation level for nested display.

        Returns:
            Multi-line string showing timing hierarchy.
        """
        prefix = "  " * indent
        lines = [f"{prefix}{self.label}: {self.elapsed_ms:.2f}ms"]
        for child in self.children:
            lines.append(child.summary(indent + 1))
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Utility: Check engine connectivity
# ---------------------------------------------------------------------------


async def check_engine_health(base_url: str, timeout_s: int = 10) -> bool:
    """Check if an LLM engine is reachable and healthy.

    Args:
        base_url: Engine base URL.
        timeout_s: Connection timeout in seconds.

    Returns:
        True if the engine responds to a health check.
    """
    import aiohttp

    url = f"{base_url.rstrip('/')}/v1/models"
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    logger.info("Engine at %s is healthy", base_url)
                    return True
                else:
                    logger.warning("Engine at %s returned status %d", base_url, resp.status)
                    return False
    except Exception as exc:
        logger.warning("Cannot reach engine at %s: %s", base_url, exc)
        return False


# ---------------------------------------------------------------------------
# Utility: Prepare request dicts from prompts
# ---------------------------------------------------------------------------


def prompts_to_requests(
    prompts: list[str], max_tokens: int = 256
) -> list[dict[str, Any]]:
    """Convert a list of prompt strings to request dicts.

    Args:
        prompts: List of prompt strings.
        max_tokens: Default max output tokens.

    Returns:
        List of dicts with 'prompt' and 'max_tokens' keys.
    """
    return [{"prompt": p, "max_tokens": max_tokens} for p in prompts]
