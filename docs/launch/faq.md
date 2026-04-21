# KVWarden — launch FAQ

## Why not just use Modal / Replicate / Runpod?

Those are managed inference clouds. You hand them your model and your traffic, they return tokens. KVWarden is the opposite: you keep the GPUs, you keep the traffic, you keep the model weights. There are three reasons teams stay on-prem or in their own cloud account and still need tenant fairness:

1. **Data residency.** Healthcare, finance, EU customers, government — the traffic can't leave your perimeter. A managed cloud is a non-starter.
2. **Custom-model support.** If you're serving a fine-tuned checkpoint, a proprietary architecture, or a quantization the managed provider doesn't support, you're running vLLM/SGLang yourself anyway. KVWarden makes that setup multi-tenant without you writing the fairness layer.
3. **Cost-at-scale.** Managed per-token pricing is fine at 10 RPS. At 1,000 RPS sustained, owning an A100 or H100 is cheaper inside six months. Once you own the GPU, you need the orchestration.

KVWarden is for the team that answered "yes" to any of those and then hit the noisy-neighbor problem on their own box.

## Why not upstream this to vLLM as a PR?

Per-tenant fairness is a middleware concern, not an engine concern — vLLM intentionally stays policy-agnostic about who a request belongs to, and merging a tenant model into the engine would either get rejected on scope grounds or bloat the engine with a responsibility it correctly refuses to own. The budget gate sits one layer up, where it can see the tenant identity the engine doesn't want to know about.

## Why not Dynamo or llm-d?

Both are excellent and both require Kubernetes. Dynamo v1.0 is NVIDIA's datacenter-shaped system; llm-d is the CNCF sandbox project aimed at pool-of-GPUs deployments. If you're running 50+ GPUs across multiple nodes, use one of those. If you're running 1–4 GPUs on a single box and your ops team does not want to learn k8s this quarter, KVWarden is the shape of tool that fits. The axis isn't "better/worse" — it's datacenter vs. single-node.

## Is this production-ready?

Honestly: no, not at scale yet. Here's the exact state. v0.1.2 on PyPI. 153 unit tests passing in ~10 seconds on CPU. The rate-limit mechanism is empirically validated at N=2 and N=6 tenants on A100 + Llama-3.1-8B + vLLM 0.19.1, with an N=8 CONFIRM and a Llama-3.1-70B TP=4 CONFIRM where the code path runs cleanly but the engine wasn't starved at our offered load. What I have not yet shipped: chaos testing, graceful engine-crash handling under active traffic, 7-day soak, or a real multi-week production deployment at a partner. I'm looking for beta users who will run it against real traffic and tell me where it breaks. File an issue with prometheus_dump.txt + server.log — that's the data I need.

## What does "tenant" mean here?

It's whatever partition key you want. Requests are routed by the `X-Tenant-ID` HTTP header, and the TenantManager treats each unique value as an independent bucket with its own token-bucket rate limit, concurrency cap, and priority weight. In practice that tends to be one of: an API key (per-customer fairness), a team identifier (per-internal-team fairness at a platform company), or a user ID (per-user fairness inside a single product). You pick the partition; KVWarden enforces isolation across it. The YAML lets you override defaults per-tenant — e.g. a paying customer can get a larger burst or higher sustained RPM than a free-tier tenant.

## Does it work with X (custom engine)?

Today: vLLM and SGLang, both via subprocess + HTTP proxy. The engine-adapter surface is public and lives at `src/kvwarden/engines/` — it's an abstract base class plus two concrete adapters at 277 LOC total, and a new engine is typically a few hundred lines. TensorRT-LLM and Llama.cpp are the two I get asked about most; neither is merged yet, both are tractable. If you want to adapt a proprietary engine, the API surface you need to implement is small: subprocess launch, health check, HTTP proxy of the completion endpoint, and shutdown. PRs welcome.

## Can I use this on CPU only?

No. KVWarden requires CUDA because the engines it wraps require CUDA. vLLM and SGLang both target GPU inference; the CPU path in either is a debug fallback, not a real serving surface. If you're doing CPU inference, look at llama.cpp directly — tenant fairness at that scale typically isn't the bottleneck that matters. If you're doing ROCm / AMD, the answer is "pending" — vLLM has ROCm support but we haven't validated KVWarden on it.

## How do I hand-wave the H100 vs A100 starvation delta if someone asks?

The honest one-liner: *A100 at 32 RPS flooder is the saturation regime where FIFO starves and the mechanism is load-bearing. H100 at the same offered load has enough headroom that FIFO doesn't starve, so the TTFT delta shrinks — the mechanism is exercised but not stressed. Gate 2.1b reruns H100 at 128 RPS or N=16 to force saturation, and we'll publish that number next.* That's the full answer, and it's better than a tidier one because it names the exact axis — offered-load-to-capacity ratio — that determines whether fairness is load-bearing. If the questioner is sophisticated, they'll recognize that framing as correct. If they're not, the phrase "H100 has more headroom so FIFO doesn't starve as badly" is the five-word version.

## What happens when a tenant exceeds their quota?

They get a 429 Too Many Requests response, and the `tenant_rejected` Prometheus counter increments with a `{tenant_id}` label so you can alert on it. The rejection happens at the budget gate, before the engine is involved — so a misbehaving tenant cannot saturate the engine queue regardless of how much load they offer. The bucket refills at `rate_limit_rpm / 60` tokens per second and caps at `rate_limit_burst` tokens, so a tenant can burst up to `rate_limit_burst` requests immediately after a quiet period and then sustains at `rate_limit_rpm` per minute. This is the mechanism that holds the quiet-tenant p99 flat in the hero bench.

## How does pricing / licensing work?

MIT-licensed, pip-installable, free to run. There is no paid tier today. If you want engineering support, a deployment review, or a custom engine adapter, reach out — but the code is and will stay open. If I raise and monetize later, the self-hosted OSS path stays free; any paid offering would be a managed version or a support contract, not a license change. That commitment is in writing now because a rug-pull would destroy the exact thing I'm building.
