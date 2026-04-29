"""Pin the W4-W6 cache-pressure-aware admission semantics (strict-xfail until impl).

T2 reframed 2026-04-28T+1: kvwarden polls vLLM `vllm:kv_cache_usage_perc`,
combines it with the existing DRR per-tenant deficit, and produces a
cache-pressure-aware admission priority. The composition lives at
`kvwarden.router.admission.compose_priority` (does NOT exist today — that's
the point of xfail-strict). Imports are deferred into each test body so
ImportError counts as the expected failure; the file collects cleanly.

When W4 lands `compose_priority`, each test body actually runs. If the
assertion passes, xpass-strict fires and forces the implementer to clear
the marker.

# T2 — issue #103, RFC at docs/rfcs/T2-cache-pressure-admission.md
"""

from __future__ import annotations

import pytest

from kvwarden.cache.manager import TenantPolicy


@pytest.mark.xfail(reason="T2 W4-W6 cache-pressure admission semantics", strict=True)
def test_zero_pressure_no_policy_passes_priority_through() -> None:
    """policy=None and gauge=0.0 -> priority equals tenant_deficit unchanged."""
    from kvwarden.router.admission import compose_priority

    out = compose_priority(
        tenant_id="quiet", base_priority=10, policy=None, kv_cache_pressure=0.0
    )
    assert out == 10


@pytest.mark.xfail(reason="T2 W4-W6 cache-pressure admission semantics", strict=True)
def test_high_pressure_flooder_costs_more_than_quiet() -> None:
    """At gauge=0.9 with flooder weight=0.1, flooder cost is ~10x quiet's."""
    from kvwarden.router.admission import compose_priority

    policy = TenantPolicy(tenant_weights={"flooder": 0.1, "quiet": 1.0})
    flooder = compose_priority(
        tenant_id="flooder", base_priority=10, policy=policy, kv_cache_pressure=0.9
    )
    quiet = compose_priority(
        tenant_id="quiet", base_priority=10, policy=policy, kv_cache_pressure=0.9
    )
    assert flooder >= 10 * quiet


@pytest.mark.xfail(reason="T2 W4-W6 cache-pressure admission semantics", strict=True)
def test_scaling_curve_at_knee_is_unity() -> None:
    """At gauge=0.5 the cache-pressure scale=1.0 (no change vs no-policy)."""
    from kvwarden.router.admission import compose_priority

    policy = TenantPolicy(tenant_weights={"flooder": 0.1})
    scaled = compose_priority(
        tenant_id="flooder", base_priority=10, policy=policy, kv_cache_pressure=0.5
    )
    assert scaled == 10


@pytest.mark.xfail(reason="T2 W4-W6 cache-pressure admission semantics", strict=True)
def test_scaling_curve_at_high_pressure_amplifies_4x() -> None:
    """At gauge=0.9 the cache-pressure scale=4.0 (RFC-pinned curve point)."""
    from kvwarden.router.admission import compose_priority

    policy = TenantPolicy(tenant_weights={"flooder": 0.25})
    scaled = compose_priority(
        tenant_id="flooder", base_priority=10, policy=policy, kv_cache_pressure=0.9
    )
    # weight=0.25 inverts to 4x cost; scale=4 at gauge=0.9; combined = 16x
    assert scaled == 160


@pytest.mark.xfail(reason="T2 W4-W6 cache-pressure admission semantics", strict=True)
def test_below_knee_no_scaling() -> None:
    """At gauge=0.3 (below the knee), priority unchanged regardless of policy."""
    from kvwarden.router.admission import compose_priority

    policy = TenantPolicy(tenant_weights={"flooder": 0.1, "quiet": 1.0})
    flooder = compose_priority(
        tenant_id="flooder", base_priority=10, policy=policy, kv_cache_pressure=0.3
    )
    quiet = compose_priority(
        tenant_id="quiet", base_priority=10, policy=policy, kv_cache_pressure=0.3
    )
    assert flooder == 10
    assert quiet == 10


@pytest.mark.xfail(reason="T2 W4-W6 cache-pressure admission semantics", strict=True)
def test_saturation_caps_scale_at_maximum() -> None:
    """At gauge=0.99 the scale is capped (no infinite multiplication)."""
    from kvwarden.router.admission import compose_priority

    policy = TenantPolicy(tenant_weights={"flooder": 0.1})
    scaled = compose_priority(
        tenant_id="flooder", base_priority=10, policy=policy, kv_cache_pressure=0.99
    )
    # Cap at 10x (1/weight) * cap_at_max scale (e.g. ~10) = 1000 ceiling
    assert scaled <= 1000


@pytest.mark.xfail(reason="T2 W4-W6 cache-pressure admission semantics", strict=True)
def test_unknown_tenant_defaults_to_weight_one() -> None:
    """A tenant_id absent from `tenant_weights` falls back to weight 1.0."""
    from kvwarden.router.admission import compose_priority

    policy = TenantPolicy(tenant_weights={"flooder": 0.1})
    out = compose_priority(
        tenant_id="mystery", base_priority=10, policy=policy, kv_cache_pressure=0.9
    )
    flooder_out = compose_priority(
        tenant_id="flooder", base_priority=10, policy=policy, kv_cache_pressure=0.9
    )
    assert out < flooder_out


@pytest.mark.xfail(reason="T2 W4-W6 cache-pressure admission semantics", strict=True)
def test_empty_weights_equivalent_to_no_policy() -> None:
    """`TenantPolicy(tenant_weights={})` matches `policy=None` even at high gauge."""
    from kvwarden.router.admission import compose_priority

    empty = TenantPolicy(tenant_weights={})
    a = compose_priority(
        tenant_id="anyone", base_priority=10, policy=empty, kv_cache_pressure=0.95
    )
    b = compose_priority(
        tenant_id="anyone", base_priority=10, policy=None, kv_cache_pressure=0.95
    )
    assert a == b


@pytest.mark.xfail(reason="T2 W4-W6 cache-pressure admission semantics", strict=True)
def test_equal_weights_preserve_priority_order() -> None:
    """Equal weights amplify uniformly; relative ordering is preserved."""
    from kvwarden.router.admission import compose_priority

    policy = TenantPolicy(tenant_weights={"a": 1.0, "b": 1.0})
    a_low = compose_priority(
        tenant_id="a", base_priority=5, policy=policy, kv_cache_pressure=0.9
    )
    b_high = compose_priority(
        tenant_id="b", base_priority=20, policy=policy, kv_cache_pressure=0.9
    )
    assert a_low < b_high


@pytest.mark.xfail(reason="T2 W4-W6 cache-pressure admission semantics", strict=True)
def test_stale_gauge_value_fail_open() -> None:
    """A stale poll (e.g. None for "missed last poll") defaults to 0 -> no scaling."""
    from kvwarden.router.admission import compose_priority

    policy = TenantPolicy(tenant_weights={"flooder": 0.1})
    out = compose_priority(
        tenant_id="flooder", base_priority=10, policy=policy, kv_cache_pressure=None
    )
    assert out == 10
