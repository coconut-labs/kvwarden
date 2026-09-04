"""Engine /metrics cache-pressure probe.

T2 Slice 1, second commit. The poller scrapes the engine's Prometheus
endpoint for `vllm:kv_cache_usage_perc` and records it into CacheManager,
where `snapshot()` surfaces it for the admission-side composition.

Nothing starts the poller yet — the config block that turns it on lands
with the shadow-mode commit. These tests drive it directly.

# T2 — issue #103, RFC at docs/rfcs/T2-cache-pressure-admission.md
"""

from __future__ import annotations

import asyncio

import pytest

from kvwarden.cache.manager import CacheManager
from kvwarden.cache.pressure import (
    KV_CACHE_PRESSURE_METRIC,
    CachePressurePoller,
    parse_cache_pressure,
)

METRICS_TEXT = """\
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="llama"} 12.0
# HELP vllm:kv_cache_usage_perc GPU KV-cache usage. 1 means 100 percent usage.
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{engine="0",model_name="llama"} 0.7345
"""

MULTI_ENGINE_TEXT = """\
# HELP vllm:kv_cache_usage_perc GPU KV-cache usage. 1 means 100 percent usage.
# TYPE vllm:kv_cache_usage_perc gauge
vllm:kv_cache_usage_perc{engine="0",model_name="llama"} 0.21
vllm:kv_cache_usage_perc{engine="1",model_name="llama"} 0.88
"""

NO_GAUGE_TEXT = """\
# HELP vllm:num_requests_running Number of requests in model execution batches.
# TYPE vllm:num_requests_running gauge
vllm:num_requests_running{engine="0",model_name="llama"} 12.0
"""


def _fetcher(mapping: dict[str, str | None]):
    """Build a fetch seam that serves canned text per URL. None = unreachable."""

    async def fetch(url: str) -> str | None:
        return mapping.get(url)

    return fetch


# ── Parsing ──────────────────────────────────────────────────────────


def test_parses_gauge_from_metrics_text() -> None:
    assert parse_cache_pressure(METRICS_TEXT) == pytest.approx(0.7345)


def test_metric_name_matches_the_rfc_gauge() -> None:
    """The name is the entire contract with vLLM. Pin it."""
    assert KV_CACHE_PRESSURE_METRIC == "vllm:kv_cache_usage_perc"


def test_missing_gauge_parses_to_none() -> None:
    """Absent gauge is distinguishable from a real 0.0 reading."""
    assert parse_cache_pressure(NO_GAUGE_TEXT) is None


def test_empty_text_parses_to_none() -> None:
    assert parse_cache_pressure("") is None


def test_malformed_text_parses_to_none() -> None:
    """A truncated or non-Prometheus body must not raise into the caller."""
    assert parse_cache_pressure("not::a metrics body {{{\n") is None


def test_multiple_engine_labels_take_the_max() -> None:
    """Worst-case engine pressure for the route, not an average that
    masks one saturated engine (RFC §Architecture)."""
    assert parse_cache_pressure(MULTI_ENGINE_TEXT) == pytest.approx(0.88)


# ── Polling ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_once_records_into_snapshot() -> None:
    cm = CacheManager()
    poller = CachePressurePoller(
        cache_manager=cm,
        endpoints=lambda: ["http://engine-a/metrics"],
        fetch=_fetcher({"http://engine-a/metrics": METRICS_TEXT}),
    )

    await poller.poll_once()

    snap = cm.snapshot()
    assert snap["kv_cache_pressure"] == pytest.approx(0.7345)
    assert snap["kv_cache_pressure_last_poll_ts"] is not None


@pytest.mark.asyncio
async def test_max_across_endpoints() -> None:
    cm = CacheManager()
    poller = CachePressurePoller(
        cache_manager=cm,
        endpoints=lambda: ["http://a/metrics", "http://b/metrics"],
        fetch=_fetcher(
            {"http://a/metrics": METRICS_TEXT, "http://b/metrics": MULTI_ENGINE_TEXT}
        ),
    )

    await poller.poll_once()

    assert cm.snapshot()["kv_cache_pressure"] == pytest.approx(0.88)


@pytest.mark.asyncio
async def test_unreachable_endpoint_soft_degrades_to_zero() -> None:
    """Poller failure means no pressure signal, not an error. Admission
    falls back to v0.1 behavior."""
    cm = CacheManager()
    cm.record_kv_cache_pressure(0.9)
    poller = CachePressurePoller(
        cache_manager=cm,
        endpoints=lambda: ["http://down/metrics"],
        fetch=_fetcher({"http://down/metrics": None}),
    )

    await poller.poll_once()

    assert cm.snapshot()["kv_cache_pressure"] == 0.0
    assert poller.consecutive_failures == 1


