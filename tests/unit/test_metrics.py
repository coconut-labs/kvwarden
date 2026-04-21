"""Regression tests for the metrics audit remediation (PR #91 follow-up).

Pins the three pillars of the 2026-04-21 audit fix:

1. Four dead metrics (cache_hits, cache_misses, cache_memory_bytes,
   gpu_memory_used_bytes) are no longer registered.
2. `kvwarden_requests_total` has the new `engine` label.
3. Three new metrics are registered with the right names + labels:
   - `kvwarden_engine_up{model,engine}` gauge
   - `kvwarden_engine_cold_start_seconds{model,engine}` histogram
   - `kvwarden_sse_stream_disconnect_total{reason}` counter

Each test creates an isolated ``CollectorRegistry`` so the suite stays
process-safe regardless of ordering.
"""

from __future__ import annotations

from prometheus_client import CollectorRegistry

from kvwarden.common.metrics import MetricsCollector

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _registered_metric_names(m: MetricsCollector) -> set[str]:
    """Return every metric family name currently in the collector's registry.

    ``fam.name`` is the prometheus_client "stem" — for a Counter declared
    as ``kvwarden_requests_total`` it returns ``kvwarden_requests``.
    """
    return {fam.name for fam in m._registry.collect()}


def _label_names_for(m: MetricsCollector, metric_stem: str) -> set[str]:
    """Return the label names present on samples for ``metric_stem``.

    ``metric_stem`` is the family-level name as prometheus_client reports
    it (e.g. ``kvwarden_requests`` for the ``kvwarden_requests_total``
    counter). Drops the histogram-only ``le`` label.
    """
    labels: set[str] = set()
    for fam in m._registry.collect():
        if fam.name != metric_stem:
            continue
        for sample in fam.samples:
            labels.update(sample.labels.keys())
    return labels - {"le"}  # histograms get `le` on _bucket samples


# ---------------------------------------------------------------------------
# 1. Dead metrics retired
# ---------------------------------------------------------------------------


class TestDeadMetricsRemoved:
    """The four dead metrics flagged in docs/metrics_audit_20260421.md §1
    must no longer appear in the Prometheus registry."""

    def test_cache_hits_not_registered(self) -> None:
        m = MetricsCollector(registry=CollectorRegistry())
        assert "kvwarden_cache_hits" not in _registered_metric_names(m)

    def test_cache_misses_not_registered(self) -> None:
        m = MetricsCollector(registry=CollectorRegistry())
        assert "kvwarden_cache_misses" not in _registered_metric_names(m)

    def test_cache_memory_bytes_not_registered(self) -> None:
        m = MetricsCollector(registry=CollectorRegistry())
        assert "kvwarden_cache_memory_bytes" not in _registered_metric_names(m)

    def test_gpu_memory_used_bytes_not_registered(self) -> None:
        m = MetricsCollector(registry=CollectorRegistry())
        assert "kvwarden_gpu_memory_used_bytes" not in _registered_metric_names(m)

    def test_no_dead_metrics_in_prometheus_output(self) -> None:
        """Belt-and-braces: a fresh collector must not render any of the
        four dead names in its exposition text."""
        m = MetricsCollector(registry=CollectorRegistry())
        out = m.prometheus_output().decode()
        for name in (
            "kvwarden_cache_hits_total",
            "kvwarden_cache_misses_total",
            "kvwarden_cache_memory_bytes",
            "kvwarden_gpu_memory_used_bytes",
        ):
            assert name not in out, f"dead metric {name} still exposed"

    def test_record_cache_access_helper_removed(self) -> None:
        """The convenience helper that fed the dead counters is gone."""
        m = MetricsCollector(registry=CollectorRegistry())
        assert not hasattr(m, "record_cache_access")
        assert not hasattr(m, "cache_hits")
        assert not hasattr(m, "cache_misses")
        assert not hasattr(m, "cache_memory_bytes")
        assert not hasattr(m, "gpu_memory_used_bytes")


# ---------------------------------------------------------------------------
# 2. `engine` label on kvwarden_requests_total
# ---------------------------------------------------------------------------


class TestRequestsTotalEngineLabel:
    """`kvwarden_requests_total` must carry a `{engine}` label so
    dashboards can split 5xx by engine kind (vllm / sglang)."""

    def test_requests_total_has_engine_label(self) -> None:
        m = MetricsCollector(registry=CollectorRegistry())
        m.record_request(
            model="llama31-8b",
            tenant="t1",
            status="ok",
            latency_s=0.2,
            engine="vllm",
        )
        # prometheus_client's family name strips the `_total` suffix on
        # counters — the wire-level metric stays `kvwarden_requests_total`.
        assert "engine" in _label_names_for(m, "kvwarden_requests")

    def test_requests_total_splits_by_engine(self) -> None:
        m = MetricsCollector(registry=CollectorRegistry())
        m.record_request(
            model="llama31-8b", tenant="t1", status="ok", latency_s=0.1, engine="vllm"
        )
        m.record_request(
            model="llama31-8b",
            tenant="t1",
            status="ok",
            latency_s=0.1,
            engine="sglang",
        )
        out = m.prometheus_output().decode()
        # Two distinct series, one per engine.
        assert 'engine="vllm"' in out
        assert 'engine="sglang"' in out

    def test_requests_total_engine_defaults_to_unknown(self) -> None:
        """Legacy callers that omit engine= must not crash; series lands
        under engine=unknown so an alert on it surfaces the gap."""
        m = MetricsCollector(registry=CollectorRegistry())
        m.record_request(model="m", tenant="t", status="ok", latency_s=0.1)
        out = m.prometheus_output().decode()
        assert 'engine="unknown"' in out


