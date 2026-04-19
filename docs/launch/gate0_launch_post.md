# Show HN: InferGrid — your quiet tenant doesn't have to lose to the noisy one

A single A100. One Llama-3.1-8B engine. Two clients sharing it: a flooder hammering at 32 requests/sec, and a quiet user trickling in 1 request/sec. On vanilla vLLM, the quiet user's p99 time-to-first-token is **28,716 ms** — a 523× starvation versus the same user alone on the same hardware.

Add InferGrid in front of vLLM and apply a per-tenant token-bucket rate limit at the budget gate. The quiet user's p99 TTFT becomes **74 ms**, within 1.35× of solo baseline. Across all twelve 10-second windows of a 120-second bench, the quiet user's max TTFT is 74.5 ms. The flooder gets 429'd above its quota; the quiet user is essentially unaware the flooder exists.

The fix is ten lines of YAML. No code change in your app. Multi-tenant inference on a shared engine doesn't have to mean noisy-neighbor TTFT roulette.

---

## The chart

3-bar comparison, **log scale on the y-axis** (the values span three orders of magnitude). Y-axis: quiet-tenant TTFT p99 in milliseconds. Three bars left to right:

- **Arm 0 — solo baseline (no contention):** 54.9 ms
- **Arm 1 — vanilla vLLM under flooder:** 28,716 ms
- **Arm 5b — InferGrid token-bucket rate limit:** 74.2 ms

Annotations: arrow from Arm 1 to Arm 5b labeled **"387× reduction in p99 starvation"**; arrow from Arm 5b to Arm 0 labeled **"1.35× of solo baseline"**.

Inset (or second small panel underneath): the per-window trace — 12 ten-second windows, two lines (Arm 5 sliding-window in red showing 5 transient spikes >1000 ms; Arm 5b token-bucket in green never exceeding 75 ms). This panel is the proof there's no warmup transient hiding in the average.

Workload caption under the chart, in 8pt: *Single NVIDIA A100-SXM4 80GB. Llama-3.1-8B-Instruct, bf16, max_model_len=4096. Flooder = 32 req/s constant, Quiet = 1 req/s. 120 s sustained per arm; n=113 quiet requests per contended arm, n=262 for solo baseline.*

A note on the two fold-change numbers in this post: **523× is vanilla vLLM's starvation factor on the quiet tenant** (28,716 / 54.9). **387× is how much of that InferGrid recovers** (28,716 / 74.2). The remaining 1.35× is residual contention vs solo baseline.

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
- **Arm 5b (DRR + token-bucket rate limit, burst=10).** Replaced the sliding window with a token bucket (PR #47). Quiet p99 = 74.2 ms, max = 74.5 ms. **Per-window trace: zero windows with p50 above 50 ms, zero windows with max above 75 ms.** The clean confirm.

The Arm 5 → Arm 5b transition is the one we'd point a hostile reviewer at: same hypothesis, two different rate-limit mechanisms, instrumented identically, and the difference falls cleanly out of the per-window data — five transient windows in Arm 5, zero in Arm 5b. We didn't tune the bench around the result; we changed the mechanism and the result moved as theory predicted.

Total spend across all six arms: **$1.70**. Raw artifacts (CSVs, server logs, engine logs, Prometheus dumps, GPU traces) are in `results/gate2_fairness_20260419/`.

---

## What this is NOT

We get to make exactly one claim from this data, and we want to be precise about which one:

- **Single hardware, single model, single engine.** A100-SXM4 80GB, Llama-3.1-8B-Instruct bf16, vLLM. Smaller models, quantized models, multi-GPU tensor-parallel, H100, consumer GPUs, SGLang, TensorRT-LLM — not measured. Multi-engine fairness is a separate experiment that hasn't shipped.
- **Synthetic two-tenant workload.** A constant 32 RPS flooder against a 1 RPS quiet user is a clean stress, not real production traffic. We have not tested >2 tenants, bursty arrivals, mixed prompt-length distributions, or non-uniform output lengths.
- **120-second bench with n=113 quiet requests per contended arm.** That makes our p99 estimate thin (~1 sample below it). Our defense is the per-window distribution and the maximum observed value (74.5 ms over 113 requests) — both consistent with the p99. For preprint-grade claims we'd rerun at 5+ minutes per arm; for a launch claim "the quiet user's worst observation across the full bench was 74.5 ms" is what we'll stand on. Note that the Arm 0 baseline ran 240 s (n=262) so its p99 is tighter than the contended arms' p99; the gap moves slightly in our favor under matched samples, not against.
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
