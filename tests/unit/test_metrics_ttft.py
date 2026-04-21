"""Tests for per-tenant TTFT histogram in MetricsCollector."""

from __future__ import annotations

from prometheus_client import CollectorRegistry

from kvwarden.common.metrics import MetricsCollector


def _ttft_count(metrics: MetricsCollector, *, model: str, tenant: str) -> int:
    """Sum the _count sample for a given (model, tenant) pair."""
    for fam in metrics._registry.collect():
        if fam.name != "kvwarden_tenant_ttft_seconds":
            continue
        for sample in fam.samples:
            if (
                sample.name == "kvwarden_tenant_ttft_seconds_count"
                and sample.labels.get("model") == model
                and sample.labels.get("tenant") == tenant
            ):
                return int(sample.value)
    return 0


def test_record_ttft_increments_count():
    m = MetricsCollector(registry=CollectorRegistry())
    m.record_ttft(model="llama31-8b", tenant="quiet", ttft_s=0.05)
    m.record_ttft(model="llama31-8b", tenant="quiet", ttft_s=0.1)
    m.record_ttft(model="llama31-8b", tenant="noisy", ttft_s=2.5)
    assert _ttft_count(m, model="llama31-8b", tenant="quiet") == 2
    assert _ttft_count(m, model="llama31-8b", tenant="noisy") == 1


def test_record_ttft_negative_ignored():
    m = MetricsCollector(registry=CollectorRegistry())
    m.record_ttft(model="llama31-8b", tenant="quiet", ttft_s=-0.1)
    assert _ttft_count(m, model="llama31-8b", tenant="quiet") == 0


def test_record_ttft_buckets_present_in_prometheus_output():
    m = MetricsCollector(registry=CollectorRegistry())
    m.record_ttft(model="llama31-8b", tenant="quiet", ttft_s=0.04)
    out = m.prometheus_output().decode()
    assert "kvwarden_tenant_ttft_seconds" in out
    # Hero buckets we care about for the launch story
    assert 'le="0.05"' in out
    assert 'le="0.1"' in out
    assert 'le="10.0"' in out


def test_record_ttft_per_tenant_independence():
    m = MetricsCollector(registry=CollectorRegistry())
    # Quiet tenant: all sub-100ms (the hero scenario)
    for v in [0.04, 0.05, 0.06, 0.07, 0.08]:
        m.record_ttft(model="llama31-8b", tenant="quiet", ttft_s=v)
    # Noisy tenant: long-tail
    for v in [0.5, 1.5, 5.0, 12.0, 28.0]:
        m.record_ttft(model="llama31-8b", tenant="noisy", ttft_s=v)
    out = m.prometheus_output().decode()
    # Independence check: each tenant should have its own series
    assert 'tenant="quiet"' in out
    assert 'tenant="noisy"' in out
