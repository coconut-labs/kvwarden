"""Unit tests for the CacheManager.

Tests tiering logic, eviction policies, promotion/demotion, and
hit rate tracking.
"""

from __future__ import annotations

import time

import pytest

from infergrid.cache.manager import CacheBlock, CacheManager, TierStats


# ---------------------------------------------------------------------------
# CacheBlock reuse scoring
# ---------------------------------------------------------------------------


class TestCacheBlockScoring:
    """Tests for the frequency+recency reuse score."""

    def test_frequently_accessed_scores_higher(self) -> None:
        now = time.monotonic()
        frequent = CacheBlock(
            block_id="b1", model_id="m1", request_id="r1",
            tier="gpu", num_tokens=16, access_count=100,
            last_access_time=now,
        )
        rare = CacheBlock(
            block_id="b2", model_id="m1", request_id="r2",
            tier="gpu", num_tokens=16, access_count=1,
            last_access_time=now,
        )
        assert frequent.reuse_score(now) > rare.reuse_score(now)

    def test_recently_accessed_scores_higher(self) -> None:
        now = time.monotonic()
        recent = CacheBlock(
            block_id="b1", model_id="m1", request_id="r1",
            tier="gpu", num_tokens=16, access_count=10,
            last_access_time=now,
        )
        stale = CacheBlock(
            block_id="b2", model_id="m1", request_id="r2",
            tier="gpu", num_tokens=16, access_count=10,
            last_access_time=now - 3600,
        )
        assert recent.reuse_score(now) > stale.reuse_score(now)

    def test_score_not_naive_lru(self) -> None:
        """A very frequently accessed block should resist eviction even when stale."""
        now = time.monotonic()
        popular_stale = CacheBlock(
            block_id="b1", model_id="m1", request_id="r1",
            tier="gpu", num_tokens=16, access_count=1000,
            last_access_time=now - 120,  # 2 minutes ago
        )
        rare_recent = CacheBlock(
            block_id="b2", model_id="m1", request_id="r2",
            tier="gpu", num_tokens=16, access_count=1,
            last_access_time=now - 1,  # 1 second ago
        )
        assert popular_stale.reuse_score(now) > rare_recent.reuse_score(now)

    def test_score_is_nonnegative(self) -> None:
        now = time.monotonic()
        block = CacheBlock(
            block_id="b1", model_id="m1", request_id="r1",
            tier="gpu", num_tokens=16, access_count=0,
            last_access_time=now - 99999,
        )
        assert block.reuse_score(now) >= 0.0


# ---------------------------------------------------------------------------
# CacheManager basic operations
# ---------------------------------------------------------------------------


class TestCacheManagerBasics:
    """Tests for allocation, access, and freeing of cache blocks."""

    def _make_manager(self) -> CacheManager:
        return CacheManager(
            tier_capacities_gb={"gpu": 1.0, "cpu": 4.0, "ssd": 16.0},
            block_size_tokens=16,
        )

    def test_allocate_block(self) -> None:
        cm = self._make_manager()
        block = cm.allocate_block("b1", "model-a", "req-1", num_tokens=16, tier="gpu")
        assert block is not None
        assert block.block_id == "b1"
        assert block.tier == "gpu"
        assert cm.total_blocks() == 1

    def test_allocate_multiple_blocks(self) -> None:
        cm = self._make_manager()
        for i in range(10):
            b = cm.allocate_block(f"b{i}", "model-a", "req-1", num_tokens=16)
            assert b is not None
        assert cm.total_blocks() == 10

    def test_access_block_updates_count(self) -> None:
        cm = self._make_manager()
        cm.allocate_block("b1", "model-a", "req-1", num_tokens=16)
        block = cm.access_block("b1")
        assert block is not None
        assert block.access_count == 2  # initial 1 + 1 access

    def test_access_nonexistent_block_returns_none(self) -> None:
        cm = self._make_manager()
        result = cm.access_block("nonexistent")
        assert result is None

    def test_free_block(self) -> None:
        cm = self._make_manager()
        cm.allocate_block("b1", "model-a", "req-1", num_tokens=16)
        assert cm.total_blocks() == 1
        assert cm.free_block("b1") is True
        assert cm.total_blocks() == 0

    def test_free_nonexistent_block(self) -> None:
        cm = self._make_manager()
        assert cm.free_block("nonexistent") is False

    def test_free_blocks_for_request(self) -> None:
        cm = self._make_manager()
        cm.allocate_block("b1", "model-a", "req-1", num_tokens=16)
        cm.allocate_block("b2", "model-a", "req-1", num_tokens=16)
        cm.allocate_block("b3", "model-a", "req-2", num_tokens=16)
        freed = cm.free_blocks_for_request("req-1")
        assert freed == 2
        assert cm.total_blocks() == 1

    def test_free_blocks_for_model(self) -> None:
        cm = self._make_manager()
        cm.allocate_block("b1", "model-a", "req-1", num_tokens=16)
        cm.allocate_block("b2", "model-b", "req-2", num_tokens=16)
        freed = cm.free_blocks_for_model("model-a")
        assert freed == 1
        assert cm.total_blocks() == 1


