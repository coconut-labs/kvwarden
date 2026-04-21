"""Token-bucket rate-limit tests for TenantRecord.

The token bucket replaced a 60-second sliding-window list-of-timestamps.
The window had a real cost — Gate 2-FAIRNESS Arm 5 saw a ~30s warmup
transient where flooder bursts were allowed before the window filled
and 429s started firing. Token bucket fires the moment the bucket runs
dry, with no warmup. Backward compat: burst defaults to rate_limit_rpm
which gives sliding-window-equivalent semantics for existing configs.

See `results/gate2_fairness_20260419/GATE2_FAIRNESS_OUTCOME.md` for the
empirical motivation.
"""

from __future__ import annotations

import asyncio

import pytest

from kvwarden.tenant.manager import TenantBudget, TenantRecord


class TestTokenBucketDefaults:
    """Backward-compat: burst=None defaults to rate_limit_rpm."""

    def test_default_capacity_matches_rpm(self) -> None:
        budget = TenantBudget(rate_limit_rpm=600)  # burst=None
        record = TenantRecord("t", budget)
        assert record._token_capacity == 600.0

    def test_explicit_burst_overrides_default(self) -> None:
        budget = TenantBudget(rate_limit_rpm=600, rate_limit_burst=10)
        record = TenantRecord("t", budget)
        assert record._token_capacity == 10.0

    def test_refill_rate_independent_of_burst(self) -> None:
        b1 = TenantBudget(rate_limit_rpm=600, rate_limit_burst=600)
        b2 = TenantBudget(rate_limit_rpm=600, rate_limit_burst=10)
        # Both refill at 10 tokens/sec; only the cap differs.
        assert TenantRecord("t1", b1)._refill_per_sec == 10.0
        assert TenantRecord("t2", b2)._refill_per_sec == 10.0


class TestTokenBucketBurst:
    """Bucket allows up to `burst` immediate acquires before throttling."""

    @pytest.mark.asyncio
    async def test_tight_bucket_throttles_at_t0(self) -> None:
        # rpm=60 (1/s refill), burst=3. Should allow 3 then 429 immediately.
        budget = TenantBudget(
            max_concurrent_requests=999, rate_limit_rpm=60, rate_limit_burst=3
        )
        record = TenantRecord("t", budget)
        for _ in range(3):
            assert await record.try_acquire() is True
            await record.release()
        assert await record.try_acquire() is False

    @pytest.mark.asyncio
    async def test_default_burst_allows_full_rpm_burst(self) -> None:
        # burst=None defaults to rpm. Backward-compat with prior sliding-window.
        budget = TenantBudget(max_concurrent_requests=999, rate_limit_rpm=5)
        record = TenantRecord("t", budget)
        for _ in range(5):
            assert await record.try_acquire() is True
            await record.release()
        assert await record.try_acquire() is False


class TestTokenBucketRefill:
    """Bucket refills at rpm/60 tokens/sec up to capacity."""

    @pytest.mark.asyncio
    async def test_refill_after_drain(self) -> None:
        # rpm=600 (10/s refill), burst=3. Drain, sleep, expect refill.
        budget = TenantBudget(
            max_concurrent_requests=999, rate_limit_rpm=600, rate_limit_burst=3
        )
        record = TenantRecord("t", budget)
        for _ in range(3):
            assert await record.try_acquire() is True
            await record.release()
        # Drained — next acquire fails.
        assert await record.try_acquire() is False
        # Sleep 0.25s → ~2.5 tokens refill (10 tokens/sec). Two more acquires ok.
        await asyncio.sleep(0.25)
        assert await record.try_acquire() is True
        await record.release()
        assert await record.try_acquire() is True
        await record.release()
        # Third would need full token — likely not yet available without more wait.
        # Don't assert on it — timing-sensitive. The two-acquire test is enough.

    @pytest.mark.asyncio
    async def test_refill_caps_at_capacity(self) -> None:
        # Long sleep should NOT overflow capacity.
        budget = TenantBudget(
            max_concurrent_requests=999, rate_limit_rpm=600, rate_limit_burst=3
        )
        record = TenantRecord("t", budget)
        # Drain.
        for _ in range(3):
            assert await record.try_acquire() is True
            await record.release()
        # Sleep way longer than would refill capacity.
        await asyncio.sleep(0.6)  # 6 tokens at 10/s, capped at 3.
        # Still only 3 acquires possible, not 6.
        for _ in range(3):
            assert await record.try_acquire() is True
            await record.release()
        assert await record.try_acquire() is False


class TestTokenBucketIsolation:
    """A failed concurrency check must NOT consume a token."""

    @pytest.mark.asyncio
    async def test_concurrency_failure_preserves_token(self) -> None:
        # Tight burst, concurrency=1. First acquire takes both. Second fails on
        # CONCURRENCY (not rate limit) — token must be preserved for later.
        budget = TenantBudget(
            max_concurrent_requests=1, rate_limit_rpm=60, rate_limit_burst=2
        )
        record = TenantRecord("t", budget)
        assert await record.try_acquire() is True
        # Tokens went 2 → 1, available went 1 → 0.
        # Second acquire should fail on concurrency, NOT consume the second token.
        assert await record.try_acquire() is False
        # Release concurrency. Token should still be available.
        await record.release()
        # Now we should be able to acquire again — token still there.
        assert await record.try_acquire() is True
        await record.release()


class TestNoWarmupTransient:
    """The motivating Gate 2-FAIRNESS Arm 5 caveat: tight bucket fires from t=0,
    no 30s warmup."""

    @pytest.mark.asyncio
    async def test_flooder_throttled_immediately(self) -> None:
        # rpm=600, burst=10 (1 second's worth at 10 RPS sustained). A flooder
        # offering 32 RPS gets 10 through immediately, then rate-limited from
        # there. With the OLD sliding window, flooder would have gotten ~600
        # through over 19 seconds before any 429s.
        budget = TenantBudget(
            max_concurrent_requests=9999,
            rate_limit_rpm=600,
            rate_limit_burst=10,
        )
        record = TenantRecord("flooder", budget)
        # Try 50 acquires back-to-back. ~10 should succeed, the rest fail.
        successes = 0
        for _ in range(50):
            if await record.try_acquire():
                successes += 1
                await record.release()
        # At t=0 with burst=10, exactly 10 should succeed (or at most 10 plus
        # a tiny refill if event loop scheduling delays arose). 50 is safely
        # bounded above by 10 + small timing slop.
        assert successes >= 10, f"expected at least the burst (10), got {successes}"
        assert successes <= 12, (
            f"expected at most the burst + tiny refill, got {successes} — "
            "warmup transient may have regressed"
        )
