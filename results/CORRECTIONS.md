# Results — Corrections & Caveats

This file records discrepancies and misleading metrics in the committed results
artifacts so the preprint and any external citations don't propagate stale or
wrong numbers. Each entry: where the issue is, what's wrong, what's correct,
and what action (if any) is required.

---

## C1 — Driver version mismatch in PR #14 commit message vs metadata

**Where:** `git log` for `PR #14` (data: Phase 1 profiling raw results) commit message vs.
the `driver` fields in three `run_metadata.json` files.

**Discrepancy:** the PR #14 commit message reads
"vLLM A100-SXM4-80GB (driver 580.126.16)" and "SGLang A100-SXM4-80GB (driver 580.126.16)".
Reality from on-disk metadata:

| Run dir | metadata driver |
|---|---|
| `results_vllm_a100-sxm_20260416_182457/run_metadata.json` | **570.195.03** |
| `results_sglang_a100-sxm_20260416_183302/run_metadata.json` | 580.126.16 |
| `results_vllm_h100-sxm_20260416_182420/run_metadata.json` | 580.126.09 |

**Correct citation for the preprint:**
- vLLM A100-SXM: NVIDIA driver **570.195.03**
- SGLang A100-SXM: NVIDIA driver **580.126.16**
- vLLM H100-SXM: NVIDIA driver **580.126.09**

The PR #14 commit message error does not affect the data itself. No re-run needed.

---

## C2 — `switch_latency.json` "TTFT" is not real TTFT

**Where:** `results/gate0_20260418/benchmarks/switch_latency.json` (Gate 0 partial bench output).

**Reported numbers:** `cold_swap_ttft_mean_ms: 31.3`, `warm_ttft_mean_ms: 30.2 / 31.2`.

**Why these are misleading:** the harness measures "first token" by timestamping
the first SSE line that begins with `data: ` (see `benchmark_multi_model.py`
around line 485-494). For a localhost connection, the **first SSE frame
arrives within the TCP round-trip window (~30 ms)** — long before actual model
generation finishes. So the recorded "TTFT" is essentially network RTT plus
the latency of the first SSE chunk header, **not** the time from request to
first generated token from the model.

**Don't cite these numbers.** Real TTFT for an 7-8B model on A100-SXM is
expected to be:
- cold (first request to a freshly-loaded model): 30-80 ms
- warm: 5-15 ms

These will be re-measured properly in Gate 1 with a corrected harness path
that times from request submit to first non-zero-token in the streamed
content (not first SSE frame).

