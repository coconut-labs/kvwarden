"""Unit tests for the AdmissionController.

Tests concurrency limiting, priority queue ordering, timeout behavior,
stats reporting, and concurrent stress scenarios.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from kvwarden.router.admission import (
    AdmissionController,
    AdmissionTimeoutError,
)

# ---------------------------------------------------------------------------
# Basic admission / release
# ---------------------------------------------------------------------------


class TestAdmissionBasic:
    """Tests for basic acquire/release behavior."""

    async def test_below_threshold_passes_immediately(self) -> None:
        """Requests below max_concurrent are admitted without queuing."""
        ac = AdmissionController(max_concurrent=4, queue_size=16)

        for _ in range(4):
            admitted = await ac.acquire(priority=0, timeout=1.0)
            assert admitted is True

        assert ac.in_flight == 4
        assert ac.queue_depth == 0

    async def test_at_threshold_queues(self) -> None:
        """Requests at max_concurrent are queued, not rejected."""
        ac = AdmissionController(max_concurrent=2, queue_size=16)

        # Fill up capacity
        assert await ac.acquire(priority=0, timeout=1.0)
        assert await ac.acquire(priority=0, timeout=1.0)

        # Next request should queue (and timeout since nobody releases)
        admitted = await ac.acquire(priority=0, timeout=0.05)
        assert admitted is False
        assert ac.in_flight == 2

    async def test_release_admits_next_queued(self) -> None:
        """Releasing a slot admits the next queued request."""
        ac = AdmissionController(max_concurrent=1, queue_size=16)

        # Fill capacity
        assert await ac.acquire(priority=0, timeout=1.0)
        assert ac.in_flight == 1

        # Start a waiter in the background
        result_holder: list[bool] = []

        async def waiter() -> None:
            result = await ac.acquire(priority=0, timeout=5.0)
            result_holder.append(result)

        task = asyncio.create_task(waiter())
        # Give the waiter time to enqueue
        await asyncio.sleep(0.01)
        assert ac.queue_depth == 1

        # Release the slot -- waiter should be admitted
        ac.release()
        await asyncio.sleep(0.01)
        await task

        assert result_holder == [True]
        assert ac.in_flight == 1  # slot transferred, not freed
        assert ac.queue_depth == 0

    async def test_release_with_empty_queue_decrements(self) -> None:
        """Releasing when no waiters exist decrements in-flight count."""
        ac = AdmissionController(max_concurrent=4, queue_size=16)

        assert await ac.acquire(priority=0, timeout=1.0)
        assert await ac.acquire(priority=0, timeout=1.0)
        assert ac.in_flight == 2

        ac.release()
        assert ac.in_flight == 1

        ac.release()
        assert ac.in_flight == 0

    async def test_queue_full_rejects(self) -> None:
        """When the queue is full, new requests are rejected immediately."""
        ac = AdmissionController(max_concurrent=1, queue_size=1)

        # Fill capacity
        assert await ac.acquire(priority=0, timeout=1.0)

        # Fill queue (this will timeout since nobody releases)
        task = asyncio.create_task(ac.acquire(priority=0, timeout=5.0))
        await asyncio.sleep(0.01)

        # Queue is full, next request should be rejected
        admitted = await ac.acquire(priority=0, timeout=1.0)
        assert admitted is False

        # Clean up
        ac.release()
        await task


# ---------------------------------------------------------------------------
# Priority ordering
# ---------------------------------------------------------------------------


class TestAdmissionPriority:
    """Tests for priority-based queue ordering."""

    async def test_priority_ordering(self) -> None:
        """Lower priority values are admitted before higher ones."""
        ac = AdmissionController(max_concurrent=1, queue_size=16)

        # Fill capacity
        assert await ac.acquire(priority=0, timeout=1.0)

        # Queue requests with different priorities
        admission_order: list[str] = []

        async def waiter(name: str, priority: int) -> None:
            result = await ac.acquire(priority=priority, timeout=5.0)
            if result:
                admission_order.append(name)

        # Start waiters: xlarge first, then short (lower priority number)
        task_xlarge = asyncio.create_task(waiter("xlarge", 3))
        await asyncio.sleep(0.005)
        task_long = asyncio.create_task(waiter("long", 2))
        await asyncio.sleep(0.005)
        task_short = asyncio.create_task(waiter("short", 0))
        await asyncio.sleep(0.005)

        # All three should be queued
        assert ac.queue_depth == 3

        # Release slots one at a time
        ac.release()
        await asyncio.sleep(0.02)
        ac.release()
        await asyncio.sleep(0.02)
        ac.release()
        await asyncio.sleep(0.02)

        await asyncio.gather(task_xlarge, task_long, task_short)

        # Short (priority 0) should be admitted first
        assert admission_order == ["short", "long", "xlarge"]

        # Clean up
        for _ in range(3):
            ac.release()

    async def test_fifo_within_same_priority(self) -> None:
        """Requests with the same priority are served in FIFO order."""
        ac = AdmissionController(max_concurrent=1, queue_size=16)

        # Fill capacity
        assert await ac.acquire(priority=0, timeout=1.0)

        admission_order: list[str] = []

        async def waiter(name: str) -> None:
            result = await ac.acquire(priority=1, timeout=5.0)
            if result:
                admission_order.append(name)

        # Queue three requests with the same priority
        task_a = asyncio.create_task(waiter("A"))
        await asyncio.sleep(0.005)
        task_b = asyncio.create_task(waiter("B"))
        await asyncio.sleep(0.005)
        task_c = asyncio.create_task(waiter("C"))
        await asyncio.sleep(0.01)

        # Release slots
        ac.release()
        await asyncio.sleep(0.02)
        ac.release()
        await asyncio.sleep(0.02)
        ac.release()
        await asyncio.sleep(0.02)

        await asyncio.gather(task_a, task_b, task_c)

        assert admission_order == ["A", "B", "C"]

        for _ in range(3):
            ac.release()


# ---------------------------------------------------------------------------
# Timeout behavior
# ---------------------------------------------------------------------------


class TestAdmissionTimeout:
    """Tests for timeout handling."""

    async def test_timeout_returns_false(self) -> None:
        """Timed-out requests return False without blocking forever."""
        ac = AdmissionController(max_concurrent=1, queue_size=16)

        # Fill capacity
        assert await ac.acquire(priority=0, timeout=1.0)

        # This should timeout
        start = time.monotonic()
        admitted = await ac.acquire(priority=0, timeout=0.05)
        elapsed = time.monotonic() - start

        assert admitted is False
        assert elapsed < 0.2  # Should timeout roughly at 50ms

        ac.release()

    async def test_timed_out_entry_is_skipped_on_release(self) -> None:
        """Cancelled entries from timed-out waiters are skipped by release()."""
        ac = AdmissionController(max_concurrent=1, queue_size=16)

        # Fill capacity
        assert await ac.acquire(priority=0, timeout=1.0)

        # This request will timeout
        admitted_slow = await ac.acquire(priority=0, timeout=0.02)
        assert admitted_slow is False

        # Queue a second waiter that will be patient
        admission_result: list[bool] = []

        async def patient_waiter() -> None:
            result = await ac.acquire(priority=0, timeout=5.0)
            admission_result.append(result)

        task = asyncio.create_task(patient_waiter())
        await asyncio.sleep(0.01)

        # Release should skip the timed-out entry and admit the patient one
        ac.release()
        await asyncio.sleep(0.01)
        await task

        assert admission_result == [True]

        ac.release()

    async def test_zero_timeout_immediate_rejection_when_full(self) -> None:
        """timeout=0 means no waiting when the engine is at capacity."""
        ac = AdmissionController(max_concurrent=1, queue_size=16)

        assert await ac.acquire(priority=0, timeout=1.0)

        # Zero timeout should fail immediately
        admitted = await ac.acquire(priority=0, timeout=0)
        assert admitted is False

        ac.release()


# ---------------------------------------------------------------------------
# Stats reporting
# ---------------------------------------------------------------------------


class TestAdmissionStats:
    """Tests for stats property accuracy."""

    async def test_initial_stats(self) -> None:
        """Stats should be correct at initialization."""
        ac = AdmissionController(max_concurrent=64, queue_size=256)
        stats = ac.stats

        assert stats["max_concurrent"] == 64
        assert stats["queue_size"] == 256
        assert stats["in_flight"] == 0
        assert stats["queue_depth"] == 0
        assert stats["total_admitted"] == 0
        assert stats["total_rejected"] == 0
        assert stats["total_timed_out"] == 0
        assert stats["admission_rate"] == 1.0  # no decisions yet -> default 1.0
        assert stats["rejection_rate"] == 0.0

    async def test_stats_after_admits_and_rejections(self) -> None:
        """Stats should reflect actual admits and rejections."""
        ac = AdmissionController(max_concurrent=1, queue_size=1)

        # One admit
        assert await ac.acquire(priority=0, timeout=1.0)

        # One timeout (queue will have this then we overflow)
        task = asyncio.create_task(ac.acquire(priority=0, timeout=5.0))
        await asyncio.sleep(0.01)

        # One rejection (queue full)
        admitted = await ac.acquire(priority=0, timeout=0.01)
        assert admitted is False

        stats = ac.stats
        assert stats["in_flight"] == 1
        assert stats["total_admitted"] == 1
        assert stats["total_rejected"] == 1

        # Clean up
        ac.release()
        await task
        ac.release()

    async def test_admission_rate_calculation(self) -> None:
        """Admission and rejection rates should be calculated correctly."""
        ac = AdmissionController(max_concurrent=2, queue_size=16)

        # 4 admits
        for _ in range(4):
            assert await ac.acquire(priority=0, timeout=1.0)
            ac.release()

        stats = ac.stats
        assert stats["total_admitted"] == 4
        assert stats["total_rejected"] == 0
        assert stats["admission_rate"] == 1.0
        assert stats["rejection_rate"] == 0.0


# ---------------------------------------------------------------------------
# Concurrent stress test
# ---------------------------------------------------------------------------


class TestAdmissionStress:
    """Stress tests for concurrent admission control."""

    async def test_100_concurrent_acquires(self) -> None:
        """100 simultaneous acquire calls should not corrupt state."""
        ac = AdmissionController(max_concurrent=10, queue_size=200)
        results: list[bool] = []
        lock = asyncio.Lock()

        async def worker(worker_id: int) -> None:
            admitted = await ac.acquire(priority=worker_id % 4, timeout=2.0)
            async with lock:
                results.append(admitted)
            if admitted:
                # Simulate some work
                await asyncio.sleep(0.01)
                ac.release()

        tasks = [asyncio.create_task(worker(i)) for i in range(100)]
        await asyncio.gather(*tasks)

        assert len(results) == 100
        # All should have been admitted (enough timeout, all release quickly)
        admitted_count = sum(1 for r in results if r)
        assert admitted_count == 100

        assert ac.in_flight == 0
        assert ac.queue_depth == 0

    async def test_stress_with_limited_capacity(self) -> None:
        """Stress test where capacity is very limited but queue is large."""
        ac = AdmissionController(max_concurrent=2, queue_size=100)
        admitted_count = 0
        lock = asyncio.Lock()

        async def worker() -> None:
            nonlocal admitted_count
            result = await ac.acquire(priority=0, timeout=5.0)
            if result:
                async with lock:
                    admitted_count += 1
                await asyncio.sleep(0.005)
                ac.release()

        tasks = [asyncio.create_task(worker()) for _ in range(50)]
        await asyncio.gather(*tasks)

        assert admitted_count == 50
        assert ac.in_flight == 0

    async def test_fast_path_overhead(self) -> None:
        """Verify that the fast path (no queuing) adds minimal overhead.

        The requirement is <1ms overhead per request when not queuing.
        """
        ac = AdmissionController(max_concurrent=1000, queue_size=16)

        # Warm up
        for _ in range(10):
            await ac.acquire(priority=0, timeout=1.0)
            ac.release()

        # Measure fast-path overhead
        iterations = 1000
        start = time.monotonic()
        for _ in range(iterations):
            await ac.acquire(priority=0, timeout=1.0)
            ac.release()
        elapsed_ms = (time.monotonic() - start) * 1000

        overhead_per_request_ms = elapsed_ms / iterations
        # Should be well under 1ms per acquire+release cycle
        assert overhead_per_request_ms < 1.0, (
            f"Fast-path overhead {overhead_per_request_ms:.3f}ms exceeds 1ms budget"
        )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


class TestAdmissionValidation:
    """Tests for constructor validation."""

    def test_invalid_max_concurrent(self) -> None:
        with pytest.raises(ValueError, match="max_concurrent"):
            AdmissionController(max_concurrent=0)

    def test_invalid_queue_size(self) -> None:
        with pytest.raises(ValueError, match="queue_size"):
            AdmissionController(queue_size=-1)

    async def test_zero_queue_size_is_valid(self) -> None:
        """A queue size of 0 means no queuing -- reject immediately at capacity."""
        ac = AdmissionController(max_concurrent=1, queue_size=0)
        assert await ac.acquire(priority=0, timeout=1.0)
        # At capacity with queue_size=0, should reject immediately
        admitted = await ac.acquire(priority=0, timeout=0.05)
        assert admitted is False
        ac.release()


# ---------------------------------------------------------------------------
# AdmissionTimeoutError
# ---------------------------------------------------------------------------


class TestAdmissionTimeoutError:
    """Tests for the AdmissionTimeoutError exception."""

    def test_error_attributes(self) -> None:
        err = AdmissionTimeoutError(queue_depth=5, in_flight=128)
        assert err.queue_depth == 5
        assert err.in_flight == 128
        assert "128" in str(err)
        assert "5" in str(err)
