# Show HN: InferGrid — your quiet tenant doesn't have to lose to the noisy one

A single A100. One Llama-3.1-8B engine. Two clients sharing it: a flooder hammering at 32 requests/sec, and a quiet user trickling in 1 request/sec. On vLLM 0.19.1 with FIFO scheduling and no per-tenant fairness lever, the quiet user's p99 time-to-first-token over a sustained 300-second bench is **1,585 ms** — 29× their solo TTFT on the same hardware.

Add InferGrid in front of vLLM and apply a per-tenant token-bucket rate limit at the budget gate. The quiet user's p99 TTFT becomes **61.5 ms**, **within 1.14× of solo baseline**. Across the 29 steady-state 10-second windows after the JIT warmup, every window's quiet p99 lands between 36 ms and 65 ms. The flooder gets 429'd above its quota; the quiet user is essentially unaware the flooder exists.

The fix is ten lines of YAML. No code change in your app. Multi-tenant inference on a shared engine doesn't have to mean noisy-neighbor TTFT roulette.

---

## The chart

![Quiet-tenant TTFT under noisy-neighbor contention — three arms on log scale; bottom panel shows the single JIT warmup window outlier and the clean 290-second steady state](figures/launch_hero_chart.png)

*Top panel:* quiet-tenant TTFT p99 across three arms on vLLM 0.19.1, 300 s sustained, n=320/321/311 quiet samples (log scale). Arm 0 solo baseline 53.9 ms; Arm 1 InferGrid FIFO without rate-limit, under the flooder, 1,585 ms (29× solo); Arm 5b InferGrid with token-bucket rate-limit, post-warmup, 61.5 ms. Net: a **26× reduction from Arm 1**, leaving the quiet tenant **within 1.14× of its solo baseline**.

*Bottom panel:* per-window quiet TTFT p99 over the full 300-second Arm 5b bench. The first 10 s window has a single JIT-compile outlier (max 5,818 ms — kernel warmup). All 29 subsequent steady-state windows land in 36-65 ms, every one of them. This panel is the rebuttal to "you hid a transient in the aggregate." We didn't hide it; we excluded the warmup window per [CORRECTIONS C7](../../results/CORRECTIONS.md) — the same way the original Arm 5 sliding-window arm had a documented warmup caveat.

A note on the fold-change numbers in this post: **29× is the starvation factor on the quiet tenant** when InferGrid runs FIFO without its rate-limit lever (1,585 / 53.9). **26× is how much of that the token-bucket lever recovers** (1,585 / 61.5). The remaining 1.14× is residual contention vs solo baseline.

