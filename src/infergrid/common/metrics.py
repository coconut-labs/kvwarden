"""Prometheus-style metrics collector for InferGrid.

Wraps prometheus_client to expose request counts, latency histograms,
cache hit rates, and GPU memory usage as both Prometheus metrics and
a plain-dict snapshot for the CLI status command.
"""

from __future__ import annotations

import time
from typing import Any

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

# Shared registry so tests can create isolated instances.
_default_registry = CollectorRegistry()


class MetricsCollector:
    """Central metrics collector for the InferGrid orchestration layer.

    All metrics are registered in a dedicated CollectorRegistry so they
    don't collide with the global default when running tests.

    Args:
        registry: Optional prometheus CollectorRegistry. A private one
            is created if not supplied.
    """

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self._registry = registry or CollectorRegistry()
        self._start_time = time.monotonic()

        # --- Request metrics ---
        self.request_count = Counter(
            "infergrid_requests_total",
            "Total number of incoming requests",
            labelnames=["model", "tenant", "status"],
            registry=self._registry,
        )
        self.request_latency = Histogram(
            "infergrid_request_latency_seconds",
            "End-to-end request latency in seconds",
            labelnames=["model"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
            registry=self._registry,
        )
        self.tokens_generated = Counter(
            "infergrid_tokens_generated_total",
            "Total output tokens generated",
            labelnames=["model"],
            registry=self._registry,
        )
        self.tokens_input = Counter(
            "infergrid_tokens_input_total",
            "Total input tokens processed",
            labelnames=["model"],
            registry=self._registry,
        )

        # --- Cache metrics ---
        self.cache_hits = Counter(
            "infergrid_cache_hits_total",
            "Number of KV cache hits",
            labelnames=["tier"],
            registry=self._registry,
        )
        self.cache_misses = Counter(
            "infergrid_cache_misses_total",
            "Number of KV cache misses",
            registry=self._registry,
        )
        self.cache_memory_bytes = Gauge(
            "infergrid_cache_memory_bytes",
            "Current cache memory usage in bytes",
            labelnames=["tier"],
            registry=self._registry,
        )

        # --- Model lifecycle ---
        self.models_loaded = Gauge(
            "infergrid_models_loaded",
            "Number of currently loaded models",
            registry=self._registry,
        )
        self.model_evictions = Counter(
            "infergrid_model_evictions_total",
            "Total number of model evictions",
            registry=self._registry,
        )

        # --- Tenant metrics ---
        self.tenant_request_count = Counter(
            "infergrid_tenant_requests_total",
            "Requests per tenant",
            labelnames=["tenant"],
            registry=self._registry,
        )
        self.tenant_rejected = Counter(
            "infergrid_tenant_requests_rejected_total",
            "Requests rejected due to budget limits",
            labelnames=["tenant", "reason"],
            registry=self._registry,
        )

        # --- GPU ---
        self.gpu_memory_used_bytes = Gauge(
            "infergrid_gpu_memory_used_bytes",
            "GPU memory used in bytes",
            labelnames=["gpu_index"],
            registry=self._registry,
        )

    # ------------------------------------------------------------------
    # Convenience helpers
    # ------------------------------------------------------------------

    def record_request(
        self,
        model: str,
        tenant: str,
        status: str,
        latency_s: float,
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        """Record a completed request with all associated metrics.

        Args:
            model: Model identifier.
            tenant: Tenant identifier.
            status: "ok" or "error".
            latency_s: Total latency in seconds.
            tokens_in: Number of input tokens.
            tokens_out: Number of output tokens.
        """
        self.request_count.labels(model=model, tenant=tenant, status=status).inc()
        self.request_latency.labels(model=model).observe(latency_s)
        self.tenant_request_count.labels(tenant=tenant).inc()
        if tokens_in > 0:
            self.tokens_input.labels(model=model).inc(tokens_in)
        if tokens_out > 0:
            self.tokens_generated.labels(model=model).inc(tokens_out)

    def record_cache_access(self, hit: bool, tier: str = "gpu") -> None:
        """Record a cache hit or miss.

        Args:
            hit: True for a cache hit, False for a miss.
            tier: Cache tier name ("gpu", "cpu", "ssd").
        """
        if hit:
            self.cache_hits.labels(tier=tier).inc()
        else:
            self.cache_misses.inc()

    def snapshot(self) -> dict[str, Any]:
        """Return a plain-dict snapshot of current metrics for the CLI.

        Returns:
            Dictionary with key metric values.
        """
        uptime = time.monotonic() - self._start_time

        # Sum across all label combinations for totals
        total_requests = 0.0
        for metric in self.request_count.collect():
            for sample in metric.samples:
                if sample.name == "infergrid_requests_total":
                    total_requests += sample.value

        total_cache_hits = 0.0
        for metric in self.cache_hits.collect():
            for sample in metric.samples:
                if sample.name == "infergrid_cache_hits_total":
                    total_cache_hits += sample.value

        total_cache_misses = 0.0
        for metric in self.cache_misses.collect():
            for sample in metric.samples:
                if sample.name == "infergrid_cache_misses_total":
                    total_cache_misses += sample.value

        total_accesses = total_cache_hits + total_cache_misses
        hit_rate = (total_cache_hits / total_accesses) if total_accesses > 0 else 0.0

        return {
            "uptime_s": round(uptime, 1),
            "total_requests": int(total_requests),
            "cache_hit_rate": round(hit_rate, 4),
            "cache_hits": int(total_cache_hits),
            "cache_misses": int(total_cache_misses),
        }

    def prometheus_output(self) -> bytes:
        """Generate Prometheus-compatible metrics output.

        Returns:
            Bytes in Prometheus text exposition format.
        """
        return generate_latest(self._registry)
