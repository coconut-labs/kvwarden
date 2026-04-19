"""Unit tests for the WorkloadRouter.

Tests routing logic, model eviction scoring, priority scheduling,
and request length classification.
"""

from __future__ import annotations

import asyncio
import gc
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import infergrid.router.router as router_module
from infergrid.common.config import InferGridConfig, ModelConfig
from infergrid.router.router import (
    BudgetExceededError,
    ModelState,
    WorkloadRouter,
    classify_request_length,
)


# ---------------------------------------------------------------------------
# classify_request_length
# ---------------------------------------------------------------------------


class TestClassifyRequestLength:
    """Tests for the request length classifier."""

    def test_short(self) -> None:
        assert classify_request_length(max_tokens=64) == "short"
        assert classify_request_length(max_tokens=256) == "short"

    def test_medium(self) -> None:
        assert classify_request_length(max_tokens=512) == "medium"
        assert classify_request_length(max_tokens=1024) == "medium"

    def test_long(self) -> None:
        assert classify_request_length(max_tokens=2048) == "long"
        assert classify_request_length(max_tokens=4096) == "long"

    def test_xlarge(self) -> None:
        assert classify_request_length(max_tokens=8192) == "xlarge"

    def test_uses_max_of_inputs(self) -> None:
        # input_tokens dominates
        assert classify_request_length(max_tokens=64, input_tokens=2048) == "long"
        # max_tokens dominates
        assert classify_request_length(max_tokens=2048, input_tokens=64) == "long"


# ---------------------------------------------------------------------------
# ModelState eviction scoring
# ---------------------------------------------------------------------------


class TestModelStateEviction:
    """Tests for the frequency+recency eviction scoring."""

    def _make_state(
        self,
        request_count: int = 0,
        seconds_since_last: float = 0.0,
    ) -> ModelState:
        config = ModelConfig(model_id="test-model")
        adapter = MagicMock()
        adapter.is_healthy = True
        now = time.monotonic()
        state = ModelState(
            config=config,
            adapter=adapter,
            request_count=request_count,
            last_request_time=now - seconds_since_last,
            loaded_time=now - 3600,
        )
        return state

    def test_unused_model_has_lowest_score(self) -> None:
        """A model with zero requests should have the lowest eviction score."""
        unused = self._make_state(request_count=0, seconds_since_last=100)
        used = self._make_state(request_count=50, seconds_since_last=10)
        now = time.monotonic()
        assert unused.eviction_score(now) < used.eviction_score(now)

    def test_stale_model_scores_lower(self) -> None:
        """A model not accessed recently should score lower (evict first)."""
        stale = self._make_state(request_count=100, seconds_since_last=3600)
        fresh = self._make_state(request_count=100, seconds_since_last=1)
        now = time.monotonic()
        assert stale.eviction_score(now) < fresh.eviction_score(now)

    def test_high_frequency_resists_eviction(self) -> None:
        """A heavily used model should resist eviction even when somewhat stale."""
        heavy = self._make_state(request_count=1000, seconds_since_last=300)
        light = self._make_state(request_count=5, seconds_since_last=60)
        now = time.monotonic()
        assert heavy.eviction_score(now) > light.eviction_score(now)

    def test_score_is_not_naive_lru(self) -> None:
        """Verify that recency alone doesn't determine eviction order.

        A model accessed 1 second ago with 1 request should be evictable
        before a model accessed 60 seconds ago with 500 requests.
        """
        recent_but_rare = self._make_state(request_count=1, seconds_since_last=1)
        old_but_popular = self._make_state(request_count=500, seconds_since_last=60)
        now = time.monotonic()
        assert recent_but_rare.eviction_score(now) < old_but_popular.eviction_score(now)

    def test_avg_latency(self) -> None:
        config = ModelConfig(model_id="test")
        adapter = MagicMock()
        state = ModelState(
            config=config,
            adapter=adapter,
            request_count=10,
            total_latency_s=5.0,
        )
        assert abs(state.avg_latency_s - 0.5) < 0.001

    def test_avg_latency_zero_requests(self) -> None:
        config = ModelConfig(model_id="test")
        adapter = MagicMock()
        state = ModelState(config=config, adapter=adapter)
        assert state.avg_latency_s == 0.0


