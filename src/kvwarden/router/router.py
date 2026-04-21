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
import os
import socket
import time
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from aiohttp import web

from kvwarden.cache.manager import CacheManager
from kvwarden.common.config import KVWardenConfig, ModelConfig
from kvwarden.common.metrics import MetricsCollector
from kvwarden.engines.base import EngineAdapter, EngineCircuitOpenError
from kvwarden.engines.sglang_adapter.adapter import SGLangAdapter
from kvwarden.engines.vllm_adapter.adapter import VLLMAdapter
from kvwarden.router.admission import AdmissionController, AdmissionTimeoutError
from kvwarden.tenant.manager import TenantManager

logger = logging.getLogger(__name__)

# Hard ceiling for any single streaming response. Guards against client-side
# slow consumers that would otherwise hold an admission slot indefinitely
# (engine-side stalls are already bounded by EngineAdapter sock_read /
# total timeouts in engines/base.py). Default 600s = 10 minutes; override
# via KVWARDEN_STREAM_MAX_DURATION_S for tests or specific deployments.
_STREAM_MAX_DURATION_S: float = float(
    os.environ.get("KVWARDEN_STREAM_MAX_DURATION_S", "600.0")
)


# ── Length buckets for multi-queue scheduling ────────────────────────

LENGTH_BUCKETS: list[tuple[str, int]] = [
    ("short", 256),
    ("medium", 1024),
    ("long", 4096),
    ("xlarge", 2**31),
]


BUCKET_PRIORITY: dict[str, int] = {
    "short": 0,
    "medium": 1,
    "long": 2,
    "xlarge": 3,
}


def _approx_tokens_in(payload: dict[str, Any]) -> int:
    """Whitespace-split approximation of input token count.

    Off by a constant factor vs a real tokenizer (BPE expands ~1.3x on
    English) but cheap, no model dependency, and sufficient for tenant
    budget accounting. The bench harness reports ground-truth counts.
    """
    text = payload.get("prompt") or " ".join(
        str(m.get("content", "")) for m in payload.get("messages", [])
    )
    return len(text.split())


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
    future: asyncio.Future[Any] = field(
        default_factory=lambda: asyncio.get_running_loop().create_future()
    )