# ---------------------------------------------------------------------------
# 3. Three new metrics registered
# ---------------------------------------------------------------------------


class TestNewMetricsRegistered:
    """Audit §2 rows 10-12: engine_up gauge, engine_cold_start histogram,
    sse_stream_disconnect counter."""

    # ---- engine_up -------------------------------------------------

    def test_engine_up_registered_with_labels(self) -> None:
        m = MetricsCollector(registry=CollectorRegistry())
        m.set_engine_up(model="llama31-8b", engine="vllm", up=True)
        assert "kvwarden_engine_up" in _registered_metric_names(m)
        assert _label_names_for(m, "kvwarden_engine_up") == {"model", "engine"}

    def test_engine_up_values(self) -> None:
        m = MetricsCollector(registry=CollectorRegistry())
        m.set_engine_up(model="m", engine="vllm", up=True)
        m.set_engine_up(model="m2", engine="sglang", up=False)
        out = m.prometheus_output().decode()
        assert 'kvwarden_engine_up{engine="vllm",model="m"} 1.0' in out
        assert 'kvwarden_engine_up{engine="sglang",model="m2"} 0.0' in out

    # ---- engine_cold_start_seconds ---------------------------------

    def test_cold_start_histogram_registered_with_labels(self) -> None:
        m = MetricsCollector(registry=CollectorRegistry())
        m.record_cold_start(model="m", engine="vllm", duration_s=42.0)
        assert "kvwarden_engine_cold_start_seconds" in _registered_metric_names(m)
        labels = _label_names_for(m, "kvwarden_engine_cold_start_seconds")
        assert labels == {"model", "engine"}

    def test_cold_start_buckets_cover_10s_to_600s(self) -> None:
        """Buckets must cover the 10 s - 600 s range called out in the
        remediation task (audit §5 queue-for-v0.1.3)."""
        m = MetricsCollector(registry=CollectorRegistry())
        m.record_cold_start(model="m", engine="vllm", duration_s=45.0)
        out = m.prometheus_output().decode()
        for edge in ("10.0", "30.0", "60.0", "120.0", "300.0", "600.0"):
            assert f'le="{edge}"' in out, f"missing cold-start bucket le={edge}"

    def test_cold_start_ignores_negative(self) -> None:
        m = MetricsCollector(registry=CollectorRegistry())
        m.record_cold_start(model="m", engine="vllm", duration_s=-1.0)
        out = m.prometheus_output().decode()
        # No observations landed → _count stays 0.
        assert (
            'kvwarden_engine_cold_start_seconds_count{engine="vllm",model="m"}'
            not in out
        )

    # ---- sse_stream_disconnect_total -------------------------------

    def test_sse_disconnect_counter_registered_with_reason_label(self) -> None:
        m = MetricsCollector(registry=CollectorRegistry())
        m.record_sse_disconnect(reason="client_disconnect")
        assert "kvwarden_sse_stream_disconnect" in _registered_metric_names(m)
        assert _label_names_for(m, "kvwarden_sse_stream_disconnect") == {"reason"}

    def test_sse_disconnect_three_reasons(self) -> None:
        """All three reason values must round-trip through the counter."""
        m = MetricsCollector(registry=CollectorRegistry())
        m.record_sse_disconnect(reason="client_disconnect")
        m.record_sse_disconnect(reason="upstream_error")
        m.record_sse_disconnect(reason="timeout")
        out = m.prometheus_output().decode()
        assert 'reason="client_disconnect"' in out
        assert 'reason="upstream_error"' in out
        assert 'reason="timeout"' in out


# ---------------------------------------------------------------------------
# Regression: existing metrics still register correctly
# ---------------------------------------------------------------------------


class TestSurvivingMetricsIntact:
    """Sanity check that the audit remediation didn't accidentally drop
    metrics it shouldn't have touched."""

    def test_tenant_ttft_still_present(self) -> None:
        m = MetricsCollector(registry=CollectorRegistry())
        m.record_ttft(model="m", tenant="t", ttft_s=0.05)
        assert "kvwarden_tenant_ttft_seconds" in _registered_metric_names(m)

    def test_request_latency_still_present(self) -> None:
        m = MetricsCollector(registry=CollectorRegistry())
        m.record_request(
            model="m", tenant="t", status="ok", latency_s=0.1, engine="vllm"
        )
        assert "kvwarden_request_latency_seconds" in _registered_metric_names(m)

    def test_models_loaded_gauge_still_present(self) -> None:
        m = MetricsCollector(registry=CollectorRegistry())
        m.models_loaded.inc()
        out = m.prometheus_output().decode()
        assert "kvwarden_models_loaded" in out

    def test_snapshot_still_returns_dict_without_cache_keys(self) -> None:
        """`snapshot()` used to emit cache_hit_rate / cache_hits / cache_misses
        backed by the now-deleted counters. After the audit fix these keys
        must be gone; uptime + total_requests remain."""
        m = MetricsCollector(registry=CollectorRegistry())
        snap = m.snapshot()
        assert "uptime_s" in snap
        assert "total_requests" in snap
        assert "cache_hit_rate" not in snap
        assert "cache_hits" not in snap
        assert "cache_misses" not in snap
