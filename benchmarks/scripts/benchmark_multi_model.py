"""Multi-model benchmark harness for KVWarden.

Measures KVWarden's core differentiator: intelligent multi-model orchestration
on 1-4 GPUs without Kubernetes. Benchmarks model switch latency, concurrent
multi-model throughput, eviction policy effectiveness, and memory utilization.

Usage:
    python benchmarks/scripts/benchmark_multi_model.py \\
        --url http://localhost:8000 \\
        --config benchmarks/configs/multi_model_scenario.yaml

    # Or with explicit models:
    python benchmarks/scripts/benchmark_multi_model.py \\
        --url http://localhost:8000 \\
        --models meta-llama/Llama-3.1-8B-Instruct Qwen/Qwen2.5-7B-Instruct \\
        --workload alternating \\
        --concurrency 1,8,32
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# Ensure profiling scripts are importable
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROFILING_SCRIPTS = _SCRIPT_DIR.parent.parent / "profiling" / "scripts"
sys.path.insert(0, str(_PROFILING_SCRIPTS))

from profiling_utils import (  # noqa: E402
    GPUMetricsCollector,
    RequestGenerator,
    TimingContext,
    check_engine_health,
    log_environment,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Multi-model request scheduling
# ---------------------------------------------------------------------------


def build_alternating_schedule(models: list[str], num_requests: int) -> list[str]:
    """Produce model names in strict round-robin order.

    Args:
        models: List of model identifiers.
        num_requests: Total requests to schedule.

    Returns:
        List of model names, one per request.
    """
    return [models[i % len(models)] for i in range(num_requests)]


def build_bursty_schedule(models: list[str], pattern: list[int]) -> list[str]:
    """Produce model names following a bursty pattern.

    Example pattern [100, 100, 50] with 2 models means:
      100 requests to model A, then 100 to model B, then 50 to model A.

    Args:
        models: List of model identifiers (at least 2).
        pattern: Counts for each burst segment.

    Returns:
        List of model names, one per request.
    """
    schedule: list[str] = []
    for idx, count in enumerate(pattern):
        model = models[idx % len(models)]
        schedule.extend([model] * count)
    return schedule


def build_concurrent_schedule(
    models: list[str], num_requests: int, seed: int = 42
) -> list[str]:
    """Produce model names with uniform random distribution.

    Args:
        models: List of model identifiers.
        num_requests: Total requests to schedule.
        seed: Random seed for reproducibility.

    Returns:
        List of model names, one per request.
    """
    rng = random.Random(seed)
    return [rng.choice(models) for _ in range(num_requests)]


# ---------------------------------------------------------------------------
# Extended metrics for multi-model scenarios
# ---------------------------------------------------------------------------


@dataclass
class MultiModelRequestMetrics:
    """Per-request metrics annotated with the target model."""

    model: str
    request_id: int
    timestamp: float
    ttft_ms: float
    tpot_ms: float
    total_latency_ms: float
    tokens_in: int
    tokens_out: int
    is_cold_start: bool = False
    error: str | None = None


@dataclass
class ModelSwitchEvent:
    """Records a model switch (eviction + load)."""

    from_model: str | None
    to_model: str
    timestamp: float
    switch_latency_ms: float


@dataclass
class MultiModelBenchmarkResults:
    """Results from a full multi-model benchmark scenario.

    Attributes:
        scenario: Scenario name.
        workload: Workload type name.
        concurrency: Concurrency level used.
        per_request: Individual request measurements.
        switch_events: Detected model switch events.
        gpu_metrics_df: GPU metrics DataFrame (if collected).
        config: Run configuration.
    """

    scenario: str
    workload: str
    concurrency: int
    per_request: list[MultiModelRequestMetrics]
    switch_events: list[ModelSwitchEvent] = field(default_factory=list)
    gpu_metrics_df: Any = None
    config: dict[str, Any] = field(default_factory=dict)
    aborted_after_n_errors: int | None = None  # R5: set if phase aborted early

    def summary(self) -> dict[str, Any]:
        """Compute aggregate statistics, broken down by model.

        Returns:
            Dictionary with overall and per-model metrics.
        """
        import numpy as np

        successful = [r for r in self.per_request if r.error is None]
        failed = [r for r in self.per_request if r.error is not None]

        if not successful:
            return {"error": "no successful requests"}

        # Overall metrics
        ttft_all = np.array([r.ttft_ms for r in successful])
        latency_all = np.array([r.total_latency_ms for r in successful])
        total_tokens_out = sum(r.tokens_out for r in successful)
        total_duration_s = max(
            r.timestamp + r.total_latency_ms / 1000.0 for r in successful
        ) - min(r.timestamp for r in successful)

        result: dict[str, Any] = {
            "scenario": self.scenario,
            "workload": self.workload,
            "concurrency": self.concurrency,
            "num_requests": len(self.per_request),
            "num_successful": len(successful),
            "num_failed": len(failed),
            "ttft_p50_ms": float(np.percentile(ttft_all, 50)),
            "ttft_p95_ms": float(np.percentile(ttft_all, 95)),
            "ttft_p99_ms": float(np.percentile(ttft_all, 99)),
            "total_latency_p50_ms": float(np.percentile(latency_all, 50)),
            "total_latency_p99_ms": float(np.percentile(latency_all, 99)),
        }

        if total_duration_s > 0:
            result["throughput_tok_per_sec"] = total_tokens_out / total_duration_s

        # Per-model breakdown
        models_seen = sorted(set(r.model for r in successful))
        per_model: dict[str, dict[str, float]] = {}
        for model in models_seen:
            model_reqs = [r for r in successful if r.model == model]
            m_ttft = np.array([r.ttft_ms for r in model_reqs])
            m_lat = np.array([r.total_latency_ms for r in model_reqs])
            m_toks = sum(r.tokens_out for r in model_reqs)
            m_cold = sum(1 for r in model_reqs if r.is_cold_start)
            short_name = model.split("/")[-1]

            per_model[short_name] = {
                "num_requests": len(model_reqs),
                "num_cold_starts": m_cold,
                "ttft_p50_ms": float(np.percentile(m_ttft, 50)),
                "ttft_p99_ms": float(np.percentile(m_ttft, 99)),
                "total_latency_p50_ms": float(np.percentile(m_lat, 50)),
                "total_latency_p99_ms": float(np.percentile(m_lat, 99)),
                "tokens_generated": m_toks,
            }

        result["per_model"] = per_model

        # Model switch metrics
        if self.switch_events:
            switch_lats = [e.switch_latency_ms for e in self.switch_events]
            result["model_switches"] = {
                "count": len(self.switch_events),
                "latency_mean_ms": float(np.mean(switch_lats)),
                "latency_p50_ms": float(np.percentile(switch_lats, 50)),
                "latency_p99_ms": float(np.percentile(switch_lats, 99)),
                "latency_max_ms": float(np.max(switch_lats)),
            }

        # GPU memory tracking
        if self.gpu_metrics_df is not None and not self.gpu_metrics_df.empty:
            df = self.gpu_metrics_df
            # Use per-GPU data (not aggregate "all")
            if "gpu_index" in df.columns:
                gpu_df = df[df["gpu_index"] != "all"]
                if not gpu_df.empty:
                    df = gpu_df

            result["gpu_memory"] = {
                "peak_used_mb": float(df["memory_used_mb"].max()),
                "mean_used_mb": float(df["memory_used_mb"].mean()),
                "total_mb": float(df["memory_total_mb"].max()),
                "peak_utilization_pct": float(
                    df["memory_used_mb"].max() / df["memory_total_mb"].max() * 100
                )
                if df["memory_total_mb"].max() > 0
                else 0.0,
                "gpu_compute_util_mean": float(df["utilization_pct"].mean()),
            }

        return result

    def to_csv(self, path: str | Path) -> None:
        """Save per-request metrics to CSV.

        Args:
            path: Output file path.
        """
        import pandas as pd

        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)

        records = []
        for r in self.per_request:
            records.append(
                {
                    "model": r.model,
                    "request_id": r.request_id,
                    "timestamp": r.timestamp,
                    "ttft_ms": r.ttft_ms,
                    "tpot_ms": r.tpot_ms,
                    "total_latency_ms": r.total_latency_ms,
                    "tokens_in": r.tokens_in,
                    "tokens_out": r.tokens_out,
                    "is_cold_start": r.is_cold_start,
                    "error": r.error,
                }
            )
        pd.DataFrame(records).to_csv(path, index=False)
        logger.info("Multi-model results saved to %s (%d requests)", path, len(records))


# ---------------------------------------------------------------------------
# Multi-model async benchmark client
# ---------------------------------------------------------------------------


class MultiModelBenchmarkClient:
    """Async client that sends requests to multiple models through KVWarden.

    KVWarden exposes a single OpenAI-compatible endpoint. The ``model`` field
    in each request payload determines which backend model handles it. This
    client coordinates request scheduling across models and detects cold-start
    model switches.

    Args:
        base_url: KVWarden server URL (e.g., http://localhost:8000).
        concurrency: Maximum concurrent in-flight requests.
        timeout_s: Per-request timeout in seconds.
        max_tokens: Default max output tokens per request.
    """

    def __init__(
        self,
        base_url: str,
        concurrency: int = 32,
        timeout_s: int = 300,
        max_tokens: int = 256,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.concurrency = concurrency
        self.timeout_s = timeout_s
        self.max_tokens = max_tokens

    async def run_schedule(
        self,
        schedule: list[str],
        requests: list[dict[str, Any]],
        gpu_collector: GPUMetricsCollector | None = None,
    ) -> MultiModelBenchmarkResults:
        """Execute a multi-model request schedule.

        For each request in the schedule, sends it to the model specified at
        that position. Detects cold-start switches by monitoring large TTFT
        spikes when the target model changes.

        Args:
            schedule: List of model names, one per request.
            requests: List of request dicts with 'prompt' and 'max_tokens'.
            gpu_collector: Optional GPU metrics collector.

        Returns:
            MultiModelBenchmarkResults with per-request and switch data.
        """
        import aiohttp

        if len(schedule) != len(requests):
            raise ValueError(
                f"Schedule length ({len(schedule)}) must match "
                f"requests length ({len(requests)})"
            )

        semaphore = asyncio.Semaphore(self.concurrency)
        all_metrics: list[MultiModelRequestMetrics] = []
        switch_events: list[ModelSwitchEvent] = []

        if gpu_collector is not None:
            gpu_collector.start()

        logger.info(
            "Starting multi-model benchmark: %d requests, concurrency=%d, "
            "target=%s, models=%s",
            len(requests),
            self.concurrency,
            self.base_url,
            sorted(set(schedule)),
        )

        # R5: phase-abort on 5 consecutive request errors. Using as_completed +
        # task cancellation so a stuck engine can't waste 30s × remaining_reqs.
        # "Consecutive" is by completion order; at c=1 this matches request-id
        # order. At c>1 it gives bounded blast radius from a stalling engine.
        phase_name = getattr(self, "phase_name", "unknown")
        consec_errors = 0
        aborted_after: int | None = None
        ABORT_THRESHOLD = 5  # noqa: N806 — module-level-style constant scoped to this method

        # Default TCPConnector caps simultaneous connections per host at 100,
        # which silently clamps any bench run with concurrency > 100. For
        # Gate 1 we ask for c=128 and c=256 — without this lift the harness
        # measures the connector limit, not the engine. Sized to 4× the
        # configured concurrency so neither connector nor pool starves.
        connector = aiohttp.TCPConnector(
            limit=max(256, self.concurrency * 4),
            limit_per_host=max(256, self.concurrency * 4),
        )
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks: list[asyncio.Task[MultiModelRequestMetrics]] = []
            for i, (model, req) in enumerate(zip(schedule, requests)):
                prompt = req.get("prompt", "") if isinstance(req, dict) else str(req)
                max_tokens = (
                    req.get("max_tokens", self.max_tokens)
                    if isinstance(req, dict)
                    else self.max_tokens
                )
                tasks.append(
                    asyncio.create_task(
                        self._send_request(
                            session, i, model, prompt, max_tokens, semaphore
                        )
                    )
                )
            raw_metrics: list[MultiModelRequestMetrics] = []
            for fut in asyncio.as_completed(tasks):
                m = await fut
                raw_metrics.append(m)
                if m.error is not None:
                    consec_errors += 1
                    if consec_errors >= ABORT_THRESHOLD:
                        aborted_after = consec_errors
                        logger.warning(
                            "phase=%s c=%d ABORT %d consecutive errors",
                            phase_name,
                            self.concurrency,
                            consec_errors,
                        )
                        for t in tasks:
                            if not t.done():
                                t.cancel()
                        break
                else:
                    consec_errors = 0
            # Drain any in-flight tasks (cancelled or otherwise) so we don't
            # leak "Task was destroyed but it is pending" warnings.
            drained = await asyncio.gather(*tasks, return_exceptions=True)
            for d in drained:
                if isinstance(d, MultiModelRequestMetrics) and d not in raw_metrics:
                    raw_metrics.append(d)
            all_metrics = raw_metrics

        if gpu_collector is not None:
            gpu_collector.stop()

        # Detect model switches by looking at sequential requests.
        # A "cold start" is when the model changed and TTFT spiked significantly.
        sorted_metrics = sorted(all_metrics, key=lambda m: m.timestamp)
        prev_model: str | None = None
        warm_ttft_by_model: dict[str, list[float]] = {}

        for m in sorted_metrics:
            if m.error is not None:
                continue
            if prev_model is not None and m.model != prev_model:
                # Model switched -- record the event
                switch_events.append(
                    ModelSwitchEvent(
                        from_model=prev_model,
                        to_model=m.model,
                        timestamp=m.timestamp,
                        switch_latency_ms=m.ttft_ms,
                    )
                )
                m.is_cold_start = True
            else:
                # Warm request -- track for baseline TTFT
                warm_ttft_by_model.setdefault(m.model, []).append(m.ttft_ms)
            prev_model = m.model

        gpu_df = gpu_collector.to_dataframe() if gpu_collector is not None else None

        return MultiModelBenchmarkResults(
            scenario="dual-model-serving",
            workload="",  # Set by caller
            concurrency=self.concurrency,
            per_request=all_metrics,
            switch_events=switch_events,
            gpu_metrics_df=gpu_df,
            aborted_after_n_errors=aborted_after,
        )

    async def _send_request(
        self,
        session: Any,
        request_id: int,
        model: str,
        prompt: str,
        max_tokens: int,
        semaphore: asyncio.Semaphore,
    ) -> MultiModelRequestMetrics:
        """Send a single streaming request to a specific model.

        Args:
            session: aiohttp ClientSession.
            request_id: Unique request identifier.
            model: Target model name for KVWarden routing.
            prompt: The prompt string.
            max_tokens: Maximum tokens to generate.
            semaphore: Concurrency limiter.

        Returns:
            MultiModelRequestMetrics with timing data.
        """
        import aiohttp

        url = f"{self.base_url}/v1/completions"
        payload = {
            "model": model,
            "prompt": prompt,
            "max_tokens": max_tokens,
            "stream": True,
        }

        async with semaphore:
            start_time = time.time()
            first_token_time: float | None = None
            tokens_out = 0

            # D1: per-request log so a stall is visible on the terminal within
            # seconds, not only in the post-run CSV. Previously a 3-hour stall
            # on c=1 produced zero output between "Starting benchmark" and the
            # 300s timeout storm.
            logger.info("req=%d MODEL=%s START", request_id, model)

            try:
                timeout = aiohttp.ClientTimeout(total=self.timeout_s)
                async with session.post(url, json=payload, timeout=timeout) as resp:
                    if resp.status != 200:
                        error_body = await resp.text()
                        return MultiModelRequestMetrics(
                            model=model,
                            request_id=request_id,
                            timestamp=start_time,
                            ttft_ms=0.0,
                            tpot_ms=0.0,
                            total_latency_ms=(time.time() - start_time) * 1000,
                            tokens_in=len(prompt.split()),
                            tokens_out=0,
                            error=f"HTTP {resp.status}: {error_body[:200]}",
                        )

                    async for line in resp.content:
                        decoded = line.decode("utf-8").strip()
                        if not decoded or not decoded.startswith("data: "):
                            continue
                        data_str = decoded[6:]
                        if data_str == "[DONE]":
                            break

                        try:
                            chunk = json.loads(data_str)
                            choices = chunk.get("choices", [])
                            if not choices:
                                continue
                            # Support /v1/completions (`text` field) AND
                            # /v1/chat/completions (`delta.content` field). On
                            # chat-template engines `text` is always missing,
                            # so without the delta fallback TTFT silently
                            # collapses to total_latency_ms.
                            content = (
                                choices[0].get("text")
                                or choices[0].get("delta", {}).get("content")
                                or ""
                            )
                            # C2 fix: TTFT is time-to-first-non-empty-content.
                            # `.strip()` rejects whitespace-only or role-only
                            # frames that vLLM/SGLang emit before real tokens —
                            # bool("\n") is True, which would re-introduce the
                            # very bug PR #28 set out to kill.
                            if content.strip():
                                if first_token_time is None:
                                    first_token_time = time.time()
                                tokens_out += 1
                        except (json.JSONDecodeError, KeyError):
                            # Malformed SSE chunk — do NOT count as a token
                            # and do NOT stamp TTFT (a parse error is not
                            # generation progress).
                            continue

            except asyncio.TimeoutError:
                logger.warning(
                    "req=%d MODEL=%s TIMEOUT after %.0fs",
                    request_id,
                    model,
                    time.time() - start_time,
                )
                return MultiModelRequestMetrics(
                    model=model,
                    request_id=request_id,
                    timestamp=start_time,
                    ttft_ms=0.0,
                    tpot_ms=0.0,
                    total_latency_ms=(time.time() - start_time) * 1000,
                    tokens_in=len(prompt.split()),
                    tokens_out=0,
                    error="timeout",
                )
            except Exception as exc:
                logger.warning("req=%d MODEL=%s ERROR %s", request_id, model, exc)
                return MultiModelRequestMetrics(
                    model=model,
                    request_id=request_id,
                    timestamp=start_time,
                    ttft_ms=0.0,
                    tpot_ms=0.0,
                    total_latency_ms=(time.time() - start_time) * 1000,
                    tokens_in=len(prompt.split()),
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

            logger.info(
                "req=%d MODEL=%s DONE tokens_out=%d latency_ms=%.0f ttft_ms=%.0f",
                request_id,
                model,
                tokens_out,
                total_latency_ms,
                ttft_ms,
            )

            return MultiModelRequestMetrics(
                model=model,
                request_id=request_id,
                timestamp=start_time,
                ttft_ms=ttft_ms,
                tpot_ms=tpot_ms,
                total_latency_ms=total_latency_ms,
                tokens_in=len(prompt.split()),
                tokens_out=tokens_out,
            )


# ---------------------------------------------------------------------------
# Benchmark scenario: model switch latency
# ---------------------------------------------------------------------------


async def benchmark_model_switch_latency(
    client: MultiModelBenchmarkClient,
    models: list[str],
    num_warmup: int = 5,
    num_switches: int = 20,
    seed: int = 42,
) -> dict[str, Any]:
    """Measure cold-swap latency when switching between models.

    Warms up model A, then sends a request to model B (cold swap),
    measures the TTFT spike, and repeats.

    Args:
        client: Multi-model benchmark client.
        models: Exactly 2 model identifiers.
        num_warmup: Warmup requests per model before measuring.
        num_switches: Number of A->B switches to measure.
        seed: Random seed.

    Returns:
        Dictionary with switch latency statistics.
    """
    gen = RequestGenerator(seed=seed)
    warmup_requests = gen.generate_fixed_length(
        num_warmup, input_len=128, output_len=64
    )
    switch_requests = gen.generate_fixed_length(
        num_switches * 2, input_len=256, output_len=128
    )

    model_a, model_b = models[0], models[1]
    switch_latencies: list[float] = []
    warm_latencies_a: list[float] = []
    warm_latencies_b: list[float] = []

    logger.info("=== Model Switch Latency Benchmark ===")
    logger.info("Model A: %s", model_a)
    logger.info("Model B: %s", model_b)

    # Step 1: Warm up model A
    logger.info("Warming up model A (%s)...", model_a)
    schedule_warmup = [model_a] * num_warmup
    warmup_result = await client.run_schedule(schedule_warmup, warmup_requests)
    warm_a = [r.ttft_ms for r in warmup_result.per_request if r.error is None]
    if warm_a:
        warm_latencies_a = warm_a
        logger.info("Model A warm TTFT: %.1fms (mean)", sum(warm_a) / len(warm_a))

    # Step 2: Alternate switches and measure
    req_idx = 0
    for switch_num in range(num_switches):
        # Send a burst to model A (ensure it is warm/loaded)
        stabilize_schedule = [model_a] * 3
        stabilize_reqs = gen.generate_fixed_length(3, input_len=128, output_len=64)
        await client.run_schedule(stabilize_schedule, stabilize_reqs)

        # Now send one request to model B (cold swap)
        cold_schedule = [model_b]
        cold_req = [switch_requests[req_idx]]
        req_idx += 1
        cold_result = await client.run_schedule(cold_schedule, cold_req)

        for r in cold_result.per_request:
            if r.error is None:
                switch_latencies.append(r.ttft_ms)
                logger.info(
                    "Switch %d/%d: A->B TTFT = %.1fms",
                    switch_num + 1,
                    num_switches,
                    r.ttft_ms,
                )

        # Send a follow-up to model B (now warm)
        warm_schedule = [model_b]
        warm_req = [switch_requests[req_idx]]
        req_idx += 1
        warm_result = await client.run_schedule(warm_schedule, warm_req)
        for r in warm_result.per_request:
            if r.error is None:
                warm_latencies_b.append(r.ttft_ms)

    import numpy as np

    result: dict[str, Any] = {
        "benchmark": "model_switch_latency",
        "model_a": model_a,
        "model_b": model_b,
        "num_switches": num_switches,
    }

    if switch_latencies:
        result["cold_swap_ttft_mean_ms"] = float(np.mean(switch_latencies))
        result["cold_swap_ttft_p50_ms"] = float(np.percentile(switch_latencies, 50))
        result["cold_swap_ttft_p99_ms"] = float(np.percentile(switch_latencies, 99))
        result["cold_swap_ttft_max_ms"] = float(np.max(switch_latencies))

    if warm_latencies_a:
        result["warm_ttft_model_a_mean_ms"] = float(np.mean(warm_latencies_a))

    if warm_latencies_b:
        result["warm_ttft_model_b_mean_ms"] = float(np.mean(warm_latencies_b))

    if switch_latencies and warm_latencies_b:
        overhead = np.mean(switch_latencies) - np.mean(warm_latencies_b)
        result["switch_overhead_ms"] = float(overhead)

    logger.info("Model switch latency results: %s", json.dumps(result, indent=2))
    return result


# ---------------------------------------------------------------------------
# Benchmark scenario: concurrent multi-model throughput
# ---------------------------------------------------------------------------


async def benchmark_concurrent_throughput(
    client: MultiModelBenchmarkClient,
    models: list[str],
    workload_config: dict[str, Any],
    seed: int = 42,
) -> MultiModelBenchmarkResults:
    """Measure throughput with two models loaded simultaneously.

    Sends alternating requests to both models to keep them both warm
    and measures aggregate throughput and per-model TTFT.

    Args:
        client: Multi-model benchmark client.
        models: Model identifiers.
        workload_config: Workload section from scenario YAML.
        seed: Random seed.

    Returns:
        MultiModelBenchmarkResults with detailed metrics.
    """
    num_requests = workload_config.get("num_requests", 200)
    workload_name = workload_config["name"]

    gen = RequestGenerator(seed=seed)
    requests = gen.generate_fixed_length(num_requests, input_len=256, output_len=128)

    if workload_name == "alternating":
        schedule = build_alternating_schedule(models, num_requests)
    elif workload_name == "bursty":
        pattern = workload_config.get("pattern", [100, 100, 50])
        schedule = build_bursty_schedule(models, pattern)
        # Adjust requests to match schedule length
        actual_len = len(schedule)
        if len(requests) < actual_len:
            extra = gen.generate_fixed_length(
                actual_len - len(requests), input_len=256, output_len=128
            )
            requests.extend(extra)
        elif len(requests) > actual_len:
            requests = requests[:actual_len]
    elif workload_name == "concurrent":
        schedule = build_concurrent_schedule(models, num_requests, seed=seed)
    else:
        raise ValueError(f"Unknown workload: {workload_name}")

    gpu_collector = GPUMetricsCollector(interval_ms=100)

    logger.info("=== Concurrent Throughput: %s ===", workload_name)
    logger.info("Schedule length: %d, Models: %s", len(schedule), models)

    result = await client.run_schedule(schedule, requests, gpu_collector=gpu_collector)
    result.workload = workload_name
    return result


# ---------------------------------------------------------------------------
# Benchmark scenario: eviction policy effectiveness
# ---------------------------------------------------------------------------


async def benchmark_eviction_policy(
    client: MultiModelBenchmarkClient,
    models: list[str],
    seed: int = 42,
) -> dict[str, Any]:
    """Measure eviction policy effectiveness with bursty traffic.

    Sends 100 requests to model A, 100 to model B, then 50 to model A.
    Measures how quickly model A is restored after eviction and compares
    against a naive LRU baseline (estimated).

    Args:
        client: Multi-model benchmark client.
        models: Model identifiers (at least 2).
        seed: Random seed.

    Returns:
        Dictionary with eviction policy metrics.
    """
    gen = RequestGenerator(seed=seed)
    pattern = [100, 100, 50]
    schedule = build_bursty_schedule(models, pattern)
    total_requests = sum(pattern)
    requests = gen.generate_fixed_length(total_requests, input_len=256, output_len=128)

    gpu_collector = GPUMetricsCollector(interval_ms=100)

    logger.info("=== Eviction Policy Benchmark ===")
    logger.info("Pattern: %s across models %s", pattern, models)

    result = await client.run_schedule(schedule, requests, gpu_collector=gpu_collector)
    result.workload = "eviction_test"

    # Analyze the three phases
    import numpy as np

    phase_a1 = result.per_request[:100]
    phase_b = result.per_request[100:200]
    phase_a2 = result.per_request[200:]

    def phase_stats(phase: list[MultiModelRequestMetrics], name: str) -> dict[str, Any]:
        ok = [r for r in phase if r.error is None]
        if not ok:
            return {"phase": name, "error": "no successful requests"}
        ttfts = [r.ttft_ms for r in ok]
        lats = [r.total_latency_ms for r in ok]
        return {
            "phase": name,
            "num_requests": len(phase),
            "num_successful": len(ok),
            "ttft_mean_ms": float(np.mean(ttfts)),
            "ttft_p50_ms": float(np.percentile(ttfts, 50)),
            "ttft_p99_ms": float(np.percentile(ttfts, 99)),
            "latency_mean_ms": float(np.mean(lats)),
            "first_request_ttft_ms": ttfts[0] if ttfts else 0.0,
        }

    model_a, model_b = models[0], models[1]
    phase_a1_stats = phase_stats(phase_a1, f"burst_1_{model_a.split('/')[-1]}")
    phase_b_stats = phase_stats(phase_b, f"burst_2_{model_b.split('/')[-1]}")
    phase_a2_stats = phase_stats(phase_a2, f"burst_3_{model_a.split('/')[-1]}_reload")

    # The key metric: how quickly model A is restored after being evicted
    reload_first_ttft = phase_a2_stats.get("first_request_ttft_ms", 0.0)
    warm_a1_ttft = phase_a1_stats.get("ttft_mean_ms", 0.0)

    eviction_result: dict[str, Any] = {
        "benchmark": "eviction_policy",
        "pattern": pattern,
        "models": models,
        "phases": [phase_a1_stats, phase_b_stats, phase_a2_stats],
        "model_a_reload_ttft_ms": reload_first_ttft,
        "model_a_warm_ttft_mean_ms": warm_a1_ttft,
        "reload_overhead_ms": reload_first_ttft - warm_a1_ttft,
    }

    # GPU memory from the three phases
    if result.gpu_metrics_df is not None and not result.gpu_metrics_df.empty:
        df = result.gpu_metrics_df
        if "gpu_index" in df.columns:
            gpu_df = df[df["gpu_index"] != "all"]
            if not gpu_df.empty:
                df = gpu_df
        eviction_result["gpu_memory_peak_mb"] = float(df["memory_used_mb"].max())
        eviction_result["gpu_memory_mean_mb"] = float(df["memory_used_mb"].mean())

    logger.info(
        "Eviction policy results: %s",
        json.dumps(eviction_result, indent=2, default=str),
    )
    return eviction_result


# ---------------------------------------------------------------------------
# CLI & main
# ---------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Multi-model benchmark harness for KVWarden",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--url",
        type=str,
        default="http://localhost:8000",
        help="KVWarden server URL",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="Path to scenario YAML config file",
    )
    parser.add_argument(
        "--models",
        nargs="+",
        default=None,
        help="Model identifiers (overrides config)",
    )
    parser.add_argument(
        "--workload",
        type=str,
        default="all",
        choices=[
            "alternating",
            "bursty",
            "concurrent",
            "switch_latency",
            "eviction",
            "all",
        ],
        help="Which workload scenario to run",
    )
    parser.add_argument(
        "--concurrency",
        type=str,
        default="1,8,32",
        help="Comma-separated concurrency levels",
    )
    parser.add_argument(
        "--num-requests",
        type=int,
        default=200,
        help="Number of requests per workload (overrides config)",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="benchmarks/results/multi_model",
        help="Output directory for results",
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
    return parser.parse_args()


def load_scenario_config(config_path: str | None) -> dict[str, Any]:
    """Load scenario configuration from YAML.

    Args:
        config_path: Path to YAML config, or None for defaults.

    Returns:
        Scenario configuration dictionary.
    """
    if config_path is None:
        return {}

    path = Path(config_path)
    if not path.exists():
        logger.warning("Config file %s not found, using defaults", config_path)
        return {}

    with open(path) as f:
        return yaml.safe_load(f) or {}


async def main() -> None:
    """Main entry point for multi-model benchmarks."""
    args = parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    logger.info("=" * 70)
    logger.info("KVWarden Multi-Model Benchmark Harness")
    logger.info("=" * 70)

    env_info = log_environment(seed=args.seed)

    # Load config
    config = load_scenario_config(args.config)

    # Resolve models
    if args.models:
        models = args.models
    elif "models" in config:
        models = [m["id"] for m in config["models"]]
    else:
        models = [
            "meta-llama/Llama-3.1-8B-Instruct",
            "Qwen/Qwen2.5-7B-Instruct",
        ]

    # `concurrent` workload distributes requests across the model list and
    # works for any N >= 1 (single-model is the realistic Gate 1 setup).
    # `switch_latency`, `alternating`, `bursty`, `eviction` compare across
    # models and need at least two.
    SINGLE_MODEL_OK_WORKLOADS = {"concurrent"}  # noqa: N806 — config-style constant in main()
    requested = (
        {args.workload}
        if args.workload != "all"
        else {
            "switch_latency",
            "alternating",
            "bursty",
            "concurrent",
            "eviction",
        }
    )
    if len(models) < 2 and not requested.issubset(SINGLE_MODEL_OK_WORKLOADS):
        logger.error(
            "Need at least 2 models for workloads %s. Got %d model(s); "
            "for single-model runs use --workload concurrent.",
            sorted(requested - SINGLE_MODEL_OK_WORKLOADS),
            len(models),
        )
        sys.exit(1)

    logger.info("Models: %s", models)
    logger.info("Server: %s", args.url)

    # Check server health
    healthy = await check_engine_health(args.url)
    if not healthy:
        logger.error(
            "KVWarden server at %s is not reachable. Start with:\n"
            "  kvwarden serve %s --gpu-budget 0.85",
            args.url,
            " ".join(models),
        )
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Parse concurrency levels
    if "concurrency_levels" in config:
        concurrency_levels = config["concurrency_levels"]
    else:
        concurrency_levels = [int(c.strip()) for c in args.concurrency.split(",")]

    # Resolve workloads from config or defaults
    if "workloads" in config:
        workload_configs = {w["name"]: w for w in config["workloads"]}
    else:
        workload_configs = {
            "alternating": {
                "name": "alternating",
                "description": "Alternate between models every request",
                "num_requests": args.num_requests,
            },
            "bursty": {
                "name": "bursty",
                "description": "100 requests to A, 100 to B, 50 to A",
                "pattern": [100, 100, 50],
            },
            "concurrent": {
                "name": "concurrent",
                "description": "50/50 random split between models",
                "num_requests": args.num_requests,
            },
        }

    # Determine which workloads to run
    if args.workload == "all":
        workloads_to_run = [
            "switch_latency",
            "alternating",
            "bursty",
            "concurrent",
            "eviction",
        ]
    else:
        workloads_to_run = [args.workload]

    all_summaries: list[dict[str, Any]] = []

    # Save run config
    run_config = {
        "url": args.url,
        "models": models,
        "concurrency_levels": concurrency_levels,
        "workloads": workloads_to_run,
        "seed": args.seed,
        "num_requests": args.num_requests,
        "environment": env_info,
    }
    with open(output_dir / "run_config.json", "w") as f:
        json.dump(run_config, f, indent=2)

    # ----- Benchmark 1: Model switch latency -----
    if "switch_latency" in workloads_to_run:
        logger.info("\n" + "=" * 60)
        logger.info("Benchmark 1: Model Switch Latency")
        logger.info("=" * 60)

        switch_client = MultiModelBenchmarkClient(
            base_url=args.url,
            concurrency=1,  # Sequential for switch measurement
            timeout_s=600,  # Cold swaps can be slow
        )
        switch_result = await benchmark_model_switch_latency(
            switch_client, models, seed=args.seed
        )
        all_summaries.append(switch_result)

        with open(output_dir / "switch_latency.json", "w") as f:
            json.dump(switch_result, f, indent=2)

    # ----- Benchmarks 2-4: Workload scenarios at each concurrency -----
    throughput_workloads = [
        w for w in workloads_to_run if w in ("alternating", "bursty", "concurrent")
    ]

    for workload_name in throughput_workloads:
        wl_config = workload_configs.get(workload_name, {"name": workload_name})
        if "num_requests" not in wl_config:
            wl_config["num_requests"] = args.num_requests

        for conc in concurrency_levels:
            logger.info("\n" + "=" * 60)
            logger.info("Workload: %s | Concurrency: %d", workload_name, conc)
            logger.info("=" * 60)

            client = MultiModelBenchmarkClient(
                base_url=args.url,
                concurrency=conc,
                timeout_s=300,
            )

            with TimingContext(f"{workload_name}_c{conc}") as timer:
                result = await benchmark_concurrent_throughput(
                    client, models, wl_config, seed=args.seed
                )
            result.concurrency = conc

            # Save per-request CSV
            csv_path = output_dir / f"{workload_name}_c{conc}.csv"
            result.to_csv(csv_path)

            # Save GPU metrics
            if result.gpu_metrics_df is not None and not result.gpu_metrics_df.empty:
                gpu_path = output_dir / f"{workload_name}_c{conc}_gpu.csv"
                result.gpu_metrics_df.to_csv(gpu_path, index=False)

            # Save summary
            summary = result.summary()
            summary["wall_time_ms"] = timer.elapsed_ms
            all_summaries.append(summary)

            summary_path = output_dir / f"{workload_name}_c{conc}_summary.json"
            with open(summary_path, "w") as f:
                json.dump(summary, f, indent=2, default=str)

            logger.info(
                "  Throughput: %.1f tok/s | TTFT p50: %.1fms | TTFT p99: %.1fms",
                summary.get("throughput_tok_per_sec", 0),
                summary.get("ttft_p50_ms", 0),
                summary.get("ttft_p99_ms", 0),
            )

    # ----- Benchmark: Eviction policy -----
    if "eviction" in workloads_to_run:
        logger.info("\n" + "=" * 60)
        logger.info("Benchmark: Eviction Policy Effectiveness")
        logger.info("=" * 60)

        eviction_client = MultiModelBenchmarkClient(
            base_url=args.url,
            concurrency=8,
            timeout_s=600,
        )
        eviction_result = await benchmark_eviction_policy(
            eviction_client, models, seed=args.seed
        )
        all_summaries.append(eviction_result)

        with open(output_dir / "eviction_policy.json", "w") as f:
            json.dump(eviction_result, f, indent=2, default=str)

    # ----- Final summary -----
    final_summary = {
        "scenario": config.get("scenario", "dual-model-serving"),
        "models": models,
        "concurrency_levels": concurrency_levels,
        "benchmarks": all_summaries,
    }

    summary_path = output_dir / "multi_model_summary.json"
    with open(summary_path, "w") as f:
        json.dump(final_summary, f, indent=2, default=str)

    logger.info("\n" + "=" * 70)
    logger.info("Multi-model benchmark complete. Results in %s", output_dir)
    logger.info("=" * 70)

    # Print key findings
    logger.info("\nKey findings:")
    for s in all_summaries:
        bench = s.get("benchmark", s.get("workload", "unknown"))
        if "cold_swap_ttft_mean_ms" in s:
            logger.info(
                "  Switch latency: %.0fms mean cold swap, %.0fms overhead",
                s["cold_swap_ttft_mean_ms"],
                s.get("switch_overhead_ms", 0),
            )
        elif "throughput_tok_per_sec" in s:
            logger.info(
                "  %s (c=%d): %.1f tok/s, TTFT p50=%.1fms",
                bench,
                s.get("concurrency", 0),
                s["throughput_tok_per_sec"],
                s.get("ttft_p50_ms", 0),
            )
        elif "reload_overhead_ms" in s:
            logger.info(
                "  Eviction: %.0fms reload overhead, %.0fms first TTFT after eviction",
                s["reload_overhead_ms"],
                s.get("model_a_reload_ttft_ms", 0),
            )


if __name__ == "__main__":
    asyncio.run(main())