# ---------------------------------------------------------------------------
# WorkloadRouter unit tests
# ---------------------------------------------------------------------------


class TestWorkloadRouter:
    """Tests for WorkloadRouter initialization and model management."""

    def _make_config(self, n_models: int = 2) -> InferGridConfig:
        models = [
            ModelConfig(model_id=f"test-org/model-{i}", engine="vllm")
            for i in range(n_models)
        ]
        return InferGridConfig(port=9999, models=models)

    def test_init(self) -> None:
        config = self._make_config()
        router = WorkloadRouter(config=config)
        assert len(router._model_configs) == 2
        assert router.loaded_models() == []

    def test_queue_depths_start_at_zero(self) -> None:
        config = self._make_config()
        router = WorkloadRouter(config=config)
        depths = router.queue_depths()
        assert all(d == 0 for d in depths.values())
        assert "short" in depths
        assert "xlarge" in depths

    def test_snapshot_structure(self) -> None:
        config = self._make_config()
        router = WorkloadRouter(config=config)
        snap = router.snapshot()
        assert "loaded_models" in snap
        assert "queue_depths" in snap
        assert "cache" in snap
        assert "tenants" in snap
        assert "metrics" in snap

    def test_allocate_port_returns_available(self) -> None:
        config = self._make_config()
        router = WorkloadRouter(config=config)
        p1 = router._allocate_port()
        p2 = router._allocate_port()
        assert p2 > p1
        # Both ports should have been verified available
        assert p1 >= 8001

    def test_create_adapter_vllm(self) -> None:
        config = self._make_config()
        router = WorkloadRouter(config=config)
        model_config = ModelConfig(model_id="test-org/model-0", engine="vllm")
        adapter = router._create_adapter(model_config, 8001)
        from infergrid.engines.vllm_adapter.adapter import VLLMAdapter
        assert isinstance(adapter, VLLMAdapter)

    def test_create_adapter_sglang(self) -> None:
        config = self._make_config()
        router = WorkloadRouter(config=config)
        model_config = ModelConfig(model_id="test-org/model-0", engine="sglang")
        adapter = router._create_adapter(model_config, 8002)
        from infergrid.engines.sglang_adapter.adapter import SGLangAdapter
        assert isinstance(adapter, SGLangAdapter)

    async def test_unknown_model_raises(self) -> None:
        config = self._make_config(n_models=0)
        router = WorkloadRouter(config=config)
        with pytest.raises(ValueError, match="Unknown model"):
            await router.ensure_model_loaded("nonexistent/model")


# ---------------------------------------------------------------------------
# Eviction ordering via WorkloadRouter
# ---------------------------------------------------------------------------


class TestEvictionOrdering:
    """Test that evict_model picks the right victim."""

    async def test_evicts_lowest_scored_model(self) -> None:
        config = InferGridConfig(port=9999, models=[])
        router = WorkloadRouter(config=config)

        # Manually inject model states
        now = time.monotonic()
        for i in range(3):
            cfg = ModelConfig(model_id=f"model-{i}")
            adapter = MagicMock()
            adapter.stop = AsyncMock()
            state = ModelState(
                config=cfg,
                adapter=adapter,
                request_count=(i + 1) * 100,  # model-0: 100, model-1: 200, model-2: 300
                last_request_time=now - i * 600,  # model-0: recent, model-2: stale
            )
            router._models[f"model-{i}"] = state
            router._model_configs[f"model-{i}"] = cfg

        # model-2 has the most requests but is the stalest.
        # model-0 has fewest requests but is most recent.
        # The eviction policy should pick based on frequency*decay.
        evicted = await router.evict_model()
        # The evicted model should be the one with the lowest combined score
        assert evicted is not None
        assert evicted in ["model-0", "model-1", "model-2"]
        assert evicted not in router._models


# ---------------------------------------------------------------------------
# Streaming admission control
# ---------------------------------------------------------------------------


