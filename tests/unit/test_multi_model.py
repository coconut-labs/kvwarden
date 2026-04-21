"""Unit tests for multi-model routing logic.

Tests the WorkloadRouter contract for multi-model scenarios:
- Request routing to correct adapter
- Dynamic model loading on unknown model requests
- GPU budget-based eviction
- In-flight request safety during model switches

The router and adapter are tested via lightweight mocks that define the
expected interface, since these are the contract specifications for the
KVWarden orchestration layer.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# Stub interfaces matching the expected KVWarden router contract
# ---------------------------------------------------------------------------


@dataclass
class AdapterStub:
    """Stub for a model adapter (e.g., VLLMAdapter).

    Tracks lifecycle state and simulates inference latency.
    """

    model_id: str
    gpu_memory_fraction: float = 0.4
    is_loaded: bool = False
    in_flight_count: int = 0
    load_time_s: float = 0.1
    inference_time_s: float = 0.05

    async def load(self) -> None:
        """Simulate model loading."""
        await asyncio.sleep(self.load_time_s)
        self.is_loaded = True

    async def unload(self) -> None:
        """Simulate model unloading (eviction)."""
        if self.in_flight_count > 0:
            raise RuntimeError(
                f"Cannot unload {self.model_id}: {self.in_flight_count} in-flight requests"
            )
        self.is_loaded = False

    async def infer(self, prompt: str, max_tokens: int = 256) -> dict[str, Any]:
        """Simulate inference."""
        if not self.is_loaded:
            raise RuntimeError(f"Model {self.model_id} is not loaded")
        self.in_flight_count += 1
        try:
            await asyncio.sleep(self.inference_time_s)
            return {"text": f"response from {self.model_id}", "tokens": max_tokens}
        finally:
            self.in_flight_count -= 1


@dataclass
class WorkloadRouterStub:
    """Stub for the KVWarden WorkloadRouter.

    Implements the routing, loading, and eviction logic that the real
    router is expected to provide. This defines the contract.
    """

    gpu_budget: float = 0.85
    adapters: dict[str, AdapterStub] = field(default_factory=dict)
    known_models: dict[str, float] = field(default_factory=dict)
    _access_history: list[tuple[str, float]] = field(default_factory=list)

    def register_model(self, model_id: str, gpu_memory_fraction: float) -> None:
        """Register a model that can be served.

        Args:
            model_id: Model identifier.
            gpu_memory_fraction: GPU memory fraction required.
        """
        self.known_models[model_id] = gpu_memory_fraction

    def _gpu_memory_used(self) -> float:
        """Calculate total GPU memory fraction currently in use."""
        return sum(a.gpu_memory_fraction for a in self.adapters.values() if a.is_loaded)

    def _pick_eviction_target(self) -> str | None:
        """Pick a model to evict based on frequency+recency scoring.

        Uses a combined score: models accessed less recently and less
        frequently are evicted first. Models with in-flight requests
        are never evicted.

        Returns:
            Model ID to evict, or None if nothing can be evicted.
        """
        loaded = [
            (mid, adapter)
            for mid, adapter in self.adapters.items()
            if adapter.is_loaded and adapter.in_flight_count == 0
        ]
        if not loaded:
            return None

        now = time.time()
        scores: dict[str, float] = {}
        for mid, _ in loaded:
            accesses = [(ts, m) for ts, m in self._access_history if m == mid]
            frequency = len(accesses)
            recency = (now - accesses[-1][0]) if accesses else float("inf")
            # Lower score = more likely to evict
            # Penalize low frequency, reward recency
            scores[mid] = frequency / (1.0 + recency)

        # Evict the model with the lowest score
        return min(scores, key=scores.get)  # type: ignore[arg-type]

    async def route(
        self, model_id: str, prompt: str, max_tokens: int = 256
    ) -> dict[str, Any]:
        """Route a request to the appropriate model adapter.

        If the model is not loaded, triggers loading (and possibly eviction
        of another model to stay within GPU budget).

        Args:
            model_id: Target model identifier.
            prompt: Request prompt.
            max_tokens: Maximum output tokens.

        Returns:
            Inference result from the adapter.

        Raises:
            ValueError: If model_id is not registered.
        """
        if model_id not in self.known_models:
            raise ValueError(f"Unknown model: {model_id}")

        self._access_history.append((time.time(), model_id))

        # Create adapter if needed
        if model_id not in self.adapters:
            self.adapters[model_id] = AdapterStub(
                model_id=model_id,
                gpu_memory_fraction=self.known_models[model_id],
            )

        adapter = self.adapters[model_id]

        # Load model if not loaded
        if not adapter.is_loaded:
            needed = adapter.gpu_memory_fraction
            # Evict models until we have enough budget
            while self._gpu_memory_used() + needed > self.gpu_budget:
                target = self._pick_eviction_target()
                if target is None:
                    raise RuntimeError(
                        f"Cannot load {model_id}: GPU budget exhausted and "
                        "no models can be evicted (in-flight requests)"
                    )
                await self.adapters[target].unload()

            await adapter.load()

        return await adapter.infer(prompt, max_tokens)

    def get_loaded_models(self) -> list[str]:
        """Return list of currently loaded model IDs."""
        return [mid for mid, a in self.adapters.items() if a.is_loaded]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRouteToCorrectAdapter:
    """Request for model A routes to correct adapter."""

    @pytest.fixture
    def router(self) -> WorkloadRouterStub:
        r = WorkloadRouterStub(gpu_budget=0.85)
        r.register_model("model-a", gpu_memory_fraction=0.4)
        r.register_model("model-b", gpu_memory_fraction=0.4)
        return r

    @pytest.mark.asyncio
    async def test_routes_to_model_a(self, router: WorkloadRouterStub) -> None:
        result = await router.route("model-a", "hello")
        assert result["text"] == "response from model-a"

    @pytest.mark.asyncio
    async def test_routes_to_model_b(self, router: WorkloadRouterStub) -> None:
        result = await router.route("model-b", "hello")
        assert result["text"] == "response from model-b"

    @pytest.mark.asyncio
    async def test_correct_adapter_loaded(self, router: WorkloadRouterStub) -> None:
        await router.route("model-a", "hello")
        assert router.adapters["model-a"].is_loaded
        assert (
            "model-b" not in router.adapters or not router.adapters["model-b"].is_loaded
        )

    @pytest.mark.asyncio
    async def test_both_models_loaded_simultaneously(
        self, router: WorkloadRouterStub
    ) -> None:
        await router.route("model-a", "hello")
        await router.route("model-b", "hello")
        # Both fit within budget (0.4 + 0.4 = 0.8 <= 0.85)
        assert router.adapters["model-a"].is_loaded
        assert router.adapters["model-b"].is_loaded

    @pytest.mark.asyncio
    async def test_unknown_model_raises(self, router: WorkloadRouterStub) -> None:
        with pytest.raises(ValueError, match="Unknown model"):
            await router.route("model-x", "hello")


class TestDynamicModelLoading:
    """Request for unknown model triggers load."""

    @pytest.fixture
    def router(self) -> WorkloadRouterStub:
        r = WorkloadRouterStub(gpu_budget=0.85)
        r.register_model("model-a", gpu_memory_fraction=0.4)
        r.register_model("model-b", gpu_memory_fraction=0.4)
        return r

    @pytest.mark.asyncio
    async def test_first_request_triggers_load(
        self, router: WorkloadRouterStub
    ) -> None:
        assert router.get_loaded_models() == []
        await router.route("model-a", "hello")
        assert "model-a" in router.get_loaded_models()

    @pytest.mark.asyncio
    async def test_second_model_loads_on_demand(
        self, router: WorkloadRouterStub
    ) -> None:
        await router.route("model-a", "hello")
        assert router.get_loaded_models() == ["model-a"]
        await router.route("model-b", "hello")
        assert sorted(router.get_loaded_models()) == ["model-a", "model-b"]

    @pytest.mark.asyncio
    async def test_loaded_model_not_reloaded(self, router: WorkloadRouterStub) -> None:
        await router.route("model-a", "hello")
        adapter_a = router.adapters["model-a"]
        # Route again -- should use same adapter without reloading
        await router.route("model-a", "world")
        assert router.adapters["model-a"] is adapter_a
        assert adapter_a.is_loaded


class TestEvictionOnBudgetExceeded:
    """Eviction happens when GPU budget exceeded."""

    @pytest.fixture
    def tight_router(self) -> WorkloadRouterStub:
        """Router with tight budget: can only hold one model at a time."""
        r = WorkloadRouterStub(gpu_budget=0.45)
        r.register_model("model-a", gpu_memory_fraction=0.4)
        r.register_model("model-b", gpu_memory_fraction=0.4)
        r.register_model("model-c", gpu_memory_fraction=0.4)
        return r

    @pytest.mark.asyncio
    async def test_evicts_when_budget_exceeded(
        self, tight_router: WorkloadRouterStub
    ) -> None:
        await tight_router.route("model-a", "hello")
        assert tight_router.adapters["model-a"].is_loaded

        # Loading model-b should evict model-a (0.4 + 0.4 > 0.45)
        await tight_router.route("model-b", "hello")
        assert tight_router.adapters["model-b"].is_loaded
        assert not tight_router.adapters["model-a"].is_loaded

    @pytest.mark.asyncio
    async def test_evicts_least_scored_model(
        self, tight_router: WorkloadRouterStub
    ) -> None:
        # Access model-a many times to give it a high frequency score
        for _ in range(10):
            await tight_router.route("model-a", "hello")

        # Now load model-b (evicts model-a since budget is tight)
        await tight_router.route("model-b", "hello")
        assert tight_router.adapters["model-b"].is_loaded

        # Access model-b just once, then request model-c
        # model-b should be evicted over model-a (if model-a were loaded)
        # Since model-a is already evicted and model-b is loaded,
        # requesting model-c evicts model-b
        await tight_router.route("model-c", "hello")
        assert tight_router.adapters["model-c"].is_loaded
        assert not tight_router.adapters["model-b"].is_loaded

    @pytest.mark.asyncio
    async def test_no_eviction_when_within_budget(self) -> None:
        router = WorkloadRouterStub(gpu_budget=0.85)
        router.register_model("model-a", gpu_memory_fraction=0.4)
        router.register_model("model-b", gpu_memory_fraction=0.4)

        await router.route("model-a", "hello")
        await router.route("model-b", "hello")
        # 0.4 + 0.4 = 0.8 <= 0.85, so both stay loaded
        assert router.adapters["model-a"].is_loaded
        assert router.adapters["model-b"].is_loaded


class TestInFlightRequestSafety:
    """Model switch doesn't lose in-flight requests."""

    @pytest.fixture
    def tight_router(self) -> WorkloadRouterStub:
        r = WorkloadRouterStub(gpu_budget=0.45)
        r.register_model("model-a", gpu_memory_fraction=0.4)
        r.register_model("model-b", gpu_memory_fraction=0.4)
        return r

    @pytest.mark.asyncio
    async def test_in_flight_prevents_eviction(
        self, tight_router: WorkloadRouterStub
    ) -> None:
        # Pre-load model-a
        await tight_router.route("model-a", "hello")

        # Simulate an in-flight request on model-a
        adapter_a = tight_router.adapters["model-a"]
        adapter_a.in_flight_count = 1

        # Trying to load model-b should fail since model-a cannot be evicted
        with pytest.raises(RuntimeError, match="GPU budget exhausted"):
            await tight_router.route("model-b", "hello")

        # model-a should still be loaded
        assert adapter_a.is_loaded

    @pytest.mark.asyncio
    async def test_in_flight_completes_before_eviction(
        self, tight_router: WorkloadRouterStub
    ) -> None:
        # Pre-load model-a
        await tight_router.route("model-a", "hello")
        adapter_a = tight_router.adapters["model-a"]

        # Start a "long" inference on model-a
        async def long_inference() -> dict[str, Any]:
            adapter_a.in_flight_count += 1
            await asyncio.sleep(0.2)
            result = {"text": "done"}
            adapter_a.in_flight_count -= 1
            return result

        # Start inference
        inference_task = asyncio.create_task(long_inference())

        # Give it a moment to start
        await asyncio.sleep(0.05)

        # model-a has in-flight, cannot be evicted yet
        assert adapter_a.in_flight_count > 0

        # Wait for inference to complete
        await inference_task

        # Now model-a can be evicted
        assert adapter_a.in_flight_count == 0
        await tight_router.route("model-b", "hello")
        assert not adapter_a.is_loaded
        assert tight_router.adapters["model-b"].is_loaded

    @pytest.mark.asyncio
    async def test_concurrent_requests_to_same_model(self) -> None:
        router = WorkloadRouterStub(gpu_budget=0.85)
        router.register_model("model-a", gpu_memory_fraction=0.4)

        # Send multiple concurrent requests to the same model
        tasks = [router.route("model-a", f"prompt {i}") for i in range(10)]
        results = await asyncio.gather(*tasks)

        assert len(results) == 10
        assert all(r["text"] == "response from model-a" for r in results)
        # All requests completed, no in-flight leftover
        assert router.adapters["model-a"].in_flight_count == 0

    @pytest.mark.asyncio
    async def test_concurrent_requests_different_models_within_budget(self) -> None:
        router = WorkloadRouterStub(gpu_budget=0.85)
        router.register_model("model-a", gpu_memory_fraction=0.4)
        router.register_model("model-b", gpu_memory_fraction=0.4)

        # Send concurrent requests to both models
        tasks = []
        for i in range(10):
            model = "model-a" if i % 2 == 0 else "model-b"
            tasks.append(router.route(model, f"prompt {i}"))

        results = await asyncio.gather(*tasks)
        assert len(results) == 10