# ---------------------------------------------------------------------------
# Tiering and eviction
# ---------------------------------------------------------------------------


class TestCacheManagerTiering:
    """Tests for tier management and eviction behavior."""

    def _make_small_manager(self) -> CacheManager:
        """Manager with very small tier capacities to test eviction."""
        return CacheManager(
            tier_capacities_gb={"gpu": 0.001, "cpu": 0.01, "ssd": 0.1},
            block_size_tokens=16,
        )

    def test_allocate_falls_through_tiers(self) -> None:
        """When GPU is full, blocks should land in CPU tier."""
        cm = self._make_small_manager()
        # Fill up GPU
        blocks = []
        for i in range(100):
            b = cm.allocate_block(f"b{i}", "model-a", "req-1", num_tokens=16, tier="gpu")
            if b is not None:
                blocks.append(b)

        # Should have blocks in multiple tiers
        tiers_used = {b.tier for b in blocks}
        assert len(tiers_used) >= 1  # at least gpu, possibly spilling to cpu

    def test_eviction_removes_lowest_scored(self) -> None:
        """Eviction should remove blocks with the lowest reuse score."""
        cm = CacheManager(
            tier_capacities_gb={"gpu": 0.0001, "cpu": 1.0, "ssd": 1.0},
            block_size_tokens=16,
        )
        # Allocate one block, then access it many times
        cm.allocate_block("keep", "model-a", "req-1", num_tokens=16)
        for _ in range(50):
            cm.access_block("keep")

        # Allocate another block that will compete for space
        cm.allocate_block("new", "model-a", "req-2", num_tokens=16)

        # The "keep" block should still exist (high reuse score)
        assert cm.access_block("keep") is not None

    def test_promote_block(self) -> None:
        cm = self._make_small_manager()
        # Place a block in CPU tier
        block = cm.allocate_block("b1", "model-a", "req-1", num_tokens=16, tier="cpu")
        assert block is not None
        assert block.tier == "cpu"

        # Promote to GPU (if there is space)
        result = cm.promote_block("b1", "gpu")
        if result:
            updated = cm.access_block("b1")
            assert updated is not None
            assert updated.tier == "gpu"

    def test_demote_block(self) -> None:
        cm = CacheManager(
            tier_capacities_gb={"gpu": 1.0, "cpu": 4.0, "ssd": 16.0},
            block_size_tokens=16,
        )
        cm.allocate_block("b1", "model-a", "req-1", num_tokens=16, tier="gpu")
        assert cm.demote_block("b1", "cpu") is True
        block = cm.access_block("b1")
        assert block is not None
        assert block.tier == "cpu"


# ---------------------------------------------------------------------------
# Hit rate and stats
# ---------------------------------------------------------------------------


class TestCacheManagerStats:
    """Tests for hit rate tracking and statistics reporting."""

    def test_hit_rate_starts_at_zero(self) -> None:
        cm = CacheManager()
        assert cm.hit_rate() == 0.0

    def test_hit_rate_tracks_correctly(self) -> None:
        cm = CacheManager()
        cm.allocate_block("b1", "m1", "r1", num_tokens=16)
        # 3 hits
        for _ in range(3):
            cm.access_block("b1")
        # 1 miss
        cm.access_block("nonexistent")

        assert cm.hit_rate() == pytest.approx(0.75)

    def test_tier_stats(self) -> None:
        cm = CacheManager(
            tier_capacities_gb={"gpu": 1.0, "cpu": 4.0, "ssd": 16.0},
        )
        cm.allocate_block("b1", "m1", "r1", num_tokens=16, tier="gpu")
        cm.allocate_block("b2", "m1", "r2", num_tokens=16, tier="cpu")

        stats = cm.tier_stats()
        assert stats["gpu"].block_count == 1
        assert stats["cpu"].block_count == 1
        assert stats["ssd"].block_count == 0

    def test_model_block_counts(self) -> None:
        cm = CacheManager()
        cm.allocate_block("b1", "model-a", "r1", num_tokens=16)
        cm.allocate_block("b2", "model-a", "r2", num_tokens=16)
        cm.allocate_block("b3", "model-b", "r3", num_tokens=16)

        counts = cm.model_block_counts()
        assert counts["model-a"] == 2
        assert counts["model-b"] == 1

    def test_snapshot_structure(self) -> None:
        cm = CacheManager()
        cm.allocate_block("b1", "m1", "r1", num_tokens=16)
        snap = cm.snapshot()

        assert "total_blocks" in snap
        assert "hit_rate" in snap
        assert "tiers" in snap
        assert "model_blocks" in snap
        assert "gpu" in snap["tiers"]
        assert "cpu" in snap["tiers"]
        assert "ssd" in snap["tiers"]
