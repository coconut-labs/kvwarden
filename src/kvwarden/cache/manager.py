"""KV cache management for KVWarden.

Tracks KV cache blocks across three tiers (GPU HBM, CPU RAM, SSD) and
implements frequency+recency weighted eviction.  This is the orchestration
layer's *bookkeeper* -- the actual KV cache memory lives inside the
vLLM/SGLang engine processes.

Optional LMCache integration: if ``lmcache`` is importable, the manager
can delegate to it.  Otherwise it operates as a standalone tracker.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Tier ordering from fastest to slowest
TIER_ORDER: list[str] = ["gpu", "cpu", "ssd"]


@dataclass
class TenantPolicy:
    """Tenant-weight policy for KV eviction (T2 — issue #103).

    Currently a no-op surface — the API is reserved so test fixtures and the
    Gate 3 bench config can be written before the W4-W6 implementation lands.
    See `docs/rfcs/T2-tenant-aware-eviction.md` for the locked semantics.

    `tenant_weights` will eventually scale a block's `reuse_score` by the
    block's `tenant_id` weight (default 1.0 for unknown tenants), so a flooder
    tenant's blocks score down under eviction pressure. Default empty dict =
    LRU-equivalent behavior; passing `None` to `reuse_score` does the same.
    """

    tenant_weights: dict[str, float] = field(default_factory=dict)


@dataclass
class CacheBlock:
    """Metadata for a single KV cache block.

    Attributes:
        block_id: Unique block identifier.
        model_id: Model this block belongs to.
        request_id: Originating request (empty string for shared prefix blocks).
        tier: Current storage tier ("gpu", "cpu", "ssd").
        num_tokens: Number of tokens stored in this block.
        access_count: Total number of accesses.
        last_access_time: Monotonic timestamp of last access.
        created_time: Monotonic timestamp of creation.
        tenant_id: Originating tenant (T2 — None until the hot path tags blocks).
    """

    block_id: str
    model_id: str
    request_id: str
    tier: str
    num_tokens: int
    access_count: int = 1
    last_access_time: float = field(default_factory=time.monotonic)
    created_time: float = field(default_factory=time.monotonic)
    tenant_id: str | None = None

    def reuse_score(
        self,
        now: float,
        freq_weight: float = 0.7,
        recency_weight: float = 0.3,
        decay_half_life_s: float = 300.0,
        *,
        policy: TenantPolicy | None = None,
    ) -> float:
        """Compute predicted reuse score (higher = more likely to be reused).

        Uses frequency * exponential-decay(recency) rather than naive LRU.

        T2 (issue #103): `policy` is the surface for tenant-weighted eviction.
        Currently ignored — the implementation lands W4-W6 after the RFC closes.
        Callers passing `policy=None` get the legacy behavior.

        Args:
            now: Current monotonic time.
            freq_weight: Weight for the frequency component.
            recency_weight: Weight for the recency component.
            decay_half_life_s: Half-life in seconds for the recency decay.
            policy: Tenant-weight policy (T2 — reserved, no-op until W4).

        Returns:
            Non-negative score; higher means keep longer.
        """
        del policy  # T2 — ignored until W4-W6.
        age_s = max(now - self.last_access_time, 0.001)
        decay = math.exp(-math.log(2) * age_s / decay_half_life_s)

        freq_score = math.log1p(self.access_count)
        recency_score = decay

        return freq_weight * freq_score + recency_weight * recency_score


@dataclass
class TierStats:
    """Aggregate statistics for one cache tier."""

    name: str
    capacity_gb: float
    used_gb: float
    block_count: int
    hit_count: int = 0

    @property
    def utilization(self) -> float:
        """Fraction of tier capacity in use."""
        if self.capacity_gb <= 0:
            return 0.0
        return min(self.used_gb / self.capacity_gb, 1.0)


class CacheManager:
    """Orchestration-layer KV cache tracker with tiered eviction.

    Manages metadata for cache blocks across GPU, CPU, and SSD tiers.
    Eviction decisions are based on a frequency+recency weighted score
    rather than naive LRU.

    Args:
        tier_capacities_gb: Mapping of tier name to capacity in GB.
            Defaults to {"gpu": 40, "cpu": 128, "ssd": 512}.
        block_size_tokens: Number of tokens per cache block.
        freq_weight: Weight for frequency in eviction scoring.
        recency_weight: Weight for recency in eviction scoring.
    """

    def __init__(
        self,
        tier_capacities_gb: dict[str, float] | None = None,
        block_size_tokens: int = 16,
        freq_weight: float = 0.7,
        recency_weight: float = 0.3,
    ) -> None:
        self._capacities = tier_capacities_gb or {
            "gpu": 40.0,
            "cpu": 128.0,
            "ssd": 512.0,
        }
        self._block_size_tokens = block_size_tokens
        self._freq_weight = freq_weight
        self._recency_weight = recency_weight

        # block_id -> CacheBlock
        self._blocks: dict[str, CacheBlock] = {}
        # tier -> set of block_ids
        self._tier_blocks: dict[str, set[str]] = {t: set() for t in TIER_ORDER}

        # Approximate bytes per token for KV cache (fp16, typical transformer)
        # 2 (K+V) * 2 bytes * num_heads * head_dim ~ varies by model,
        # use a conservative 512 bytes/token as a rough default.
        self._bytes_per_token: int = 512

        # Hit/miss counters per tier
        self._hits: dict[str, int] = {t: 0 for t in TIER_ORDER}
        self._misses: int = 0

        # LMCache integration (optional)
        self._lmcache: Any = None
        try:
            import lmcache  # type: ignore[import-untyped]

            self._lmcache = lmcache
            logger.info("LMCache detected -- integration available")
        except ImportError:
            logger.debug("LMCache not installed, using standalone tracking")

    # ------------------------------------------------------------------
    # Block lifecycle
    # ------------------------------------------------------------------

    def allocate_block(
        self,
        block_id: str,
        model_id: str,
        request_id: str,
        num_tokens: int,
        tier: str = "gpu",
        *,
        tenant_id: str | None = None,
    ) -> CacheBlock | None:
        """Allocate a new cache block in the specified tier.

        If the tier is full, evicts the lowest-scoring block first.
        If eviction frees enough space, the new block is placed; otherwise
        the block is placed in the next-lower tier.

        Args:
            block_id: Unique identifier for this block.
            model_id: Model that owns this block.
            request_id: Originating request ID.
            num_tokens: Tokens to store.
            tier: Preferred tier ("gpu", "cpu", "ssd").
            tenant_id: Originating tenant (T2 — currently optional; the hot
                path will start tagging blocks in W4-W6).

        Returns:
            The allocated CacheBlock, or None if no tier has space.
        """
        needed_gb = self._tokens_to_gb(num_tokens)

        # Try preferred tier, then fall through to lower tiers
        tier_idx = TIER_ORDER.index(tier) if tier in TIER_ORDER else 0
        for t in TIER_ORDER[tier_idx:]:
            if self._tier_used_gb(t) + needed_gb <= self._capacities.get(t, 0):
                return self._place_block(
                    block_id, model_id, request_id, num_tokens, t, tenant_id=tenant_id
                )

            # Try evicting from this tier
            evicted = self._evict_from_tier(t, needed_gb)
            if evicted:
                return self._place_block(
                    block_id, model_id, request_id, num_tokens, t, tenant_id=tenant_id
                )

        logger.warning(
            "No tier has space for block %s (%d tokens)", block_id, num_tokens
        )
        return None

    def access_block(self, block_id: str) -> CacheBlock | None:
        """Record an access to an existing block.

        Updates access count and timestamp.

        Args:
            block_id: Block identifier.

        Returns:
            The updated block, or None if not found.
        """
        block = self._blocks.get(block_id)
        if block is None:
            self._misses += 1
            return None

        block.access_count += 1
        block.last_access_time = time.monotonic()
        self._hits[block.tier] = self._hits.get(block.tier, 0) + 1
        return block

    def free_block(self, block_id: str) -> bool:
        """Free a cache block.

        Args:
            block_id: Block identifier.

        Returns:
            True if the block was found and freed.
        """
        block = self._blocks.pop(block_id, None)
        if block is None:
            return False
        self._tier_blocks[block.tier].discard(block_id)
        return True

    def free_blocks_for_request(self, request_id: str) -> int:
        """Free all blocks belonging to a specific request.

        Args:
            request_id: Request identifier.

        Returns:
            Number of blocks freed.
        """
        to_free = [bid for bid, b in self._blocks.items() if b.request_id == request_id]
        for bid in to_free:
            self.free_block(bid)
        return len(to_free)

    def free_blocks_for_model(self, model_id: str) -> int:
        """Free all blocks belonging to a specific model.

        Args:
            model_id: Model identifier.

        Returns:
            Number of blocks freed.
        """
        to_free = [bid for bid, b in self._blocks.items() if b.model_id == model_id]
        for bid in to_free:
            self.free_block(bid)
        return len(to_free)

    # ------------------------------------------------------------------
    # Promote / demote between tiers
    # ------------------------------------------------------------------

    def promote_block(self, block_id: str, target_tier: str) -> bool:
        """Move a block to a faster (higher) tier.

        Args:
            block_id: Block identifier.
            target_tier: Destination tier.

        Returns:
            True if the promotion succeeded.
        """
        block = self._blocks.get(block_id)
        if block is None:
            return False

        target_idx = TIER_ORDER.index(target_tier) if target_tier in TIER_ORDER else -1
        current_idx = TIER_ORDER.index(block.tier) if block.tier in TIER_ORDER else -1
        if target_idx >= current_idx:
            return False  # not actually a promotion

        needed_gb = self._tokens_to_gb(block.num_tokens)
        if self._tier_used_gb(target_tier) + needed_gb > self._capacities.get(
            target_tier, 0
        ):
            self._evict_from_tier(target_tier, needed_gb)
            if self._tier_used_gb(target_tier) + needed_gb > self._capacities.get(
                target_tier, 0
            ):
                return False

        self._tier_blocks[block.tier].discard(block_id)
        block.tier = target_tier
        self._tier_blocks[target_tier].add(block_id)
        return True

    def demote_block(self, block_id: str, target_tier: str) -> bool:
        """Move a block to a slower (lower) tier.

        Args:
            block_id: Block identifier.
            target_tier: Destination tier.

        Returns:
            True if the demotion succeeded.
        """
        block = self._blocks.get(block_id)
        if block is None:
            return False

        needed_gb = self._tokens_to_gb(block.num_tokens)
        if self._tier_used_gb(target_tier) + needed_gb > self._capacities.get(
            target_tier, 0
        ):
            return False

        self._tier_blocks[block.tier].discard(block_id)
        block.tier = target_tier
        self._tier_blocks[target_tier].add(block_id)
        return True

    # ------------------------------------------------------------------
    # Eviction
    # ------------------------------------------------------------------

    def _evict_from_tier(
        self,
        tier: str,
        needed_gb: float,
        *,
        policy: TenantPolicy | None = None,
    ) -> bool:
        """Evict lowest-scored blocks from a tier to free space.

        Evicted blocks are demoted to the next-lower tier if possible,
        otherwise they are discarded.

        Args:
            tier: Tier to evict from.
            needed_gb: Space needed in GB.
            policy: Tenant-weight policy passed through to `reuse_score`
                (T2 — currently a no-op; the W4-W6 implementation will use it).

        Returns:
            True if enough space was freed.
        """
        now = time.monotonic()
        tier_block_ids = list(self._tier_blocks[tier])
        if not tier_block_ids:
            return False

        # Sort by reuse score ascending (evict lowest first)
        scored = sorted(
            tier_block_ids,
            key=lambda bid: self._blocks[bid].reuse_score(
                now,
                self._freq_weight,
                self._recency_weight,
                policy=policy,
            ),
        )

        freed_gb = 0.0
        tier_idx = TIER_ORDER.index(tier)
        next_tier = TIER_ORDER[tier_idx + 1] if tier_idx + 1 < len(TIER_ORDER) else None

        for bid in scored:
            if freed_gb >= needed_gb:
                return True

            block = self._blocks[bid]
            block_gb = self._tokens_to_gb(block.num_tokens)

            # Try demoting to next tier
            if next_tier is not None:
                if self.demote_block(bid, next_tier):
                    freed_gb += block_gb
                    continue

            # No lower tier or demotion failed -- discard
            self.free_block(bid)
            freed_gb += block_gb

        return freed_gb >= needed_gb

    # ------------------------------------------------------------------
    # Stats and reporting
    # ------------------------------------------------------------------

    def tier_stats(self) -> dict[str, TierStats]:
        """Get per-tier statistics.

        Returns:
            Dictionary mapping tier names to TierStats.
        """
        stats: dict[str, TierStats] = {}
        for tier in TIER_ORDER:
            stats[tier] = TierStats(
                name=tier,
                capacity_gb=self._capacities.get(tier, 0),
                used_gb=self._tier_used_gb(tier),
                block_count=len(self._tier_blocks[tier]),
                hit_count=self._hits.get(tier, 0),
            )
        return stats

    def model_block_counts(self) -> dict[str, int]:
        """Get block counts per model.

        Returns:
            Dictionary mapping model_id to number of cache blocks.
        """
        counts: dict[str, int] = {}
        for block in self._blocks.values():
            counts[block.model_id] = counts.get(block.model_id, 0) + 1
        return counts

    def total_blocks(self) -> int:
        """Return total number of tracked cache blocks."""
        return len(self._blocks)

    def hit_rate(self) -> float:
        """Compute overall cache hit rate.

        Returns:
            Hit rate as a float between 0.0 and 1.0.
        """
        total_hits = sum(self._hits.values())
        total = total_hits + self._misses
        if total == 0:
            return 0.0
        return total_hits / total

    def snapshot(self) -> dict[str, Any]:
        """Plain-dict snapshot for CLI status display.

        Returns:
            Dictionary with cache statistics.
        """
        ts = self.tier_stats()
        return {
            "total_blocks": self.total_blocks(),
            "hit_rate": round(self.hit_rate(), 4),
            "tiers": {
                name: {
                    "capacity_gb": s.capacity_gb,
                    "used_gb": round(s.used_gb, 3),
                    "utilization": round(s.utilization, 3),
                    "block_count": s.block_count,
                    "hits": s.hit_count,
                }
                for name, s in ts.items()
            },
            "model_blocks": self.model_block_counts(),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _tokens_to_gb(self, num_tokens: int) -> float:
        """Convert a token count to approximate GB of KV cache."""
        return (num_tokens * self._bytes_per_token) / (1024**3)

    def _tier_used_gb(self, tier: str) -> float:
        """Compute current usage in GB for a tier."""
        total_tokens = sum(
            self._blocks[bid].num_tokens
            for bid in self._tier_blocks.get(tier, set())
            if bid in self._blocks
        )
        return self._tokens_to_gb(total_tokens)

    def _place_block(
        self,
        block_id: str,
        model_id: str,
        request_id: str,
        num_tokens: int,
        tier: str,
        *,
        tenant_id: str | None = None,
    ) -> CacheBlock:
        """Place a block in a tier (internal helper, no capacity check)."""
        now = time.monotonic()
        block = CacheBlock(
            block_id=block_id,
            model_id=model_id,
            request_id=request_id,
            tier=tier,
            num_tokens=num_tokens,
            access_count=1,
            last_access_time=now,
            created_time=now,
            tenant_id=tenant_id,
        )
        self._blocks[block_id] = block
        self._tier_blocks[tier].add(block_id)
        return block