*If you read an earlier version of this post quoting 523× / 387× / 1.35×, those numbers were honest for the original 5-arm Gate 2-FAIRNESS bench on **vLLM 0.8.5** in a 120-second window where the queue never reached steady state (Arm 1's per-window p99 grew monotonically from 3.1 s at t=5 to 28.8 s at t=115). vLLM v1's continuous batcher absorbs cold-start backpressure better, so the steady-state starvation on the version users install today is 29× rather than 523×. Both numbers are correct for their respective benches; the v3 numbers in this post are what you'll measure if you `pip install vllm` today.*

---

## The mechanism — why this fix lives where it lives, and why simpler ones failed

vLLM's continuous-batch scheduler is **tenant-blind by design**. Every admitted request goes into the same engine queue and gets batched by arrival, not by who sent it. Once the flooder fills the engine queue, the quiet user's request sits behind 30 flooder prefills no matter what header it carried.

**Why naive admission caps don't work.** Our previous experiment (Gate 1.5, on H100 SXM5, 16,000 requests across four concurrency steps with sustained pressure) showed that capping concurrent admissions on a single-model single-engine workload doesn't lower tail latency — vLLM's own scheduler absorbs overload as well as a coarse upstream cap does, and at moderate concurrency the cap actively *adds* queue-wait. Raw numbers are in `results/gate1_5_20260419/GATE1_5_OUTCOME.md`. Lesson: admission cap is the wrong lever for tail TTFT.

**Why DRR alone doesn't work.** We tried Deficit Round Robin admission priority next (Arm 3, then Arm 4 with a tight cap=16). DRR successfully reorders the *admission* queue by tenant deficit, but the saturation lives one layer down — inside vLLM's continuous batcher. The reordering never propagates. Arm 4's tight cap made things worse: slot release rate (16 / ~45s avg hold ≈ 0.36 slot/s) couldn't service even the quiet user's 1 RPS without queueing.

**What works: rate-limit at the budget gate, *before* engine admission.** If the flooder is 429'd at the rate-limit before its requests ever reach vLLM's queue, the queue stays composed of quiet's traffic plus only the flooder's quota share. The engine never saturates. The quiet user's TTFT stays at baseline.

The token-bucket variant matters: a 60-second sliding window takes ~19 seconds to accumulate enough flooder history to trigger 429s, and during that warmup the engine saturates anyway. The token bucket fires from t=0.

```yaml
# configs/quickstart_fairness.yaml — relevant diff vs the default config
+tenant_defaults:
+  max_concurrent_requests: 512
+  rate_limit_rpm: 600          # 10 RPS sustained refill per tenant
+  rate_limit_burst: 10         # 1 second of burst capacity; engages from t=0
+  priority: 1
+  scheduling: drr              # belt-and-suspenders; rate_limit is the heavy lifter
+
+tenants:                       # tenants are matched on the X-Tenant-ID request header
+  - id: noisy
+  - id: quiet
```

That's the diff. No code change in the application — tenants are routed by the `X-Tenant-ID` request header. The `rate_limit_burst` field landed in PR #47.

---

## What we measured honestly — five arms before we trusted the result

The headline number is the end of an arc, not its beginning. We measured the same workload five times before we'd state it.

- **Arm 0 (solo baseline).** Quiet user alone for 240 s, no flooder. p50 = 28.5 ms, p99 = 54.9 ms, n = 262. This is what "fair" looks like.
- **Arm 1 (vanilla vLLM, no fairness).** Flooder + quiet, FIFO admission, no rate limit. Quiet p50 = 15,087 ms; p99 = 28,716 ms. **523× starvation at p99.** This is the problem statement.
- **Arm 3 (DRR with cap=256).** Cap never bound (all admits ≤1 ms in Prometheus). DRR was a no-op. Quiet p99 = 31,970 ms. *Falsified: priority reordering at admission doesn't help when admission isn't queueing.*
- **Arm 4 (DRR with cap=16).** Cap bound hard (1232 admits queued 10–30 s), but vLLM's internal queue still dominated. Flooder mass-timed-out (2821 errors). Quiet p99 = 53,590 ms — *worse* than no fairness. *Falsified: tight cap moves latency without removing it.*
- **Arm 5 (DRR + sliding-window rate limit, 600 RPM).** Steady-state win, but a 30-second warmup transient because the 60s sliding window had to fill before 429s fired. Quiet p99 = 5,378 ms full-bench. *Promising mechanism, defective implementation.*
- **Arm 5b (DRR + token-bucket rate limit, burst=10).** Replaced the sliding window with a token bucket (PR #47). Original 120 s bench: quiet p99 = 74 ms, max = 74.5 ms. **Per-window trace: zero windows with p50 above 50 ms, zero windows with max above 75 ms.** The clean confirm.

The Arm 5 → Arm 5b transition is the one we'd point a hostile reviewer at: same hypothesis, two different rate-limit mechanisms, instrumented identically, and the difference falls cleanly out of the per-window data — five transient windows in Arm 5, zero in Arm 5b. We didn't tune the bench around the result; we changed the mechanism and the result moved as theory predicted.

**Pre-launch v3 re-run (vLLM 0.19.1, 300 s sustained, dedicated single-arm A100 pods).** Reran Arms 0, 1, 5b on the version users install today, with 3× the sample size. Quiet p99 numbers came back: solo 53.9 ms (n=320), Arm 1 FIFO 1,585 ms (n=321), Arm 5b token-bucket **61.5 ms post-warmup (n=311), within 1.14× of solo**. The lever still does its job — even more cleanly on the newer scheduler. Raw artifacts: `results/gate2_preprint_v3/`. Two latent bugs surfaced and fixed during the re-run as PRs #58 (TOCTOU race in `ensure_model_loaded`) and #59 (`/health` returned 200 OK while engines cold-loading).

**Pre-launch N=6 generalization (Track C, vLLM 0.19.1, 300 s).** Then we asked: does the guarantee scale? Same engine, same flooder, but now 5 quiet tenants instead of 1. Aggregate quiet p99 (post-warmup, n=1,456): **61.0 ms — 1.13× of solo, the same number the 2-tenant case gave us**. Per-tenant range across the 5 quiet tenants: 56-65 ms p99. Worst tenant (`quiet_2`) lands at 1.21× of solo. The token bucket holds across N. Full writeup: `results/gate2_n6_v3/GATE2_N6_OUTCOME.md`.

Total spend across the full series: **~$17** (original $1.70 + v3 single-arm pods + Track B histogram + Track C N=6 + Track D inconclusive). Raw artifacts in `results/gate2_fairness_20260419/` (original 120 s), `results/gate2_preprint_v3/` (300 s 2-tenant preprint), and `results/gate2_n6_v3/` (300 s 6-tenant generalization).

---

## What this is NOT

We get to make exactly one claim from this data, and we want to be precise about which one:

- **Single hardware, single model, single engine.** A100-SXM4 80GB, Llama-3.1-8B-Instruct bf16, vLLM. Smaller models, quantized models, multi-GPU tensor-parallel, H100, consumer GPUs, SGLang, TensorRT-LLM — not measured. Multi-engine fairness is a separate experiment that hasn't shipped.
- **Synthetic two-tenant workload.** A constant 32 RPS flooder against a 1 RPS quiet user is a clean stress, not real production traffic. We have not tested >2 tenants, bursty arrivals, mixed prompt-length distributions, or non-uniform output lengths.
- **300-second bench with n=311-321 quiet requests per arm.** Sample size is 3× the original 120 s bench; p99 has multiple supporting observations rather than the single-sample-below-the-tail problem of n=113. The hero p99 of 61.5 ms excludes the first 10 s warmup window because of one JIT-compile transient — explicit in [CORRECTIONS C7](../../results/CORRECTIONS.md) and visible in the bottom-panel chart. All 29 steady-state windows have quiet p99 in 36-65 ms. The N=6 generalization (Track C) has n=1,456 post-warmup samples — p99 stable to four significant figures.
- **N=6 only.** We've validated the guarantee at 1, 2, and 6 tenants. N=10+ is a roadmap item, not a current claim.
- **vLLM 0.19.1.** The bench is on the version users will `pip install` today. The original Gate 2-FAIRNESS bench (preserved in `results/gate2_fairness_20260419/`) ran on vLLM 0.8.5 — those numbers are still in the repo as historical evidence.
- **The earlier "scheduling cliff" pitch is gone.** Our previous experiment (Gate 1.5, sustained-pressure powered admission-cap test) decisively falsified the framing that admission caps lower tail latency on single-model single-tenant workloads. The mechanism in this post is *specifically* for multi-tenant contention. If your workload is one tenant, this fix does nothing for you.
- **No claims about cost, throughput, or QoS guarantees beyond what we measured.** Throughput numbers and steady-state behavior are reported; we don't extrapolate.

---

## Try it

```bash
# Install (PyPI placeholder + GitHub fallback)
pip install infergrid               # or: pip install git+https://github.com/coconut-labs/infergrid

# Start it on top of your vLLM model
infergrid serve --config configs/quickstart_fairness.yaml

# Hit it as two tenants
curl localhost:8000/v1/completions -H "X-Tenant-ID: noisy" \
  -d '{"model":"llama31-8b","prompt":"...","stream":true}'
curl localhost:8000/v1/completions -H "X-Tenant-ID: quiet" \
  -d '{"model":"llama31-8b","prompt":"...","stream":true}'

# Watch the rate-limit fire (and the engine queue stay composed)
curl localhost:8000/metrics | grep -E "tenant_rejected|admission_queue_depth"
```

Repo: **github.com/coconut-labs/infergrid**. Configs reproducing every arm above: `configs/gate2_fairness_*.yaml`. Full Gate 2-FAIRNESS writeup with all per-window traces and raw CSVs: `results/gate2_fairness_20260419/`.

If the experiment doesn't reproduce on your hardware, file an issue with your `prometheus_dump.txt` and `server.log` — that's worth more to us than a star.

---

## Acknowledgments

InferGrid sits on top of vLLM and SGLang; both teams' work is what makes any of this possible. Shadow-review thanks to **Jay** for repeatedly red-teaming framings I'd over-anchored on (including the original "scheduling cliff" pitch this post replaces). Compute was self-funded on RunPod (~$1.70 for this experiment, ~$12 across the full series).

— Shrey
