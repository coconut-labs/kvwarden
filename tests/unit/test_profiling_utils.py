"""Unit tests for profiling utilities.

Tests RequestGenerator, BenchmarkResults, TimingContext, and
GPUMetricsCollector (with mocked pynvml).
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

import sys
from pathlib import Path

# Add profiling scripts to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "profiling" / "scripts"))

from profiling_utils import (
    BenchmarkResults,
    GPUMetricsCollector,
    RequestGenerator,
    RequestMetrics,
    TimingContext,
)


# ---------------------------------------------------------------------------
# RequestGenerator tests
# ---------------------------------------------------------------------------


class TestRequestGenerator:
    """Tests for the RequestGenerator class."""

    def test_generate_sharegpt_batch_size(self) -> None:
        """Test that generate_sharegpt_batch returns the correct number of prompts."""
        gen = RequestGenerator(seed=42)
        prompts = gen.generate_sharegpt_batch(10)
        assert len(prompts) == 10

    def test_generate_sharegpt_batch_strings(self) -> None:
        """Test that generate_sharegpt_batch returns non-empty strings."""
        gen = RequestGenerator(seed=42)
        prompts = gen.generate_sharegpt_batch(5)
        for prompt in prompts:
            assert isinstance(prompt, str)
            assert len(prompt) > 0

    def test_generate_sharegpt_deterministic(self) -> None:
        """Test that the same seed produces the same prompts."""
        gen1 = RequestGenerator(seed=123)
        gen2 = RequestGenerator(seed=123)
        prompts1 = gen1.generate_sharegpt_batch(5)
        prompts2 = gen2.generate_sharegpt_batch(5)
        assert prompts1 == prompts2

    def test_generate_fixed_length_count(self) -> None:
        """Test that generate_fixed_length returns correct count."""
        gen = RequestGenerator(seed=42)
        requests = gen.generate_fixed_length(20, input_len=512, output_len=256)
        assert len(requests) == 20

    def test_generate_fixed_length_structure(self) -> None:
        """Test that generate_fixed_length returns dicts with required keys."""
        gen = RequestGenerator(seed=42)
        requests = gen.generate_fixed_length(5, input_len=512, output_len=256)
        for req in requests:
            assert "prompt" in req
            assert "max_tokens" in req
            assert req["max_tokens"] == 256
            assert isinstance(req["prompt"], str)
            assert len(req["prompt"]) > 0

    def test_generate_mixed_length_count(self) -> None:
        """Test that generate_mixed_length returns correct count."""
        gen = RequestGenerator(seed=42)
        distribution = {128: 0.5, 1024: 0.3, 4096: 0.2}
        requests = gen.generate_mixed_length(30, distribution)
        assert len(requests) == 30

    def test_generate_mixed_length_structure(self) -> None:
        """Test that generate_mixed_length returns properly structured requests."""
        gen = RequestGenerator(seed=42)
        distribution = {128: 0.4, 512: 0.6}
        requests = gen.generate_mixed_length(10, distribution)
        for req in requests:
            assert "prompt" in req
            assert "max_tokens" in req
            assert req["max_tokens"] >= 64  # minimum output length


# ---------------------------------------------------------------------------
# BenchmarkResults tests
# ---------------------------------------------------------------------------


class TestBenchmarkResults:
    """Tests for the BenchmarkResults class."""

    @staticmethod
    def _make_metrics(n: int = 100) -> list[RequestMetrics]:
        """Create synthetic request metrics for testing."""
        rng = np.random.RandomState(42)
        metrics = []
        base_time = time.time()
        for i in range(n):
            ttft = float(rng.lognormal(5.0, 0.5))
            tpot = float(rng.lognormal(3.0, 0.3))
            tokens_out = rng.randint(50, 300)
            total_latency = ttft + tpot * tokens_out
            metrics.append(
                RequestMetrics(
                    request_id=i,
                    timestamp=base_time + i * 0.1,
                    ttft_ms=ttft,
                    tpot_ms=tpot,
                    total_latency_ms=total_latency,
                    tokens_in=100,
                    tokens_out=int(tokens_out),
                )
            )
        return metrics

    def test_summary_has_percentiles(self) -> None:
        """Test that summary computes p50, p95, p99."""
        metrics = self._make_metrics(100)
        results = BenchmarkResults(per_request_metrics=metrics)
        summary = results.summary()

        assert "ttft_p50_ms" in summary
        assert "ttft_p95_ms" in summary
        assert "ttft_p99_ms" in summary
        assert "tpot_p50_ms" in summary
        assert "tpot_p95_ms" in summary
        assert "tpot_p99_ms" in summary

    def test_summary_percentile_ordering(self) -> None:
        """Test that p50 <= p95 <= p99."""
        metrics = self._make_metrics(200)
        results = BenchmarkResults(per_request_metrics=metrics)
        summary = results.summary()

        assert summary["ttft_p50_ms"] <= summary["ttft_p95_ms"]
        assert summary["ttft_p95_ms"] <= summary["ttft_p99_ms"]
        assert summary["tpot_p50_ms"] <= summary["tpot_p95_ms"]
        assert summary["tpot_p95_ms"] <= summary["tpot_p99_ms"]

    def test_summary_p50_accuracy(self) -> None:
        """Test that p50 is close to the actual median."""
        # Use known values
        metrics = []
        base_time = time.time()
        for i in range(101):
            metrics.append(
                RequestMetrics(
                    request_id=i,
                    timestamp=base_time + i * 0.01,
                    ttft_ms=float(i),  # 0..100
                    tpot_ms=10.0,
                    total_latency_ms=float(i) + 100,
                    tokens_in=50,
                    tokens_out=10,
                )
            )
        results = BenchmarkResults(per_request_metrics=metrics)
        summary = results.summary()

        assert abs(summary["ttft_p50_ms"] - 50.0) < 1.0

    def test_summary_throughput(self) -> None:
        """Test that throughput is computed correctly."""
        metrics = self._make_metrics(50)
        results = BenchmarkResults(per_request_metrics=metrics)
        summary = results.summary()

        assert "throughput_tok_per_sec" in summary
        assert summary["throughput_tok_per_sec"] > 0

    def test_summary_with_failures(self) -> None:
        """Test that summary handles failed requests gracefully."""
        metrics = [
            RequestMetrics(
                request_id=0,
                timestamp=time.time(),
                ttft_ms=100.0,
                tpot_ms=20.0,
                total_latency_ms=500.0,
                tokens_in=50,
                tokens_out=20,
            ),
            RequestMetrics(
                request_id=1,
                timestamp=time.time(),
                ttft_ms=0.0,
                tpot_ms=0.0,
                total_latency_ms=100.0,
                tokens_in=50,
                tokens_out=0,
                error="connection refused",
            ),
        ]
        results = BenchmarkResults(per_request_metrics=metrics)
        summary = results.summary()

        assert summary["num_successful"] == 1
        assert summary["num_failed"] == 1

    def test_summary_all_failed(self) -> None:
        """Test summary when all requests fail."""
        metrics = [
            RequestMetrics(
                request_id=0,
                timestamp=time.time(),
                ttft_ms=0.0,
                tpot_ms=0.0,
                total_latency_ms=100.0,
                tokens_in=50,
                tokens_out=0,
                error="timeout",
            ),
        ]
        results = BenchmarkResults(per_request_metrics=metrics)
        summary = results.summary()
        assert "error" in summary

    def test_to_dataframe(self) -> None:
        """Test DataFrame conversion."""
        metrics = self._make_metrics(10)
        results = BenchmarkResults(per_request_metrics=metrics)
        df = results.to_dataframe()

        assert len(df) == 10
        assert "request_id" in df.columns
        assert "ttft_ms" in df.columns
        assert "tpot_ms" in df.columns
        assert "total_latency_ms" in df.columns

    def test_to_csv(self, tmp_path: Path) -> None:
        """Test CSV export."""
        metrics = self._make_metrics(10)
        results = BenchmarkResults(per_request_metrics=metrics)
        csv_path = tmp_path / "results.csv"
        results.to_csv(csv_path)

        assert csv_path.exists()
        import pandas as pd

        df = pd.read_csv(csv_path)
        assert len(df) == 10


# ---------------------------------------------------------------------------
# TimingContext tests
# ---------------------------------------------------------------------------


class TestTimingContext:
    """Tests for the TimingContext context manager."""

    def test_measures_time(self) -> None:
        """Test that TimingContext measures elapsed time."""
        with TimingContext("test") as t:
            time.sleep(0.05)

        assert t.elapsed_ms >= 40  # Allow some tolerance
        assert t.elapsed_ms < 200  # But not too much

    def test_label(self) -> None:
        """Test that label is stored correctly."""
        with TimingContext("my_operation") as t:
            pass

        assert t.label == "my_operation"

    def test_nesting(self) -> None:
        """Test that nested TimingContexts form a hierarchy."""
        with TimingContext("outer") as outer:
            time.sleep(0.02)
            with TimingContext("inner") as inner:
                time.sleep(0.02)

        assert outer.elapsed_ms > inner.elapsed_ms
        assert len(outer.children) == 1
        assert outer.children[0] is inner
        assert inner.parent is outer

    def test_summary_format(self) -> None:
        """Test the summary string format."""
        with TimingContext("root") as root:
            with TimingContext("child1") as _:
                pass
            with TimingContext("child2") as _:
                pass

        summary = root.summary()
        assert "root" in summary
        assert "child1" in summary
        assert "child2" in summary

    def test_zero_work(self) -> None:
        """Test timing with essentially zero work."""
        with TimingContext("empty") as t:
            pass

        assert t.elapsed_ms >= 0
        assert t.elapsed_ms < 10


# ---------------------------------------------------------------------------
# GPUMetricsCollector tests
# ---------------------------------------------------------------------------


class TestGPUMetricsCollector:
    """Tests for GPUMetricsCollector (mocked pynvml)."""

    def test_init_without_gpu(self) -> None:
        """Test that GPUMetricsCollector initializes gracefully without GPUs."""
        # This should not raise even without NVIDIA hardware
        collector = GPUMetricsCollector()
        assert collector._gpu_indices is not None

    def test_empty_dataframe(self) -> None:
        """Test that to_dataframe returns correct columns when empty."""
        collector = GPUMetricsCollector()
        df = collector.to_dataframe()

        expected_cols = [
            "timestamp", "gpu_index", "utilization_pct",
            "memory_used_mb", "memory_total_mb", "power_draw_w",
            "sm_clock_mhz",
        ]
        for col in expected_cols:
            assert col in df.columns
        assert len(df) == 0

    def test_start_stop_without_gpu(self) -> None:
        """Test start/stop cycle works without GPU hardware."""
        collector = GPUMetricsCollector()
        collector.start()
        time.sleep(0.1)
        collector.stop()
        # Should complete without errors

    def test_to_csv(self, tmp_path: Path) -> None:
        """Test CSV export creates file."""
        collector = GPUMetricsCollector()
        csv_path = tmp_path / "gpu_metrics.csv"
        collector.to_csv(csv_path)
        assert csv_path.exists()
