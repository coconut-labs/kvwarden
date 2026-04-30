"""Two-tenant, single-model fairness benchmark for Gate 2-FAIRNESS.

Measures whether a quiet tenant's TTFT survives when a flooder shares
the same upstream model engine. Three arms of the Gate 2-FAIRNESS
experiment use this script:

    Arm 0: quiet-only baseline (120-240s sustained, 0 flooder)
    Arm 1: raw vLLM (KVWarden passthrough, admission=no-op) — starvation baseline
    Arm 2: KVWarden FIFO scheduling — current behavior (middle arm)
    Arm 3: KVWarden DRR scheduling (scheduling: drr) — the bet

Per-tenant Poisson arrivals. Fixed wall-clock, not fixed request count
(Gate 1.5 lesson: you need ≥100s sustained per cell for Little's Law
to actually exercise the hypothesis). Streaming completions, TTFT is
time-to-first-non-empty-content (C2/C5-fixed path, buffered SSE parser
not strictly necessary here because this script doesn't gate a cap on
token count, but kept aligned with PR #37 for consistency).

Usage:
    python benchmarks/scripts/benchmark_two_tenant_single_model.py \\
      --url http://localhost:8000 \\
      --model meta-llama/Llama-3.1-8B-Instruct \\
      --flooder-rps 32 --quiet-rps 1 \\
      --duration-s 120 \\
      --max-tokens 128 \\
      --output-dir /workspace/results/gate2_fairness_arm3/benchmarks

The output-dir gets two CSVs (tenant_flooder.csv, tenant_quiet.csv)
plus summary.json with per-tenant and aggregate p50/p95/p99 TTFT + count
+ throughput. The Gate 2-FAIRNESS runbook prescribes the success and
failure criteria based on these files.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import random
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


# A small, fixed prompt set. Length kept in the "chat-ish short" regime
# so the bench measures queue wait, not prompt processing time. Bench
# intent is to surface starvation, not exercise prefill.
PROMPTS: list[str] = [
    "Explain quantum entanglement to a 10-year-old in three sentences.",
    "Summarize the plot of The Great Gatsby in exactly two sentences.",
    "What are three reasons to get enough sleep?",
    "Translate 'I would like a cup of coffee' to French.",
    "Write a haiku about morning fog rolling over a coast.",
    "What does 'object-oriented programming' mean in one paragraph?",
    "Name three famous bridges in the world and one fact about each.",
    "Explain the difference between affect and effect with one example.",
]


@dataclass
class RequestResult:
    tenant: str
    request_id: int
    submit_time: float
    ttft_ms: float
    total_latency_ms: float
    tokens_out: int
    error: str = ""


async def send_one(
    session: aiohttp.ClientSession,
    base_url: str,
    tenant_id: str,
    request_id: int,
    model: str,
    prompt: str,
    max_tokens: int,
    timeout_s: float,
) -> RequestResult:
    """Send one streaming completion, measure TTFT + total latency."""
    url = f"{base_url}/v1/completions"
    payload = {
        "model": model,
        "prompt": prompt,
        "max_tokens": max_tokens,
        "stream": True,
    }
    headers = {"X-Tenant-ID": tenant_id}

    submit = time.time()
    first_token_time: float | None = None
    tokens_out = 0

    try:
        timeout = aiohttp.ClientTimeout(total=timeout_s)
        async with session.post(
            url, json=payload, headers=headers, timeout=timeout
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                return RequestResult(
                    tenant=tenant_id,
                    request_id=request_id,
                    submit_time=submit,
                    ttft_ms=0.0,
                    total_latency_ms=(time.time() - submit) * 1000,
                    tokens_out=0,
                    error=f"HTTP {resp.status}: {body[:180]}",
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
                    # TTFT v2: .strip() rejects whitespace-only frames (PR #31).
                    if content.strip():
                        if first_token_time is None:
                            first_token_time = time.time()
                        tokens_out += 1
                except (json.JSONDecodeError, KeyError):
                    continue
    except asyncio.TimeoutError:
        return RequestResult(
            tenant=tenant_id,
            request_id=request_id,
            submit_time=submit,
            ttft_ms=0.0,
            total_latency_ms=(time.time() - submit) * 1000,
            tokens_out=0,
            error="timeout",
        )
    except Exception as exc:
        return RequestResult(
            tenant=tenant_id,
            request_id=request_id,
            submit_time=submit,
            ttft_ms=0.0,
            total_latency_ms=(time.time() - submit) * 1000,
            tokens_out=0,
            error=str(exc),
        )

    end = time.time()
    total_ms = (end - submit) * 1000
    ttft_ms = (
        (first_token_time - submit) * 1000 if first_token_time is not None else total_ms
    )
    return RequestResult(
        tenant=tenant_id,
        request_id=request_id,
        submit_time=submit,
        ttft_ms=ttft_ms,
        total_latency_ms=total_ms,
        tokens_out=tokens_out,
    )


async def tenant_poisson_loop(
    tenant_id: str,
    rps: float,
    duration_s: float,
    session: aiohttp.ClientSession,
    base_url: str,
    model: str,
    max_tokens: int,
    timeout_s: float,
    rng: random.Random,
    results: list[RequestResult],
) -> None:
    """Fire requests with exponential inter-arrival times; collect results."""
    if rps <= 0:
        return
    end_time = time.time() + duration_s
    request_id = 0
    in_flight: list[asyncio.Task[RequestResult]] = []
    mean_interarrival = 1.0 / rps

    while time.time() < end_time:
        prompt = rng.choice(PROMPTS)
        task = asyncio.create_task(
            send_one(
                session,
                base_url,
                tenant_id,
                request_id,
                model,
                prompt,
                max_tokens,
                timeout_s,
            )
        )
        in_flight.append(task)
        request_id += 1
        await asyncio.sleep(rng.expovariate(1.0 / mean_interarrival))

    # Drain in-flight. timeout_s upper-bounds total drain time.
    for t in asyncio.as_completed(in_flight, timeout=timeout_s + 10):
        try:
            results.append(await t)
        except Exception as exc:
            logger.warning("tenant=%s drain failure: %s", tenant_id, exc)


def summarize(results: list[RequestResult]) -> dict[str, Any]:
    """Percentile summary for a list of results. Filters out errors."""
    ok = [r for r in results if not r.error and r.ttft_ms > 0]
    errs = [r for r in results if r.error]
    if not ok:
        return {
            "count_ok": 0,
            "count_err": len(errs),
            "ttft_p50_ms": -1,
            "ttft_p95_ms": -1,
            "ttft_p99_ms": -1,
            "ttft_max_ms": -1,
            "total_p50_ms": -1,
            "total_p99_ms": -1,
            "tokens_out_mean": 0,
        }
    ttfts = sorted(r.ttft_ms for r in ok)
    totals = sorted(r.total_latency_ms for r in ok)
    n = len(ok)

    def p(xs: list[float], q: float) -> float:
        idx = min(n - 1, max(0, int(q * n)))
        return xs[idx]

    return {
        "count_ok": n,
        "count_err": len(errs),
        "ttft_p50_ms": round(p(ttfts, 0.50), 1),
        "ttft_p95_ms": round(p(ttfts, 0.95), 1),
        "ttft_p99_ms": round(p(ttfts, 0.99), 1),
        "ttft_max_ms": round(ttfts[-1], 1),
        "total_p50_ms": round(p(totals, 0.50), 1),
        "total_p99_ms": round(p(totals, 0.99), 1),
        "tokens_out_mean": round(sum(r.tokens_out for r in ok) / n, 1),
    }


async def main_async(args: argparse.Namespace) -> int:
    outdir = Path(args.output_dir)
    outdir.mkdir(parents=True, exist_ok=True)

    # Gate 1.5 lesson: raise TCPConnector limits per actual offered concurrency.
    # Flooder 32 RPS × ~5s per-request avg = ~160 in-flight at steady state.
    # Give 3× headroom so we never silently clamp.
    concurrency_budget = max(
        256, int((args.flooder_rps + args.quiet_rps) * args.max_tokens * 0.05)
    )
    connector = aiohttp.TCPConnector(
        limit=concurrency_budget, limit_per_host=concurrency_budget
    )

    logger.info(
        "Bench config: flooder=%.1f RPS, quiet=%.1f RPS, duration=%ds, model=%s, max_tokens=%d",
        args.flooder_rps,
        args.quiet_rps,
        args.duration_s,
        args.model,
        args.max_tokens,
    )
    logger.info(
        "TCPConnector limit=%d, limit_per_host=%d",
        concurrency_budget,
        concurrency_budget,
    )

    rng_flooder = random.Random(args.seed)
    rng_quiet = random.Random(args.seed + 1)

    flooder_results: list[RequestResult] = []
    quiet_results: list[RequestResult] = []

    start_wall = time.time()
    async with aiohttp.ClientSession(connector=connector) as session:
        await asyncio.gather(
            tenant_poisson_loop(
                "flooder",
                args.flooder_rps,
                args.duration_s,
                session,
                args.url,
                args.model,
                args.max_tokens,
                args.timeout_s,
                rng_flooder,
                flooder_results,
            ),
            tenant_poisson_loop(
                "quiet_user",
                args.quiet_rps,
                args.duration_s,
                session,
                args.url,
                args.model,
                args.max_tokens,
                args.timeout_s,
                rng_quiet,
                quiet_results,
            ),
        )
    wall_s = time.time() - start_wall

    for tenant, results in [
        ("flooder", flooder_results),
        ("quiet_user", quiet_results),
    ]:
        csv_path = outdir / f"tenant_{tenant}.csv"
        with open(csv_path, "w", newline="") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=list(RequestResult.__dataclass_fields__.keys())
            )
            writer.writeheader()
            for r in results:
                writer.writerow(asdict(r))
        logger.info("Wrote %s (%d rows)", csv_path, len(results))

    summary = {
        "wall_time_s": round(wall_s, 2),
        "flooder": summarize(flooder_results),
        "quiet_user": summarize(quiet_results),
        "bench_args": {
            "flooder_rps": args.flooder_rps,
            "quiet_rps": args.quiet_rps,
            "duration_s": args.duration_s,
            "max_tokens": args.max_tokens,
            "model": args.model,
            "seed": args.seed,
        },
    }
    summary_path = outdir / "summary.json"
    with open(summary_path, "w") as fh:
        json.dump(summary, fh, indent=2)
    logger.info("Wrote %s", summary_path)

    # Print a terminal-readable recap
    q = summary["quiet_user"]
    f = summary["flooder"]
    logger.info("=" * 60)
    logger.info(
        "QUIET TTFT  p50=%sms p99=%sms max=%sms (n=%s, err=%s)",
        q["ttft_p50_ms"],
        q["ttft_p99_ms"],
        q["ttft_max_ms"],
        q["count_ok"],
        q["count_err"],
    )
    logger.info(
        "FLOOD TTFT  p50=%sms p99=%sms max=%sms (n=%s, err=%s)",
        f["ttft_p50_ms"],
        f["ttft_p99_ms"],
        f["ttft_max_ms"],
        f["count_ok"],
        f["count_err"],
    )
    logger.info("=" * 60)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--model", required=True)
    p.add_argument("--flooder-rps", type=float, default=32.0)
    p.add_argument("--quiet-rps", type=float, default=1.0)
    p.add_argument("--duration-s", type=float, default=120.0)
    p.add_argument("--max-tokens", type=int, default=128)
    p.add_argument("--timeout-s", type=float, default=60.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--output-dir", required=True)
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
