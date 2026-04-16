"""WorkloadRouter -- the central request routing and model lifecycle manager.

Accepts incoming OpenAI-compatible API requests, routes them to the
correct engine backend, and manages model loading/eviction using a
frequency+recency weighted policy (not naive LRU).

Implements a multi-queue architecture with length-bucketed scheduling
so that short requests are fast-tracked to avoid head-of-line blocking.
"""

from __future__ import annotations

import asyncio
import logging
import math
import socket
import time
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web

from infergrid.cache.manager import CacheManager
from infergrid.common.config import InferGridConfig, ModelConfig
from infergrid.common.metrics import MetricsCollector
from infergrid.engines.base import EngineAdapter
from infergrid.engines.sglang_adapter.adapter import SGLangAdapter
from infergrid.engines.vllm_adapter.adapter import VLLMAdapter
from infergrid.tenant.manager import TenantManager

logger = logging.getLogger(__name__)


# ── Length buckets for multi-queue scheduling ────────────────────────

LENGTH_BUCKETS: list[tuple[str, int]] = [
    ("short", 256),
    ("medium", 1024),
    ("long", 4096),
    ("xlarge", 2**31),
]


def classify_request_length(max_tokens: int, input_tokens: int = 0) -> str:
    """Classify a request into a length bucket.

    Uses the larger of max_tokens and input_tokens for bucketing.

    Args:
        max_tokens: Requested maximum output tokens.
        input_tokens: Estimated input token count.

    Returns:
        Bucket name ("short", "medium", "long", "xlarge").
    """
    size = max(max_tokens, input_tokens)
    for name, threshold in LENGTH_BUCKETS:
        if size <= threshold:
            return name
    return "xlarge"


# ── Per-model tracking ──────────────────────────────────────────────

@dataclass
class ModelState:
    """Runtime state for a loaded model.

    Attributes:
        config: Static model configuration.
        adapter: The engine adapter managing the subprocess.
        request_count: Total requests routed to this model.
        total_latency_s: Sum of request latencies (for avg calculation).
        last_request_time: Monotonic timestamp of last request.
        loaded_time: Monotonic timestamp when the model was loaded.
    """

    config: ModelConfig
    adapter: EngineAdapter
    request_count: int = 0
    total_latency_s: float = 0.0
    last_request_time: float = field(default_factory=time.monotonic)
    loaded_time: float = field(default_factory=time.monotonic)

    @property
    def avg_latency_s(self) -> float:
        """Average request latency in seconds."""
        if self.request_count == 0:
            return 0.0
        return self.total_latency_s / self.request_count

    def eviction_score(self, now: float, decay_half_life_s: float = 600.0) -> float:
        """Compute eviction priority (lower = evict sooner).

        Uses frequency * recency-decay, NOT naive LRU.

        Args:
            now: Current monotonic time.
            decay_half_life_s: Half-life for recency decay.

        Returns:
            Non-negative score; lower = better eviction candidate.
        """
        age_s = max(now - self.last_request_time, 0.001)
        decay = math.exp(-math.log(2) * age_s / decay_half_life_s)
        freq = math.log1p(self.request_count)
        return freq * decay


# ── Priority request queue ──────────────────────────────────────────

@dataclass
class PendingRequest:
    """A request waiting in the scheduling queue."""

    request_id: str
    model_id: str
    path: str
    payload: dict[str, Any]
    tenant_id: str
    bucket: str
    enqueue_time: float = field(default_factory=time.monotonic)
    future: asyncio.Future[Any] = field(default_factory=lambda: asyncio.get_running_loop().create_future())


