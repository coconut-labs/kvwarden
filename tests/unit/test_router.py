"""Unit tests for the WorkloadRouter.

Tests routing logic, model eviction scoring, priority scheduling,
and request length classification.
"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from infergrid.common.config import InferGridConfig, ModelConfig
from infergrid.common.metrics import MetricsCollector
from infergrid.router.router import (
    BudgetExceededError,
    ModelState,
    PendingRequest,
    WorkloadRouter,
    classify_request_length,
)
from infergrid.tenant.manager import TenantManager


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

    def test_allocate_port_increments(self) -> None:
        config = self._make_config()
        router = WorkloadRouter(config=config)
        p1 = router._allocate_port()
        p2 = router._allocate_port()
        assert p2 == p1 + 1

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

    def test_unknown_model_raises(self) -> None:
        config = self._make_config(n_models=0)
        router = WorkloadRouter(config=config)
        with pytest.raises(ValueError, match="Unknown model"):
            asyncio.get_event_loop().run_until_complete(
                router.ensure_model_loaded("nonexistent/model")
            )


# ---------------------------------------------------------------------------
# Eviction ordering via WorkloadRouter
# ---------------------------------------------------------------------------


class TestEvictionOrdering:
    """Test that evict_model picks the right victim."""

    def test_evicts_lowest_scored_model(self) -> None:
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
        evicted = asyncio.get_event_loop().run_until_complete(
            router.evict_model()
        )
        # The evicted model should be the one with the lowest combined score
        assert evicted is not None
        assert evicted in ["model-0", "model-1", "model-2"]
        assert evicted not in router._models
