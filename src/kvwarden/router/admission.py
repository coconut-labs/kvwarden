"""Admission controller for engine overload prevention.

Limits the number of concurrent requests forwarded to an inference engine
to keep it below its throughput-saturation cliff point. Requests that
arrive when the engine is at capacity are queued with priority ordering
(lower priority value = higher priority) and FIFO tie-breaking.

Profiling context:
    vLLM A100: c=128->c=256 yields +0.4% throughput but +1434% TTFT
    SGLang A100: c=128->c=256 yields -1.2% throughput but +928% TTFT
    The admission controller prevents this cliff by capping concurrency.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

if TYPE_CHECKING:
    from kvwarden.cache.manager import TenantPolicy

logger = logging.getLogger(__name__)


# ── Cache-pressure priority scaling (T2 — issue #103) ────────────────
#
# RFC: docs/rfcs/T2-cache-pressure-admission.md. Pure functions, no I/O.
# The engine's KV cache pressure is orthogonal to kvwarden's per-tenant
# deficit ledger; these compose the two into the single int the
# AdmissionController orders by.
#
# Watermarks below are the RFC's placeholders, NOT measured values. The
# Gate 3 M4 probe that would fix them has not landed capacity, so they
# stay overridable per-deployment rather than baked in.

CACHE_PRESSURE_SOFT_THRESHOLD: float = 0.5
CACHE_PRESSURE_HARD_THRESHOLD: float = 0.9
CACHE_PRESSURE_SATURATION_CEILING: float = 4.0


def cache_load_scaling(
    pressure: float | None,
    *,
    soft_threshold: float = CACHE_PRESSURE_SOFT_THRESHOLD,
    hard_threshold: float = CACHE_PRESSURE_HARD_THRESHOLD,
    ceiling: float = CACHE_PRESSURE_SATURATION_CEILING,
) -> float:
    """Map engine KV cache pressure to a deficit amplification factor.

    Piecewise linear: identity below the soft threshold, a straight ramp
    to the ceiling between the thresholds, saturated above the hard one.

    Two invariants hold under any tuning pass (RFC §Design):

    - **Identity at zero pressure.** ``cache_load_scaling(0.0) == 1.0``,
      so v0.1 admission behavior is recovered exactly on a cold cache.
    - **Bounded above.** The ceiling caps amplification so one saturated
      engine cannot starve a tenant whose deficit happens to be small.

    The RFC sketch ramps ``1.0 + 6.0 * (p - 0.5)`` and then jumps to 4.0,
    which is discontinuous at the hard threshold (3.4 → 4.0). Ramping to
    the ceiling instead removes the jump and preserves both RFC-pinned
    points, ``scale(0.5) == 1.0`` and ``scale(0.9) == 4.0``.

    Args:
        pressure: Gauge value in [0.0, 1.0]. ``None`` means no reading
            (poller down, or never polled) and yields identity —
            fail-open, never fail-shut. Out-of-range values are clamped,
            defensively against an engine-side gauge bug.
        soft_threshold: Pressure below which scaling is identity.
        hard_threshold: Pressure at and above which scaling saturates.
        ceiling: Maximum amplification factor.

    Returns:
        A float >= 1.0, monotone non-decreasing in ``pressure``.
    """
    if pressure is None:
        return 1.0

    p = min(1.0, max(0.0, float(pressure)))

    if p <= soft_threshold:
        return 1.0
    if p >= hard_threshold:
        return ceiling

    span = hard_threshold - soft_threshold
    if span <= 0.0:
        # Degenerate config (soft >= hard): the ramp has no width, so the
        # curve is a step at the threshold.
        return ceiling
    return 1.0 + (ceiling - 1.0) * (p - soft_threshold) / span


def compose_priority(
    tenant_id: str,
    base_priority: int,
    policy: TenantPolicy | None,
    kv_cache_pressure: float | None,
    *,
    soft_threshold: float = CACHE_PRESSURE_SOFT_THRESHOLD,
    hard_threshold: float = CACHE_PRESSURE_HARD_THRESHOLD,
    ceiling: float = CACHE_PRESSURE_SATURATION_CEILING,
) -> int:
    """Amplify a tenant's DRR deficit by engine cache pressure.

    ``base_priority`` is the **deficit component only** — the caller adds
    the length-bucket tie-breaker after this returns. Passing a
    bucket-inclusive value would amplify the tie-breaker too and break
    "bucket breaks ties within tenant tier" (`router.py:458-476`).

    A tenant's ``tenant_weights`` entry is an admission *cost* weight, so
    a low weight means an expensive tenant: cost is ``1 / weight``. The
    cost is blended in proportionally to how far the scale has travelled
    toward the ceiling, so it arrives smoothly rather than as a cliff at
    the soft threshold — at ``scale == 1.0`` no weight applies at all, at
    the ceiling the full ``1 / weight`` does.

    Args:
        tenant_id: Tenant the request belongs to.
        base_priority: The tenant's DRR deficit score. Lower is served
            first, matching AdmissionController and BUCKET_PRIORITY.
        policy: Tenant weights, or ``None`` for uniform weighting.
        kv_cache_pressure: Latest engine gauge reading, or ``None``.

    Returns:
        The amplified priority. Equals ``base_priority`` exactly whenever
        the scale is identity, whatever the policy says.
    """
    scale = cache_load_scaling(
        kv_cache_pressure,
        soft_threshold=soft_threshold,
        hard_threshold=hard_threshold,
        ceiling=ceiling,
    )
    if scale <= 1.0:
        return base_priority

    weight = 1.0
    if policy is not None:
        weight = policy.tenant_weights.get(tenant_id, 1.0)
    if not weight > 0.0:
        # A non-positive weight is a config error, not a request to
        # divide by zero. There is no config validator to catch it
        # upstream yet, so fall back to uniform rather than blow up in
        # the request path.
        weight = 1.0

    cost = 1.0 / weight
    blended = 1.0 + (cost - 1.0) * (scale - 1.0) / (ceiling - 1.0)
    return round(base_priority * scale * blended)


@dataclass(order=True)
class _QueueEntry:
    """Internal priority queue entry.

    Ordered by (priority, sequence) so that lower priority values
    are dequeued first, with FIFO ordering among equal priorities.

    Attributes:
        priority: Lower values are dequeued first.
        sequence: Monotonic counter for FIFO tie-breaking.
        event: Signalled when the request is admitted.
        cancelled: Set to True if the waiter timed out.
    """

    priority: int
    sequence: int
    event: asyncio.Event = field(compare=False, default_factory=asyncio.Event)
    cancelled: bool = field(compare=False, default=False)


class AdmissionTimeoutError(Exception):
    """Raised when a request times out waiting for admission."""

    def __init__(self, queue_depth: int, in_flight: int) -> None:
        self.queue_depth = queue_depth
        self.in_flight = in_flight
        super().__init__(
            f"Admission timeout: {in_flight} in-flight, {queue_depth} queued"
        )


class AdmissionController:
    """Prevents engine overload by controlling request admission rate.

    Uses a concurrency counter protected by an asyncio.Lock, with a
    PriorityQueue for waiters when the engine is at capacity. This
    design supports priority ordering (short requests first) while
    maintaining FIFO order within the same priority level.

    The overhead when not queuing (in-flight < max_concurrent) is a
    single lock acquire/release, which is sub-microsecond on modern
    hardware -- well under the 1ms budget.

    Args:
        max_concurrent: Maximum requests forwarded to the engine at once.
        queue_size: Maximum number of requests waiting in the admission queue.
        registry: Optional Prometheus CollectorRegistry for metrics.
    """

    def __init__(
        self,
        max_concurrent: int = 128,
        queue_size: int = 1024,
        registry: CollectorRegistry | None = None,
    ) -> None:
        if max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1, got {max_concurrent}")
        if queue_size < 0:
            raise ValueError(f"queue_size must be >= 0, got {queue_size}")

        self._max_concurrent = max_concurrent
        self._queue_size = queue_size
        self._in_flight = 0
        self._sequence = 0
        self._lock = asyncio.Lock()
        self._waiters: asyncio.PriorityQueue[_QueueEntry] = asyncio.PriorityQueue()

        # Metrics tracking (non-Prometheus, always available)
        self._total_admitted: int = 0
        self._total_rejected: int = 0
        self._total_timed_out: int = 0

        # Prometheus metrics (optional)
        self._registry = registry
        if registry is not None:
            self._prom_queue_depth = Gauge(
                "kvwarden_admission_queue_depth",
                "Number of requests waiting in the admission queue",
                registry=registry,
            )
            self._prom_in_flight = Gauge(
                "kvwarden_admission_in_flight",
                "Number of requests currently being processed by the engine",
                registry=registry,
            )
            self._prom_wait_seconds = Histogram(
                "kvwarden_admission_wait_seconds",
                "Time spent waiting for admission",
                buckets=(
                    0.001,
                    0.005,
                    0.01,
                    0.05,
                    0.1,
                    0.25,
                    0.5,
                    1.0,
                    5.0,
                    10.0,
                    30.0,
                ),
                registry=registry,
            )
            self._prom_rejected_total = Counter(
                "kvwarden_admission_rejected_total",
                "Total requests rejected (queue full or timed out)",
                labelnames=["reason"],
                registry=registry,
            )
            self._prom_admitted_total = Counter(
                "kvwarden_admission_admitted_total",
                "Total requests admitted to the engine",
                registry=registry,
            )
        else:
            self._prom_queue_depth = None
            self._prom_in_flight = None
            self._prom_wait_seconds = None
            self._prom_rejected_total = None
            self._prom_admitted_total = None

        logger.info(
            "AdmissionController initialized: max_concurrent=%d, queue_size=%d",
            max_concurrent,
            queue_size,
        )

    @property
    def max_concurrent(self) -> int:
        """Maximum concurrent requests allowed."""
        return self._max_concurrent

    @property
    def in_flight(self) -> int:
        """Current number of in-flight requests."""
        return self._in_flight

    @property
    def queue_depth(self) -> int:
        """Current number of requests waiting in the queue.

        NOTE: This may overcount when timed-out waiters have left cancelled
        entries in the PriorityQueue. Cancelled entries are drained lazily
        during release(). For accurate live counts, subtract _total_timed_out
        from the cumulative queue puts, but the qsize approximation is
        sufficient for metrics and admission decisions.
        """
        return self._waiters.qsize()

    async def acquire(self, priority: int = 0, timeout: float = 30.0) -> bool:
        """Wait for admission to forward a request to the engine.

        If the engine has capacity (in_flight < max_concurrent), the
        request is admitted immediately. Otherwise it is queued by
        priority (lower = higher priority) with FIFO tie-breaking.

        Args:
            priority: Request priority. Lower values are served first.
                Convention: 0=short, 1=medium, 2=long, 3=xlarge.
            timeout: Maximum seconds to wait. 0 means no waiting.

        Returns:
            True if admitted, False if the queue is full or timeout expired.
        """
        wait_start = time.monotonic()

        async with self._lock:
            if self._in_flight < self._max_concurrent:
                # Fast path: engine has capacity
                self._in_flight += 1
                self._total_admitted += 1
                self._record_admission(wait_start)
                return True

            # Slow path: engine at capacity, check queue space
            if self._waiters.qsize() >= self._queue_size:
                self._total_rejected += 1
                self._record_rejection("queue_full")
                return False

            # Enqueue this waiter
            entry = _QueueEntry(
                priority=priority,
                sequence=self._sequence,
            )
            self._sequence += 1
            await self._waiters.put(entry)
            self._update_queue_depth_metric()

        # Wait outside the lock for admission
        try:
            await asyncio.wait_for(entry.event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            entry.cancelled = True
            self._total_timed_out += 1
            self._record_rejection("timeout")
            self._update_queue_depth_metric()
            return False

        # Admitted via release()
        wait_elapsed = time.monotonic() - wait_start
        self._total_admitted += 1
        self._record_wait(wait_elapsed)
        return True

    def release(self) -> None:
        """Release an admission slot after a request completes.

        If there are waiters in the queue, the highest-priority one
        is admitted immediately (the slot transfers directly). If the
        queue is empty, the in-flight count is decremented.

        Must be called exactly once for each successful acquire().
        """
        # We need to drain cancelled entries and find the next live waiter.
        # This runs synchronously because PriorityQueue.get_nowait() is
        # instant and we hold no awaitable state.
        while True:
            try:
                entry = self._waiters.get_nowait()
            except asyncio.QueueEmpty:
                # No waiters -- just free the slot
                self._in_flight -= 1
                self._update_in_flight_metric()
                self._update_queue_depth_metric()
                return

            if entry.cancelled:
                # Skip cancelled entries (timed-out waiters)
                continue

            # Transfer the slot to this waiter (in_flight stays the same)
            entry.event.set()
            self._update_queue_depth_metric()
            return

    @property
    def stats(self) -> dict[str, Any]:
        """Current admission controller statistics.

        Returns:
            Dictionary with queue depth, in-flight count, totals, and rates.
        """
        total_decisions = (
            self._total_admitted + self._total_rejected + self._total_timed_out
        )
        admission_rate = (
            self._total_admitted / total_decisions if total_decisions > 0 else 1.0
        )
        rejection_rate = (
            (self._total_rejected + self._total_timed_out) / total_decisions
            if total_decisions > 0
            else 0.0
        )

        return {
            "max_concurrent": self._max_concurrent,
            "queue_size": self._queue_size,
            "in_flight": self._in_flight,
            "queue_depth": self.queue_depth,
            "total_admitted": self._total_admitted,
            "total_rejected": self._total_rejected,
            "total_timed_out": self._total_timed_out,
            "admission_rate": round(admission_rate, 4),
            "rejection_rate": round(rejection_rate, 4),
        }

    def _record_admission(self, wait_start: float) -> None:
        """Record a fast-path admission in Prometheus metrics."""
        elapsed = time.monotonic() - wait_start
        if self._prom_admitted_total is not None:
            self._prom_admitted_total.inc()
        if self._prom_wait_seconds is not None:
            self._prom_wait_seconds.observe(elapsed)
        self._update_in_flight_metric()

    def _record_wait(self, elapsed: float) -> None:
        """Record a queued admission wait time in Prometheus metrics."""
        if self._prom_admitted_total is not None:
            self._prom_admitted_total.inc()
        if self._prom_wait_seconds is not None:
            self._prom_wait_seconds.observe(elapsed)

    def _record_rejection(self, reason: str) -> None:
        """Record a rejection in Prometheus metrics."""
        if self._prom_rejected_total is not None:
            self._prom_rejected_total.labels(reason=reason).inc()

    def _update_in_flight_metric(self) -> None:
        """Sync the Prometheus in-flight gauge."""
        if self._prom_in_flight is not None:
            self._prom_in_flight.set(self._in_flight)

    def _update_queue_depth_metric(self) -> None:
        """Sync the Prometheus queue depth gauge."""
        if self._prom_queue_depth is not None:
            self._prom_queue_depth.set(self.queue_depth)
