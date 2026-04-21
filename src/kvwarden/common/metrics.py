"""Prometheus-style metrics collector for KVWarden.

Wraps prometheus_client to expose request counts, latency histograms,
per-tenant TTFT, engine lifecycle, and SSE disconnect counters as both
Prometheus metrics and a plain-dict snapshot for the CLI status command.
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
    """Central metrics collector for the KVWarden orchestration layer.

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
        # `engine` label (added in the 2026-04-21 metrics audit remediation)
        # lets dashboards split 5xx/latency by engine kind — the single
        # highest-value SRE question for a middleware tool that sits
        # between clients and either vLLM or SGLang.
        self.request_count = Counter(
            "kvwarden_requests_total",
            "Total number of incoming requests",
            labelnames=["model", "tenant", "status", "engine"],
            registry=self._registry,
        )
        self.request_latency = Histogram(
            "kvwarden_request_latency_seconds",
            "End-to-end request latency in seconds",
            labelnames=["model"],
            buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0),
            registry=self._registry,
        )
        self.tokens_generated = Counter(
            "kvwarden_tokens_generated_total",
            "Total output tokens generated",
            labelnames=["model"],
            registry=self._registry,
        )
        self.tokens_input = Counter(
            "kvwarden_tokens_input_total",
            "Total input tokens processed",
            labelnames=["model"],
            registry=self._registry,
        )

        # --- Model lifecycle ---
        self.models_loaded = Gauge(
            "kvwarden_models_loaded",
            "Number of currently loaded models",
            registry=self._registry,
        )
        self.model_evictions = Counter(
            "kvwarden_model_evictions_total",
            "Total number of model evictions",
            registry=self._registry,
        )

        # --- Engine lifecycle (added 2026-04-21, audit §2 row 10-11) ---
        # Gauge tracks up/down state for each (model, engine) pair so
        # SREs can wire a trivial "engine down for >1m" alert. Histogram
        # captures cold-start latency; buckets cover 10s (warm model on
        # fast disk) through 600s (first-time HF pull + CUDA init).
        self.engine_up = Gauge(
            "kvwarden_engine_up",
            "1 if the engine subprocess is healthy, 0 otherwise",
            labelnames=["model", "engine"],
            registry=self._registry,
        )
        self.engine_cold_start_seconds = Histogram(
            "kvwarden_engine_cold_start_seconds",
            "Seconds from adapter.start() invocation to first healthy response",
            labelnames=["model", "engine"],
            buckets=(10.0, 30.0, 60.0, 120.0, 300.0, 600.0),
            registry=self._registry,
        )

        # --- SSE stream disconnects (added 2026-04-21, audit §2 row 12) ---
        # Reasons: client_disconnect | upstream_error | timeout. Fired
        # from the router's SSE pass-through path — see
        # router.handle_request and router._stream_with_admission.
        self.sse_stream_disconnects = Counter(
            "kvwarden_sse_stream_disconnect_total",
            "SSE streams that terminated abnormally, by reason",
            labelnames=["reason"],
            registry=self._registry,
        )

        # --- Tenant metrics ---
        self.tenant_request_count = Counter(
            "kvwarden_tenant_requests_total",
            "Requests per tenant",
            labelnames=["tenant"],
            registry=self._registry,
        )
        self.tenant_rejected = Counter(
            "kvwarden_tenant_requests_rejected_total",
            "Requests rejected due to budget limits",
            labelnames=["tenant", "reason"],
            registry=self._registry,
        )
        # Per-tenant TTFT histogram. The hero metric — quiet-tenant
        # starvation manifests at p99 here. Buckets span solo baseline
        # (~50ms) to severe starvation (>10s).
        self.tenant_ttft_seconds = Histogram(
            "kvwarden_tenant_ttft_seconds",
            "Time to first token by tenant + model (Prometheus histogram)",
            labelnames=["model", "tenant"],
            buckets=(
                0.025,
                0.05,
                0.075,
                0.1,
                0.15,
                0.25,
                0.5,
                1.0,
                2.5,
                5.0,
                10.0,
                30.0,
                60.0,
            ),
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
        engine: str = "unknown",
        tokens_in: int = 0,
        tokens_out: int = 0,
    ) -> None:
        """Record a completed request with all associated metrics.

        Args:
            model: Model identifier.
            tenant: Tenant identifier.
            status: "ok", "error", or "timeout".
            latency_s: Total latency in seconds.
            engine: Engine kind ("vllm" / "sglang"). Defaults to
                "unknown" so ad-hoc callers don't crash; production
                call sites should always pass the real engine kind.
            tokens_in: Number of input tokens.
            tokens_out: Number of output tokens.
        """
        self.request_count.labels(
            model=model, tenant=tenant, status=status, engine=engine
        ).inc()
        self.request_latency.labels(model=model).observe(latency_s)
        self.tenant_request_count.labels(tenant=tenant).inc()
        if tokens_in > 0:
            self.tokens_input.labels(model=model).inc(tokens_in)
        if tokens_out > 0:
            self.tokens_generated.labels(model=model).inc(tokens_out)

    def record_ttft(self, model: str, tenant: str, ttft_s: float) -> None:
        """Record per-tenant TTFT. Call exactly once per request, on the
        first non-empty SSE token (sse_frames transition 0→1).

        Args:
            model: Model identifier.
            tenant: Tenant identifier.
            ttft_s: Time to first token in seconds.
        """
        if ttft_s < 0:
            return
        self.tenant_ttft_seconds.labels(model=model, tenant=tenant).observe(ttft_s)

    def set_engine_up(self, model: str, engine: str, up: bool) -> None:
        """Flip the engine_up gauge for a (model, engine) pair.

        Args:
            model: Model identifier.
            engine: Engine kind ("vllm" / "sglang").
            up: True iff the engine is currently healthy.
        """
        self.engine_up.labels(model=model, engine=engine).set(1 if up else 0)

    def record_cold_start(self, model: str, engine: str, duration_s: float) -> None:
        """Record the seconds from engine bring-up start to first healthy response.

        Args:
            model: Model identifier.
            engine: Engine kind ("vllm" / "sglang").
            duration_s: Elapsed seconds.
        """
        if duration_s < 0:
            return
        self.engine_cold_start_seconds.labels(model=model, engine=engine).observe(
            duration_s
        )

    def record_sse_disconnect(self, reason: str) -> None:
        """Increment the SSE abnormal-termination counter.

        Args:
            reason: One of "client_disconnect", "upstream_error", "timeout".
        """
        self.sse_stream_disconnects.labels(reason=reason).inc()

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
                if sample.name == "kvwarden_requests_total":
                    total_requests += sample.value

        return {
            "uptime_s": round(uptime, 1),
            "total_requests": int(total_requests),
        }

    def prometheus_output(self) -> bytes:
        """Generate Prometheus-compatible metrics output.

        Returns:
            Bytes in Prometheus text exposition format.
        """
        return generate_latest(self._registry)
