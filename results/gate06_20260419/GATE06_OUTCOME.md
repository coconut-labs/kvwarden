# Gate 0.6 — Multi-model bench completion on REAL vLLM (validation)

**Date:** 2026-04-19
**Hardware:** 1× NVIDIA A100-SXM4-80GB on RunPod (SECURE)
**Pod ID:** p23g245o9eu58n (terminated)
**Duration:** ~2h 8m wall clock (00:16 → 02:39 UTC)
**Cost:** ~$3.17 (under $5 ceiling, slightly over the $3 estimate due to first-time deps install + model download)
**Git head on pod:** `a610a14` (main with PRs #14-23 merged: compat pins, harness resilience, circuit breaker, D3 logs)

## Verdict: PASS

**The thing Gate 0 deferred — bench harness completing against real vLLM under multi-model load — works.** 75 requests admitted, 0 rejected, 0 timed-out, 0 OOMs, 0 mid-stream stalls. Both engines healthy at shutdown. Engine stderr was captured per PR #16 (234 KB combined for the two vLLM processes).

## Numbers

Multi-model alternating workload, 25 requests per concurrency level (Llama-3.1-8B + Qwen-2.5-7B):

| Concurrency | Throughput | TTFT p50 | TTFT p99 |
|---:|---:|---:|---:|
| 1 | 84.0 tok/s | 33.4 ms | 36.2 ms |
| 8 | 269.9 tok/s | 150.3 ms | 163.0 ms |
| 32 | 812.2 tok/s | 381.8 ms | 397.1 ms |

**Caveat (per `results/CORRECTIONS.md` C2):** TTFT here is "time to first SSE frame," not real first-token. The throughput numbers ARE end-to-end and reliable; the TTFT numbers underestimate by ~30 ms (network RTT). For Gate 1 the harness gets a TTFT-fix patch.

**Why throughput is lower than Phase 1 single-model baselines:** this is alternating multi-model at small batch (25 reqs). Each engine sees ~half the offered load. Phase 1 vLLM single-model A100 hit 5,334 tok/s at c=128 with 200 reqs — that workload spends more time in steady state.

## Per-model accounting (from `status_after.json`)

- **Llama-3.1-8B-Instruct:** 40 requests served, router-internal avg latency 27.8 ms, healthy, uptime 104 s
- **Qwen-2.5-7B-Instruct:** 37 requests served, router-internal avg latency 22.3 ms, healthy, uptime 58 s
- **Admission controller:** 77 admitted (75 bench + 2 smoke), 0 rejected, 0 timed_out

Qwen's shorter uptime (58 s) shows the serial-load discipline: Llama loaded first at 00:21:09 and started serving smoke at ~00:22:00; Qwen registered at ~00:22:43 and served its first request at ~00:22:47.

## What this validates

| Component | Status |
|---|---|
| PR #16 dep pins (transformers<5, numpy<2.3) | survived a clean install on a fresh pod |
| PR #16 per-engine log file capture | both engine logs present in `engine_logs/`, 121 + 113 KB |
| PR #21 session reuse + sock_read timeouts | harness ran 75 requests without engine drift |
| PR #22 circuit breaker (R4) | not triggered (no stalls in this run); code path alive but untested on this pod |
| PR #22 late `response.prepare()` | every stream returned cleanly |
| PR #23 router request-ID trace logs | every request has `req_id=...` ENTER/EXIT line in server.log |
| PR #25 R5 phase-abort | not triggered (no stalls); guards against future incidents |
| `gate0_multi_model.yaml` config | served two engines on 80 GB at 0.35+0.35 util |

## Artifacts in this directory

- `bench.log` — per-request START/DONE lines from PR #21 + concurrency summaries
- `server.log` — infergrid serve, including PR #23 `req_id=... EXIT N` lines for every request
- `engine_logs/infergrid_engine_vllm_*.log` — full vLLM stdout+stderr per engine (PR #16, ~234 KB combined). For the next incident.
- `smoke.jsonl` — both model smoke responses
- `status_before.json` / `status_after.json` — router snapshots
- `prometheus_dump.txt` — metrics including admission gauges
- `gpu_trace.csv` — 1 Hz GPU samples for the run
- `gate0_multi_model.yaml` — config used (same as Gate 0)
- `nvidia_smi_final.txt`, `pip_freeze.txt`, `git_head.txt` — env snapshot

## Gate 1 readiness

Per the L3 god-planner pre-flight checklist:
- [x] main contains PR #16/#21/#22/#23 (Gate 0.6 ran from main at `a610a14`)
- [x] smoke_bench passes locally (PR #24)
- [x] Multi-model bench harness completes on real vLLM
- [x] Engine log capture wired
- [ ] H100 SXM5 spot price ≤ $4/hr (verify before provisioning)
- [ ] Hard cost cutoff at $10

**Gate 1 is unblocked.** Recommended next: spin H100 SXM5 with `configs/gate1_admission.yaml` (Arm A) and `configs/gate1_admission_off.yaml` (Arm B), use `scripts/gate_pod_bootstrap.sh`. Expected wall ~1.8 h, cost ~$7.30, hard cutoff $10.
