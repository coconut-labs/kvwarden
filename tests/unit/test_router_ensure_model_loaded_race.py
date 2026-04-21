"""Regression test for the ensure_model_loaded TOCTOU race.

Before the fix: N concurrent requests for a cold model triggered N
parallel load_model() calls (each calling adapter.start()). With 32
in-flight on a cold engine that's a 32x subprocess explosion and a
guaranteed OOM before any request completes.

After the fix: concurrent ensure_model_loaded() calls for the same
model serialize through a per-model asyncio.Lock; exactly one
load_model() call fires and every waiter gets the same ModelState.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from kvwarden.common.config import KVWardenConfig, ModelConfig
from kvwarden.router.router import WorkloadRouter


@pytest.mark.asyncio
async def test_ensure_model_loaded_serializes_concurrent_calls(monkeypatch):
    """32 concurrent ensure_model_loaded() calls → exactly 1 load_model()."""
    cfg = KVWardenConfig(
        models=[
            ModelConfig(
                model_id="llama31-8b",
                short_name="llama31-8b",
                engine="vllm",
                dtype="bfloat16",
            )
        ],
    )
    router = WorkloadRouter(cfg)

    load_call_count = 0
    load_started = asyncio.Event()
    release_load = asyncio.Event()

    async def slow_load(config):
        nonlocal load_call_count
        load_call_count += 1
        load_started.set()
        # Block until the test releases us; this gives the other coros
        # a chance to race into load_model() if the lock is missing.
        await release_load.wait()
        # Fake a ModelState
        state = MagicMock()
        state.config = config
        router._models[config.model_id] = state
        return state

    monkeypatch.setattr(router, "load_model", slow_load)

    # Fan out 32 concurrent ensure_model_loaded() calls.
    tasks = [
        asyncio.create_task(router.ensure_model_loaded("llama31-8b")) for _ in range(32)
    ]

    # Wait for the first load to start.
    await asyncio.wait_for(load_started.wait(), timeout=1.0)
    # Give the other 31 coros a chance to race.
    await asyncio.sleep(0.05)
    # Release the load.
    release_load.set()

    results = await asyncio.gather(*tasks)

    assert load_call_count == 1, (
        f"expected exactly 1 load_model() call, got {load_call_count} — the race is NOT fixed"
    )
    assert all(r is results[0] for r in results), (
        "all waiters must receive the same ModelState"
    )


@pytest.mark.asyncio
async def test_ensure_model_loaded_lock_is_per_model(monkeypatch):
    """Two different model_ids load concurrently — not serialized by the lock."""
    cfg = KVWardenConfig(
        models=[
            ModelConfig(
                model_id="llama31-8b", short_name="l", engine="vllm", dtype="bfloat16"
            ),
            ModelConfig(
                model_id="qwen25-7b", short_name="q", engine="vllm", dtype="bfloat16"
            ),
        ],
    )
    router = WorkloadRouter(cfg)

    in_flight = 0
    max_in_flight = 0
    release = asyncio.Event()

    async def overlapping_load(config):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await release.wait()
        state = MagicMock()
        state.config = config
        router._models[config.model_id] = state
        in_flight -= 1
        return state

    monkeypatch.setattr(router, "load_model", overlapping_load)

    t1 = asyncio.create_task(router.ensure_model_loaded("llama31-8b"))
    t2 = asyncio.create_task(router.ensure_model_loaded("qwen25-7b"))
    await asyncio.sleep(0.05)
    release.set()
    await asyncio.gather(t1, t2)

    assert max_in_flight == 2, (
        f"different models should load concurrently; got max_in_flight={max_in_flight}"
    )
