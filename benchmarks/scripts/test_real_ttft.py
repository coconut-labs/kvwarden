#!/usr/bin/env python3
"""Discriminator tests for the real-TTFT fix in benchmark_multi_model.py.

Caveat C2 from `results/CORRECTIONS.md`. The bench used to time TTFT to the
first SSE `data: ...` line, regardless of whether that frame contained any
real content. v1 of the fix (PR #28) moved the timestamp inside the
`if choices[0].text != ""` branch. v2 (PR #31) tightened it further:

- whitespace-only frames must NOT count as the first token (`bool("\n")` is
  truthy in Python, so v1 still under-counted)
- chat-completions chunks (`delta.content`) must be honored (v1 only looked
  at `text`, so chat-template engines silently reported TTFT == 0)
- malformed SSE chunks (`json.JSONDecodeError`) must NOT count as a token

Each test spawns a mock engine variant and drives the actual
`MultiModelBenchmarkClient._send_request` code path.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
import time
from pathlib import Path

import aiohttp

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

from benchmark_multi_model import MultiModelBenchmarkClient  # noqa: E402

DELAY_S = 0.5


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


async def wait_ready(url: str, timeout_s: float = 10.0) -> None:
    end = time.time() + timeout_s
    async with aiohttp.ClientSession() as s:
        while time.time() < end:
            try:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=1)) as r:
                    if r.status == 200:
                        return
            except Exception:
                pass
            await asyncio.sleep(0.1)
    raise RuntimeError(f"mock not ready at {url} after {timeout_s}s")


def spawn_mock(*, port: int, model: str, ok_latency_s: float, extra_args: list[str]) -> subprocess.Popen:
    return subprocess.Popen(
        [
            sys.executable,
            str(HERE / "mock_engine.py"),
            "--port", str(port),
            "--model", model,
            "--ok-latency-s", str(ok_latency_s),
            "--log-level", "WARNING",
            *extra_args,
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


async def run_case(
    name: str, mock_args: list[str], ok_latency_s: float, max_tokens: int,
) -> tuple[float, int]:
    """Returns (ttft_ms, tokens_out) from a single bench request to a mock."""
    port = free_port()
    proc = spawn_mock(
        port=port, model=name, ok_latency_s=ok_latency_s, extra_args=mock_args,
    )
    try:
        base_url = f"http://127.0.0.1:{port}"
        await wait_ready(f"{base_url}/v1/models", timeout_s=10.0)
        client = MultiModelBenchmarkClient(
            base_url=base_url, concurrency=1, timeout_s=30, max_tokens=max_tokens,
        )
        sem = asyncio.Semaphore(1)
        async with aiohttp.ClientSession() as session:
            m = await client._send_request(session, 0, name, "hello", max_tokens, sem)
        if m.error:
            raise RuntimeError(f"bench request error: {m.error}")
        return (m.ttft_ms, m.tokens_out)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


# Per-case checks. Each case picks the metric the bug actually corrupts:
#
#   empty-preamble: SSE-first-frame bug stamps TTFT on the empty frame, so
#     pre-PR-#28 reports ttft≈5ms. Fix reports ttft > 400ms.
#
#   whitespace-preamble: same SSE-first-frame timing PLUS bool(" ") truthy
#     in PR #28's check, so PR #28 still reports ttft≈5ms here. PR #31's
#     `.strip()` fixes it; reports ttft > 400ms.
#
#   chat-shape: vLLM/SGLang chat completions emit `delta.content`, not
#     `text`. Pre-PR-#31 the `choices[0].get("text", "")` never matches,
#     so tokens_out stays 0 AND first_token_time stays None (TTFT collapses
#     to total_latency_ms). Discriminator: tokens_out must equal max_tokens.
#     We don't gate on TTFT here because aiohttp StreamResponse coalesces
#     small writes under longer per-chunk sleeps — the tokens_out signal
#     is unaffected by that buffering.
async def main() -> int:
    results: list[tuple[str, str, bool]] = []

    # Case 1 + 2: TTFT discriminators (delay-then-content).
    timing_cases = [
        ("empty-preamble", ["--delay-first-content-s", str(DELAY_S)]),
        ("whitespace-preamble", [
            "--first-chunk-whitespace", "1",
            "--delay-first-content-s", str(DELAY_S),
        ]),
    ]
    for name, args in timing_cases:
        try:
            ttft, _ = await run_case(name, args, ok_latency_s=0.05, max_tokens=8)
            passed = ttft > 400.0
            results.append((name, f"ttft_ms={ttft:.1f} (need > 400)", passed))
        except Exception as exc:
            results.append((name, f"ERROR: {exc}", False))

    # Case 3: chat-shape token-count discriminator.
    try:
        _, tokens_out = await run_case(
            "chat-shape",
            ["--chat-completions-shape"],
            ok_latency_s=0.05, max_tokens=8,
        )
        passed = tokens_out == 8
        results.append(("chat-shape", f"tokens_out={tokens_out} (need == 8)", passed))
    except Exception as exc:
        results.append(("chat-shape", f"ERROR: {exc}", False))

    print("\n=== real-TTFT discriminator results ===")
    any_failed = False
    for name, detail, passed in results:
        status = "PASS" if passed else "FAIL"
        print(f"  {status}  {name:<22}  {detail}")
        if not passed:
            any_failed = True

    if any_failed:
        print("\nFAIL: at least one TTFT discriminator regressed.")
        return 1
    print("\nOK: all real-TTFT discriminators passed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
