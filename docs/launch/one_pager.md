# KVWarden — one-pager

**Positioning.** KVWarden is open-source middleware that gives small teams per-tenant fairness for LLM inference on a single shared GPU, without Kubernetes.

## The problem

Multi-tenant LLM inference on a shared engine is a starvation problem. vLLM's continuous-batch scheduler is tenant-blind by design: once a flooder fills the engine queue, every other user's request sits behind flooder prefills. Every existing solution that fixes this requires Kubernetes (Dynamo, llm-d, Mammoth, AIBrix) or accepts no-fairness (Ollama, vanilla vLLM/SGLang). The "multi-tenant on a small shared box without K8s" cell is empty.

## The wedge

Three things engines cannot do internally:

1. **Per-tenant token-bucket rate limiting at the budget gate** — load-bearing mechanism, validated empirically. Quiet-tenant p99 TTFT under contention: 1,585 ms (FIFO) → 61.5 ms (KVWarden token bucket), 1.14× of the solo baseline, A100 + vLLM 0.19.1, 300 s sustained, n=311. Generalized to N=6 tenants at 61.0 ms aggregate p99 (1.13× of solo).
2. **Multi-model lifecycle on a single GPU** — frequency+recency eviction, hot-swap routing, no K8s.
3. **OpenAI-compatible HTTP API** in front of multiple engines, so app code doesn't change.

Ten lines of YAML. No application code change. Tenants routed by HTTP header.

## Competitive map

| System | K8s required | Multi-tenant fairness | Shape |
|---|:---:|:---:|---|
| NVIDIA Dynamo | Yes | No | Datacenter, NVIDIA-only |
| llm-d (CNCF) | Yes | No | Datacenter pool-of-GPUs |
| Mammoth (Modular) | Yes | No | Datacenter, multi-silicon |
| Gimlet Labs ($92M Series A) | Managed cloud | Yes | Proprietary cloud, multi-silicon compiler |
| Ollama | No | No | Single-node, LRU model eviction |
| Vanilla vLLM/SGLang | No | No | Single engine, one model |
| **KVWarden** | **No** | **Yes (validated)** | **1–4 GPUs, self-hosted** |

Gimlet is the only production competitor with fairness, and they're a managed multi-silicon cloud — a different category from "self-hosted on your GPU." KVWarden is for the team that keeps the hardware.

## Traction and validation

- **PyPI:** `kvwarden 0.1.2` live.
- **Landing page:** kvwarden.org with waitlist.
- **Code:** 4,100 LOC src, 2,900 LOC tests, 153 unit tests passing, CI-gated lint + format.
- **Empirical gate ladder v0.2 — 4 gates, 3 CONFIRM + 1 PASS:**
  - Gate 2.1 (N=8 tenants, A100, Llama-3.1-8B): CONFIRM
  - Gate 2.4 (Mixtral-8×7B MoE, 2×A100 TP=2): CONFIRM with engine-headroom caveat
  - Gate 2.3 (Llama-3.1-70B, 4×H100 TP=4): CONFIRM — mechanism scales 8B→70B and 1-GPU→TP=4; p50 ratio flat (1.03× at 8B → 1.07× at 70B)
  - Gate 2.2 (mixed prompt-length distribution): PASS
- **Total compute spend across full experimental arc:** ~$17 on RunPod, self-funded.

## Honest caveats

Validated: A100 starvation regime at 32 RPS flooder, 2-tenant and 6-tenant, vLLM 0.19.1, Llama-3.1-8B. Mechanism code path exercised cleanly at 70B TP=4 and N=8. Not yet validated: saturated-H100 regime (Gate 2.1b on deck, bumps flooder to 128 RPS or N=16 to force H100 starvation). Not yet shipped: 7-day soak, chaos testing, production design-partner deployment. This is v0.1, not v1.0. Beta users wanted.

## Use of funds — $500K–$1.5M pre-seed

12–18 months runway to convert the validated mechanism into production-ready middleware with one lighthouse customer:

- **Senior infra hire (~$300K loaded, 12 mo):** chaos/failure-mode engineering, multi-engine routing, production observability, engine-adapter surface hardening.
- **One paid design partner (~$60K credits + integration time):** their traffic becomes the soak test; their failure modes become the roadmap; their logo becomes the reference.
- **Compute (~$30K/yr):** Gate 2.1b through Gate 2.5 (32K long-context fairness) plus on-demand H100 SECURE for frontier benches.
- **Founder runway + contractor design (~$180K):** focused solo builder plus one design contractor for the LP and CLI polish.

Lower end ($500K) buys ~12 months solo + design partner. Upper end ($1.5M) buys the senior hire and extends to 18 months.

## The ask

Not formally raising today. Talking to angels and pre-seed funds who care about the inference infra gap between Ollama and Dynamo/llm-d and want to see the N=16 saturated-H100 number when it lands. If that's you, happy to share the full gate-ladder writeup and a live demo of the hero bench reproducing on a fresh RunPod pod in ~20 minutes.

Contact: Shrey Patel, patelshrey77@gmail.com. Repo: github.com/coconut-labs/kvwarden. Waitlist: kvwarden.org.
