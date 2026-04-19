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