@pytest.mark.asyncio
async def test_partial_failure_uses_the_reachable_endpoint() -> None:
    cm = CacheManager()
    poller = CachePressurePoller(
        cache_manager=cm,
        endpoints=lambda: ["http://down/metrics", "http://up/metrics"],
        fetch=_fetcher(
            {"http://down/metrics": None, "http://up/metrics": METRICS_TEXT}
        ),
    )

    await poller.poll_once()

    assert cm.snapshot()["kv_cache_pressure"] == pytest.approx(0.7345)
    assert poller.consecutive_failures == 0


@pytest.mark.asyncio
async def test_no_endpoints_records_zero() -> None:
    """No engine loaded yet is not a failure."""
    cm = CacheManager()
    poller = CachePressurePoller(cache_manager=cm, endpoints=list, fetch=_fetcher({}))

    await poller.poll_once()

    assert cm.snapshot()["kv_cache_pressure"] == 0.0
    assert poller.consecutive_failures == 0


@pytest.mark.asyncio
async def test_fetch_exception_soft_degrades() -> None:
    """An aiohttp error inside fetch must never escape into the caller."""

    async def boom(url: str) -> str | None:
        raise OSError("connection reset")

    cm = CacheManager()
    poller = CachePressurePoller(
        cache_manager=cm, endpoints=lambda: ["http://x/metrics"], fetch=boom
    )

    await poller.poll_once()

    assert cm.snapshot()["kv_cache_pressure"] == 0.0
    assert poller.consecutive_failures == 1


@pytest.mark.asyncio
async def test_failure_counter_resets_on_recovery() -> None:
    served: dict[str, str | None] = {"http://x/metrics": None}
    cm = CacheManager()
    poller = CachePressurePoller(
        cache_manager=cm, endpoints=lambda: ["http://x/metrics"], fetch=_fetcher(served)
    )

    await poller.poll_once()
    await poller.poll_once()
    assert poller.consecutive_failures == 2

    served["http://x/metrics"] = METRICS_TEXT
    await poller.poll_once()
    assert poller.consecutive_failures == 0


@pytest.mark.asyncio
async def test_gauge_absent_from_a_live_endpoint_is_a_failure() -> None:
    """Reachable but no gauge means the metric was renamed or the engine
    isn't vLLM. That is a failure to signal, not a 0.0 reading."""
    cm = CacheManager()
    poller = CachePressurePoller(
        cache_manager=cm,
        endpoints=lambda: ["http://x/metrics"],
        fetch=_fetcher({"http://x/metrics": NO_GAUGE_TEXT}),
    )

    await poller.poll_once()

    assert cm.snapshot()["kv_cache_pressure"] == 0.0
    assert poller.consecutive_failures == 1


@pytest.mark.asyncio
async def test_run_loop_starts_and_cancels_cleanly() -> None:
    cm = CacheManager()
    poller = CachePressurePoller(
        cache_manager=cm,
        endpoints=lambda: ["http://x/metrics"],
        fetch=_fetcher({"http://x/metrics": METRICS_TEXT}),
        interval_s=0.01,
    )

    task = asyncio.create_task(poller.run())
    for _ in range(200):
        await asyncio.sleep(0.005)
        if cm.snapshot()["kv_cache_pressure"] > 0.0:
            break
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    assert cm.snapshot()["kv_cache_pressure"] == pytest.approx(0.7345)
    assert task.done()


# ── CacheManager surface ─────────────────────────────────────────────


def test_bare_manager_exposes_pressure_keys() -> None:
    """A CacheManager with no poller attached still surfaces both keys, so
    /status and the admission path never KeyError on a cold start."""
    snap = CacheManager().snapshot()
    assert snap["kv_cache_pressure"] == 0.0
    assert snap["kv_cache_pressure_last_poll_ts"] is None


def test_recorded_pressure_is_clamped_into_range() -> None:
    """Defensive against an engine-side gauge bug."""
    cm = CacheManager()
    cm.record_kv_cache_pressure(1.7)
    assert cm.snapshot()["kv_cache_pressure"] == 1.0
    cm.record_kv_cache_pressure(-0.4)
    assert cm.snapshot()["kv_cache_pressure"] == 0.0


def test_recording_none_means_no_signal() -> None:
    cm = CacheManager()
    cm.record_kv_cache_pressure(0.8)
    cm.record_kv_cache_pressure(None)
    assert cm.snapshot()["kv_cache_pressure"] == 0.0


def test_adapter_advertises_its_metrics_url() -> None:
    """The poller's endpoint list comes from the adapters; each engine
    advertises where it serves Prometheus."""
    from kvwarden.engines.vllm_adapter.adapter import VLLMAdapter

    adapter = VLLMAdapter(model_id="meta-llama/Llama-3.1-8B-Instruct", port=8001)
    assert adapter.metrics_url == "http://localhost:8001/metrics"
