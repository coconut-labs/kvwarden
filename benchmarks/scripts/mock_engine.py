#!/usr/bin/env python3
"""Mock vLLM-compatible engine for local Gate 0.5 bench harness reproduction.

Mimics just enough of vLLM's OpenAI-compatible surface to drive `infergrid serve`
and the multi-model benchmark harness without a GPU. After a configurable number
of successful requests, subsequent POST /v1/completions handlers enter a long
sleep, mimicking the Gate 0 symptom where vLLM stopped returning response
headers. GET /v1/models stays healthy so the router's health checks pass.

Usage:
    python3 mock_engine.py --port 8002 --model meta-llama/Llama-3.1-8B-Instruct \
        --hang-after 40 --ok-latency-s 0.5

Env overrides:
    MOCK_HANG_AFTER  — request count before entering stall mode
    MOCK_OK_LATENCY  — seconds to sleep on successful requests
    MOCK_STALL_S     — seconds to sleep when stalled (default: 10_000)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import time
from aiohttp import web


logger = logging.getLogger("mock_engine")


class MockEngineState:
    def __init__(self, model: str, hang_after: int, ok_latency: float, stall_s: float):
        self.model = model
        self.hang_after = hang_after
        self.ok_latency = ok_latency
        self.stall_s = stall_s
        self.request_count = 0
        self.stalled_count = 0


async def handle_models(request: web.Request) -> web.Response:
    state: MockEngineState = request.app["state"]
    return web.json_response({
        "object": "list",
        "data": [
            {
                "id": state.model,
                "object": "model",
                "created": int(time.time()),
                "owned_by": "mock",
            }
        ],
    })


async def handle_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


async def handle_completions(request: web.Request) -> web.StreamResponse:
    state: MockEngineState = request.app["state"]
    state.request_count += 1
    rid = state.request_count

    body = await request.json()
    prompt = body.get("prompt", "")
    max_tokens = int(body.get("max_tokens", 32))
    stream = bool(body.get("stream", False))

    # Enter stall mode after `hang_after` successful requests
    if state.hang_after > 0 and rid > state.hang_after:
        state.stalled_count += 1
        logger.info("req=%d STALL for %.0fs (hang_after=%d)", rid, state.stall_s, state.hang_after)
        try:
            await asyncio.sleep(state.stall_s)
        except asyncio.CancelledError:
            logger.info("req=%d cancelled after %.1fs", rid, state.stall_s)
            raise
        # If we ever wake up, return something harmless
        return web.Response(status=504, text="mock stall exit")

    logger.info("req=%d OK (prompt_len=%d, max_tokens=%d, stream=%s, ok_latency=%.2fs)",
                rid, len(prompt), max_tokens, stream, state.ok_latency)

    # Simulate engine work
    await asyncio.sleep(state.ok_latency)

    if not stream:
        return web.json_response({
            "id": f"mock-{rid}",
            "object": "text_completion",
            "created": int(time.time()),
            "model": state.model,
            "choices": [{
                "index": 0,
                "text": f"mock response to req {rid}",
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": len(prompt.split()),
                "completion_tokens": 5,
                "total_tokens": len(prompt.split()) + 5,
            },
        })

    # SSE stream response
    resp = web.StreamResponse(
        status=200,
        reason="OK",
        headers={"Content-Type": "text/event-stream"},
    )
    await resp.prepare(request)
    for tok_i in range(max(1, max_tokens)):
        chunk = {
            "id": f"mock-{rid}",
            "object": "text_completion.chunk",
            "created": int(time.time()),
            "model": state.model,
            "choices": [{"index": 0, "text": f"t{tok_i} ", "finish_reason": None}],
        }
        await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
        # Small inter-token delay to look like generation
        await asyncio.sleep(max(0.001, state.ok_latency / max(1, max_tokens)))
    await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp


def make_app(state: MockEngineState) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_get("/v1/models", handle_models)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/v1/completions", handle_completions)
    app.router.add_post("/v1/chat/completions", handle_completions)  # share handler
    return app


def main() -> None:
    p = argparse.ArgumentParser(description="Mock vLLM engine for Gate 0.5 repro")
    p.add_argument("--port", type=int, required=True)
    p.add_argument("--model", default="mock-model")
    p.add_argument(
        "--hang-after",
        type=int,
        default=int(os.environ.get("MOCK_HANG_AFTER", "0")),
        help="Enter stall mode after N successful requests (0=never)",
    )
    p.add_argument(
        "--ok-latency-s",
        type=float,
        default=float(os.environ.get("MOCK_OK_LATENCY", "0.2")),
        help="Seconds to sleep on successful requests",
    )
    p.add_argument(
        "--stall-s",
        type=float,
        default=float(os.environ.get("MOCK_STALL_S", "10000")),
        help="Seconds to sleep when stalled",
    )
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    state = MockEngineState(
        model=args.model,
        hang_after=args.hang_after,
        ok_latency=args.ok_latency_s,
        stall_s=args.stall_s,
    )
    logger.info(
        "mock engine starting: port=%d model=%s hang_after=%d ok_latency=%.2fs stall=%.0fs",
        args.port, args.model, args.hang_after, args.ok_latency_s, args.stall_s,
    )
    web.run_app(make_app(state), host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
