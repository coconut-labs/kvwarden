"""Shadow-mode recording for cache-pressure admission.

Measures the cache-pressure lever without pulling it. For each request the
recorder computes the priority the lever *would* have produced and reports
the difference against the deficit the live path actually used — while the
live path keeps handing the AdmissionController exactly what it handed it
in v0.1.

Why shadow first: the watermarks in the curve are the RFC's placeholders,
not measured values, and the Gate 3 probe that would fix them has not
landed capacity. Shipping the lever enabled would mean gating real traffic
on numbers nobody has measured. Shadow mode turns the same deployment into
the measurement.

Nothing here is constructed unless the `cache_pressure_admission` block is
enabled, so the Prometheus series register lazily. A feature that is meant
to be invisible when off must not change /metrics scrape output when off.

# T2 — issue #103, RFC at docs/rfcs/T2-cache-pressure-admission.md
"""

from __future__ import annotations

import logging

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

from kvwarden.cache.manager import TenantPolicy
from kvwarden.common.config import CachePressureAdmissionConfig
from kvwarden.router.admission import cache_load_scaling, compose_priority

logger = logging.getLogger(__name__)


class CachePressureShadow:
    """Records the counterfactual priority of the cache-pressure lever.

    Args:
        config: The `cache_pressure_admission` block. Its watermarks and
            tenant weights drive the counterfactual.
        registry: Prometheus registry to register into. Series are omitted
            entirely when no registry is supplied.
    """

    def __init__(
        self,
        config: CachePressureAdmissionConfig,
        registry: CollectorRegistry | None = None,
    ) -> None:
        self._config = config
        self._policy = TenantPolicy(tenant_weights=dict(config.tenant_weights))
        self._last_failures = 0

        self._prom_pressure: Gauge | None = None
        self._prom_scale: Histogram | None = None
        self._prom_delta: Histogram | None = None
        self._prom_poll_failures: Counter | None = None

        if registry is not None:
            self._prom_pressure = Gauge(
                "kvwarden_kv_cache_pressure",
                "Latest engine KV cache utilisation, max across engine instances",
                registry=registry,
            )
            self._prom_scale = Histogram(
                "kvwarden_cache_pressure_scale",
                "Deficit amplification factor the pressure curve produced",
                buckets=(1.0, 1.25, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 6.0, 10.0),
                registry=registry,
            )
            self._prom_delta = Histogram(
                "kvwarden_shadow_priority_delta",
                "Priority the cache-pressure lever would add to this tenant's "
                "deficit. Shadow only — not applied to admission.",
                labelnames=["tenant"],
                buckets=(0.0, 1.0, 10.0, 100.0, 1_000.0, 10_000.0, 100_000.0),
                registry=registry,
            )
            self._prom_poll_failures = Counter(
                "kvwarden_cache_pressure_poll_failures_total",
                "Scrape rounds that produced no reading from any engine",
                registry=registry,
            )

        logger.info(
            "cache-pressure shadow recording enabled: soft=%.2f hard=%.2f "
            "ceiling=%.2f weights=%d tenant(s). Priorities are NOT modified.",
            config.soft_threshold,
            config.hard_threshold,
            config.saturation_ceiling,
            len(config.tenant_weights),
        )

    def evaluate(
        self, tenant_id: str, base_priority: int, pressure: float | None
    ) -> int:
        """Priority the lever would add to this tenant's deficit.

        The length-bucket tie-breaker is deliberately not part of this: it
        is added downstream of the composition and cancels in the
        difference, so what is left isolates the cache-pressure effect from
        the scheduling discipline in force.

        Args:
            tenant_id: Tenant the request belongs to.
            base_priority: The tenant's DRR deficit score.
            pressure: Latest gauge reading, or ``None`` for no signal.

        Returns:
            ``shadow_priority - base_priority``. Zero whenever the curve is
            identity, whatever the tenant weights say.
        """
        shadow = compose_priority(
            tenant_id=tenant_id,
            base_priority=base_priority,
            policy=self._policy,
            kv_cache_pressure=pressure,
            soft_threshold=self._config.soft_threshold,
            hard_threshold=self._config.hard_threshold,
            ceiling=self._config.saturation_ceiling,
        )
        return shadow - base_priority

    def record(self, tenant_id: str, base_priority: int, pressure: float | None) -> int:
        """Evaluate the counterfactual and report it to Prometheus."""
        delta = self.evaluate(tenant_id, base_priority, pressure)
        if self._prom_delta is not None:
            self._prom_delta.labels(tenant=tenant_id).observe(delta)
        if self._prom_scale is not None:
            self._prom_scale.observe(
                cache_load_scaling(
                    pressure,
                    soft_threshold=self._config.soft_threshold,
                    hard_threshold=self._config.hard_threshold,
                    ceiling=self._config.saturation_ceiling,
                )
            )
        return delta

    def observe_poll(self, pressure: float, consecutive_failures: int) -> None:
        """Report one scrape round.

        The poller tracks failures as a run length that resets on recovery;
        the Prometheus series is a counter, so only the increments are
        forwarded.
        """
        if self._prom_pressure is not None:
            self._prom_pressure.set(pressure)
        if self._prom_poll_failures is not None:
            increment = max(0, consecutive_failures - self._last_failures)
            if increment:
                self._prom_poll_failures.inc(increment)
        self._last_failures = consecutive_failures
