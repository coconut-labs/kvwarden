"""Engine KV-cache-pressure probe.

Scrapes an inference engine's Prometheus endpoint for its KV cache
utilisation gauge and records the reading into :class:`CacheManager`, where
``snapshot()`` surfaces it for the admission-side priority composition in
``router/admission.py``.

The engine owns the cache substrate. We do not patch its eviction and we do
not claim per-tenant cache visibility — the gauge is global per engine
instance, labelled only by ``engine`` and ``model_name``. What kvwarden adds
is the *joint* signal: engine cache pressure composed with a tenant fairness
ledger that lives outside the engine.

Everything here is off the request hot path. The loop caches the latest
reading in memory; admission reads that cached value and never blocks on
HTTP. A scrape that fails records 0.0 — no pressure signal, so admission
degrades to v0.1 behavior rather than erroring.

# T2 — issue #103, RFC at docs/rfcs/T2-cache-pressure-admission.md
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

import aiohttp
from prometheus_client.parser import text_string_to_metric_families

from kvwarden.cache.manager import CacheManager

logger = logging.getLogger(__name__)

# The entire contract with vLLM. Documented at
# docs.vllm.ai/en/latest/design/metrics/ as "Fraction of used KV cache
# blocks (0-1)"; verified against primary source 2026-04-28, NOT against a
# live engine from this repo's test suite. A rename in a vLLM minor is the
# failure mode `requirements-gpu.txt` pins against, and that the poller's
# failure counter surfaces at runtime.
KV_CACHE_PRESSURE_METRIC: str = "vllm:kv_cache_usage_perc"

# Default scrape cadence. vLLM 0.19.1 on A100 under 4 RPS updates this gauge
# at p50 0.25s (P1 pre-flight, results/p1_gauge_preflight_20260502/), so a
# faster poll buys nothing but load.
DEFAULT_POLL_INTERVAL_S: float = 0.25

# Per-scrape HTTP timeout. Must stay well under the poll interval so a hung
# engine cannot stack overlapping scrapes.
_SCRAPE_TIMEOUT_S: float = 0.2

FetchFn = Callable[[str], Awaitable[str | None]]
EndpointsFn = Callable[[], list[str]]


def parse_cache_pressure(
    text: str, metric_name: str = KV_CACHE_PRESSURE_METRIC
) -> float | None:
    """Extract the KV cache utilisation gauge from a Prometheus text body.

    When one endpoint reports several engine instances, the maximum is
    returned: composition against per-tenant deficit should reflect
    worst-case pressure for the route, not an average that masks one
    saturated engine (RFC §Architecture).

    Args:
        text: Raw ``/metrics`` response body.
        metric_name: Gauge to look for.

    Returns:
        The largest sample value, or ``None`` if the gauge is absent or the
        body does not parse. ``None`` is deliberately distinct from ``0.0``:
        a missing gauge means no signal, an empty cache means no pressure.
    """
    try:
        families = list(text_string_to_metric_families(text))
    except Exception as exc:  # malformed / truncated / not Prometheus at all
        logger.debug("cache-pressure: unparseable /metrics body: %s", exc)
        return None

    values = [
        sample.value
        for family in families
        if family.name == metric_name
        for sample in family.samples
        if sample.name == metric_name
    ]
    if not values:
        return None
    return max(values)


class CachePressurePoller:
    """Background loop that keeps ``CacheManager`` fed with engine pressure.

    Args:
        cache_manager: Where readings are recorded.
        endpoints: Callable returning the ``/metrics`` URLs to scrape. A
            callable rather than a list so the poller picks up models that
            load and unload while it runs.
        interval_s: Seconds between scrapes.
        fetch: Test seam. Overrides the aiohttp scrape with a coroutine that
            maps a URL to a response body, or ``None`` for unreachable.
    """

    def __init__(
        self,
        cache_manager: CacheManager,
        endpoints: EndpointsFn,
        *,
        interval_s: float = DEFAULT_POLL_INTERVAL_S,
        fetch: FetchFn | None = None,
        metric_name: str = KV_CACHE_PRESSURE_METRIC,
    ) -> None:
        self._cache_manager = cache_manager
        self._endpoints = endpoints
        self._interval_s = interval_s
        self._fetch = fetch or self._fetch_http
        self._metric_name = metric_name
        self._session: aiohttp.ClientSession | None = None
        self._consecutive_failures = 0

    @property
    def consecutive_failures(self) -> int:
        """Scrape rounds in a row that produced no reading from any endpoint.

        Zero when at least one endpoint answered with the gauge present. A
        climbing count on a running engine is the signal that the gauge was
        renamed upstream.
        """
        return self._consecutive_failures

    async def poll_once(self) -> float:
        """Scrape every endpoint once and record the maximum reading.

        Never raises. An endpoint that is unreachable, slow, or missing the
        gauge contributes nothing; if no endpoint contributes, 0.0 is
        recorded and admission falls back to v0.1 priority composition.

        Returns:
            The pressure value recorded, in [0.0, 1.0].
        """
        urls = list(self._endpoints())
        readings: list[float] = []

        for url in urls:
            reading = await self._read_endpoint(url)
            if reading is not None:
                readings.append(reading)

        if urls and not readings:
            self._consecutive_failures += 1
            if self._consecutive_failures in (1, 10, 100):
                logger.warning(
                    "cache-pressure: no reading from %d endpoint(s), "
                    "%d consecutive failures; admission degrades to DRR-only",
                    len(urls),
                    self._consecutive_failures,
                )
        else:
            # No endpoints at all means no engine is loaded yet. That is a
            # cold start, not a failure to signal.
            self._consecutive_failures = 0

        pressure = max(readings) if readings else 0.0
        self._cache_manager.record_kv_cache_pressure(pressure)
        return pressure

    async def run(self) -> None:
        """Poll forever at the configured cadence until cancelled."""
        logger.info(
            "cache-pressure poller started: metric=%s interval=%.3fs",
            self._metric_name,
            self._interval_s,
        )
        try:
            while True:
                await self.poll_once()
                await asyncio.sleep(self._interval_s)
        except asyncio.CancelledError:
            raise
        finally:
            await self.close()

    async def close(self) -> None:
        """Release the scrape session, if one was opened."""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _read_endpoint(self, url: str) -> float | None:
        try:
            body = await self._fetch(url)
        except Exception as exc:
            # Any transport error at all: connection refused, DNS, reset,
            # timeout. The poller is advisory; it must not take the router
            # down with it.
            logger.debug("cache-pressure: scrape of %s failed: %s", url, exc)
            return None
        if body is None:
            return None
        return parse_cache_pressure(body, self._metric_name)

    async def _fetch_http(self, url: str) -> str | None:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_SCRAPE_TIMEOUT_S)
            )
        async with self._session.get(url) as resp:
            if resp.status != 200:
                logger.debug("cache-pressure: %s returned HTTP %d", url, resp.status)
                return None
            return await resp.text()
