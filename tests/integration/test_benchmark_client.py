"""Integration tests for AsyncBenchmarkClient.

Uses aiohttp's test server to simulate an OpenAI-compatible API endpoint,
then verifies the benchmark client correctly measures TTFT, TPOT, and
handles errors gracefully.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

import pytest

# Add profiling scripts to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "profiling" / "scripts"))

from profiling_utils import AsyncBenchmarkClient, BenchmarkResults

pytest_plugins = ["pytest_asyncio"]


# ---------------------------------------------------------------------------
# Mock OpenAI-compatible server
# ---------------------------------------------------------------------------


def create_mock_app():
    """Create a mock aiohttp app that simulates an OpenAI-compatible API.

    Returns:
        aiohttp Application with /v1/completions and /v1/models endpoints.
    """
    from aiohttp import web

    async def handle_models(request: web.Request) -> web.Response:
        """Handle GET /v1/models."""
        return web.json_response(
            {
                "object": "list",
                "data": [
                    {
                        "id": "test-model",
                        "object": "model",
                        "owned_by": "test",
                    }
                ],
            }
        )

    async def handle_completions(request: web.Request) -> web.StreamResponse:
        """Handle POST /v1/completions with streaming response."""
        body = await request.json()
        prompt = body.get("prompt", "")
        max_tokens = body.get("max_tokens", 10)
        stream = body.get("stream", False)

        if not stream:
            return web.json_response(
                {
                    "id": "cmpl-test",
                    "object": "text_completion",
                    "choices": [
                        {
                            "text": "Hello " * min(max_tokens, 10),
                            "index": 0,
                            "finish_reason": "length",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": len(prompt.split()),
                        "completion_tokens": min(max_tokens, 10),
                    },
                }
            )

        # Streaming response
        response = web.StreamResponse(
            status=200,
            headers={"Content-Type": "text/event-stream"},
        )
        await response.prepare(request)

        num_tokens = min(max_tokens, 10)
        for i in range(num_tokens):
            chunk = {
                "id": "cmpl-test",
                "object": "text_completion",
                "choices": [
                    {
                        "text": "word ",
                        "index": 0,
                        "finish_reason": None if i < num_tokens - 1 else "length",
                    }
                ],
            }
            data = f"data: {json.dumps(chunk)}\n\n"
            await response.write(data.encode("utf-8"))
            await asyncio.sleep(0.01)  # Small delay to simulate generation

        await response.write(b"data: [DONE]\n\n")
        return response

    app = web.Application()
    app.router.add_get("/v1/models", handle_models)
    app.router.add_post("/v1/completions", handle_completions)
    return app


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.fixture
async def mock_server(aiohttp_server):
    """Create and start a mock OpenAI-compatible server."""
    app = create_mock_app()
    server = await aiohttp_server(app)
    return server


@pytest.fixture
def base_url(mock_server) -> str:
    """Get the base URL of the mock server."""
    return f"http://{mock_server.host}:{mock_server.port}"


class TestAsyncBenchmarkClient:
    """Tests for AsyncBenchmarkClient against a mock server."""

    @pytest.mark.asyncio
    async def test_basic_benchmark(self, base_url: str) -> None:
        """Test that the client can run a basic benchmark."""
        client = AsyncBenchmarkClient(
            base_url=base_url,
            model_name="test-model",
            concurrency_level=2,
            timeout_s=30,
        )

        requests = [
            {"prompt": "Hello world", "max_tokens": 5},
            {"prompt": "Test prompt", "max_tokens": 5},
        ]

        results = await client.run(requests)

        assert isinstance(results, BenchmarkResults)
        assert len(results.per_request_metrics) == 2

    @pytest.mark.asyncio
    async def test_ttft_measurement(self, base_url: str) -> None:
        """Test that TTFT is measured correctly (>0 for successful requests)."""
        client = AsyncBenchmarkClient(
            base_url=base_url,
            model_name="test-model",
            concurrency_level=1,
            timeout_s=30,
        )

        requests = [{"prompt": "Measure TTFT", "max_tokens": 5}]
        results = await client.run(requests)

        metric = results.per_request_metrics[0]
        assert metric.error is None
        assert metric.ttft_ms > 0
        assert metric.total_latency_ms > 0

    @pytest.mark.asyncio
    async def test_tpot_measurement(self, base_url: str) -> None:
        """Test that TPOT is measured for multi-token responses."""
        client = AsyncBenchmarkClient(
            base_url=base_url,
            model_name="test-model",
            concurrency_level=1,
            timeout_s=30,
        )

        requests = [{"prompt": "Measure TPOT", "max_tokens": 10}]
        results = await client.run(requests)

        metric = results.per_request_metrics[0]
        assert metric.error is None
        # TPOT should be positive when multiple tokens are generated
        if metric.tokens_out > 1:
            assert metric.tpot_ms > 0

    @pytest.mark.asyncio
    async def test_concurrent_requests(self, base_url: str) -> None:
        """Test handling of concurrent requests."""
        client = AsyncBenchmarkClient(
            base_url=base_url,
            model_name="test-model",
            concurrency_level=4,
            timeout_s=30,
        )

        requests = [{"prompt": f"Request {i}", "max_tokens": 5} for i in range(8)]
        results = await client.run(requests)

        assert len(results.per_request_metrics) == 8
        successful = sum(1 for m in results.per_request_metrics if m.error is None)
        assert successful == 8

    @pytest.mark.asyncio
    async def test_summary_computation(self, base_url: str) -> None:
        """Test that summary statistics are computed correctly."""
        client = AsyncBenchmarkClient(
            base_url=base_url,
            model_name="test-model",
            concurrency_level=2,
            timeout_s=30,
        )

        requests = [{"prompt": f"Request {i}", "max_tokens": 5} for i in range(10)]
        results = await client.run(requests)

        summary = results.summary()
        assert summary["num_requests"] == 10
        assert summary["num_successful"] == 10
        assert summary["num_failed"] == 0
        assert summary["ttft_p50_ms"] > 0

    @pytest.mark.asyncio
    async def test_connection_error_handling(self) -> None:
        """Test that connection errors are handled gracefully."""
        client = AsyncBenchmarkClient(
            base_url="http://localhost:19999",  # Nothing listening here
            model_name="test-model",
            concurrency_level=1,
            timeout_s=5,
        )

        requests = [{"prompt": "This should fail", "max_tokens": 5}]
        results = await client.run(requests)

        assert len(results.per_request_metrics) == 1
        metric = results.per_request_metrics[0]
        assert metric.error is not None

    @pytest.mark.asyncio
    async def test_dataframe_export(self, base_url: str) -> None:
        """Test DataFrame export from results."""
        client = AsyncBenchmarkClient(
            base_url=base_url,
            model_name="test-model",
            concurrency_level=1,
            timeout_s=30,
        )

        requests = [{"prompt": "Test", "max_tokens": 5}]
        results = await client.run(requests)

        df = results.to_dataframe()
        assert len(df) == 1
        assert "ttft_ms" in df.columns
        assert "tpot_ms" in df.columns

    @pytest.mark.asyncio
    async def test_csv_export(self, base_url: str, tmp_path: Path) -> None:
        """Test CSV file export."""
        client = AsyncBenchmarkClient(
            base_url=base_url,
            model_name="test-model",
            concurrency_level=1,
            timeout_s=30,
        )

        requests = [{"prompt": "Test", "max_tokens": 5}]
        results = await client.run(requests)

        csv_path = tmp_path / "test_results.csv"
        results.to_csv(csv_path)
        assert csv_path.exists()