class WorkloadRouter:
    """Central request router and model lifecycle manager.

    Manages a pool of engine adapters, routes requests to the right model,
    and handles model loading/eviction based on demand.

    Args:
        config: KVWarden configuration.
        metrics: Metrics collector instance.
        cache_manager: KV cache manager instance.
        tenant_manager: Tenant manager instance.
    """

    def __init__(
        self,
        config: KVWardenConfig,
        metrics: MetricsCollector | None = None,
        cache_manager: CacheManager | None = None,
        tenant_manager: TenantManager | None = None,
    ) -> None:
        self.config = config
        self.metrics = metrics or MetricsCollector()
        self.cache_manager = cache_manager or CacheManager()
        self.tenant_manager = tenant_manager or TenantManager()

        # Admission control -- prevents engine overload
        self.admission_controller = AdmissionController(
            max_concurrent=config.max_concurrent,
            queue_size=config.admission_queue_size,
            registry=self.metrics._registry,
        )

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

        # Per-model load locks. Serializes concurrent load_model() calls
        # for the same model_id; without this, N concurrent requests on a
        # cold model spawn N engine subprocesses in parallel (each sees
        # the cache empty in `ensure_model_loaded` and races into
        # `load_model`). On a 32-RPS flooder hitting a cold engine, this
        # is a 5-10x fork explosion that OOMs the GPU before any request
        # can complete.
        self._load_locks: dict[str, asyncio.Lock] = {}
        self._load_locks_guard = asyncio.Lock()

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

        # Pre-load configured models. Sequentially — concurrent
        # adapter.start() calls would compete for GPU memory.
        load_failures: list[tuple[str, str]] = []
        for model_cfg in self.config.models:
            try:
                await self.load_model(model_cfg)
            except Exception as exc:
                logger.error("Pre-load FAILED for %s: %s", model_cfg.model_id, exc)
                load_failures.append((model_cfg.model_id, str(exc)))

        if load_failures:
            # Surface pre-load failures loudly. /health will report the
            # missing models as 503 so a load-balancer / launch-day demo
            # never sends real traffic to a half-loaded server.
            logger.error(
                "WorkloadRouter started with %d/%d models loaded; failures: %s",
                len(self._models),
                len(self.config.models),
                load_failures,
            )
        else:
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

        # Measure cold-start latency for the engine histogram. Observe only
        # on success — aborted starts pollute the histogram with failure
        # cases that don't represent "how long warmup takes."
        cold_start_begin = time.monotonic()
        await adapter.start(timeout_s=self.config.engine_start_timeout_s)
        cold_start_s = time.monotonic() - cold_start_begin
        self.metrics.record_cold_start(
            model=config.model_id,
            engine=config.engine,
            duration_s=cold_start_s,
        )

        state = ModelState(config=config, adapter=adapter)
        self._models[config.model_id] = state
        self._model_configs[config.model_id] = config
        self.metrics.models_loaded.inc()
        # Engine just passed its first health check — set up gauge to 1.
        # Paired with the set-to-0 in unload_model.
        self.metrics.set_engine_up(model=config.model_id, engine=config.engine, up=True)

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
        # Engine subprocess has been torn down — flip gauge to 0.
        self.metrics.set_engine_up(model=model_id, engine=state.config.engine, up=False)

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

        async with self._load_locks_guard:
            lock = self._load_locks.setdefault(model_id, asyncio.Lock())

        async with lock:
            # Re-check under lock — another waiter may have completed.
            if model_id in self._models:
                return self._models[model_id]
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

        Enforces tenant budgets, applies admission control to prevent
        engine overload, tracks metrics, and uses length-bucketed
        scheduling for priority ordering.

        Args:
            model_id: Target model identifier.
            path: API path (e.g. "/v1/chat/completions").
            payload: JSON request body.
            tenant_id: Tenant making the request.
            stream: Whether to stream the response.

        Returns:
            The engine's JSON response, or an async stream.

        Raises:
            BudgetExceededError: If the tenant has exceeded its budget.
            AdmissionTimeoutError: If the request timed out waiting for
                admission to the engine.
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

        # Determine admission priority. Two disciplines:
        #   "fifo" (default) — length-bucketed priority only (short < long)
        #   "drr" — deficit round-robin: tenant with more in-flight requests
        #     waits behind quieter tenants. priority_score() already counts
        #     this request (try_acquire above incremented active_requests).
        max_tokens = payload.get("max_tokens", 256)
        bucket = classify_request_length(max_tokens)
        bucket_priority = BUCKET_PRIORITY.get(bucket, 1)
        if self.config.tenant_defaults.scheduling == "drr":
            tenant_record = await self.tenant_manager.get_tenant(tenant_id)
            tenant_score = tenant_record.priority_score() if tenant_record else 1
            # Tenant deficit dominates; bucket breaks ties within tenant tier.
            priority = tenant_score * 100 + bucket_priority
        else:
            priority = bucket_priority

        # Admission control -- wait for engine capacity
        admitted = await self.admission_controller.acquire(
            priority=priority, timeout=30.0
        )
        if not admitted:
            await self.tenant_manager.release_for_tenant(tenant_id)
            raise AdmissionTimeoutError(
                queue_depth=self.admission_controller.queue_depth,
                in_flight=self.admission_controller.in_flight,
            )

        # If we leave this scope without handing the slot to the streaming
        # wrapper below, release it ourselves. The wrapper sets `released=True`
        # so we only release once.
        released = False
        try:
            # Ensure model is loaded
            state = await self.ensure_model_loaded(model_id)

            # Forward the request
            result = await state.adapter.forward_request(path, payload, stream=stream)

            state.request_count += 1
            state.last_request_time = time.monotonic()

            # For streaming: result is an async generator that has NOT been
            # consumed yet. We must defer ALL accounting (latency, tokens,
            # tenant budget, metrics) into the wrapper's finally — recording
            # them here would log placeholder values (latency = handoff time,
            # tokens_out = 0) and never correct them post-stream. Both the
            # admission slot and the budget reflect the true stream lifetime.
            if stream and hasattr(result, "__aiter__"):
                tokens_in = _approx_tokens_in(payload)
                wrapped = self._stream_with_admission(
                    result,
                    model_id=model_id,
                    tenant_id=tenant_id,
                    state=state,
                    tokens_in=tokens_in,
                    start_time=start_time,
                )
                released = True  # ownership transferred to wrapper
                return wrapped

            # Non-streaming: usage is in the response dict, accounting is
            # accurate at this point.
            elapsed = time.monotonic() - start_time
            state.total_latency_s += elapsed
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
                engine=state.config.engine,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
            )
            await self.tenant_manager.record_completion(
                tenant_id,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                gpu_seconds=elapsed,
            )
            return result

        except BudgetExceededError:
            raise
        except Exception:
            elapsed = time.monotonic() - start_time
            # state may or may not be bound if ensure_model_loaded failed;
            # recover gracefully via the known model config map.
            engine_kind = "unknown"
            try:
                engine_kind = state.config.engine  # type: ignore[name-defined]
            except NameError:
                cfg = self._model_configs.get(model_id)
                if cfg is not None:
                    engine_kind = cfg.engine
            self.metrics.record_request(
                model=model_id,
                tenant=tenant_id,
                status="error",
                latency_s=elapsed,
                engine=engine_kind,
            )
            raise
        finally:
            if not released:
                self.admission_controller.release()
                await self.tenant_manager.release_for_tenant(tenant_id)

    async def _stream_with_admission(
        self,
        inner: Any,
        *,
        model_id: str,
        tenant_id: str,
        state: ModelState,
        tokens_in: int,
        start_time: float,
    ) -> Any:
        """Stream inner iterator while holding admission + accounting slots.

        Releases the admission slot, tenant slot, and records request +
        budget metrics in `finally` so cancellation, client disconnect
        (aiohttp closes the response), and normal completion all converge
        on a single accounting path. Also `aclose()`s the inner engine
        generator so its aiohttp `session.post()` unwinds eagerly — without
        this, an aborted client leaves the engine connection open and the
        engine keeps generating tokens until the inner generator is GC'd.

        Token accounting: the inner iterator yields raw socket reads from
        `aiohttp.resp.content.iter_any()`, NOT SSE frames. One read can
        contain many frames (localhost) or one frame can span many reads
        (slow network). Counting raw chunks therefore varies with TCP
        fragmentation by 5-50× — that breaks tenant budget. We buffer and
        split on the SSE record terminator (`\\n\\n`), then count `data:`
        frames that aren't `[DONE]`. Off by ~2-3 frames per stream
        (role/finish_reason envelopes), but stable across network
        conditions. The bench harness still reports ground-truth tokens
        with full JSON parse; the router uses sse_frames for tenant budget
        + Prometheus.
        """
        sse_frames = 0
        chunk_count = 0  # raw socket reads, observability only
        buf = b""
        status = "error"  # flipped to "ok" only on clean exhaustion
        try:
            # Hard fence: even with admission honest and engine timeouts in
            # place, a slow client could hold a slot for the entire stream.
            # Bound it. asyncio.timeout() raises asyncio.TimeoutError which
            # the wrapper's finally still cleans up.
            async with asyncio.timeout(_STREAM_MAX_DURATION_S):
                async for chunk in inner:
                    chunk_count += 1
                    buf += chunk
                    while b"\n\n" in buf:
                        frame, buf = buf.split(b"\n\n", 1)
                        # Tolerate \r\n line endings; strip is safe here.
                        line = frame.strip()
                        if not line.startswith(b"data: "):
                            continue
                        body = line[6:]
                        if body == b"[DONE]":
                            continue
                        sse_frames += 1
                        if sse_frames == 1:
                            self.metrics.record_ttft(
                                model=model_id,
                                tenant=tenant_id,
                                ttft_s=time.monotonic() - start_time,
                            )
                    yield chunk
            status = "ok"
        except asyncio.TimeoutError:
            logger.warning(
                "stream max-duration exceeded model=%s tenant=%s after %.1fs (chunks=%d frames=%d)",
                model_id,
                tenant_id,
                _STREAM_MAX_DURATION_S,
                chunk_count,
                sse_frames,
            )
            status = "timeout"
            # Fire the SSE disconnect counter — router-internal max-duration
            # fence counts as a `timeout` reason in dashboards.
            self.metrics.record_sse_disconnect(reason="timeout")
            # Re-raise so the outer handle_request can return 504 to client.
            raise
        finally:
            if hasattr(inner, "aclose"):
                try:
                    await inner.aclose()
                except Exception:
                    pass
            elapsed_real = time.monotonic() - start_time
            state.total_latency_s += elapsed_real
            try:
                self.metrics.record_request(
                    model=model_id,
                    tenant=tenant_id,
                    status=status,
                    latency_s=elapsed_real,
                    engine=state.config.engine,
                    tokens_in=tokens_in,
                    tokens_out=sse_frames,
                )
                await self.tenant_manager.record_completion(
                    tenant_id,
                    tokens_in=tokens_in,
                    tokens_out=sse_frames,
                    gpu_seconds=elapsed_real,
                )
            except Exception as exc:
                logger.warning(
                    "stream metrics record failed model=%s tenant=%s: %s",
                    model_id,
                    tenant_id,
                    exc,
                )
            self.admission_controller.release()
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
            return web.json_response({"error": "invalid JSON body"}, status=400)

        model_id = payload.get("model", "")
        if not model_id:
            return web.json_response(
                {"error": "missing 'model' field in request body"}, status=400
            )

        tenant_id = request.headers.get("X-Tenant-ID", "default")
        path = request.path
        stream = payload.get("stream", False)

        # D3: per-request trace ID. Stable across the lifetime of one request,
        # printed at entry, exit, and all error branches so a stuck request can
        # be tracked end-to-end in the log without grepping by timestamp. The
        # client can also pass X-Request-ID to correlate with their own log.
        req_id = request.headers.get("X-Request-ID") or (
            f"r{int(time.time() * 1000) % 1_000_000_000:09d}"
        )
        logger.info(
            "req_id=%s ENTER model=%s tenant=%s stream=%s path=%s",
            req_id,
            model_id,
            tenant_id,
            stream,
            path,
        )

        try:
            result = await self.route_request(
                model_id=model_id,
                path=path,
                payload=payload,
                tenant_id=tenant_id,
                stream=stream,
            )

            if stream and hasattr(result, "__aiter__"):
                # Delay response.prepare() until the FIRST chunk arrives so an
                # engine failure before any byte is sent can still return a
                # clean 500 (instead of an uncommittable mid-stream error).
                # Gate 0.5 repro showed the SSE client hangs for its full
                # client-side timeout when the server commits 200 then closes
                # with no body.
                response: web.StreamResponse | None = None
                try:
                    async for chunk in result:
                        if response is None:
                            response = web.StreamResponse(
                                status=200,
                                headers={"Content-Type": "text/event-stream"},
                            )
                            await response.prepare(request)
                        await response.write(chunk)
                except (asyncio.TimeoutError, aiohttp.ServerTimeoutError) as exc:
                    # Engine-side timeout: counts as `upstream_error` for
                    # the SSE disconnect counter. The router-internal
                    # _STREAM_MAX_DURATION_S fence also raises
                    # asyncio.TimeoutError but increments with reason=timeout
                    # from _stream_with_admission — here we're catching
                    # connection-level timeouts from the engine read.
                    self.metrics.record_sse_disconnect(reason="upstream_error")
                    if response is None:
                        # Engine failed before first byte — clean 504.
                        logger.warning(
                            "req_id=%s EXIT 504 engine_timeout_pre_byte detail=%s",
                            req_id,
                            exc,
                        )
                        return web.json_response(
                            {"error": "engine timeout", "detail": str(exc)},
                            status=504,
                        )
                    # Already committed 200 headers; close the stream.
                    logger.warning(
                        "req_id=%s EXIT 200_partial engine_timeout_mid_stream detail=%s",
                        req_id,
                        exc,
                    )
                    await response.write_eof()
                    return response
                except (
                    ConnectionResetError,
                    aiohttp.ClientConnectionError,
                ) as exc:
                    # Client went away mid-stream. aiohttp's
                    # StreamResponse.write() raises one of these when the
                    # TCP peer has closed. Record the disconnect and bail
                    # — the response is already un-recoverable.
                    self.metrics.record_sse_disconnect(reason="client_disconnect")
                    logger.info(
                        "req_id=%s EXIT client_disconnect detail=%s",
                        req_id,
                        exc,
                    )
                    # Best-effort EOF; ignore if the socket is already gone.
                    if response is not None:
                        try:
                            await response.write_eof()
                        except Exception:
                            pass
                        return response
                    return web.json_response(
                        {"error": "client disconnected"}, status=499
                    )

                if response is None:
                    # Iterator yielded nothing — treat as empty successful response.
                    response = web.StreamResponse(
                        status=200,
                        headers={"Content-Type": "text/event-stream"},
                    )
                    await response.prepare(request)
                await response.write_eof()
                logger.info("req_id=%s EXIT 200 stream_complete", req_id)
                return response

            logger.info("req_id=%s EXIT 200 json_complete", req_id)
            return web.json_response(result)

        except AdmissionTimeoutError as exc:
            logger.warning(
                "req_id=%s EXIT 503 admission_timeout queue=%d in_flight=%d",
                req_id,
                exc.queue_depth,
                exc.in_flight,
            )
            return web.json_response(
                {
                    "error": "server overloaded",
                    "detail": str(exc),
                    "queue_depth": exc.queue_depth,
                    "in_flight": exc.in_flight,
                },
                status=503,
            )
        except EngineCircuitOpenError as exc:
            # Engine has hit the consecutive-timeout threshold; shed fast.
            logger.warning("req_id=%s EXIT 503 circuit_open detail=%s", req_id, exc)
            return web.json_response(
                {"error": "engine unavailable", "detail": str(exc)},
                status=503,
            )
        except BudgetExceededError as exc:
            logger.warning("req_id=%s EXIT 429 budget_exceeded detail=%s", req_id, exc)
            return web.json_response({"error": str(exc)}, status=429)
        except ValueError as exc:
            logger.warning("req_id=%s EXIT 404 value_error detail=%s", req_id, exc)
            return web.json_response({"error": str(exc)}, status=404)
        except Exception as exc:
            logger.exception("req_id=%s EXIT 500 unhandled: %s", req_id, exc)
            return web.json_response({"error": f"internal error: {exc}"}, status=500)

    async def handle_models(self, request: web.Request) -> web.Response:
        """Handle GET /v1/models -- list loaded models."""
        models_data = []
        for mid, state in self._models.items():
            models_data.append(
                {
                    "id": mid,
                    "object": "model",
                    "owned_by": "kvwarden",
                    "engine": state.config.engine,
                    "healthy": state.adapter.is_healthy,
                }
            )
        return web.json_response(
            {
                "object": "list",
                "data": models_data,
            }
        )

    async def handle_health(self, request: web.Request) -> web.Response:
        """Handle GET /health.

        Returns 200 only when every configured model has a live engine
        adapter loaded. Returns 503 + a JSON breakdown otherwise. The
        503 path matters for launch-day flow: if /health returned 200
        while engines were still cold, the first benchmark request
        triggered a fork-bomb before the per-model lock fix landed.
        Healthy pre-warmup now also gates the first incoming request.
        """
        configured = {m.model_id for m in self.config.models}
        loaded = set(self._models.keys())
        missing = sorted(configured - loaded)
        if missing:
            return web.json_response(
                {
                    "status": "loading",
                    "missing_models": missing,
                    "loaded_models": sorted(loaded),
                },
                status=503,
            )
        return web.json_response({"status": "ok", "loaded_models": sorted(loaded)})

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
            "admission": self.admission_controller.stats,
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
