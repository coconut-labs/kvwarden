# Gate 2-lite — Multi-Model Contention Design Doc

**Date:** 2026-04-19
**Status:** SCAFFOLD (configs + design committed; bench script + runbook to follow next session)
**Owner:** Shrey Patel
**Budget:** ~$8 ceiling on 1× A100-SXM4 @ ~$1.89/hr × ~4h

---

## Why this exists

Gates 0, 0.5, 0.6, and 1 all measured KVWarden as a **single-model + admission control** thing. But the actual KVWarden pitch is **lightweight multi-model orchestration on 1-4 GPUs without K8s** (Gap #2 in `docs/inference_orchestration_gaps_report.md`). That has never been benchmarked. Without Gate 2-lite, the launch post is anchored on a side feature, not the differentiator.

## Hypothesis

When two models are co-loaded on a single 80GB GPU under mixed two-tenant workload (chat short-prompt + RAG 8K-prompt), KVWarden keeps per-tenant p99 TTFT within **1.5× of solo-engine baseline**, while raw uvicorn fronting two `vllm serve` processes either OOMs or shows >2× per-tenant degradation, and a thin round-robin proxy with no admission/lifecycle logic falls between the two.

If this holds: the WorkloadRouter + AdmissionController + TenantManager stack is doing real work; the pitch is justified.
If both baselines tie KVWarden: the thin proxy isn't load-bearing and the pitch needs rethinking before launch.

## Arms

### Arm 1 — KVWarden full stack
- Config: `configs/gate2_multi_tenant.yaml`
- Llama-3.1-8B + Qwen2.5-7B co-loaded, `gpu_memory_utilization: 0.40` each (0.80 total + 0.20 KV/activation headroom on 80GB A100, per PR #15 review notes).
- Admission ON (`max_concurrent: 96` — tighter than Gate 1's 128 because two engines share the GPU compute pipeline).
- Per-tenant budget enforced (`tenant_defaults.max_concurrent_requests: 48`).
- WorkloadRouter mediates which engine handles which model.

### Arm 2 — Raw uvicorn (vLLM single-model, no middleware)
- No KVWarden binary. Run `python -m vllm.entrypoints.openai.api_server` directly with Llama-3.1-8B only on port 8000.
- Bench targets only the Llama tenant; the Qwen tenant has nowhere to go.
- This is the "what does a user get with no orchestration" baseline. We expect Arm 2 to look great on solo Llama latency but fail-by-omission on multi-model — so the pitch is "Arm 2 cannot serve both models at all on 1 GPU; Arm 1 can".
- **Failure mode this arm tests:** if Arm 2 outperforms Arm 1 on the Llama tenant by >2×, KVWarden's overhead is unacceptable.

### Arm 3 — Round-robin thin proxy
- Config: `configs/gate2_round_robin.yaml`
- Same two models co-loaded, but `max_concurrent: 9999` (admission effectively OFF), `tenant_defaults.max_concurrent_requests: 9999` (per-tenant budget OFF), no priority queueing.
- Routes to whichever engine the request asks for, no scheduling intelligence.
- This is the "what if KVWarden were just a multiplexer" baseline.
- **Failure mode this arm tests:** if Arm 3 ≈ Arm 1, the WorkloadRouter + AdmissionController + TenantManager stack isn't load-bearing — the value is purely "we co-load two engines and proxy", which is a 50-line script, not a product.

## Workload

Two tenants, both streaming, run in parallel for 120 seconds sustained per arm:

- **Tenant A (chat):** Llama-3.1-8B target. Short prompts (~256 tokens), short outputs (~128 tokens), 8 RPS sustained.
- **Tenant B (RAG):** Qwen2.5-7B target. Long prompts (~8000 tokens, near `max_model_len=4096` boundary — will need to bump max_model_len to 8192 for both models, see config), short outputs (~256 tokens), 2 RPS sustained but with bursts up to 6 RPS.

120 seconds × 8 RPS = 960 chat requests + 120 × 2 RPS ≈ 240 RAG requests per arm. Long enough for steady-state Little's Law math (avoid the Gate 1 mistake).

**Why this shape:** chat tenant is the "easy" workload that vLLM handles well. RAG tenant is the "hard" workload that should make vLLM's KV cache thrash, contending with the chat tenant for GPU time. This is where admission control and per-tenant fairness should matter.

## Success / failure criteria

**SUCCESS (pitch validated):**
- Arm 1 per-tenant p99 TTFT within 1.5× of Arm 2's solo-Llama baseline AND 1.5× of a solo-Qwen baseline (run separately, or estimate from Gate 0.6's per-model numbers).
- Arm 1 has zero OOMs, zero 5xx.
- Arm 3 shows EITHER OOM under burst OR >2× per-tenant TTFT degradation vs Arm 1 on the RAG tenant during burst.

**FAILURE (pitch broken, rethink before launch):**
- Arm 1 ≈ Arm 3 on all metrics → admission + tenant management isn't load-bearing in the multi-model regime either.
- Arm 1's Llama p99 > 2× Arm 2's Llama p99 → the multi-model overhead is too high; users would prefer to pin one model per GPU.

**AMBIGUOUS (publishable, but not as the headline):**
- Arm 1 wins on burst handling but loses on solo latency → pitch becomes "KVWarden for spiky multi-tenant, raw vLLM for steady-state single-model". Narrower but still real.

## Bench script

New: `benchmarks/scripts/benchmark_two_tenant.py` (to be written next session). Skeleton requirements:

- Two `aiohttp.ClientSession` instances, one per tenant, with proper `TCPConnector(limit=256, limit_per_host=256)` (Gate 1 lesson PR #34).
- Per-tenant Poisson arrivals at the target RPS, with a configurable burst window for tenant B.
- 120s sustained wall, fixed seed (42).
- Outputs per-tenant CSV (`tenant_chat.csv`, `tenant_rag.csv`) with `req_id, ts_submit, ttft_ms, total_latency_ms, tokens_out`.
- Reuses the buffered SSE-frame parser from `benchmark_multi_model.py` (PR #37) for stable `tokens_out`.
- Workload-aware: `--workload two_tenant` so the existing single-model gate (PR #34 R5) doesn't reject it.

## Cost ceiling

A100-SXM4 SECURE on RunPod: ~$1.89/hr × ~4h = ~$7.56. **$8 hard ceiling.** Engine bring-up for two co-loaded models: ~10 min (two HF downloads in parallel, ~10 GB total). Each arm: ~3 min bench wall + ~5 min teardown. Total per arm: ~20 min. Three arms: ~60 min. Add 60 min for engine bring-up + iteration → ~2h. Buffer to $8 covers unexpected reruns.

`MAX_POD_SECS=14400` (4h) per pod spin. Pod-restart between Arm 1 and Arm 3 is **not** required (both use KVWarden's own server which cleans up on shutdown), but recommended between Arm 2 and Arm 1/3 (Arm 2 uses raw vLLM; same memory-leak risk as Gate 1 documented).

## What this PR ships

- This design doc.
- `configs/gate2_multi_tenant.yaml` (Arm 1).
- `configs/gate2_round_robin.yaml` (Arm 3).
- Arm 2 needs no KVWarden config (it bypasses the binary entirely).

## What lands next session

- `benchmarks/scripts/benchmark_two_tenant.py`
- `docs/launch/gate2_runbook.md` (wraps the launch + abort-rules + reading-the-result loop)
- Gate 2-lite dress rehearsal script (CPU-only mock, like `gate1_dress_rehearsal.sh`)
- Runbook execution after Gate 1.5 lands.
