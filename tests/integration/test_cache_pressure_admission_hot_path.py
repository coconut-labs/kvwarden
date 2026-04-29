"""Pin the eventual cache-pressure -> admission hot-path wiring (skip until W4).

T2 reframed 2026-04-28T+1: kvwarden polls vLLM's `/metrics` for
`vllm:kv_cache_usage_perc`, surfaces the gauge in `cache_manager.snapshot()`
under the key `kv_cache_pressure`, and AdmissionController scales priority
by `(cache_load × tenant_deficit)`. None of that wiring exists yet; bodies
are skeletal and the skip marker keeps CI green until W4-W6 lands.

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


@pytest.mark.skip(reason="T2 W4-W6 admission wiring")
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


@pytest.mark.skip(reason="T2 W4-W6 admission wiring")
def test_two_tenant_pressure_defers_flooder() -> None:
    """Under flooder spike + cache pressure, flooder admission is rejected
    or deferred once the gauge crosses threshold; quiet tenant unaffected."""
    cm = _make_manager()
    # TODO(T2-W4): drive flooder spike, simulate snapshot["kv_cache_pressure"]
    # > 0.9, assert AdmissionController defers flooder.acquire() while
    # quiet.acquire() proceeds at normal priority.
    del cm  # silence unused-var while skipped


@pytest.mark.skip(reason="T2 W4-W6 admission wiring")
def test_pressure_recovery_resumes_normal_priority() -> None:
    """After cache pressure drops below the knee, admission resumes
    normal (DRR-only) priority composition for all tenants."""
    cm = _make_manager()
    # TODO(T2-W4): simulate snapshot["kv_cache_pressure"] dropping from 0.95
    # to 0.3. Assert priority returned by compose_priority for both tenants
    # falls back to the unscaled DRR deficit-only path.
    del cm  # silence unused-var while skipped


@pytest.mark.skip(reason="T2 W4-W6 admission wiring")
def test_snapshot_exposes_pressure_key_and_metadata() -> None:
    """`CacheManager.snapshot()` must expose `kv_cache_pressure` (float in
    [0,1]) plus poll metadata (last poll timestamp) once the W4 poller is
    wired. Today the snapshot has no such key."""
    cm = _make_manager()
    # TODO(T2-W4): the W4 poller writes to cm._kv_cache_pressure
    # (or equivalent). snapshot() surfaces it under "kv_cache_pressure"
    # plus a "kv_cache_pressure_last_poll_ts" timestamp for staleness checks.
    snap = cm.snapshot()
    assert "kv_cache_pressure" in snap
    assert "kv_cache_pressure_last_poll_ts" in snap
