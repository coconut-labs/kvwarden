"""N-tenant fairness benchmark — extends Gate 2-FAIRNESS to N>2 tenants.

Same single-model harness as benchmark_two_tenant_single_model.py but
sends through 1 flooder + N quiet tenants, each with its own
X-Tenant-ID. Per-tenant Poisson arrivals, fixed wall-clock duration.

Track C in the pre-launch sprint asks: does InferGrid's token-bucket
guarantee hold when N=6 (1 flooder + 5 quiet)? If quiet tenants stay
within ~1.5× of solo across all 5, that is a stronger fairness claim
than the 2-tenant launch chart. If one quiet tenant degrades while
others stay clean, that is a real bug, surfaced before launch instead
of by a user.

Usage:
    python benchmarks/scripts/benchmark_n_tenant_single_model.py \\
      --url http://localhost:8000 \\
      --model meta-llama/Llama-3.1-8B-Instruct \\
      --flooder-rps 32 \\
      --quiet-rps 1 \\
      --num-quiet 5 \\
      --duration-s 300 \\
      --max-tokens 64 \\
      --output-dir /workspace/results/gate2_n_tenants

Outputs per-tenant CSVs (tenant_flooder.csv, tenant_quiet_0.csv, ...)
plus summary.json with aggregate + per-tenant percentiles.
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
                return RequestResult(
                    tenant=tenant_id, request_id=request_id, submit_time=submit,
                    ttft_ms=0.0, total_latency_ms=(time.time() - submit) * 1000,
                    tokens_out=0, error=f"HTTP {resp.status}: {body[:180]}",
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
        return RequestResult(
            tenant=tenant_id, request_id=request_id, submit_time=submit,
            ttft_ms=0.0, total_latency_ms=(time.time() - submit) * 1000,
            tokens_out=0, error=str(exc),
        )
    end = time.time()
    total_ms = (end - submit) * 1000
    ttft_ms = (first_token_time - submit) * 1000 if first_token_time is not None else total_ms
    return RequestResult(
        tenant=tenant_id, request_id=request_id, submit_time=submit,
        ttft_ms=ttft_ms, total_latency_ms=total_ms, tokens_out=tokens_out,
    )


async def tenant_poisson_loop(
    tenant_id: str, rps: float, duration_s: float,
    session: aiohttp.ClientSession, base_url: str, model: str,
    max_tokens: int, timeout_s: float, rng: random.Random,
    results: list[RequestResult],
) -> None:
    if rps <= 0:
        return
    end_time = time.time() + duration_s
    request_id = 0
    in_flight: list[asyncio.Task[RequestResult]] = []
    mean_interarrival = 1.0 / rps
    while time.time() < end_time:
        prompt = rng.choice(PROMPTS)
        task = asyncio.create_task(send_one(
            session, base_url, tenant_id, request_id, model, prompt,
            max_tokens, timeout_s,
        ))
        in_flight.append(task)
        request_id += 1
        await asyncio.sleep(rng.expovariate(1.0 / mean_interarrival))
    for t in asyncio.as_completed(in_flight, timeout=timeout_s + 10):
        try:
            results.append(await t)
        except Exception as exc:
            logger.warning("tenant=%s drain failure: %s", tenant_id, exc)


def summarize(results: list[RequestResult]) -> dict[str, Any]:
    ok = [r for r in results if not r.error and r.ttft_ms > 0]
    errs = [r for r in results if r.error]
    if not ok:
        return {
            "count_ok": 0, "count_err": len(errs),
            "ttft_p50_ms": -1, "ttft_p95_ms": -1, "ttft_p99_ms": -1, "ttft_max_ms": -1,
            "total_p50_ms": -1, "total_p99_ms": -1, "tokens_out_mean": 0,
        }
    ttfts = sorted(r.ttft_ms for r in ok)
    totals = sorted(r.total_latency_ms for r in ok)
    n = len(ok)

    def p(xs: list[float], q: float) -> float:
        idx = min(n - 1, max(0, int(q * n)))
        return xs[idx]

    return {
        "count_ok": n, "count_err": len(errs),
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

    # 1 flooder + N quiet tenants: budget for total offered concurrency.
    # Each quiet at quiet_rps × 5s avg + flooder at flooder_rps × 5s avg.
    expected_inflight = (args.flooder_rps + args.num_quiet * args.quiet_rps) * 5
    concurrency_budget = max(256, int(expected_inflight * 3))
    connector = aiohttp.TCPConnector(limit=concurrency_budget, limit_per_host=concurrency_budget)

    logger.info(
        "Bench: 1 flooder @ %.1f RPS + %d quiet @ %.1f RPS each, duration=%ds, model=%s",
        args.flooder_rps, args.num_quiet, args.quiet_rps, args.duration_s, args.model,
    )
    logger.info("TCPConnector limit=%d", concurrency_budget)

    flooder_results: list[RequestResult] = []
    quiet_results: list[list[RequestResult]] = [[] for _ in range(args.num_quiet)]

    start_wall = time.time()
    async with aiohttp.ClientSession(connector=connector) as session:
        coros = [
            tenant_poisson_loop(
                "flooder", args.flooder_rps, args.duration_s,
                session, args.url, args.model, args.max_tokens, args.timeout_s,
                random.Random(args.seed), flooder_results,
            ),
        ]
        for i in range(args.num_quiet):
            coros.append(
                tenant_poisson_loop(
                    f"quiet_{i}", args.quiet_rps, args.duration_s,
                    session, args.url, args.model, args.max_tokens, args.timeout_s,
                    random.Random(args.seed + 1 + i), quiet_results[i],
                )
            )
        await asyncio.gather(*coros)
    wall_s = time.time() - start_wall

    # Per-tenant CSVs
    for tenant, results in [("flooder", flooder_results)] + [
        (f"quiet_{i}", quiet_results[i]) for i in range(args.num_quiet)
    ]:
        csv_path = outdir / f"tenant_{tenant}.csv"
        with open(csv_path, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(RequestResult.__dataclass_fields__.keys()))
            writer.writeheader()
            for r in results:
                writer.writerow(asdict(r))
        logger.info("Wrote %s (%d rows)", csv_path, len(results))

    quiet_summaries = {f"quiet_{i}": summarize(quiet_results[i]) for i in range(args.num_quiet)}
    # Aggregate quiet percentiles across all quiet samples (the launch claim).
    all_quiet = [r for sub in quiet_results for r in sub]
    summary = {
        "wall_time_s": round(wall_s, 2),
        "flooder": summarize(flooder_results),
        "quiet_aggregate": summarize(all_quiet),
        "quiet_per_tenant": quiet_summaries,
        "bench_args": {
            "flooder_rps": args.flooder_rps, "quiet_rps": args.quiet_rps,
            "num_quiet": args.num_quiet, "duration_s": args.duration_s,
            "max_tokens": args.max_tokens, "model": args.model, "seed": args.seed,
        },
    }
    summary_path = outdir / "summary.json"
    with open(summary_path, "w") as fh:
        json.dump(summary, fh, indent=2)
    logger.info("Wrote %s", summary_path)

    # Recap
    qa = summary["quiet_aggregate"]
    f = summary["flooder"]
    logger.info("=" * 60)
    logger.info(
        "QUIET AGG (n=%d) p50=%sms p99=%sms max=%sms",
        qa["count_ok"], qa["ttft_p50_ms"], qa["ttft_p99_ms"], qa["ttft_max_ms"],
    )
    logger.info(
        "FLOOD     (n=%d) p50=%sms p99=%sms max=%sms",
        f["count_ok"], f["ttft_p50_ms"], f["ttft_p99_ms"], f["ttft_max_ms"],
    )
    # Per-tenant fairness — what's the spread across quiet tenants?
    quiet_p99s = [s["ttft_p99_ms"] for s in quiet_summaries.values() if s["ttft_p99_ms"] > 0]
    if quiet_p99s:
        logger.info(
            "Per-quiet p99 spread: min=%.1f max=%.1f ratio=%.2fx",
            min(quiet_p99s), max(quiet_p99s),
            max(quiet_p99s) / max(min(quiet_p99s), 0.001),
        )
    logger.info("=" * 60)
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="http://localhost:8000")
    p.add_argument("--model", required=True)
    p.add_argument("--flooder-rps", type=float, default=32.0)
    p.add_argument("--quiet-rps", type=float, default=1.0)
    p.add_argument("--num-quiet", type=int, default=5)
    p.add_argument("--duration-s", type=float, default=300.0)
    p.add_argument("--max-tokens", type=int, default=64)
    p.add_argument("--timeout-s", type=float, default=60.0)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--output-dir", required=True)
    args = p.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
