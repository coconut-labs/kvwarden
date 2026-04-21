# Twitter thread — KVWarden launch

**1/**
A100 + Llama-3.1-8B + vLLM 0.19.1. One flooder at 32 RPS, one quiet user at 1 RPS, sharing the engine for 300 s.

Vanilla FIFO: quiet user's p99 TTFT = 1,585 ms.
With KVWarden's per-tenant token bucket: 61.5 ms. 1.14× of solo baseline.

Ten lines of YAML.

**2/**
The problem: vLLM's continuous-batch scheduler is tenant-blind by design. Once a flooder fills the engine queue, your quiet user sits behind 30 flooder prefills no matter what header their request carried. The engine has no concept of a tenant. Adding one at the app layer is too late.

**3/**
We tried the obvious fixes first and they failed.

- Admission cap alone: vLLM's internal queue absorbs overload as well as a coarse upstream cap. Sometimes worse.
- DRR priority at admission: reordering the admission queue is a no-op when the saturation lives one layer down in the engine's batcher.

**4/**
What works: rate-limit the flooder at the budget gate, BEFORE its requests reach the engine queue. Token bucket, not sliding window — the bucket fires from t=0; a 60 s sliding window takes ~19 s to trigger 429s, during which the engine saturates anyway.

The fix lives where the queue is.

**5/**
The YAML diff:

```
tenant_defaults:
  max_concurrent_requests: 512
  rate_limit_rpm: 600        # 10 RPS sustained refill per tenant
  rate_limit_burst: 10       # 1 s burst; engages from t=0
  priority: 1
  scheduling: drr
tenants:
  - id: noisy
  - id: quiet
```

Tenants matched on X-Tenant-ID header.

**6/**
Install and run:

```
pip install kvwarden
kvwarden serve --config configs/quickstart_fairness.yaml
curl localhost:8000/v1/completions -H "X-Tenant-ID: quiet" -d '{...}'
```

PyPI: v0.1.2. 153 unit tests. 4,100 LOC src, ~3,500 LOC middleware, OpenAI-compatible HTTP API, no Kubernetes.

**7/**
Honest caveats. This is validated at:
- 1× A100-SXM4 80GB
- Llama-3.1-8B bf16, vLLM 0.19.1
- 2 tenants (hero) + 6 tenants (Track C generalization, aggregate p99 61.0 ms, 1.13× of solo)
- 300 s sustained bench, n=311–1,456 post-warmup

**8/**
What's NOT validated: H100 replication under our offered load had smaller deltas (1.05–1.67×) because H100 has enough headroom that FIFO doesn't starve at 32 RPS. The mechanism is exercised, not stressed. Gate 2.1b (H100 at 128 RPS, N=16) is the saturated-regime rerun on deck.

**9/**
Also not proven: "works at 70B." Gate 2.3 ran the same code path on Llama-3.1-70B TP=4 across 4× H100 cleanly — zero engine OOMs, zero NCCL errors, mechanism engages — but the offered-load TTFT delta there is 1.62× because the engine isn't starved. Scale-invariant code path, not scale-invariant starvation.

**10/**
Competitive wedge. Dynamo and llm-d require Kubernetes and target datacenter shapes. Ollama gives you LRU model eviction but no per-tenant fairness. Vanilla vLLM/SGLang is one model per process, no tenant concept. The "multi-tenant on a small shared GPU without K8s" cell was empty. KVWarden fills it.

**11/**
Full launch post with all 5 arms, the per-window traces, the warmup-window caveat, and the 70B replication details:

https://github.com/coconut-labs/kvwarden/blob/main/docs/launch/gate0_launch_post.md

Repo: https://github.com/coconut-labs/kvwarden
Waitlist: https://kvwarden.org

**12/**
What I actually need: adversarial benches. Run it against your real traffic, your real tenant shape, your real prompt distribution. File an issue with prometheus_dump.txt + server.log when it breaks. That's worth more than a star — it's the data that tells me where the mechanism stops holding.