class TestScheduleBuilders:
    """Test the workload schedule generation functions."""

    def test_alternating_schedule(self) -> None:
        import sys
        from pathlib import Path

        # Import from benchmark script
        script_dir = Path(__file__).resolve().parent.parent.parent
        sys.path.insert(0, str(script_dir / "benchmarks" / "scripts"))
        from benchmark_multi_model import build_alternating_schedule

        schedule = build_alternating_schedule(["A", "B"], 6)
        assert schedule == ["A", "B", "A", "B", "A", "B"]

    def test_alternating_schedule_three_models(self) -> None:
        import sys
        from pathlib import Path

        script_dir = Path(__file__).resolve().parent.parent.parent
        sys.path.insert(0, str(script_dir / "benchmarks" / "scripts"))
        from benchmark_multi_model import build_alternating_schedule

        schedule = build_alternating_schedule(["A", "B", "C"], 6)
        assert schedule == ["A", "B", "C", "A", "B", "C"]

    def test_bursty_schedule(self) -> None:
        import sys
        from pathlib import Path

        script_dir = Path(__file__).resolve().parent.parent.parent
        sys.path.insert(0, str(script_dir / "benchmarks" / "scripts"))
        from benchmark_multi_model import build_bursty_schedule

        schedule = build_bursty_schedule(["A", "B"], [3, 2, 1])
        assert schedule == ["A", "A", "A", "B", "B", "A"]

    def test_concurrent_schedule_distribution(self) -> None:
        import sys
        from pathlib import Path

        script_dir = Path(__file__).resolve().parent.parent.parent
        sys.path.insert(0, str(script_dir / "benchmarks" / "scripts"))
        from benchmark_multi_model import build_concurrent_schedule

        schedule = build_concurrent_schedule(["A", "B"], 1000, seed=42)
        count_a = schedule.count("A")
        count_b = schedule.count("B")
        # With 1000 samples, expect roughly 50/50 (within 10%)
        assert 400 < count_a < 600, f"Expected ~500 A, got {count_a}"
        assert 400 < count_b < 600, f"Expected ~500 B, got {count_b}"

    def test_concurrent_schedule_reproducible(self) -> None:
        import sys
        from pathlib import Path

        script_dir = Path(__file__).resolve().parent.parent.parent
        sys.path.insert(0, str(script_dir / "benchmarks" / "scripts"))
        from benchmark_multi_model import build_concurrent_schedule

        s1 = build_concurrent_schedule(["A", "B"], 100, seed=42)
        s2 = build_concurrent_schedule(["A", "B"], 100, seed=42)
        assert s1 == s2
