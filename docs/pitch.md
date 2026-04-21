# KVWarden — Investor Summary

## Problem

When two clients share a single inference engine on a single GPU — the canonical setup for any small team without datacenter infrastructure — the engine has no concept of *who* is asking. A noisy neighbor running 32 RPS degrades a quiet user running 1 RPS to **29× their solo TTFT at p99** (1,585 ms vs 53.9 ms solo). Vanilla vLLM, vanilla SGLang. The engines don't know about tenants.

Every existing fix is either (a) a Kubernetes-mandatory datacenter orchestrator (Dynamo, llm-d, Mammoth) or (b) Ollama-class single-tenant tooling. The 1-4 GPU, multi-tenant developer segment is unserved.

## Solution

**KVWarden** is middleware that sits in front of vLLM/SGLang and adds the one thing engines structurally cannot do: per-tenant fairness. One pip install:

```
pip install kvwarden
kvwarden serve --config configs/quickstart_fairness.yaml
```

A token-bucket rate limit at the budget gate (10-line YAML config) brings the same degraded quiet user to **61.5 ms p99 — within 1.14× of solo baseline** under the same flooder. The quiet user is essentially unaware the flooder exists.

![Quiet-tenant TTFT under noisy-neighbor contention](figures/launch_hero_chart.png)

Source: `results/gate2_preprint_v3/` (300 s sustained per arm, vLLM 0.19.1, n=320/321/311 quiet samples). Methodology: hero p99 excludes first 10 s warmup window (single JIT-compile transient; all 29 subsequent steady-state windows have p99 36-65 ms). See [CORRECTIONS C7](../results/CORRECTIONS.md) for the version-pin caveat. Original 5-arm 120 s experiment at vLLM 0.8.5 in `results/gate2_fairness_20260419/`.

KVWarden also ships:
- **Multi-model lifecycle management** — frequency+recency eviction (not LRU), hot-swap routing, no-K8s
- **OpenAI-compatible HTTP API** in front of multiple engines, so application code doesn't change
- **Per-tenant DRR admission priority + Prometheus metrics** for production observability

## Market Validation

- **Gimlet Labs** — $92M Menlo Ventures Series A, eight-figure revenues, top-3 frontier lab + top-3 hyperscaler customers. Validates inference orchestration as a category.
- **Modular** — acquired BentoML, launched Mammoth, valued at $1.6B. Datacenter K8s play.
- **NVIDIA Dynamo v1.0** — shipped March 2026 with 18+ deployment partners. NVIDIA-only, datacenter-only.
- **llm-d** — entered CNCF Sandbox (March 2026), backed by Red Hat, Google Cloud, IBM, CoreWeave. K8s-mandatory.

All require Kubernetes or managed cloud. The 1-4 GPU multi-tenant developer segment is unserved.

## Traction

| Item | State |
|---|---|
| Core implementation | 2,872 LOC src + 144 unit tests passing (WorkloadRouter, AdmissionController, TenantManager, CacheManager) |
| Honest TTFT measurement harness | Rebuilt mid-project after shadow review (PRs #28/#31); see `results/CORRECTIONS.md` |
| Single-model admission cap claim | **Falsified** by Gate 1.5 (16,000 req H100 SXM5, B/A=1.04× — engines absorb overload as well as a coarse upstream cap does) |
| Per-tenant fairness claim | **Confirmed** by Gate 2-FAIRNESS Arm 5b (vLLM 0.8.5, 120s): 523× starvation → 74ms p99 (1.35× of solo). **Re-validated by preprint v3 (vLLM 0.19.1, 300s steady-state)**: Arm 1 = 1,585ms, Arm 5b = 61.5ms post-warmup → 26× reduction, **1.14× of solo**. |
| Total empirical compute spend | ~$13 across 11 GPU runs (A100/H100, RunPod) |
| Ready-to-ship launch artifacts | Launch post (1559w, advisor-pressure-tested), hero chart PNG, quickstart yaml, tuning guide doc |

## Differentiation

| | K8s Required | Multi-Model | Per-Tenant Fairness | Target Scale |
|---|:---:|:---:|:---:|---|
| **KVWarden** | **No** | **Yes (freq+recency)** | **Yes (token bucket + DRR)** | **1-4 GPUs, multi-tenant** |
| Dynamo v1.0 | Yes | Yes | No | Datacenter |
| llm-d v0.5 | Yes | 1 model/pool | No | Datacenter |
| Mammoth (Modular) | Yes | Yes | No | Datacenter |
| Ollama | No | LRU only | No | Single-tenant single-node |
| Vanilla vLLM/SGLang | No | One model per process | No | Single-node |

KVWarden is the only project combining no-K8s deployment with **measured per-tenant fairness** on shared engines.

## Honest scope (what we are NOT claiming)

- Single hardware: A100-SXM4 80GB. Smaller models, multi-GPU tensor-parallel, H100 contended (vs uncontended), consumer GPUs — not measured.
- Single engine: vLLM. SGLang has the matching adapter shipped but the fairness experiment hasn't been re-run on it.
- Synthetic two-tenant workload (32 RPS flooder vs 1 RPS quiet). Real multi-tenant with >2 clients, bursty arrivals, mixed prompt lengths — not measured at this scale.
- Per-arm n=113 quiet samples. Tight at p99; defense is the per-window distribution + max observation. Preprint-grade re-runs at 5+ minutes per arm are queued for post-launch.

## Ask

Seed funding to convert validated tenant-fairness pitch into a product:

- **Weeks 1-4 (post-launch):** Multi-model contention experiment (Gate 2-lite scoped but not yet run); enterprise-grade tenant tiers (gold/silver/bronze) on top of existing budget infra.
- **Weeks 5-8:** KV cache tiering integration (LMCache); larger-scale fairness benchmarks with N>2 tenants; SGLang adapter parity validation.
- **Weeks 9-12:** Reference deployments with 3-5 design partners; arXiv preprint covering the full 7-arm Gate 2-FAIRNESS experimental arc; public beta announcement.

## Team

**Shrey Patel** — Founder. Built the core system, ran all empirical work, authored the gap analysis and tuning guide. Prior background informs the "engineer-grade rigor over marketing veneer" approach taken throughout the project.

## Why now

The frontier-engine race (vLLM 0.x, SGLang 0.x) has converged on continuous batching. Engine-internal scheduling is approaching local optima — the next gains come from layers above. KVWarden's empirical work shows that *per-tenant* fairness specifically is a layer engines structurally can't reach (they have no tenant concept). That niche has no incumbent and no Kubernetes-tax.

---

**Repo:** [github.com/coconut-labs/kvwarden](https://github.com/coconut-labs/kvwarden) · **Hero data:** `results/gate2_fairness_20260419/` · **Tuning guide:** `docs/tuning_guide.md` · **License:** MIT
