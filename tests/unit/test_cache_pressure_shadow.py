"""Shadow-mode cache-pressure admission recording.

T2 Slice 1, third commit. The `cache_pressure_admission` config block is
off by default. Turned on, it starts the /metrics poller and records what
the cache-pressure lever *would* do to each request's priority — and
changes nothing about the priority actually handed to the
AdmissionController. Enforcement is Slice 2.

The load-bearing test in this file is the flag-off one: with the block
absent or disabled, no series are registered, no task is started, and the
priority passed to `acquire()` is bit-for-bit what v0.1 passed.

# T2 — issue #103, RFC at docs/rfcs/T2-cache-pressure-admission.md
"""

from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from kvwarden.common.config import (
    CachePressureAdmissionConfig,
    KVWardenConfig,
    ModelConfig,
    TenantDefaults,
)
from kvwarden.router.router import ModelState, WorkloadRouter
from kvwarden.router.shadow import CachePressureShadow

# ── Config parsing ───────────────────────────────────────────────────


def _write(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "cfg.yaml"
    p.write_text(textwrap.dedent(body))
    return p


def test_block_defaults_to_disabled() -> None:
    assert CachePressureAdmissionConfig().enabled is False


def test_absent_block_yields_disabled_defaults(tmp_path: Path) -> None:
    cfg = KVWardenConfig.from_yaml(_write(tmp_path, "port: 8080\n"))
    assert cfg.cache_pressure_admission.enabled is False
    assert cfg.cache_pressure_admission.poll_interval_ms == 250


def test_block_is_parsed_not_silently_dropped(tmp_path: Path) -> None:
    """`from_yaml` is `raw.get()` with defaults and has no validator, so an
    unthreaded block would parse as a silent no-op that looks like a bug."""
    cfg = KVWardenConfig.from_yaml(
        _write(
            tmp_path,
            """
            cache_pressure_admission:
              enabled: true
              poll_interval_ms: 500
              soft_threshold: 0.4
              hard_threshold: 0.95
              saturation_ceiling: 6.0
              tenant_weights:
                flooder: 0.25
            """,
        )
    )
    block = cfg.cache_pressure_admission
    assert block.enabled is True
    assert block.poll_interval_ms == 500
    assert block.soft_threshold == pytest.approx(0.4)
    assert block.hard_threshold == pytest.approx(0.95)
    assert block.saturation_ceiling == pytest.approx(6.0)
    assert block.tenant_weights == {"flooder": 0.25}


def test_unknown_key_inside_the_block_raises(tmp_path: Path) -> None:
    """Matches `ModelConfig(**m)`: a typo inside a block is loud, even
    though a typo'd top-level key is still silently dropped (debt #2)."""
    with pytest.raises(TypeError):
        KVWardenConfig.from_yaml(
            _write(
                tmp_path,
                """
                cache_pressure_admission:
                  enabled: true
                  polL_interval_ms: 500
                """,
            )
        )


# ── Recorder ─────────────────────────────────────────────────────────


def _shadow(**kw) -> CachePressureShadow:
    block = CachePressureAdmissionConfig(enabled=True, **kw)
    return CachePressureShadow(block)


def test_delta_is_zero_at_zero_pressure() -> None:
    shadow = _shadow(tenant_weights={"flooder": 0.25})
    assert shadow.evaluate("flooder", base_priority=10, pressure=0.0) == 0


def test_delta_is_zero_below_the_soft_threshold() -> None:
    shadow = _shadow(tenant_weights={"flooder": 0.25})
    assert shadow.evaluate("flooder", base_priority=10, pressure=0.3) == 0


def test_delta_reflects_the_curve_under_pressure() -> None:
    """weight 0.25 at p=0.9: 10 -> 160, so the lever would add 150."""
    shadow = _shadow(tenant_weights={"flooder": 0.25})
    assert shadow.evaluate("flooder", base_priority=10, pressure=0.9) == 150


def test_delta_honours_configured_watermarks() -> None:
    """A deployment-tuned ceiling changes the recorded counterfactual."""
    shadow = _shadow(saturation_ceiling=2.0)
    assert shadow.evaluate("quiet", base_priority=10, pressure=0.95) == 10


def test_unknown_tenant_uses_uniform_weight() -> None:
    shadow = _shadow(tenant_weights={"flooder": 0.25})
    assert shadow.evaluate("mystery", base_priority=10, pressure=0.9) == 30


def test_recorded_series_appear_in_prometheus_output() -> None:
    from prometheus_client import CollectorRegistry, generate_latest

    registry = CollectorRegistry()
    shadow = CachePressureShadow(
        CachePressureAdmissionConfig(enabled=True, tenant_weights={"flooder": 0.25}),
        registry=registry,
    )
    shadow.observe_poll(0.9, consecutive_failures=0)
    shadow.record("flooder", base_priority=10, pressure=0.9)

    out = generate_latest(registry).decode()
    assert "kvwarden_kv_cache_pressure" in out
    assert "kvwarden_cache_pressure_scale" in out
    assert "kvwarden_shadow_priority_delta" in out
    assert "kvwarden_cache_pressure_poll_failures_total" in out


def test_poll_failures_counter_tracks_increments_only() -> None:
    from prometheus_client import CollectorRegistry

    registry = CollectorRegistry()
    shadow = CachePressureShadow(
        CachePressureAdmissionConfig(enabled=True), registry=registry
    )
    shadow.observe_poll(0.0, consecutive_failures=1)
    shadow.observe_poll(0.0, consecutive_failures=2)
    shadow.observe_poll(0.5, consecutive_failures=0)
    shadow.observe_poll(0.0, consecutive_failures=1)

    value = registry.get_sample_value("kvwarden_cache_pressure_poll_failures_total")
    assert value == 3.0


# ── Router wiring ────────────────────────────────────────────────────


def _router(block: CachePressureAdmissionConfig | None) -> WorkloadRouter:
    cfg = KVWardenConfig(
        models=[ModelConfig(model_id="m", engine="vllm")],
        tenant_defaults=TenantDefaults(scheduling="drr"),
        cache_pressure_admission=block or CachePressureAdmissionConfig(),
    )
    router = WorkloadRouter(config=cfg)
    adapter = MagicMock()
    adapter.is_healthy = True
    adapter.metrics_url = "http://localhost:8001/metrics"
    adapter.forward_request = AsyncMock(
        return_value={
            "choices": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )
    adapter.stop = AsyncMock()
    router._models["m"] = ModelState(config=ModelConfig(model_id="m"), adapter=adapter)
    return router


async def _one_request(router: WorkloadRouter, tenant_id: str = "t") -> None:
    await router.route_request(
        model_id="m",
        path="/v1/completions",
        payload={"max_tokens": 32},
        tenant_id=tenant_id,
    )


@pytest.mark.asyncio
async def test_flag_off_registers_no_series_and_starts_no_task() -> None:
    router = _router(None)
    assert router._cache_pressure_shadow is None
    assert router._cache_pressure_task is None

    out = router.metrics.prometheus_output().decode()
    assert "kv_cache_pressure" not in out
    assert "shadow_priority_delta" not in out


@pytest.mark.asyncio
async def test_flag_off_leaves_the_priority_path_untouched() -> None:
    router = _router(None)
    router.admission_controller.acquire = AsyncMock(return_value=True)

    await _one_request(router)

    # drr with one in-flight request: priority_score() == 1*10 + 1 == 11,
    # composed as 11*100 + bucket_priority(short=0).
    assert router.admission_controller.acquire.await_args.kwargs["priority"] == 1100


@pytest.mark.asyncio
async def test_flag_on_does_not_change_the_priority_handed_to_admission() -> None:
    """Shadow-only. The lever is measured, never applied."""
    block = CachePressureAdmissionConfig(enabled=True, tenant_weights={"t": 0.25})
    router = _router(block)
    router.cache_manager.record_kv_cache_pressure(0.95)
    router.admission_controller.acquire = AsyncMock(return_value=True)

    await _one_request(router)

    assert router.admission_controller.acquire.await_args.kwargs["priority"] == 1100


@pytest.mark.asyncio
async def test_flag_on_records_the_counterfactual() -> None:
    block = CachePressureAdmissionConfig(enabled=True, tenant_weights={"t": 0.25})
    router = _router(block)
    router.cache_manager.record_kv_cache_pressure(0.9)
    router.admission_controller.acquire = AsyncMock(return_value=True)

    await _one_request(router)

    out = router.metrics.prometheus_output().decode()
    assert "kvwarden_shadow_priority_delta" in out
    # deficit 1100 at scale 4 with weight 0.25 -> 17600, delta 16500.
    observed = router.metrics._registry.get_sample_value(
        "kvwarden_shadow_priority_delta_sum", {"tenant": "t"}
    )
    assert observed == pytest.approx(16500.0)


@pytest.mark.asyncio
async def test_shadow_records_under_fifo_scheduling_too() -> None:
    """The default discipline is fifo, which has no deficit in its live
    priority. The shadow still reports the counterfactual, so an operator
    running the default config sees a signal rather than silence."""
    block = CachePressureAdmissionConfig(enabled=True, tenant_weights={"t": 0.25})
    cfg = KVWardenConfig(
        models=[ModelConfig(model_id="m", engine="vllm")],
        cache_pressure_admission=block,
    )
    router = WorkloadRouter(config=cfg)
    adapter = MagicMock()
    adapter.is_healthy = True
    adapter.forward_request = AsyncMock(
        return_value={
            "choices": [],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )
    router._models["m"] = ModelState(config=ModelConfig(model_id="m"), adapter=adapter)
    router.cache_manager.record_kv_cache_pressure(0.9)
    router.admission_controller.acquire = AsyncMock(return_value=True)

    await _one_request(router)

    assert router.admission_controller.acquire.await_args.kwargs["priority"] == 0
    observed = router.metrics._registry.get_sample_value(
        "kvwarden_shadow_priority_delta_sum", {"tenant": "t"}
    )
    assert observed is not None and observed > 0


@pytest.mark.asyncio
async def test_poller_task_runs_and_stops_with_the_router() -> None:
    block = CachePressureAdmissionConfig(enabled=True, poll_interval_ms=10)
    router = _router(block)
    router.load_model = AsyncMock(side_effect=lambda cfg: router._models["m"])

    await router.start()
    assert router._cache_pressure_task is not None
    assert not router._cache_pressure_task.done()

    await router.stop()
    await asyncio.sleep(0)
    assert router._cache_pressure_task is None


@pytest.mark.asyncio
async def test_poller_scrapes_the_loaded_adapters_metrics_url() -> None:
    """The endpoint list is a callable so models loaded after start are
    picked up."""
    block = CachePressureAdmissionConfig(enabled=True)
    router = _router(block)
    assert router._cache_pressure_poller is not None
    assert router._cache_pressure_poller._endpoints() == [
        "http://localhost:8001/metrics"
    ]
