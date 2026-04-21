#!/usr/bin/env python3
"""Mock vLLM-compatible engine for local Gate 0.5 bench harness reproduction.

Mimics just enough of vLLM's OpenAI-compatible surface to drive `kvwarden serve`
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
    def __init__(
        self,
        model: str,
        hang_after: int,
        ok_latency: float,
        stall_s: float,
        delay_first_content_s: float = 0.0,
        first_chunk_whitespace: int = 0,
        chat_completions_shape: bool = False,
    ):
        self.model = model
        self.hang_after = hang_after
        self.ok_latency = ok_latency
        self.stall_s = stall_s
        self.delay_first_content_s = delay_first_content_s
        self.first_chunk_whitespace = first_chunk_whitespace
        self.chat_completions_shape = chat_completions_shape
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

    def _make_chunk(text_value: str | None, finish: str | None = None) -> dict[str, Any]:
        # Chat-completions shape uses `delta.content`; classic completions
        # uses `text`. Real engines pick based on endpoint; we let the test
        # override so the harness can be exercised against both shapes.
        if state.chat_completions_shape:
            choice: dict[str, Any] = {
                "index": 0,
                "delta": {"content": text_value} if text_value is not None else {"role": "assistant"},
                "finish_reason": finish,
            }
            obj = "chat.completion.chunk"
        else:
            choice = {"index": 0, "text": text_value if text_value is not None else "", "finish_reason": finish}
            obj = "text_completion.chunk"
        return {
            "id": f"mock-{rid}",
            "object": obj,
            "created": int(time.time()),
            "model": state.model,
            "choices": [choice],
        }

    # Discriminator for real-TTFT v2:
    #   --first-chunk-whitespace N: emit N whitespace-only frames before
    #     content. A broken `bool(text)` check counts whitespace as a token
    #     and stamps TTFT early; the v2 fix uses `text.strip()` and is honest.
    #   --delay-first-content-s D: emit one empty/role-only frame, sleep D,
    #     then real content. A broken harness that times the first SSE frame
    #     reports D≈0; the v2 fix reports ~D.
    if state.first_chunk_whitespace > 0:
        for _ in range(state.first_chunk_whitespace):
            await resp.write(f"data: {json.dumps(_make_chunk(' '))}\n\n".encode())
        if state.delay_first_content_s > 0:
            await asyncio.sleep(state.delay_first_content_s)
    elif state.delay_first_content_s > 0:
        # Empty-text (or role-only delta) preamble.
        await resp.write(f"data: {json.dumps(_make_chunk(None))}\n\n".encode())
        await asyncio.sleep(state.delay_first_content_s)

    for tok_i in range(max(1, max_tokens)):
        await resp.write(f"data: {json.dumps(_make_chunk(f't{tok_i} '))}\n\n".encode())
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
    p.add_argument(
        "--delay-first-content-s",
        type=float,
        default=float(os.environ.get("MOCK_DELAY_FIRST_CONTENT", "0")),
        help="On streaming responses, emit an empty-text SSE frame, then sleep this long before the first real-content chunk (real-TTFT discriminator)",
    )
    p.add_argument(
        "--first-chunk-whitespace",
        type=int,
        default=int(os.environ.get("MOCK_FIRST_CHUNK_WHITESPACE", "0")),
        help="Emit N whitespace-only SSE frames before any real content (TTFT v2 discriminator: ensures `text.strip()` is checked, not bool(text))",
    )
    p.add_argument(
        "--chat-completions-shape",
        action="store_true",
        default=os.environ.get("MOCK_CHAT_SHAPE", "0") not in ("", "0"),
        help="Emit chat.completion.chunk shape (delta.content) instead of text_completion.chunk (text). Tests harness chat-template path.",
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
        delay_first_content_s=args.delay_first_content_s,
        first_chunk_whitespace=args.first_chunk_whitespace,
        chat_completions_shape=args.chat_completions_shape,
    )
    logger.info(
        "mock engine starting: port=%d model=%s hang_after=%d ok_latency=%.2fs stall=%.0fs delay_first_content=%.2fs first_chunk_ws=%d chat_shape=%s",
        args.port, args.model, args.hang_after, args.ok_latency_s, args.stall_s, args.delay_first_content_s, args.first_chunk_whitespace, args.chat_completions_shape,
    )
    web.run_app(make_app(state), host="127.0.0.1", port=args.port, print=None)


if __name__ == "__main__":
    main()
