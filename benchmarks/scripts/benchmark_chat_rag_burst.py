"""Track D — Multi-model OOM-under-burst benchmark.

Tests whether InferGrid's admission + per-tenant budget prevents OOM/
timeout cliffs when a long-prompt RAG tenant bursts on a co-loaded
multi-model A100 (Llama + Qwen).

Workload shape:
  - chat tenant: Llama-3.1-8B, steady 5 RPS, short prompts (~64 tok)
  - rag tenant: Qwen2.5-7B, BURSTY — 30s of 15 RPS with 4-6K token
    prompts, then 60s idle. Three burst cycles over 5 minutes.

Two arms:
  D1 (gate2_multi_tenant.yaml): InferGrid full stack — admission cap +
     per-tenant budget. Hypothesis: bursts get bounded by admission,
     no OOM, p99 chat latency stays low.
  D2 (gate2_round_robin.yaml): thin proxy, no admission (max_concurrent
     9999, rpm 999999). Hypothesis: RAG burst saturates GPU, chat
     tenant gets HoL-blocked or OOM kills the engine.

Pre-committed null rule (god-planner): if D1 ≈ D2 with no OOM observed
in either, cut Track D from the launch post — don't air a null result.

Outputs:
  tenant_chat.csv, tenant_rag.csv, summary.json with per-arm
  fail_count, oom_count (heuristic: HTTP 5xx + "out of memory" in body),
  ttft + total_latency p50/p99 per tenant, throughput.

Usage:
  python benchmarks/scripts/benchmark_chat_rag_burst.py \\
    --url http://localhost:8000 \\
    --chat-model meta-llama/Llama-3.1-8B-Instruct \\
    --rag-model Qwen/Qwen2.5-7B-Instruct \\
    --duration-s 300 \\
    --output-dir /workspace/results/gate2_d_inferGrid
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import aiohttp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


CHAT_PROMPTS: list[str] = [
    "Explain quantum entanglement to a 10-year-old in three sentences.",
    "Summarize the plot of The Great Gatsby in exactly two sentences.",
    "What are three reasons to get enough sleep?",
    "Translate 'I would like a cup of coffee' to French.",
    "Write a haiku about morning fog rolling over a coast.",
]


def _make_long_prompt(rng: random.Random, target_tokens: int) -> str:
    """Generate a roughly target_tokens-long prompt by repeating short
    English snippets. Approx whitespace-token = 0.7 * BPE so we
    over-estimate words to compensate."""
    snippets = [
        "The quick brown fox jumps over the lazy dog.",
        "Pack my box with five dozen liquor jugs.",
        "How vexingly quick daft zebras jump over a lazy fox.",
        "Sphinx of black quartz, judge my vow over the lazy dog.",
        "The five boxing wizards jump quickly across the dog.",
    ]
    target_words = int(target_tokens / 0.7)
    parts: list[str] = []
    cur_words = 0
    while cur_words < target_words:
        s = rng.choice(snippets)
        parts.append(s)
        cur_words += len(s.split())
    return " ".join(parts) + "\n\nSummarize the above in one sentence."


@dataclass
class RequestResult:
    tenant: str
    request_id: int
    submit_time: float
    ttft_ms: float
    total_latency_ms: float
    tokens_out: int
    http_status: int = 200
    is_oom: bool = False
    error: str = ""


_OOM_PAT = re.compile(r"out of memory|cuda oom|cudaerror|allocation", re.IGNORECASE)


async def send_one(
    session: aiohttp.ClientSession, base_url: str, tenant_id: str,
    request_id: int, model: str, prompt: str, max_tokens: int, timeout_s: float,
) -> RequestResult:
    url = f"{base_url}/v1/completions"
    payload = {"model": model, "prompt": prompt, "max_tokens": max_tokens, "stream": True}
    headers = {"X-Tenant-ID": tenant_id}
    submit = time.time()
    first_token_time: float | None = None
    tokens_out = 0
    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with session.post(url, json=payload, headers=headers, timeout=timeout) as resp:
            if resp.status != 200:
                body = await resp.text()
                is_oom = bool(_OOM_PAT.search(body))
                return RequestResult(
                    tenant=tenant_id, request_id=request_id, submit_time=submit,
                    ttft_ms=0.0, total_latency_ms=(time.time() - submit) * 1000,
                    tokens_out=0, http_status=resp.status, is_oom=is_oom,
                    error=f"HTTP {resp.status}: {body[:120]}",
                )
            async for line in resp.content:
                decoded = line.decode("utf-8").strip()
                if not decoded or not decoded.startswith("data: "):
                    continue
                data_str = decoded[6:]
                if data_str == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                    choices = chunk.get("choices", [])
                    if not choices:
                        continue
                    content = (
                        choices[0].get("text")
                        or choices[0].get("delta", {}).get("content")
                        or ""
                    )
                    if content.strip():
                        if first_token_time is None:
                            first_token_time = time.time()
                        tokens_out += 1
                except (json.JSONDecodeError, KeyError):
                    continue
    except asyncio.TimeoutError:
        return RequestResult(
            tenant=tenant_id, request_id=request_id, submit_time=submit,
            ttft_ms=0.0, total_latency_ms=(time.time() - submit) * 1000,
            tokens_out=0, error="timeout",
        )
    except Exception as exc:
        msg = str(exc)
        return RequestResult(
            tenant=tenant_id, request_id=request_id, submit_time=submit,
            ttft_ms=0.0, total_latency_ms=(time.time() - submit) * 1000,
            tokens_out=0, is_oom=bool(_OOM_PAT.search(msg)), error=msg,
        )
    end = time.time()
    total_ms = (end - submit) * 1000
    ttft_ms = (first_token_time - submit) * 1000 if first_token_time is not None else total_ms
    return RequestResult(
        tenant=tenant_id, request_id=request_id, submit_time=submit,
        ttft_ms=ttft_ms, total_latency_ms=total_ms, tokens_out=tokens_out,
    )


async def chat_steady_loop(
    base_url: str, model: str, rps: float, duration_s: float, max_tokens: int,
    timeout_s: float, session: aiohttp.ClientSession, results: list[RequestResult],
) -> None:
    rng = random.Random(42)
    end = time.time() + duration_s
    rid = 0
    in_flight: list[asyncio.Task[RequestResult]] = []
    interarrival = 1.0 / rps
    while time.time() < end:
        prompt = rng.choice(CHAT_PROMPTS)
        in_flight.append(asyncio.create_task(send_one(
            session, base_url, "chat", rid, model, prompt, max_tokens, timeout_s,
        )))
        rid += 1
        await asyncio.sleep(rng.expovariate(1.0 / interarrival))
    for t in asyncio.as_completed(in_flight, timeout=timeout_s + 10):
        try: results.append(await t)
        except Exception as e: logger.warning("chat drain: %s", e)


async def rag_burst_loop(
    base_url: str, model: str, burst_rps: float, burst_dur_s: float,
    idle_s: float, n_bursts: int, max_tokens: int, prompt_tokens: int,
    timeout_s: float, session: aiohttp.ClientSession,
    results: list[RequestResult], start_offset_s: float = 0.0,
) -> None:
    rng = random.Random(123)
    rid = 0
    in_flight: list[asyncio.Task[RequestResult]] = []
    if start_offset_s > 0:
        await asyncio.sleep(start_offset_s)
    interarrival = 1.0 / burst_rps
    for burst_idx in range(n_bursts):
        burst_end = time.time() + burst_dur_s
        logger.info("RAG burst %d/%d starts (%.0fs of %.0f RPS)",
                    burst_idx + 1, n_bursts, burst_dur_s, burst_rps)
        while time.time() < burst_end:
            prompt = _make_long_prompt(rng, prompt_tokens)
            in_flight.append(asyncio.create_task(send_one(
                session, base_url, "rag", rid, model, prompt, max_tokens, timeout_s,
            )))
            rid += 1
            await asyncio.sleep(rng.expovariate(1.0 / interarrival))
        if burst_idx < n_bursts - 1:
            logger.info("RAG idle %.0fs", idle_s)
            await asyncio.sleep(idle_s)
    for t in asyncio.as_completed(in_flight, timeout=timeout_s + 10):
        try: results.append(await t)
        except Exception as e: logger.warning("rag drain: %s", e)


def summarize(results: list[RequestResult]) -> dict[str, Any]:
    ok = [r for r in results if not r.error and r.ttft_ms > 0]
    err = [r for r in results if r.error]
    oom = [r for r in results if r.is_oom]
    if not ok:
        return {
            "count_ok": 0, "count_err": len(err), "count_oom": len(oom),
            "ttft_p50_ms": -1, "ttft_p95_ms": -1, "ttft_p99_ms": -1, "ttft_max_ms": -1,
            "total_p99_ms": -1, "tokens_out_mean": 0,
        }
    ttfts = sorted(r.ttft_ms for r in ok)
    totals = sorted(r.total_latency_ms for r in ok)
    n = len(ok)

    def p(xs: list[float], q: float) -> float:
        return xs[min(n - 1, max(0, int(q * n)))]

    return {
        "count_ok": n, "count_err": len(err), "count_oom": len(oom),
        "ttft_p50_ms": round(p(ttfts, 0.50), 1),
        "ttft_p95_ms": round(p(ttfts, 0.95), 1),
        "ttft_p99_ms": round(p(ttfts, 0.99), 1),
        "ttft_max_ms": round(ttfts[-1], 1),
        "total_p99_ms": round(p(totals, 0.99), 1),
        "tokens_out_mean": round(sum(r.tokens_out for r in ok) / n, 1),
    }


async def main_async(args: argparse.Namespace) -> int:
    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)

    # Burst budget: 15 RPS × 30s × 3 = 1350 RAG requests max in flight at peak.
    # Plus 5 RPS chat × 5 min = 1500 chat. Headroom 4x.
    connector = aiohttp.TCPConnector(limit=2048, limit_per_host=2048)

    chat_results: list[RequestResult] = []
    rag_results: list[RequestResult] = []

    logger.info(
        "Bench: chat=%s @ %.1f RPS steady, rag=%s burst %dx (%.0fs at %.1f RPS, %.0fs idle), prompt=%d tok",
        args.chat_model, args.chat_rps, args.rag_model,
        args.n_bursts, args.burst_dur_s, args.burst_rps, args.idle_s, args.rag_prompt_tokens,
    )

    start_wall = time.time()
    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(
            chat_steady_loop(
                args.url, args.chat_model, args.chat_rps, args.duration_s,
                args.chat_max_tokens, args.timeout_s, session, chat_results,
            ),
            rag_burst_loop(
                args.url, args.rag_model, args.burst_rps, args.burst_dur_s,
                args.idle_s, args.n_bursts, args.rag_max_tokens, args.rag_prompt_tokens,
                args.timeout_s, session, rag_results, args.rag_start_offset_s,
            ),
        )
    wall_s = time.time() - start_wall

    for tenant, results in [("chat", chat_results), ("rag", rag_results)]:
        path = outdir / f"tenant_{tenant}.csv"
        with open(path, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(RequestResult.__dataclass_fields__.keys()))
            writer.writeheader()
            for r in results:
                writer.writerow(asdict(r))
        logger.info("Wrote %s (%d rows)", path, len(results))

    summary = {
        "wall_time_s": round(wall_s, 2),
        "chat": summarize(chat_results),
        "rag": summarize(rag_results),
        "bench_args": {
            "chat_model": args.chat_model, "rag_model": args.rag_model,
            "chat_rps": args.chat_rps, "burst_rps": args.burst_rps,
            "burst_dur_s": args.burst_dur_s, "idle_s": args.idle_s,
            "n_bursts": args.n_bursts, "duration_s": args.duration_s,
            "rag_prompt_tokens": args.rag_prompt_tokens,
        },
    }
    with open(outdir / "summary.json", "w") as fh:
        json.dump(summary, fh, indent=2)

    c, r = summary["chat"], summary["rag"]
    logger.info("=" * 60)
    logger.info(
        "CHAT n=%d err=%d oom=%d  ttft_p99=%sms",
        c["count_ok"], c["count_err"], c["count_oom"], c["ttft_p99_ms"],
    )
    logger.info(
        "RAG  n=%d err=%d oom=%d  ttft_p99=%sms",
        r["count_ok"], r["count_err"], r["count_oom"], r["ttft_p99_ms"],
    )
    logger.info("=" * 60)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--chat-model", required=True)
    p.add_argument("--rag-model", required=True)
    p.add_argument("--chat-rps", type=float, default=5.0)
    p.add_argument("--chat-max-tokens", type=int, default=64)
    p.add_argument("--burst-rps", type=float, default=15.0)
    p.add_argument("--burst-dur-s", type=float, default=30.0)
    p.add_argument("--idle-s", type=float, default=60.0)
    p.add_argument("--n-bursts", type=int, default=3)
    p.add_argument("--rag-prompt-tokens", type=int, default=4096)
    p.add_argument("--rag-max-tokens", type=int, default=128)
    p.add_argument("--rag-start-offset-s", type=float, default=10.0)
    p.add_argument("--duration-s", type=float, default=300.0)
    p.add_argument("--timeout-s", type=float, default=120.0)
    p.add_argument("--output-dir", required=True)
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