class WorkloadRouter:
    """Central request router and model lifecycle manager.

    Manages a pool of engine adapters, routes requests to the right model,
    and handles model loading/eviction based on demand.

    Args:
        config: InferGrid configuration.
        metrics: Metrics collector instance.
        cache_manager: KV cache manager instance.
        tenant_manager: Tenant manager instance.
    """

    def __init__(
        self,
        config: InferGridConfig,
        metrics: MetricsCollector | None = None,
        cache_manager: CacheManager | None = None,
        tenant_manager: TenantManager | None = None,
    ) -> None:
        self.config = config
        self.metrics = metrics or MetricsCollector()
        self.cache_manager = cache_manager or CacheManager()
        self.tenant_manager = tenant_manager or TenantManager()

        # model_id -> ModelState (only for currently loaded models)
        self._models: dict[str, ModelState] = {}

        # All known model configs (loaded or not)
        self._model_configs: dict[str, ModelConfig] = {
            m.model_id: m for m in config.models
        }

        # Port allocator for engine subprocesses
        self._next_port = 8001

        # Multi-queue: bucket_name -> asyncio.Queue
        self._queues: dict[str, asyncio.Queue[PendingRequest]] = {
            name: asyncio.Queue() for name, _ in LENGTH_BUCKETS
        }

        # Queue workers
        self._workers: list[asyncio.Task[None]] = []
        self._running = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the router and pre-load configured models."""
        self._running = True

        # Start queue workers -- short queue gets more workers for priority
        worker_counts = {"short": 4, "medium": 2, "long": 1, "xlarge": 1}
        for bucket_name, count in worker_counts.items():
            for i in range(count):
                task = asyncio.create_task(
                    self._queue_worker(bucket_name),
                    name=f"worker-{bucket_name}-{i}",
                )
                self._workers.append(task)

        # Pre-load configured models
        for model_cfg in self.config.models:
            try:
                await self.load_model(model_cfg)
            except Exception as exc:
                logger.error(
                    "Failed to pre-load model %s: %s", model_cfg.model_id, exc
                )

        logger.info(
            "WorkloadRouter started with %d models, %d queue workers",
            len(self._models),
            len(self._workers),
        )

    async def stop(self) -> None:
        """Stop all queue workers and engine subprocesses."""
        self._running = False

        # Cancel workers
        for task in self._workers:
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

        # Stop all engines
        for model_id in list(self._models.keys()):
            await self.unload_model(model_id)

        logger.info("WorkloadRouter stopped")

    # ------------------------------------------------------------------
    # Model lifecycle
    # ------------------------------------------------------------------

    async def load_model(self, config: ModelConfig) -> ModelState:
        """Load a model by starting its engine subprocess.

        Args:
            config: Model configuration.

        Returns:
            The ModelState for the newly loaded model.

        Raises:
            RuntimeError: If the model is already loaded.
        """
        if config.model_id in self._models:
            return self._models[config.model_id]

        port = config.port if config.port > 0 else self._allocate_port()
        adapter = self._create_adapter(config, port)

        await adapter.start(timeout_s=self.config.engine_start_timeout_s)

        state = ModelState(config=config, adapter=adapter)
        self._models[config.model_id] = state
        self._model_configs[config.model_id] = config
        self.metrics.models_loaded.inc()

        logger.info("Loaded model %s on port %d", config.model_id, port)
        return state

    async def unload_model(self, model_id: str) -> bool:
        """Unload a model by stopping its engine subprocess.

        Args:
            model_id: Model identifier.

        Returns:
            True if the model was found and unloaded.
        """
        state = self._models.pop(model_id, None)
        if state is None:
            return False

        await state.adapter.stop()
        self.cache_manager.free_blocks_for_model(model_id)
        self.metrics.models_loaded.dec()
        self.metrics.model_evictions.inc()

        logger.info("Unloaded model %s", model_id)
        return True

    async def evict_model(self) -> str | None:
        """Evict the least-valuable loaded model to free GPU resources.

        Uses frequency+recency scoring, NOT naive LRU.

        Returns:
            The evicted model_id, or None if no models are loaded.
        """
        if not self._models:
            return None

        now = time.monotonic()
        victim_id = min(
            self._models.keys(),
            key=lambda mid: self._models[mid].eviction_score(now),
        )

        await self.unload_model(victim_id)
        logger.info("Evicted model %s (lowest eviction score)", victim_id)
        return victim_id

    async def ensure_model_loaded(self, model_id: str) -> ModelState:
        """Ensure a model is loaded, loading or hot-swapping as needed.

        Args:
            model_id: Model identifier.

        Returns:
            The ModelState for the (now loaded) model.

        Raises:
            ValueError: If the model is not in the known config.
        """
        if model_id in self._models:
            return self._models[model_id]

        config = self._model_configs.get(model_id)
        if config is None:
            raise ValueError(
                f"Unknown model {model_id!r}. "
                f"Known models: {list(self._model_configs.keys())}"
            )

        # If we are at capacity, evict first
        max_models = max(1, len(self.config.models))
        if len(self._models) >= max_models:
            await self.evict_model()

        return await self.load_model(config)

    # ------------------------------------------------------------------
    # Request routing
    # ------------------------------------------------------------------

    async def route_request(
        self,
        model_id: str,
        path: str,
        payload: dict[str, Any],
        tenant_id: str = "default",
        stream: bool = False,
    ) -> Any:
        """Route a single request to the appropriate engine.

        Enforces tenant budgets, tracks metrics, and uses length-bucketed
        scheduling for priority ordering.

        Args:
            model_id: Target model identifier.
            path: API path (e.g. "/v1/chat/completions").
            payload: JSON request body.
            tenant_id: Tenant making the request.
            stream: Whether to stream the response.

        Returns:
            The engine's JSON response, or an async stream.
        """
        start_time = time.monotonic()

        # Tenant budget check
        allowed = await self.tenant_manager.try_acquire_for_tenant(tenant_id)
        if not allowed:
            self.metrics.tenant_rejected.labels(
                tenant=tenant_id, reason="budget_exceeded"
            ).inc()
            raise BudgetExceededError(
                f"Tenant {tenant_id!r} has exceeded its request budget"
            )

        try:
            # Ensure model is loaded
            state = await self.ensure_model_loaded(model_id)

            # Forward the request
            result = await state.adapter.forward_request(path, payload, stream=stream)

            # Update tracking
            elapsed = time.monotonic() - start_time
            state.request_count += 1
            state.total_latency_s += elapsed
            state.last_request_time = time.monotonic()

            tokens_out = 0
            tokens_in = 0
            if isinstance(result, dict):
                usage = result.get("usage", {})
                tokens_out = usage.get("completion_tokens", 0)
                tokens_in = usage.get("prompt_tokens", 0)

            self.metrics.record_request(
                model=model_id,
                tenant=tenant_id,
                status="ok",
                latency_s=elapsed,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
            await self.tenant_manager.record_completion(
                tenant_id, tokens_in=tokens_in, tokens_out=tokens_out,
                gpu_seconds=elapsed,
            )
            return result

        except BudgetExceededError:
            raise
        except Exception as exc:
            elapsed = time.monotonic() - start_time
            self.metrics.record_request(
                model=model_id, tenant=tenant_id,
                status="error", latency_s=elapsed,
            )
            raise
        finally:
            await self.tenant_manager.release_for_tenant(tenant_id)

    async def enqueue_request(
        self,
        model_id: str,
        path: str,
        payload: dict[str, Any],
        tenant_id: str = "default",
    ) -> Any:
        """Enqueue a request into the length-bucketed scheduling system.

        Short requests are prioritized over long ones to minimize
        head-of-line blocking.

        Args:
            model_id: Target model.
            path: API path.
            payload: Request body.
            tenant_id: Tenant ID.

        Returns:
            The eventual response (awaited from the internal future).
        """
        max_tokens = payload.get("max_tokens", 256)
        bucket = classify_request_length(max_tokens)

        request = PendingRequest(
            request_id=f"{model_id}-{time.monotonic_ns()}",
            model_id=model_id,
            path=path,
            payload=payload,
            tenant_id=tenant_id,
            bucket=bucket,
        )

        await self._queues[bucket].put(request)
        return await request.future

    # ------------------------------------------------------------------
    # Queue workers
    # ------------------------------------------------------------------

    async def _queue_worker(self, bucket: str) -> None:
        """Process requests from a single length bucket queue.

        Args:
            bucket: The bucket name to service.
        """
        queue = self._queues[bucket]
        while self._running:
            try:
                request = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            try:
                result = await self.route_request(
                    model_id=request.model_id,
                    path=request.path,
                    payload=request.payload,
                    tenant_id=request.tenant_id,
                )
                if not request.future.done():
                    request.future.set_result(result)
            except Exception as exc:
                if not request.future.done():
                    request.future.set_exception(exc)

    # ------------------------------------------------------------------
    # aiohttp request handler
    # ------------------------------------------------------------------

    async def handle_request(self, request: web.Request) -> web.StreamResponse:
        """aiohttp handler for incoming OpenAI-compatible API requests.

        Args:
            request: The incoming aiohttp request.

        Returns:
            JSON response or streaming SSE response.
        """
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"error": "invalid JSON body"}, status=400
            )

        model_id = payload.get("model", "")
        if not model_id:
            return web.json_response(
                {"error": "missing 'model' field in request body"}, status=400
            )

        tenant_id = request.headers.get("X-Tenant-ID", "default")
        path = request.path
        stream = payload.get("stream", False)

        try:
            result = await self.route_request(
                model_id=model_id,
                path=path,
                payload=payload,
                tenant_id=tenant_id,
                stream=stream,
            )

            if stream and hasattr(result, "__aiter__"):
                response = web.StreamResponse(
                    status=200,
                    headers={"Content-Type": "text/event-stream"},
                )
                await response.prepare(request)
                async for chunk in result:
                    await response.write(chunk)
                await response.write_eof()
                return response

            return web.json_response(result)

        except BudgetExceededError as exc:
            return web.json_response(
                {"error": str(exc)}, status=429
            )
        except ValueError as exc:
            return web.json_response(
                {"error": str(exc)}, status=404
            )
        except Exception as exc:
            logger.exception("Request failed: %s", exc)
            return web.json_response(
                {"error": f"internal error: {exc}"}, status=500
            )

    async def handle_models(self, request: web.Request) -> web.Response:
        """Handle GET /v1/models -- list loaded models."""
        models_data = []
        for mid, state in self._models.items():
            models_data.append({
                "id": mid,
                "object": "model",
                "owned_by": "infergrid",
                "engine": state.config.engine,
                "healthy": state.adapter.is_healthy,
            })
        return web.json_response({
            "object": "list",
            "data": models_data,
        })

    async def handle_health(self, request: web.Request) -> web.Response:
        """Handle GET /health."""
        return web.json_response({"status": "ok"})

    # ------------------------------------------------------------------
    # Status / introspection
    # ------------------------------------------------------------------

    def loaded_models(self) -> list[str]:
        """Return list of currently loaded model IDs."""
        return list(self._models.keys())

    def model_stats(self) -> dict[str, dict[str, Any]]:
        """Return per-model statistics.

        Returns:
            Dictionary mapping model_id to stats dict.
        """
        now = time.monotonic()
        stats: dict[str, dict[str, Any]] = {}
        for mid, state in self._models.items():
            stats[mid] = {
                "engine": state.config.engine,
                "port": state.adapter.port,
                "request_count": state.request_count,
                "avg_latency_s": round(state.avg_latency_s, 4),
                "eviction_score": round(state.eviction_score(now), 4),
                "healthy": state.adapter.is_healthy,
                "uptime_s": round(now - state.loaded_time, 1),
            }
        return stats

    def queue_depths(self) -> dict[str, int]:
        """Return current queue depths per bucket.

        Returns:
            Dictionary mapping bucket name to queue size.
        """
        return {name: q.qsize() for name, q in self._queues.items()}

    def snapshot(self) -> dict[str, Any]:
        """Full status snapshot for the CLI.

        Returns:
            Dictionary with models, queues, cache, and tenant data.
        """
        return {
            "loaded_models": self.model_stats(),
            "queue_depths": self.queue_depths(),
            "cache": self.cache_manager.snapshot(),
            "tenants": self.tenant_manager.snapshot(),
            "metrics": self.metrics.snapshot(),
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _allocate_port(self) -> int:
        """Allocate the next available port for an engine.

        Checks that the port is actually free by attempting a socket
        bind before returning it.
        """
        while True:
            port = self._next_port
            self._next_port += 1
            if self._is_port_available(port):
                return port

    @staticmethod
    def _is_port_available(port: int) -> bool:
        """Check whether a TCP port is free to bind.

        Args:
            port: Port number to check.

        Returns:
            True if the port can be bound.
        """
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind(("127.0.0.1", port))
                return True
        except OSError:
            return False

    def _create_adapter(self, config: ModelConfig, port: int) -> EngineAdapter:
        """Create the appropriate engine adapter.

        Args:
            config: Model configuration.
            port: Port to use.

        Returns:
            An EngineAdapter instance (VLLMAdapter or SGLangAdapter).
        """
        if config.engine == "sglang":
            return SGLangAdapter(
                model_id=config.model_id,
                port=port,
                gpu_memory_utilization=config.gpu_memory_utilization,
                tensor_parallel_size=config.tensor_parallel_size,
                dtype=config.dtype,
                max_model_len=config.max_model_len,
                extra_args=config.extra_args,
            )
        else:
            return VLLMAdapter(
                model_id=config.model_id,
                port=port,
                gpu_memory_utilization=config.gpu_memory_utilization,
                tensor_parallel_size=config.tensor_parallel_size,
                dtype=config.dtype,
                max_model_len=config.max_model_len,
                extra_args=config.extra_args,
            )


# ── Custom exceptions ───────────────────────────────────────────────

class BudgetExceededError(Exception):
    """Raised when a tenant exceeds their request budget."""
