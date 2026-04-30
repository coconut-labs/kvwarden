"""Pin the TenantPolicy dataclass surface (live, passes against the W1 stub).

# T2 — issue #103, RFC at docs/rfcs/T2-cache-pressure-admission.md
# (TenantPolicy semantics reframed 2026-04-28T+1: tenant_weights is the
# admission-cost weight, not the eviction weight. Surface unchanged.)
"""

from __future__ import annotations

import time

from kvwarden.cache.manager import CacheBlock, TenantPolicy


class TestTenantPolicySurface:
    """Tests for the TenantPolicy dataclass itself."""

    def test_default_tenant_weights_is_empty_dict(self) -> None:
        policy = TenantPolicy()
        assert policy.tenant_weights == {}

    def test_instantiate_with_explicit_weights(self) -> None:
        policy = TenantPolicy(tenant_weights={"flooder": 0.1, "quiet": 1.0})
        assert policy.tenant_weights["flooder"] == 0.1
        assert policy.tenant_weights["quiet"] == 1.0

    def test_tenant_weights_is_mutable(self) -> None:
        policy = TenantPolicy()
        policy.tenant_weights["vip"] = 2.0
        assert policy.tenant_weights == {"vip": 2.0}

    def test_dataclass_equality(self) -> None:
        a = TenantPolicy(tenant_weights={"x": 0.5})
        b = TenantPolicy(tenant_weights={"x": 0.5})
        c = TenantPolicy(tenant_weights={"x": 0.6})
        assert a == b
        assert a != c

    def test_default_factory_isolates_instances(self) -> None:
        a = TenantPolicy()
        b = TenantPolicy()
        a.tenant_weights["only_in_a"] = 1.0
        assert "only_in_a" not in b.tenant_weights

    def test_cacheblock_tenant_id_defaults_to_none(self) -> None:
        block = CacheBlock(
            block_id="b1",
            model_id="m1",
            request_id="r1",
            tier="gpu",
            num_tokens=16,
        )
        assert block.tenant_id is None

    def test_cacheblock_accepts_tenant_id(self) -> None:
        block = CacheBlock(
            block_id="b1",
            model_id="m1",
            request_id="r1",
            tier="gpu",
            num_tokens=16,
            tenant_id="flooder",
        )
        assert block.tenant_id == "flooder"

    def test_reuse_score_with_none_policy_matches_no_policy(self) -> None:
        """policy=None must be a no-op alias for the legacy signature.

        Regression guard for the W4 change — passes on the stub today and
        must continue to pass once the W4 semantics land.
        """
        now = time.monotonic()
        block = CacheBlock(
            block_id="b1",
            model_id="m1",
            request_id="r1",
            tier="gpu",
            num_tokens=16,
            access_count=5,
            last_access_time=now - 10,
            tenant_id="flooder",
        )
        assert block.reuse_score(now) == block.reuse_score(now, policy=None)

    def test_empty_weights_equivalent_to_no_policy(self) -> None:
        """`TenantPolicy(tenant_weights={})` must score identically to `policy=None`.

        Pins the LRU-equivalent contract: empty dict == every tenant at weight
        1.0 == legacy behavior. Passes today (stub strips policy); the W4 impl
        must preserve this — a buggy impl that treated missing keys as 0.0
        would break this test.
        """
        now = time.monotonic()
        block = CacheBlock(
            block_id="b1",
            model_id="m1",
            request_id="r1",
            tier="gpu",
            num_tokens=16,
            access_count=5,
            last_access_time=now - 10,
            tenant_id="anyone",
        )
        empty_policy = TenantPolicy(tenant_weights={})
        assert block.reuse_score(now, policy=empty_policy) == block.reuse_score(now)
