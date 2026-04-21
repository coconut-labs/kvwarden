"""Tests that /health gates on full pre-load completion.

Before the fix, /health returned 200 unconditionally; load balancers
and launch-day demos could send real traffic at a server whose engines
were still cold, triggering the lazy-load fork-bomb pattern even after
the per-model lock fix landed.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp.test_utils import make_mocked_request

from kvwarden.common.config import KVWardenConfig, ModelConfig
from kvwarden.router.router import WorkloadRouter


def _cfg(model_ids: list[str]) -> KVWardenConfig:
    return KVWardenConfig(
        models=[
            ModelConfig(model_id=mid, short_name=mid, engine="vllm", dtype="bfloat16")
            for mid in model_ids
        ],
    )


@pytest.mark.asyncio
async def test_health_503_when_no_models_loaded():
    router = WorkloadRouter(_cfg(["llama31-8b", "qwen25-7b"]))
    resp = await router.handle_health(make_mocked_request("GET", "/health"))
    assert resp.status == 503
    body = resp.body.decode()
    assert "loading" in body
    assert "llama31-8b" in body
    assert "qwen25-7b" in body


@pytest.mark.asyncio
async def test_health_503_when_partial_load():
    router = WorkloadRouter(_cfg(["llama31-8b", "qwen25-7b"]))
    router._models["llama31-8b"] = MagicMock()
    resp = await router.handle_health(make_mocked_request("GET", "/health"))
    assert resp.status == 503
    body = resp.body.decode()
    assert "qwen25-7b" in body  # missing
    assert "llama31-8b" in body  # loaded


@pytest.mark.asyncio
async def test_health_200_when_all_loaded():
    router = WorkloadRouter(_cfg(["llama31-8b"]))
    router._models["llama31-8b"] = MagicMock()
    resp = await router.handle_health(make_mocked_request("GET", "/health"))
    assert resp.status == 200
    assert b'"ok"' in resp.body
    assert b"llama31-8b" in resp.body