**Status (2026-04-19, PR #28 + #31):** harness fix landed in two passes.

**v1 (PR #28):** `benchmark_multi_model.py` now sets `first_token_time` only
when `choices[0].text` is non-empty.

**v2 (PR #31):** PR #28 was incomplete and shipped two more silent failure
modes that the v1 discriminator did not catch:
- `bool(" ")` is truthy in Python, so a whitespace-only first chunk (which
  vLLM/SGLang both emit during warm-up tokenization) was still counted as
  the first token. Now uses `text.strip()`.
- Chat-completions endpoints emit `delta.content`, not `text`. The v1 check
  always saw `""`, so on chat-template engines TTFT silently collapsed to
  `total_latency_ms` and `tokens_out` was always 0. Now falls back to
  `choices[0].get("delta", {}).get("content")`.
- `JSONDecodeError`/`KeyError` in chunk parse used to *increment* `tokens_out`
  and stamp `first_token_time`. A parse error is not generation progress;
  v2 `continues` instead.

`benchmarks/scripts/test_real_ttft.py` now has three discriminator cases
(empty preamble, whitespace preamble, chat-shape token-count). On broken
code the new whitespace and chat-shape cases fail loud (52 ms vs need >400,
tokens_out=0 vs need 8). Numbers in `switch_latency.json` and
`gate06_20260419/` predate both fixes; under-counted by ~30 ms RTT. Gate 1
onward will be honest TTFT.

---

## C3 — Router `avg_latency_s: 0.0102` excludes streaming tail

**Where:** `results/gate0_20260418/status_after.json` — the
`loaded_models["..."].avg_latency_s` field.

**What it actually measures:** time spent inside `route_request` from
admission to the moment `forward_request` returns the async iterator —
not the full request lifetime. For streaming responses, this excludes the
generation time entirely.

**Correct interpretation:** "router-internal handler overhead per request"
(useful for proving the router is not a bottleneck). It is **not** the
end-to-end latency a client sees.

---

## C5 — Pre-PR-#29 admission control was a no-op for streaming workloads

**Where:** Any pre-PR-#29 numbers, gauges, or claims about admission
engagement under streaming load. In particular, `gate0_20260418/` and
`gate06_20260419/` ran with the broken admission release path.

**What was wrong:** `route_request` released the admission slot in the
outer `finally`. For `stream=True`, `await forward_request(...)` returned
the async generator object immediately — slot freed microseconds after
acquire. Confirmed empirically by the smoke poller: 149 samples during a
c=32 phase, peak `infergrid_admission_in_flight = 0` with `cap=16`. For
streaming traffic — i.e. all of our benches — the admission cap was
effectively infinite.

**Why Gate 0 / Gate 0.5 / Gate 0.6 conclusions still stand:** none of
those gates' verdicts depended on admission engagement.
- Gate 0: validated multi-model co-residency; `0 rejected` was correct
  because we never offered enough load to need admission.
- Gate 0.5: harness-resilience local fix; admission was not in scope.
- Gate 0.6: validated bench harness completion on real vLLM; admission
  was not the variable under test.

**Why Gate 1 onward critically depends on this:** the Arm A vs Arm B
hypothesis IS admission engagement. Without PR #29 the experiment was
unmeasurable — both arms would have admitted all 256 reqs simultaneously.

**Status (2026-04-19, PR #29 + #37):**
- PR #29 wraps the streaming iterator so admission releases at stream end.
- PR #30 + #32 + #33 closed adjacent leaks and added a max-stream-duration
  fence.
- PR #37 fixed a follow-up bug: PR #32's `chunk_count` proxy varied 5-50×
  with TCP fragmentation (`iter_any()` yields raw socket reads, not SSE
  frames). Now buffered SSE-frame parsing in the wrapper. Discriminator
  test enforces it: pre-fix reports `tokens_out=90` for a byte-fragmented
  3-frame stream; post-fix reports `tokens_out=2`.

Action: any external citation of admission behavior MUST be from a run
post-PR-#29 (and ideally post-PR-#37 for token accounting).

---

## C4 — Gate 0 bench ran on a branch predating PR #16's engine log capture

**Where:** Gate 0 was executed against branch `feat/gate0-config` before
PR #16 merged. The pod's vLLM subprocess wrote stderr only to in-memory PIPE
with a 2 KB tail slice, so when vLLM hung at request ~46 we lost the actual
root cause.

**Action:** PR #16 (now merged) writes per-engine stdout+stderr to disk under
`INFERGRID_ENGINE_LOG_DIR` (default `/tmp`). All future Gate runs MUST clone
main (post-PR #16) so the engine logs are preserved.

---

## C6 — Pre-PR-#58 cold-start fork-bomb invalidated the first Track A re-run

**Where:** `results/gate2_preprint_pod1_evidence/gate2_preprint_20260419_195055/arm1_flooder_*/`
(Pod `8ldnojj8xq8abg`, terminated). Track A v1 attempted on 2026-04-19.

**What we observed:**
- Arm 0 (solo, no flooder) completed cleanly: `quiet_user.ttft_p99_ms = 62.5` (n=321).
- Arm 1 (vanilla vLLM under 32 RPS flooder) reported `count_ok = 0, count_err = 303` for the
  quiet tenant. `server.log` showed repeated `TimeoutError: vLLM server did not become healthy
  within 420s` and the bench process died with `RuntimeError: can't start new thread`. `ps aux`
  on the pod showed 6+ simultaneous `vllm.entrypoints.openai.api_server` processes.

**Root cause:** `WorkloadRouter.ensure_model_loaded()` had a TOCTOU race. After the inter-arm
`pkill -9 -f vllm`, the next arm's first request found the model cache empty; with N concurrent
in-flight requests at 32 RPS, every request raced into `load_model()` which spawned its own
engine subprocess. Each subprocess held `gpu_memory_utilization=0.85` × 80 GB ≈ 68 GB of GPU
memory; 6+ engines instantly OOM'd the GPU and none became healthy.

A second compounding bug: `handle_health()` returned `200 OK` unconditionally, so the runner's
warmup loop ("wait for /health") returned immediately while the engines were still cold,
allowing the bench to start flooding before any engine was ready.

**Fix (landed):**
- PR #58 (`fix/ensure-model-loaded-race`) introduces per-model `asyncio.Lock` so concurrent
  `ensure_model_loaded()` callers serialize through one `load_model()` invocation.
- PR for `fix/eager-load-and-health` makes `handle_health` return 503 with `missing_models`
  until every configured model is in `_models`, and surfaces pre-load failures with
  `logger.error` (instead of swallowing them silently).

**Action for citations:** the only Pod 1 number that is preserved as evidence is the
documentation of the bug itself (server.log + summary.json). Track A's preprint numbers
**must** come from the v2 re-run on Pod 2 (`gate2_preprint_20260419_201158/`) where the
race fix was in effect and the runner does an explicit sequential warmup curl before
starting the bench. Any citation that mixes v1 (Pod 1) and v2 numbers is wrong.

**Lessons captured for the launch quickstart:**
- The README quickstart implied "pip install / `infergrid serve` / curl" works immediately.
  After this fix, `/health` is the correct readiness probe; users who curl /v1/completions
  before /health is 200 will get 503, not a hung 30+ second cold-start.
- Pre-load failures are now loud, not silent.

---

## C7 — vLLM version delta between original Gate 2-FAIRNESS and v3 preprint

**Where:** Original 5-arm Gate 2-FAIRNESS (`results/gate2_fairness_20260419/`) ran on vLLM **0.8.5**
(see `gate2f_arm1_*/results/.../engine_logs/*.log` line 1: `vLLM API server version 0.8.5`).
The pre-launch preprint re-run (`results/gate2_preprint_v3/`) ran on vLLM **0.19.1** —
the version users will install today.

**What changed:** vLLM 0.8.5 → 0.19.1 spans the v0 → v1 engine transition with significantly
more aggressive continuous batching and better backpressure handling. Per-window analysis
of the original Arm 1 (`tenant_quiet_user.csv`) shows TTFT growing monotonically from
3,129 ms p99 at t=5 s to 28,790 ms p99 at t=115 s — the queue never reached steady state
in the 120 s window. The 28,716 ms aggregate p99 quoted in the launch hero number was
real for that bench, but reflected an unbounded queue, not a steady-state behavior.

In the v3 300 s preprint re-run on vLLM 0.19.1, Arm 1 reaches steady state with
quiet p99 = 1,585 ms (n=321). The per-window p99 stays in 800-2,500 ms range —
flat, not growing.

**Hero numbers (revised, vLLM 0.19.1, 300 s sustained):**

| Arm | Quiet p99 | Sample size | Note |
|---|---:|---:|---|
| Arm 0 (solo) | 53.9 ms | n=320 | Within noise of original 54.9 ms |
| Arm 1 (FIFO, no rate-limit) | 1,585 ms | n=321 | Steady state on 0.19.1 — 29× of solo |
| Arm 5b (token bucket) | 61.5 ms post-warmup | n=311 | First 10 s excluded (1 JIT outlier window: 5,818 ms max). Aggregate including warmup: 1,230 ms. Post-warmup: **1.14× of solo**. All 29 steady-state windows have p99 36-65 ms. |

**Action:**
- All citations of the launch hero number **must** use the v3 numbers (1.14× of solo,
  26× reduction). The 388× / 523× framing reflected pre-steady-state queue dynamics on
  an old vLLM version and should not be used in any external communication.
- The original 5-arm `GATE2_FAIRNESS_OUTCOME.md` is preserved as historical evidence;
  do not delete or revise its numbers — they were honest for vLLM 0.8.5 in 120 s.
- The launch chart is regenerated from v3 data via `scripts/generate_launch_chart_v3.py`.
- Methodology: hero p99 excludes the first 10 s warmup window (vLLM JIT-compile
  transient — same caveat the original Arm 5 sliding-window arm had).
- For users pinning to vLLM 0.8.5 (an unusual choice; that version is ~12 months old),
  the original numbers still apply.

