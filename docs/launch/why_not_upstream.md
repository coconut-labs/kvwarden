# Why not upstream KVWarden to vLLM?

The single question that matters for the pitch. Evidence-backed answer below.

---

## The two-sentence answer

**vLLM maintainers explicitly scope the engine to per-request scheduling policy (FCFS, priority, SJF) and have told contributors proposing tenant-aware fairness — weights, SLA tiers, bearer auth — to keep it in their own fork, because tenant identity and rate-limit state don't belong behind an OpenAI-compatible engine API. Every serious multi-tenant LLM deployment — LiteLLM, Kong AI Gateway, Envoy AI Gateway, vLLM's own production-stack router, llm-d, NVIDIA Dynamo — puts rate limiting and admission control at a proxy layer above the engine, which is also what lets a middleware like KVWarden stay stable across vLLM's 13.8-day release cadence and recurring breaking CLI changes.**

---

## The longer case

### 1. vLLM maintainers have stated the separation of concerns, verbatim

On [PR #31909 — "Add Weighted Fair Queuing scheduler for proportional resource allocation"](https://github.com/vllm-project/vllm/pull/31909) — exactly the kind of tenant-weighted fairness knob KVWarden exposes — vLLM committer Roger Wang (`@ywang96`) replied 2026-01-09:

> "IMO priority and FCFS themselves can satisfy the majority of use cases — plus the new notion of `weight` isn't really OpenAI standard so I'm not sure how much value this adds — plus it'll add non-trivial maintenance overhead. If this is for research purposes, I strongly suggest keeping this in your own fork!"

The author conceded; the PR went stale. This isn't hostile — it's a consistent stance. [Issue #21605 — "fairness heuristics for the batched requests"](https://github.com/vllm-project/vllm/issues/21605) sat four months with zero maintainer engagement and was auto-closed 2026-02-10. [PR #15641 — "adds native FastAPI bearer auth"](https://github.com/vllm-project/vllm/pull/15641) was closed without merge. [PR #38295](https://github.com/vllm-project/vllm/pull/38295) explicitly documents that vLLM's `--api-key` has no effect on non-HTTP paths. The pattern is clear: **vLLM accepts per-request scheduling policy, rejects tenant identity / auth / fairness-by-who**.

### 2. Scheduler-policy vs. tenant-identity — KVWarden is on the right side of the line

A careful skeptic will notice [PR #29366 — SJF Scheduling Policy](https://github.com/vllm-project/vllm/pull/29366) was APPROVED by maintainer `@hmellor` (HuggingFace + vLLM core team) 2025-12-18, and [PR #30545 — OpenAI `service_tier`](https://github.com/vllm-project/vllm/pull/30545) is in active review. Doesn't that contradict the argument? No. Both are **per-request** policy — they use fields already on the request object (prompt length, arrival time, OpenAI `service_tier` hint) with no notion of *who* sent it.

Rate-limit-by-tenant is different in kind: it requires identity, quota state, a refill clock, and a lifecycle that outlives any single request. KVWarden's token bucket fires *before* admission (quiet-tenant p99 TTFT: 1,585 ms vanilla → 61.5 ms with KVWarden). Upstreaming that forces vLLM to own tenancy state, auth config, and quota lifecycle — the exact concerns `@ywang96` flagged as "non-trivial maintenance overhead" that "isn't OpenAI standard."

### 3. Engine-version churn makes in-engine middleware a liability

Per [pypi.org/pypi/vllm/json](https://pypi.org/pypi/vllm/json) pulled 2026-04-21: vLLM shipped **26 releases in the trailing 12 months, averaging 13.8 days between releases**. Recent releases have actively removed public surface area — [v0.13.0](https://github.com/vllm-project/vllm/releases/tag/v0.13.0) replaced `VLLM_ATTENTION_BACKEND` env with `--attention-backend` CLI; [v0.15.0](https://github.com/vllm-project/vllm/releases/tag/v0.15.0) removed `vllm:time_per_output_token_seconds`, DeepSpeedFp8, RTN, HQQ quantization and "deprecated environment variables"; [v0.16.0](https://github.com/vllm-project/vllm/releases/tag/v0.16.0) removed BitBlas, Marlin 24, `reasoning_content`, legacy pooling items, and `VLLM_ALL2ALL_BACKEND`.

A middleware sitting HTTP-above vLLM and speaking its OpenAI-compatible wire protocol is insulated. Upstream-merged code isn't: KVWarden's fairness contract would need re-validation every ~14 days against an engine whose public API is actively shrinking.

### 4. Multi-engine reality: vLLM is not the only target

KVWarden targets both vLLM and SGLang. NVIDIA Dynamo treats SGLang, TensorRT-LLM, and vLLM as interchangeable backends ([ai-dynamo/dynamo](https://github.com/ai-dynamo/dynamo)). Upstreaming fairness into vLLM fragments the story — "we do fairness upstream on vLLM, but at the proxy on everything else" — and forces a divergent codepath. Middleware keeps a single admission contract across engines.

### 5. Prior art has already converged on "above the engine"

Every serious multi-tenant LLM-over-vLLM deployment does admission control at a proxy/router layer. This is the dominant pattern, not an KVWarden invention:

| Project | Layer | Multi-tenant mechanism |
|---|---|---|
| [LiteLLM Proxy](https://docs.litellm.ai/docs/proxy/rate_limit_tiers) | HTTP proxy / gateway | Budget tiers, TPM/RPM limits per key/team/user, 4-level hierarchy (org → team → user → key). Implementation is gateway-only; LiteLLM's own rate-limit logic has [known concurrency bypass bugs under load](https://github.com/BerriAI/litellm/issues/18730). |
| [Kong AI Rate Limiting Advanced](https://developer.konghq.com/plugins/ai-rate-limiting-advanced/) | API gateway plugin | Token-aware (prompt/completion/total) rate limit per model per consumer. Explicitly positioned upstream of any inference engine. |
| [Envoy AI Gateway](https://aigateway.envoyproxy.io/docs/0.1/capabilities/usage-based-ratelimiting/) | Gateway / service mesh | Token-based global rate limit combining user and model identifiers. Rides on Envoy's existing Global Rate Limit API. |
| [vllm-project/production-stack](https://github.com/vllm-project/production-stack) | K8s-native router | Round-robin, session-stickiness, prefix-aware routing. The vLLM team's own reference stack does *no* rate limiting in-router — it explicitly relies on an upstream reverse proxy for that. |
| [llm-d](https://github.com/llm-d/llm-d) | K8s scheduler above vLLM | "Fairness and prioritization for multi-tenant serving" per the README — but implemented in the inference scheduler component, **not in vLLM itself**. |
| [NVIDIA Dynamo](https://github.com/ai-dynamo/dynamo) | Datacenter orchestration | SLA-driven autoscaling and KV-aware routing; no per-tenant admission primitive in the engine layer. |

Six independent projects, four organizations, same architectural conclusion: **per-tenant admission lives above the engine**.

---

## Evidence table

| Link | Date | Outcome |
|---|---|---|
| [PR #31909 — WFQ scheduler](https://github.com/vllm-project/vllm/pull/31909) | 2026-01-07 | Maintainer `@ywang96`: "`weight` isn't really OpenAI standard... non-trivial maintenance overhead... keep this in your own fork." Author conceded; stale 2026-04-10. |
| [Issue #21605 — fairness heuristics](https://github.com/vllm-project/vllm/issues/21605) | 2025-07-25 | Zero maintainer engagement. Auto-closed 2026-02-10. |
| [PR #15641 — native FastAPI bearer auth](https://github.com/vllm-project/vllm/pull/15641) | 2025-03 | Closed without merge. |
| [PR #29366 — SJF policy](https://github.com/vllm-project/vllm/pull/29366) | 2025-11-25 | APPROVED by `@hmellor` 2025-12-18 — **per-request, no tenant identity**. |
| [PR #30545 — OpenAI `service_tier`](https://github.com/vllm-project/vllm/pull/30545) | 2025-12-12 | Open; per-request priority hint, not tenant-bound. |
| [PR #30449 — SLA-tiered scheduling](https://github.com/vllm-project/vllm/pull/30449) | 2025-12-11 | `@hmellor` CHANGES_REQUESTED; stale 2026-03-29. |
| [PR #32332 — SLO request tiers](https://github.com/vllm-project/vllm/pull/32332) | 2026-01-14 | Stale; `@hmellor` "nit on the config" only — no endorsement. |
| [PR #38295 — `--api-key` has no effect on gRPC](https://github.com/vllm-project/vllm/pull/38295) | 2026-03-31 | Confirms vLLM's auth is explicitly out-of-scope for non-HTTP paths. |
| [v0.15.0 release](https://github.com/vllm-project/vllm/releases/tag/v0.15.0) | 2026-01-29 | Removed deprecated env vars, metrics, quantization methods. |
| [v0.16.0 release](https://github.com/vllm-project/vllm/releases/tag/v0.16.0) | 2026-02-26 | Removed BitBlas, Marlin 24, `reasoning_content`, `VLLM_ALL2ALL_BACKEND`. |
| [PyPI release metadata](https://pypi.org/pypi/vllm/json) | 2026-04-21 pull | 26 releases / 12 months, **13.8-day avg cadence**. |

---

## What a smart skeptic would raise

**"What if vLLM DID accept an upstream fairness PR tomorrow — doesn't that invalidate the whole project?"**

Honest answer: partial rebuttal, not a kill. A merged upstream version would (a) only cover vLLM, leaving SGLang/TRT-LLM/llama.cpp deployments still needing the proxy layer; (b) have to live inside the OpenAI-compatible request API, which means it would look like `service_tier` (per-request priority hint) rather than per-tenant bucket state, so the identity/quota/lifecycle problem still has to live in a gateway; (c) inherit vLLM's 13.8-day release cadence and ongoing breaking-CLI pattern. llm-d does claim "fairness and prioritization for multi-tenant serving" — but implements it in its own scheduler component *above* the engine, which is exactly KVWarden's architectural stance (just targeting the Kubernetes/datacenter segment instead of KVWarden's between-Ollama-and-Dynamo middle). That convergence reinforces the thesis: everyone who has shipped multi-tenant LLM serving, including the vLLM project itself, puts admission control above the engine.

---

*Source pulled 2026-04-21. vLLM release data from pypi.org/pypi/vllm/json; issue/PR data from GitHub API; quotes verbatim from cited URLs.*
