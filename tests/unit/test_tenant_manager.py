"""Unit tests for the TenantManager.

Tests budget enforcement, rate limiting, usage tracking, and
tenant isolation.
"""

from __future__ import annotations

from kvwarden.tenant.manager import TenantBudget, TenantManager, TenantRecord

# ---------------------------------------------------------------------------
# TenantBudget
# ---------------------------------------------------------------------------


class TestTenantBudget:
    """Tests for TenantBudget defaults and construction."""

    def test_defaults(self) -> None:
        budget = TenantBudget()
        assert budget.max_concurrent_requests == 64
        assert budget.rate_limit_rpm == 600
        assert budget.max_gpu_memory_gb == 40.0
        assert budget.priority == 1

    def test_custom_values(self) -> None:
        budget = TenantBudget(
            max_concurrent_requests=10,
            rate_limit_rpm=100,
            max_gpu_memory_gb=20.0,
            priority=5,
        )
        assert budget.max_concurrent_requests == 10
        assert budget.rate_limit_rpm == 100


# ---------------------------------------------------------------------------
# TenantRecord
# ---------------------------------------------------------------------------


class TestTenantRecord:
    """Tests for the TenantRecord class."""

    def test_snapshot_structure(self) -> None:
        budget = TenantBudget(max_concurrent_requests=4, rate_limit_rpm=60)
        record = TenantRecord("tenant-1", budget)

        snap = record.snapshot()
        assert snap["tenant_id"] == "tenant-1"
        assert "budget" in snap
        assert "usage" in snap
        assert snap["budget"]["max_concurrent_requests"] == 4
        assert snap["usage"]["request_count"] == 0

    async def test_acquire_and_release(self) -> None:
        budget = TenantBudget(max_concurrent_requests=2, rate_limit_rpm=1000)
        record = TenantRecord("tenant-1", budget)

        # Should be able to acquire 2 slots
        assert await record.try_acquire() is True
        assert await record.try_acquire() is True
        # Third should fail (at capacity)
        assert await record.try_acquire() is False

        # Release one
        await record.release()
        # Now can acquire again
        assert await record.try_acquire() is True

    async def test_rate_limiting(self) -> None:
        # Rate limit of 3 requests per minute
        budget = TenantBudget(max_concurrent_requests=100, rate_limit_rpm=3)
        record = TenantRecord("tenant-1", budget)

        # First 3 should succeed
        for _ in range(3):
            acquired = await record.try_acquire()
            assert acquired is True
            await record.release()

        # 4th should be rate-limited
        assert await record.try_acquire() is False

    async def test_record_completion(self) -> None:
        budget = TenantBudget()
        record = TenantRecord("tenant-1", budget)

        await record.record_completion(tokens_in=100, tokens_out=50, gpu_seconds=1.5)
        assert record.usage.request_count == 1
        assert record.usage.token_count_in == 100
        assert record.usage.token_count_out == 50
        assert abs(record.usage.gpu_seconds - 1.5) < 0.001

        await record.record_completion(tokens_in=200, tokens_out=100, gpu_seconds=2.0)
        assert record.usage.request_count == 2
        assert record.usage.token_count_in == 300


# ---------------------------------------------------------------------------
# TenantManager
# ---------------------------------------------------------------------------


class TestTenantManager:
    """Tests for the TenantManager class."""

    async def test_register_and_get(self) -> None:
        mgr = TenantManager()
        record = await mgr.register_tenant("t1")
        assert record.tenant_id == "t1"

        fetched = await mgr.get_tenant("t1")
        assert fetched is record

    async def test_get_nonexistent_returns_none(self) -> None:
        mgr = TenantManager()
        assert await mgr.get_tenant("nope") is None

    async def test_get_or_create_auto_registers(self) -> None:
        mgr = TenantManager()
        record = await mgr.get_or_create_tenant("new-tenant")
        assert record.tenant_id == "new-tenant"
        assert "new-tenant" in mgr.list_tenants()

    async def test_custom_default_budget(self) -> None:
        default = TenantBudget(max_concurrent_requests=5, rate_limit_rpm=10)
        mgr = TenantManager(default_budget=default)
        record = await mgr.get_or_create_tenant("t1")
        assert record.budget.max_concurrent_requests == 5
        assert record.budget.rate_limit_rpm == 10

    async def test_register_with_custom_budget(self) -> None:
        mgr = TenantManager()
        budget = TenantBudget(max_concurrent_requests=2, rate_limit_rpm=30)
        record = await mgr.register_tenant("t1", budget=budget)
        assert record.budget.max_concurrent_requests == 2

    async def test_try_acquire_auto_registers(self) -> None:
        mgr = TenantManager()
        result = await mgr.try_acquire_for_tenant("auto-tenant")
        assert result is True
        assert "auto-tenant" in mgr.list_tenants()
        await mgr.release_for_tenant("auto-tenant")

    async def test_budget_enforcement(self) -> None:
        budget = TenantBudget(max_concurrent_requests=1, rate_limit_rpm=1000)
        mgr = TenantManager()
        await mgr.register_tenant("t1", budget=budget)

        # First request allowed
        assert await mgr.try_acquire_for_tenant("t1") is True
        # Second request blocked (only 1 concurrent allowed)
        assert await mgr.try_acquire_for_tenant("t1") is False
        # Release and try again
        await mgr.release_for_tenant("t1")
        assert await mgr.try_acquire_for_tenant("t1") is True
        await mgr.release_for_tenant("t1")

    async def test_tenant_isolation(self) -> None:
        """One tenant's burst should not affect another tenant."""
        mgr = TenantManager()
        budget_small = TenantBudget(max_concurrent_requests=1, rate_limit_rpm=1000)
        budget_large = TenantBudget(max_concurrent_requests=10, rate_limit_rpm=1000)
        await mgr.register_tenant("small", budget=budget_small)
        await mgr.register_tenant("large", budget=budget_large)

        # Small tenant maxes out
        assert await mgr.try_acquire_for_tenant("small") is True
        assert await mgr.try_acquire_for_tenant("small") is False

        # Large tenant is unaffected
        assert await mgr.try_acquire_for_tenant("large") is True
        assert await mgr.try_acquire_for_tenant("large") is True

        await mgr.release_for_tenant("small")
        await mgr.release_for_tenant("large")
        await mgr.release_for_tenant("large")

    async def test_record_completion_via_manager(self) -> None:
        mgr = TenantManager()
        await mgr.register_tenant("t1")
        await mgr.record_completion("t1", tokens_in=50, tokens_out=25)

        record = await mgr.get_tenant("t1")
        assert record is not None
        assert record.usage.request_count == 1
        assert record.usage.token_count_in == 50

    async def test_list_tenants(self) -> None:
        mgr = TenantManager()
        await mgr.register_tenant("a")
        await mgr.register_tenant("b")
        await mgr.register_tenant("c")
        tenants = mgr.list_tenants()
        assert sorted(tenants) == ["a", "b", "c"]

    async def test_snapshot(self) -> None:
        mgr = TenantManager()
        await mgr.register_tenant("t1")
        await mgr.record_completion("t1", tokens_in=100, tokens_out=50)

        snap = mgr.snapshot()
        assert "t1" in snap
        assert snap["t1"]["usage"]["request_count"] == 1
        assert snap["t1"]["usage"]["token_count_in"] == 100
