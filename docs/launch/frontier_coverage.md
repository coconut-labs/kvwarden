# Why kvwarden generalizes: one mechanism, three architectures

## The claim

The kvwarden admission layer is architecturally scale-invariant. The same per-tenant token-bucket rate limit at the admission gate keeps the quiet-tenant-to-solo p50 TTFT ratio bounded at 1.07x-1.94x across an 8B dense model on one GPU, a 70B dense model sharded across four GPUs, and a mixture-of-experts model sharded across two. The upper-end 1.94x on Mixtral is driven by engine-load delta (1 RPS solo vs 19 RPS offered), not by admission-layer asymmetry — flood_p50 and quiet_p50 in that arm are 92.9 ms and 93.1 ms, i.e. tenants are treated identically.

This is not a claim that we ran three models. It is a claim about what kind of mechanism kvwarden is — a property of the request-admission control plane, not of any specific model architecture or sharding topology.

## The bench matrix

| Model | Arch | Params | GPU(s) | Solo p99 (ms) | FIFO p99 under flooder (ms) | Token-bucket p99 under flooder (ms) | Fairness ratio (TB p50 / solo p50) | Starvation delta (FIFO p99 / solo p99) |
|---|---|---:|---|---:|---:|---:|---:|---:|
| Llama-3.1-8B-Instruct | dense | 8B | 1x A100-SXM4 80GB | 53.9 | 1585.0 | 61.5 [1] | 1.07x [2] | 29.4x |
| Llama-3.1-70B-Instruct | dense, TP=4 | 70B | 4x H100 SXM 80GB | 766.8 [3] | [not measured: FIFO-contended arm omitted; 8B FIFO carries the starvation baseline] | 1238.6 [3] | 1.07x | [not measured] |
| Mixtral-8x7B-Instruct-v0.1 | MoE (8 experts, top-2), TP=2 | 46.7B total / 12.9B active | 2x H100 SXM 80GB | 84.9 | 122.7 [4] | 109.7 | 1.94x [5] | 1.45x [4] |

[1] Post-warmup, first 10-second JIT window excluded per `results/CORRECTIONS.md` C7. Aggregate including warmup: 1230.2 ms.
[2] Steady-state 8B p50 is ~30 ms vs 28.5 ms solo; 1.07x uses post-warmup steady-state p50s.
[3] 70B aggregate p99 is warmup-tail-dominated by per-tenant cold-starts (solo per-tenant p99 spread 29.9x, contended 26.4x — ratio carries, absolute includes ~2s cold-start).
[4] Mixtral FIFO did not starve: 16-RPS flooder + 128-token outputs on 2x H100 TP=2 left engine headroom. Property of the workload, not of kvwarden. This gate validates fairness holds on MoE + TP, not MoE resilience under saturation.
[5] 1.94x is engine-utilization delta (1 RPS solo vs 19 RPS offered), not admission-layer asymmetry — in the contended arm flood_p50 ≈ quiet_p50 (92.9 vs 93.1 ms).

## Why "scale-invariant" is the right framing

The mechanism lives before the engine boundary. Admission decisions happen on request arrival and tenant identity — not on KV cache state, attention shape, expert routing, or tensor-parallel topology. A tenant's token bucket is decremented by request count; the request is 429'd if the bucket is empty; admitted requests enter the engine queue alongside every other admitted request. Nothing in that path inspects what the engine is serving.

So engine-internal details are orthogonal to whether a fair admission policy produces fair p99 outcomes. Dense vs MoE, TP=1 vs TP=4, 8B vs 70B — routing, all-reduce, and model dispatch all happen inside the engine, after admission. If the mechanism depended on those internals, we'd expect a data point to fall out of the matrix: an MoE expert-hot-spot causing per-tenant p50 divergence, a TP-shard imbalance propagating head-of-line blocking, a KV-pressure regime where the rate-limit doesn't fire in time. None of those happened across the three gates.

## What's NOT proven by this matrix

The matrix does NOT prove:
- **N > 8 tenants.** Published generalization caps at N=8 (8B). Gate 2.3 ran N=4 at 70B TP=4, Gate 2.4 ran N=4 at Mixtral TP=2. N=16 or N=32 is unmeasured — the Gate 3 bet in the 90-day plan.
- **Long-context prompts.** All three used short fixed prompts (64-128 token outputs). Mixed-prompt-length (Gate 2.2) has not been crossed with 70B or MoE.
- **Multi-node sharding.** All three ran single-node. TP=4 crossed GPU boundaries on NVLink within one H100 SXM box; 2+ nodes via InfiniBand is untested.
- **Zero cost-of-fairness.** Admitted traffic is capped at the token-bucket rate — aggregate tokens/second is lower than unregulated FIFO. Fairness at a fixed cost, not free fairness.
- **Engine saturation on Mixtral.** The FIFO arm didn't starve at this flooder rate. Re-proving starvation-recovery on MoE needs higher flooder RPS or a TP=1 engine.

Next measurement: Gate 3 (N>=16 tenants) is the 90-day bet.

## Reproduce

Each flavor needs a kvwarden server running with the flavor config, plus a vllm 0.19.1 engine loaded with the matching model. Wait for `/health` to return 200 before running the bench (race-fix in PR #58).

```bash
# 8B dense on 1x A100 (hero, preprint v3)
kvwarden serve --config configs/gate2_fairness_token_bucket.yaml --port 8000
kvwarden bench reproduce-hero --flavor 2tenant    # or --flavor n6, n8

# 70B dense on 4x H100 TP=4 (Gate 2.3) — no reproduce-hero flavor yet
kvwarden serve --config configs/gate23_fairness_70b_tp4.yaml --port 8000
# Bench args in results/gate23_70b_tp4_20260421/GATE23_70B_TP4_OUTCOME.md

# Mixtral MoE on 2x H100 TP=2 (Gate 2.4) — no reproduce-hero flavor yet
kvwarden serve --config configs/gate24_fairness_mixtral_tp2.yaml --port 8000
# Bench args in results/gate24_mixtral_20260421/GATE24_MIXTRAL_OUTCOME.md
```

Only the 8B flavor has a single-command reproduce-hero path today. 70B and Mixtral reproduce wrappers are a roadmap item, not ship-time.

## Bench provenance

| Flavor | Bench directory | Config YAML |
|---|---|---|
| 8B dense, 1x A100 | [`results/gate2_preprint_v3/`](../../results/gate2_preprint_v3/) | [`configs/gate2_fairness_token_bucket.yaml`](../../configs/gate2_fairness_token_bucket.yaml) |
| 70B dense, TP=4, 4x H100 | [`results/gate23_70b_tp4_20260421/`](../../results/gate23_70b_tp4_20260421/) | [`configs/gate23_fairness_70b_tp4.yaml`](../../configs/gate23_fairness_70b_tp4.yaml) |
| Mixtral 8x7B MoE, TP=2, 2x H100 | [`results/gate24_mixtral_20260421/`](../../results/gate24_mixtral_20260421/) | [`configs/gate24_fairness_mixtral_tp2.yaml`](../../configs/gate24_fairness_mixtral_tp2.yaml) |

Each results directory contains: `summary.json` per arm, `tenant_*.csv` per tenant, `server.log`, `pip_freeze.txt` for the engine pin, and the gate's `*_OUTCOME.md` with methodology and caveats. The 8B hero numbers also appear in `results/CORRECTIONS.md` C7, which documents the vLLM 0.8.5 → 0.19.1 version delta and the 10-second warmup-window exclusion all three gates use.
