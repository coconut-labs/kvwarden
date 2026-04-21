"""Per-tenant resource management for KVWarden.

Tracks budgets, enforces rate limits, and isolates tenants so that
one tenant's burst cannot starve others.  Everything is in-memory --
no external database required.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any


@dataclass
class TenantBudget:
    """Resource budget for a single tenant."""

    max_concurrent_requests: int = 64
    rate_limit_rpm: int = 600
    # Token-bucket burst capacity (max tokens). None = sliding-window-equivalent
    # (capacity = rate_limit_rpm). Set to a small value (e.g. rate_limit_rpm/60)
    # for a tight bucket that engages from t=0 with no warmup transient — the
    # Gate 2-FAIRNESS lesson. Bucket refills at rate_limit_rpm/60 tokens/sec.
    rate_limit_burst: int | None = None
    max_gpu_memory_gb: float = 40.0
    priority: int = 1


@dataclass
class TenantUsage:
    """Live usage counters for a single tenant."""

    request_count: int = 0
    token_count_in: int = 0
    token_count_out: int = 0
    gpu_seconds: float = 0.0
    active_requests: int = 0


class TenantRecord:
    """Combines budget, usage, and concurrency control for one tenant.

    Args:
        tenant_id: Unique tenant identifier.
        budget: Resource budget for this tenant.
    """

    def __init__(self, tenant_id: str, budget: TenantBudget) -> None:
        self.tenant_id = tenant_id
        self.budget = budget
        self.usage = TenantUsage()
        self._semaphore = asyncio.Semaphore(budget.max_concurrent_requests)
        self._available = budget.max_concurrent_requests
        self._lock = asyncio.Lock()
        # Token-bucket rate limit. capacity = burst (default rate_limit_rpm
        # for sliding-window backward compat); refill_rate = rpm/60.
        # Replaces the prior 60s-window list-of-timestamps. The window
        # design had a real cost: at t=0 it allowed `rate_limit_rpm`
        # bursts before any 429s fired (Gate 2-FAIRNESS Arm 5 caveat —
        # ~30s warmup transient before quiet tenant saw baseline TTFT).
        # Token bucket fires immediately when the bucket runs dry.
        self._token_capacity: float = float(
            budget.rate_limit_burst
            if budget.rate_limit_burst is not None
            else budget.rate_limit_rpm
        )
        self._tokens: float = self._token_capacity
        self._refill_per_sec: float = budget.rate_limit_rpm / 60.0
        self._last_refill_t: float = time.monotonic()

    async def try_acquire(self) -> bool:
        """Try to acquire a request slot without blocking.

        Checks the token-bucket rate limit AND concurrency cap atomically.
        A failed concurrency check does NOT consume a token (we won't
        process the request, so don't penalize the rate budget).

        Returns:
            True if the request is allowed, False if it should be rejected.
        """
        async with self._lock:
            # Refill tokens up to capacity based on elapsed wall time.
            now = time.monotonic()
            self._tokens = min(
                self._token_capacity,
                self._tokens + (now - self._last_refill_t) * self._refill_per_sec,
            )
            self._last_refill_t = now

            if self._tokens < 1.0:
                return False
            if self._available <= 0:
                return False

            # Both gates pass — consume.
            self._tokens -= 1.0
            self._available -= 1
            self.usage.active_requests += 1

        # Acquire the semaphore outside the lock; matches `_available`
        # so this is non-blocking in practice.
        await self._semaphore.acquire()
        return True

    async def release(self) -> None:
        """Release a request slot after completion."""
        self._semaphore.release()
        self._available += 1
        async with self._lock:
            self.usage.active_requests = max(0, self.usage.active_requests - 1)

    def priority_score(self) -> int:
        """Deficit-Round-Robin priority for the AdmissionController.

        Lower returned value = served first (matches AdmissionController and
        BUCKET_PRIORITY convention). A tenant that has hogged more in-flight
        slots gets a higher score and waits behind a quieter tenant. Ties
        broken by FIFO sequence inside the AdmissionController.

        Formula: ``active_requests * 10 + budget.priority``.
        - ``active_requests * 10`` is the deficit weight: each in-flight
          request pushes the tenant's next request 10 priority "rungs" back.
        - ``budget.priority`` is the per-tenant baseline (lower is more
          important; default 1). Use it for static tenant tiers.

        Read-only — does not mutate state, no lock required. The
        ``active_requests`` int read is atomic in CPython.
        """
        return self.usage.active_requests * 10 + self.budget.priority

    async def record_completion(
        self,
        tokens_in: int = 0,
        tokens_out: int = 0,
        gpu_seconds: float = 0.0,
    ) -> None:
        """Record usage from a completed request.

        Args:
            tokens_in: Input tokens consumed.
            tokens_out: Output tokens generated.
            gpu_seconds: GPU-seconds used.
        """
        async with self._lock:
            self.usage.request_count += 1
            self.usage.token_count_in += tokens_in
            self.usage.token_count_out += tokens_out
            self.usage.gpu_seconds += gpu_seconds

    def snapshot(self) -> dict[str, Any]:
        """Return a plain-dict snapshot of this tenant's state.

        Returns:
            Dictionary with budget and usage data.
        """
        return {
            "tenant_id": self.tenant_id,
            "budget": {
                "max_concurrent_requests": self.budget.max_concurrent_requests,
                "rate_limit_rpm": self.budget.rate_limit_rpm,
                "rate_limit_burst": self._token_capacity,
                "max_gpu_memory_gb": self.budget.max_gpu_memory_gb,
                "priority": self.budget.priority,
            },
            "usage": {
                "request_count": self.usage.request_count,
                "token_count_in": self.usage.token_count_in,
                "token_count_out": self.usage.token_count_out,
                "gpu_seconds": round(self.usage.gpu_seconds, 2),
                "active_requests": self.usage.active_requests,
                "rate_limit_tokens_remaining": round(self._tokens, 2),
            },
        }


class TenantManager:
    """In-memory tenant registry with budget enforcement.

    Provides request isolation so one tenant's burst cannot starve others.
    Thread-safe via asyncio locks.

    Args:
        default_budget: Default budget applied to auto-registered tenants.
    """

    def __init__(self, default_budget: TenantBudget | None = None) -> None:
        self._default_budget = default_budget or TenantBudget()
        self._tenants: dict[str, TenantRecord] = {}
        self._lock = asyncio.Lock()

    async def register_tenant(
        self,
        tenant_id: str,
        budget: TenantBudget | None = None,
    ) -> TenantRecord:
        """Register a new tenant or update an existing one.

        Args:
            tenant_id: Unique tenant identifier.
            budget: Resource budget. Uses default if not specified.

        Returns:
            The TenantRecord for this tenant.
        """
        async with self._lock:
            if tenant_id in self._tenants:
                if budget is not None:
                    self._tenants[tenant_id].budget = budget
                return self._tenants[tenant_id]

            record = TenantRecord(
                tenant_id=tenant_id,
                budget=budget
                or TenantBudget(
                    max_concurrent_requests=self._default_budget.max_concurrent_requests,
                    rate_limit_rpm=self._default_budget.rate_limit_rpm,
                    rate_limit_burst=self._default_budget.rate_limit_burst,
                    max_gpu_memory_gb=self._default_budget.max_gpu_memory_gb,
                    priority=self._default_budget.priority,
                ),
            )
            self._tenants[tenant_id] = record
            return record

    async def get_tenant(self, tenant_id: str) -> TenantRecord | None:
        """Look up a tenant by ID.

        Args:
            tenant_id: Tenant identifier.

        Returns:
            TenantRecord if found, None otherwise.
        """
        return self._tenants.get(tenant_id)

    async def get_or_create_tenant(self, tenant_id: str) -> TenantRecord:
        """Get an existing tenant or auto-register with defaults.

        Args:
            tenant_id: Tenant identifier.

        Returns:
            The TenantRecord for this tenant.
        """
        record = self._tenants.get(tenant_id)
        if record is not None:
            return record
        return await self.register_tenant(tenant_id)

    async def try_acquire_for_tenant(self, tenant_id: str) -> bool:
        """Attempt to acquire a request slot for the given tenant.

        Auto-registers unknown tenants with default budgets.

        Args:
            tenant_id: Tenant identifier.

        Returns:
            True if the request is allowed.
        """
        record = await self.get_or_create_tenant(tenant_id)
        return await record.try_acquire()

    async def release_for_tenant(self, tenant_id: str) -> None:
        """Release a request slot for the given tenant.

        Args:
            tenant_id: Tenant identifier.
        """
        record = self._tenants.get(tenant_id)
        if record is not None:
            await record.release()

    async def record_completion(
        self,
        tenant_id: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        gpu_seconds: float = 0.0,
    ) -> None:
        """Record usage for a completed request.

        Args:
            tenant_id: Tenant identifier.
            tokens_in: Input tokens consumed.
            tokens_out: Output tokens generated.
            gpu_seconds: GPU-seconds used.
        """
        record = self._tenants.get(tenant_id)
        if record is not None:
            await record.record_completion(tokens_in, tokens_out, gpu_seconds)

    def list_tenants(self) -> list[str]:
        """Return all registered tenant IDs.

        Returns:
            List of tenant ID strings.
        """
        return list(self._tenants.keys())

    def snapshot(self) -> dict[str, Any]:
        """Return a plain-dict snapshot of all tenants.

        Returns:
            Dictionary mapping tenant IDs to their snapshots.
        """
        return {tid: record.snapshot() for tid, record in self._tenants.items()}
