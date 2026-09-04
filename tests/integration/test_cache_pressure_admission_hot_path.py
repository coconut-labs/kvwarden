"""Cache-pressure -> admission hot-path wiring.

T2 reframed 2026-04-28T+1: kvwarden polls vLLM's `/metrics` for
`vllm:kv_cache_usage_perc`, surfaces the gauge in `cache_manager.snapshot()`
under the key `kv_cache_pressure`, and composes it with the per-tenant DRR
deficit.

Slice 1 landed the snapshot surface, the poller, and shadow-mode recording,
so the snapshot test below is live. The other three assert that admission
*acts* on the pressure — deferring a flooder, then recovering — which is
Slice 2. They stay skipped until enforcement lands, since Slice 1
deliberately leaves the admitted priority unchanged.

# T2 — issue #103, RFC at docs/rfcs/T2-cache-pressure-admission.md
"""

from __future__ import annotations

import pytest

from kvwarden.cache.manager import CacheManager


def _make_manager() -> CacheManager:
    return CacheManager(
        tier_capacities_gb={"gpu": 0.001, "cpu": 0.01, "ssd": 0.1},
        block_size_tokens=16,
    )


@pytest.mark.skip(reason="T2 Slice 2 — enforcement; Slice 1 is shadow-only")
def test_admission_reads_cache_pressure_from_snapshot() -> None:
    """End-to-end: a request hits AdmissionController, controller reads
    `cache_manager.snapshot()["kv_cache_pressure"]`, priority is composed."""
    cm = _make_manager()
    # TODO(T2-W4): wire AdmissionController to read
    # cache_manager.snapshot()["kv_cache_pressure"]. Today the snapshot has
    # no such key — the W4 poller surfaces it from vLLM /metrics.
    snap = cm.snapshot()
    assert "kv_cache_pressure" in snap
    # TODO(T2-W4): admission_controller.acquire(tenant_id="quiet") should
    # call compose_priority(tenant_id="quiet", base_priority=10, policy=p,
    # kv_cache_pressure=snap["kv_cache_pressure"]) under the hood.


@pytest.mark.skip(reason="T2 Slice 2 — enforcement; Slice 1 is shadow-only")
def test_two_tenant_pressure_defers_flooder() -> None:
    """Under flooder spike + cache pressure, flooder admission is rejected
    or deferred once the gauge crosses threshold; quiet tenant unaffected."""
    cm = _make_manager()
    # TODO(T2-W4): drive flooder spike, simulate snapshot["kv_cache_pressure"]
    # > 0.9, assert AdmissionController defers flooder.acquire() while
    # quiet.acquire() proceeds at normal priority.
    del cm  # silence unused-var while skipped


@pytest.mark.skip(reason="T2 Slice 2 — enforcement; Slice 1 is shadow-only")
def test_pressure_recovery_resumes_normal_priority() -> None:
    """After cache pressure drops below the knee, admission resumes
    normal (DRR-only) priority composition for all tenants."""
    cm = _make_manager()
    # TODO(T2-W4): simulate snapshot["kv_cache_pressure"] dropping from 0.95
    # to 0.3. Assert priority returned by compose_priority for both tenants
    # falls back to the unscaled DRR deficit-only path.
    del cm  # silence unused-var while skipped


def test_snapshot_exposes_pressure_key_and_metadata() -> None:
    """`CacheManager.snapshot()` exposes `kv_cache_pressure` (float in [0,1])
    plus the last-poll timestamp, on a manager with no poller attached."""
    cm = _make_manager()
    snap = cm.snapshot()
    assert snap["kv_cache_pressure"] == 0.0
    assert snap["kv_cache_pressure_last_poll_ts"] is None

    cm.record_kv_cache_pressure(0.62)
    snap = cm.snapshot()
    assert snap["kv_cache_pressure"] == pytest.approx(0.62)
    assert snap["kv_cache_pressure_last_poll_ts"] is not None