class TestStreamingAdmission:
    """Regression: admission slot must be held for the full streaming lifetime.

    Pre-fix bug: route_request awaits forward_request (which returns the
    async-generator object immediately for stream=True), then its outer finally
    fires admission_controller.release() — so the slot is freed in microseconds
    while the engine is still generating tokens. The smoke_bench poller showed
    peak in_flight = 0 with cap = 16 over 149 samples. That makes admission
    cap a no-op for streaming traffic.

    Fix: route_request hands the slot to a wrapper async generator whose own
    finally releases. These tests pin that contract.
    """

    def _make_router_with_slow_stream(
        self, *, max_concurrent: int, chunk_count: int = 5, chunk_delay_s: float = 0.05,
    ) -> WorkloadRouter:
        cfg = InferGridConfig(
            port=9999,
            max_concurrent=max_concurrent,
            models=[ModelConfig(model_id="m", engine="vllm")],
        )
        router = WorkloadRouter(config=cfg)

        async def fake_stream():
            for i in range(chunk_count):
                await asyncio.sleep(chunk_delay_s)
                yield f"data: chunk{i}\n\n".encode()

        adapter = MagicMock()
        adapter.is_healthy = True
        # forward_request is `await`ed; AsyncMock with side_effect returning the
        # async-gen object mirrors the real adapter contract.
        adapter.forward_request = AsyncMock(side_effect=lambda *a, **kw: fake_stream())
        state = ModelState(config=ModelConfig(model_id="m"), adapter=adapter)
        router._models["m"] = state
        return router

    async def test_in_flight_is_held_during_stream(self) -> None:
        router = self._make_router_with_slow_stream(max_concurrent=4)

        # Start one stream and begin iterating
        result = await router.route_request(
            model_id="m", path="/v1/completions",
            payload={"stream": True, "max_tokens": 32}, stream=True,
        )
        agen = result.__aiter__()
        first = await agen.__anext__()
        assert first.startswith(b"data: ")

        # Mid-stream: slot must still be held
        assert router.admission_controller.in_flight == 1, (
            "admission slot was released before stream finished — streaming "
            "bypass regression"
        )

        # Drain
        async for _ in agen:
            pass

        # Done: slot released
        assert router.admission_controller.in_flight == 0

    async def test_streaming_respects_concurrency_cap(self) -> None:
        # cap=2, 3 concurrent streams → one must queue
        router = self._make_router_with_slow_stream(max_concurrent=2, chunk_count=3, chunk_delay_s=0.04)

        async def consume():
            r = await router.route_request(
                model_id="m", path="/v1/completions",
                payload={"stream": True, "max_tokens": 32}, stream=True,
            )
            async for _ in r:
                pass

        tasks = [asyncio.create_task(consume()) for _ in range(3)]
        # Let the first wave grab slots
        await asyncio.sleep(0.02)

        adm = router.admission_controller
        assert adm.in_flight == 2, f"expected in_flight=2, got {adm.in_flight}"
        assert adm.queue_depth == 1, f"expected queue_depth=1, got {adm.queue_depth}"

        await asyncio.gather(*tasks)
        assert adm.in_flight == 0
        assert adm.queue_depth == 0

    async def test_slot_released_when_consumer_aborts_midstream(self) -> None:
        router = self._make_router_with_slow_stream(
            max_concurrent=2, chunk_count=20, chunk_delay_s=0.05,
        )

        result = await router.route_request(
            model_id="m", path="/v1/completions",
            payload={"stream": True, "max_tokens": 256}, stream=True,
        )
        agen = result.__aiter__()
        await agen.__anext__()
        assert router.admission_controller.in_flight == 1

        # Consumer abandons. aclose() runs the wrapper's finally, releasing
        # the slot. (A truly-leaked iterator would only release on GC; aclose
        # is the well-behaved path and is what aiohttp does on disconnect.)
        await agen.aclose()
        assert router.admission_controller.in_flight == 0

    async def test_streaming_budget_accounting_uses_real_values(self) -> None:
        """Regression: tenant.record_completion must reflect the actual stream,
        not a placeholder logged at handoff time.

        Pre-PR-#32 the route_request body called record_request /
        record_completion with `latency_s = time-to-generator-object`
        (microseconds) and `tokens_out = 0`, then released admission. Tenant
        budget never saw the real stream cost. PR #32 moves the recording
        into the wrapper's finally with chunk_count and real elapsed.
        """
        router = self._make_router_with_slow_stream(
            max_concurrent=4, chunk_count=5, chunk_delay_s=0.04,
        )

        tm_calls: list[dict] = []

        async def fake_record_completion(tenant_id, **kwargs):
            tm_calls.append({"tenant_id": tenant_id, **kwargs})

        router.tenant_manager.record_completion = fake_record_completion  # type: ignore[assignment]

        result = await router.route_request(
            model_id="m", path="/v1/completions",
            payload={"prompt": "two words", "stream": True, "max_tokens": 5},
            stream=True,
        )
        chunks: list = []
        async for c in result:
            chunks.append(c)
        # Wrapper iteration is done — finally has run.

        assert len(tm_calls) == 1, f"expected 1 record_completion call, got {len(tm_calls)}"
        call = tm_calls[0]
        # Real elapsed: 5 chunks × 40ms ≈ 200ms (≥ 150 ms allows for noise).
        assert call["gpu_seconds"] >= 0.15, (
            f"gpu_seconds={call['gpu_seconds']} — accounting recorded the "
            "handoff time, not the real stream lifetime"
        )
        # tokens_out is the chunk count (coarse but non-zero — was 0 pre-fix).
        assert call["tokens_out"] == 5, (
            f"tokens_out={call['tokens_out']} — chunk count not recorded"
        )
        # tokens_in came from prompt split.
        assert call["tokens_in"] == 2, (
            f"tokens_in={call['tokens_in']} — _approx_tokens_in regression"
        )

    async def test_max_stream_duration_releases_slot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Slow-client guard: a stream that exceeds the configured max
        duration must abort and release the admission slot (PR #33)."""
        monkeypatch.setattr(router_module, "_STREAM_MAX_DURATION_S", 0.15)

        # 50 chunks × 50 ms = 2.5s — well past the 0.15s fence.
        router = self._make_router_with_slow_stream(
            max_concurrent=2, chunk_count=50, chunk_delay_s=0.05,
        )

        result = await router.route_request(
            model_id="m", path="/v1/completions",
            payload={"prompt": "p", "stream": True, "max_tokens": 50},
            stream=True,
        )
        with pytest.raises(asyncio.TimeoutError):
            async for _ in result:
                pass

        assert router.admission_controller.in_flight == 0, (
            "admission slot leaked when max-stream-duration fence fired"
        )

    async def test_slot_released_after_gc_without_aclose(self) -> None:
        """When a consumer drops the wrapper without calling aclose() (and
        without aiohttp's disconnect path), the slot SHOULD eventually
        release once Python GCs the async generator. CPython runs the
        finalizer on the asyncio loop's idle pass, so we collect + yield.

        This documents the GC-path behavior (and is the reason PR #33 also
        adds a max-duration fence as a hard upper bound — GC timing is
        cooperative and not guaranteed).
        """
        router = self._make_router_with_slow_stream(
            max_concurrent=2, chunk_count=50, chunk_delay_s=0.05,
        )

        result = await router.route_request(
            model_id="m", path="/v1/completions",
            payload={"prompt": "p", "stream": True, "max_tokens": 50},
            stream=True,
        )
        agen = result.__aiter__()
        await agen.__anext__()
        assert router.admission_controller.in_flight == 1

        # Drop both refs WITHOUT aclose. The wrapper finalizer is queued
        # by CPython on collection; asyncio's _asyncgen_finalizer_hook
        # schedules its aclose on the loop, which runs on next idle.
        del result, agen
        for _ in range(3):
            gc.collect()
            await asyncio.sleep(0.05)

        assert router.admission_controller.in_flight == 0, (
            "GC-path did not release admission slot. The async-generator "
            "finalizer hook may not be wired (sys.set_asyncgen_hooks). "
            "Without it, abandoned streams leak slots until the loop dies."
        )
