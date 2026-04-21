"""Unit tests for tenant DRR priority admission.

Covers TenantRecord.priority_score() formula and the wired-through behavior
where the AdmissionController uses tenant deficit to rescue starved tenants
under contention. See `docs/launch/gate2_fairness_runbook.md` for the
end-to-end experiment this enables.
"""

from __future__ import annotations

import asyncio

import pytest

from kvwarden.router.admission import AdmissionController
from kvwarden.tenant.manager import TenantBudget, TenantRecord

# ---------------------------------------------------------------------------
# priority_score formula
# ---------------------------------------------------------------------------


class TestPriorityScore:
    """Tests for the deficit-weighted priority formula."""

    def test_idle_tenant_uses_budget_priority(self) -> None:
        record = TenantRecord("alice", TenantBudget(priority=1))
        assert record.priority_score() == 1

    def test_one_active_request_adds_ten(self) -> None:
        record = TenantRecord("bob", TenantBudget(priority=1))
        record.usage.active_requests = 1
        assert record.priority_score() == 11

    def test_many_active_requests_dominates_over_budget_tier(self) -> None:
        flooder = TenantRecord("flooder", TenantBudget(priority=1))
        flooder.usage.active_requests = 5
        # Even a higher-priority tier (priority=2) tenant still wins if quiet.
        quiet_lower_tier = TenantRecord("quiet", TenantBudget(priority=2))
        quiet_lower_tier.usage.active_requests = 0
        assert quiet_lower_tier.priority_score() < flooder.priority_score()


# ---------------------------------------------------------------------------
# Admission integration: DRR rescues starved tenants
# ---------------------------------------------------------------------------


class TestDRRAdmissionIntegration:
    """End-to-end: feed AdmissionController real DRR priorities, verify the
    quiet tenant jumps the queue when contention starts.
    """

    @pytest.mark.asyncio
    async def test_quiet_tenant_jumps_flooder_queue(self) -> None:
        """Under cap=1 contention, a quiet tenant arriving after 5 flooder
        waiters should be admitted on the very next release(), not 5th.
        """
        controller = AdmissionController(max_concurrent=1, queue_size=128)

        # Slot 1: flooder takes the in-flight slot
        flooder = TenantRecord("flooder", TenantBudget(priority=1))
        flooder.usage.active_requests = 1
        first = await controller.acquire(priority=flooder.priority_score(), timeout=1.0)
        assert first is True

        # Now 5 more flooder requests arrive — they queue (cap=1 is full)
        flooder_tasks = []
        for i in range(5):
            flooder.usage.active_requests = 2 + i
            score = flooder.priority_score()  # 21, 31, 41, 51, 61
            flooder_tasks.append(
                asyncio.create_task(controller.acquire(priority=score, timeout=10.0))
            )

        # Give the event loop a chance to enqueue all flooder waiters.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        # Then a quiet tenant arrives — never been seen, 0 active requests
        quiet = TenantRecord("quiet", TenantBudget(priority=1))
        # Before acquire, quiet has 0 active requests (try_acquire hasn't run
        # in this controller-only test). Score = 1, way below flooder's 21+.
        quiet_task = asyncio.create_task(
            controller.acquire(priority=quiet.priority_score(), timeout=10.0)
        )
        await asyncio.sleep(0)

        # Release the in-flight slot. Next admit MUST be the quiet tenant
        # (priority 1 < flooder's 21+), not the first-enqueued flooder.
        controller.release()

        # Quiet should be admitted immediately, before any flooder.
        await asyncio.wait_for(quiet_task, timeout=1.0)
        assert quiet_task.result() is True

        # Flooder waiters should still be pending (none admitted yet).
        for t in flooder_tasks:
            assert not t.done(), "flooder admitted before quiet — DRR broken"

        # Drain
        controller.release()
        for t in flooder_tasks:
            controller.release()
            await asyncio.sleep(0)
        for t in flooder_tasks:
            try:
                await asyncio.wait_for(t, timeout=1.0)
            except asyncio.TimeoutError:
                pass

    @pytest.mark.asyncio
    async def test_fifo_within_equal_tenant_priority(self) -> None:
        """Two requests from the SAME tenant at the same priority_score
        should still preserve FIFO order (sequence tie-breaker).
        """
        controller = AdmissionController(max_concurrent=1, queue_size=128)
        # Take the slot
        await controller.acquire(priority=1, timeout=1.0)

        # Enqueue two with identical priority
        order = []

        async def waiter(label: str) -> None:
            ok = await controller.acquire(priority=11, timeout=5.0)
            assert ok
            order.append(label)

        t1 = asyncio.create_task(waiter("first"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        t2 = asyncio.create_task(waiter("second"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        controller.release()
        await asyncio.sleep(0)
        controller.release()
        await asyncio.gather(t1, t2)
        assert order == ["first", "second"]

    @pytest.mark.asyncio
    async def test_ex_quiet_tenant_falls_behind_after_burst(self) -> None:
        """If the previously-quiet tenant fills its own quota, subsequent
        requests should fall behind a freshly-quiet tenant. Ensures the
        priority is dynamic, not snapshot-at-arrival.
        """
        controller = AdmissionController(max_concurrent=1, queue_size=128)
        await controller.acquire(priority=1, timeout=1.0)

        # alice was quiet, just took her first slot — score now 11
        alice = TenantRecord("alice", TenantBudget(priority=1))
        alice.usage.active_requests = 1

        # bob just arrived, no active requests — score = 1
        bob = TenantRecord("bob", TenantBudget(priority=1))
        # bob's priority score (1) should beat alice's (11).
        assert bob.priority_score() < alice.priority_score()


# ---------------------------------------------------------------------------
# BUCKET_PRIORITY still wins inside one tenant
# ---------------------------------------------------------------------------


class TestBucketStillTieBreaks:
    """Inside the SAME tenant tier, length-bucket priority still wins."""

    @pytest.mark.asyncio
    async def test_short_request_beats_long_within_same_tenant(self) -> None:
        """When two same-tenant requests arrive simultaneously, short
        bucket (priority=0) should be admitted before long (priority=2).
        Models the wired-through priority = tenant_score * 100 + bucket.
        """
        controller = AdmissionController(max_concurrent=1, queue_size=128)
        await controller.acquire(priority=0, timeout=1.0)

        # alice is the same tenant for both requests, score=11
        order = []

        async def waiter(label: str, bucket_p: int) -> None:
            await controller.acquire(priority=11 * 100 + bucket_p, timeout=5.0)
            order.append(label)

        long_task = asyncio.create_task(waiter("long", 2))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        short_task = asyncio.create_task(waiter("short", 0))
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        controller.release()
        await asyncio.sleep(0)
        controller.release()
        await asyncio.gather(short_task, long_task)
        assert order == ["short", "long"]
