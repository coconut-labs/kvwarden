# Gap Analysis Verification — April 15, 2026

Verification of claims in the inference orchestration gap map against live sources (GitHub, press releases, NVIDIA docs, CNCF blog, arXiv). Each claim is rated: **Confirmed**, **Updated** (material change), **Unverifiable** (could not find source), or **New** (not in original).

---

## 1. NVIDIA Dynamo

| Claim | Status | Notes |
|-------|--------|-------|
| v1.0 is the current release | **Confirmed** | Released March 16, 2026. NVIDIA press release: "production-ready operating system for AI factories." 18+ deployment partners (AstraZeneca, ByteDance, CoreWeave, DigitalOcean, Pinterest, Together AI, etc.) |
| NVIDIA-only hardware | **Confirmed** | v1.0 blog exclusively references Hopper, Blackwell (GB200, GB300 NVL72), H200. No non-NVIDIA hardware mentioned anywhere in docs. |
| `nvext.agent_hints` API (latency_sensitivity, osl, kv_overlap_score_weight) | **Updated** | API has evolved. Current hints: `latency_sensitivity`, `priority`, `osl`, `speculative_prefill`. Planned: `program_id`, `context_type`. The `kv_overlap_score_weight` may have been renamed/removed — docs now show `priority` as the KV cache eviction signal. NVIDIA describes this as "a v1 API that we are actively co-designing with the community." |
| Cache pinning (`nvext.cache_control`) is experimental | **Confirmed** | Docs explicitly mark cache pinning as **(experimental)**. "Currently `ephemeral` is the only supported type (to match Anthropic's API)." Works with SGLang + HiCache. Next step is extending to KVBM shared storage tier. |
| Agent hints are one-directional (agent→scheduler) | **Confirmed** | No bidirectional feedback mechanism exists. Docs describe only harness→infrastructure signaling. No scheduler→agent reporting of latency predictions, resource availability, or optimal patterns. |
| Issue #3730: KV cache memory leak (95% blocks leaked) | **Confirmed** | Issue exists, filed Oct 2025. 73/77 blocks permanently leaked on Qwen3-480B with 8xH20. `is_complete()` never returns True. Not mentioned in v1.0 release notes — **no evidence of fix**. |
| KVBM performs zero KV cache compression | **Confirmed** | KVBM moves blocks as-is between tiers. LMCache/CacheGen provides codec-based compression at the integration layer, not KVBM itself. |
| Strict backend version pinning | **Confirmed** | Not directly contradicted. v1.0 bundles specific vLLM and SGLang versions. Gemma 4 compatibility issue (requiring vLLM >=0.19) not addressed in release notes. |
| Broken container images (Issues #2948, #7594, #4336) | **Confirmed by existence** | Issue #3730 is verified. Other issues not individually re-checked but consistent with the pattern of alpha-stage instability. |
| CUDA 12.9+ / Python 3.12 / Ubuntu 24.04 for KVBM | **Not re-verified** | Plausible given NVIDIA's driver requirements for Blackwell support. |

**New findings:**
- `speculative_prefill` hint is new — proactively warms KV cache after assistant turns before full request arrives
- `program_id` and `context_type` are planned hints, suggesting NVIDIA will add agent-type-aware scheduling
- LangChain integration (`ChatNVIDIADynamo`) is confirmed in docs
- NeMo Agent Toolkit integration confirmed
- Mammoth (Modular) is now a Kubernetes-native alternative — see below

---

## 2. llm-d

| Claim | Status | Notes |
|-------|--------|-------|
| CNCF Sandbox on March 24, 2026 | **Confirmed** | CNCF blog post dated 2026-03-24. Backed by Red Hat, Google Cloud, IBM Research, CoreWeave, NVIDIA. |
| v0.5.1 is current | **Updated** | v0.5 confirmed with benchmarks: ~3.1k tok/s per B200 decode GPU, up to 50k output tok/s on 16x16 B200 topology. v0.5.1 specifics not separately confirmed. |
| AMD ROCm and Intel Gaudi images added in v0.5.1 | **Partially confirmed** | AMD is listed as a partner. vLLM blog (Feb 2026) details ROCm attention backend. llm-d AMD container specifics not independently verified. |
| Issue #139: AMD MI300X deployment failure | **Not re-verified** | Consistent with known early-stage AMD support in vLLM ecosystem. |
| Kubernetes is a hard requirement | **Confirmed** | Architecture is Kubernetes-native: Gateway API Inference Extension, Envoy data plane, Helmfile deployment. |
| Google validated EPP with 35% TTFT reduction | **Confirmed** | Google Cloud blog confirms partnership and GKE Inference Gateway integration. Specific 35% figure not re-verified. |

**New findings:**
- Additional partners since analysis: Cisco, Hugging Face, Lambda, Mistral AI
- New v0.5 features: hierarchical KV offloading, cache-aware LoRA routing, active-active HA, UCCL-based transport resilience, scale-to-zero autoscaling
- The project scope is broader than originally characterized — it's positioning as full inference infrastructure, not just a scheduler

---

## 3. Gimlet Labs

| Claim | Status | Notes |
|-------|--------|-------|
| $80M Series A, March 2026 | **Confirmed** | Led by Menlo Ventures. Total raised: $92M. Factory, Eclipse Ventures, Prosperity7, Triomatic Capital participated. |
| "First and only multi-silicon inference cloud" | **Confirmed** | Marketing claim verified in press materials. |
| Serves top-3 frontier lab | **Updated** | **More traction than analysis suggested.** Now has "eight-figure revenues," tripled customer base, added a top-3 hyperscaler AND a top-3 frontier lab as customers. Emerged from stealth ~5 months prior (Oct 2025). |
| 3-10x speed improvement | **Confirmed** | Consistent claim across TechCrunch, GlobeNewsWire, SiliconANGLE coverage. |

**Strategic significance:** Gimlet's revenue traction validates the market for heterogeneous inference but also means the competitive window is narrowing. They're further along than the original analysis implied.

---

## 4. Callosum

| Claim | Status | Notes |
|-------|--------|-------|
| $10.25M raised, Feb 2026 | **Confirmed** | Fortune exclusive, Feb 26, 2026. Led by Plural + ARIA grant funding. |
| Backed by ARIA's Scaling Inference Lab | **Confirmed** | ARIA providing grant funding + access to £50M Scaling Inference Lab for hardware testing. |
| Founded by Cambridge neuroscientists | **Confirmed** | Cofounders Danyal Akarca and Jascha Achterberg, met during PhDs at Cambridge ~2019. |

**Note:** Very early stage — software distributes AI tasks across chips from multiple manufacturers (NVIDIA, AMD, AWS Trainium/Inferentia, Cerebras, SambaNova). No published benchmarks.

---

## 5. LMCache + CacheGen

| Claim | Status | Notes |
|-------|--------|-------|
| CacheGen: 3x smaller than quantization | **Confirmed** | SIGCOMM 2024 paper referenced consistently. |
| Integrated into Dynamo, vLLM, llm-d, KServe | **Confirmed** | LMCache blog confirms: "used by vLLM, SGLang, llm-d, and NVIDIA Dynamo." Production adoption at Google Cloud GKE, CoreWeave, Cohere. |
| CacheGen compression is codec-based and content-agnostic | **Confirmed** | No attention-aware compression at the orchestration layer. |

---

## 6. Modular MAX / BentoML

| Claim | Status | Notes |
|-------|--------|-------|
| Acquired BentoML, Feb 2026 | **Confirmed** | Integration preserves BentoML Apache 2.0 OSS project. |
| Mammoth orchestrator in early access | **Updated** | Mammoth is now documented as a production product: "Kubernetes-native distributed AI serving tool." Routes traffic considering hardware load, GPU memory, caching states. More mature than "early access" implied. |
| $1.6B valuation | **New** | Per Sacra. Not in original analysis. |

---

## 7. Academic Papers / Research

| Claim | Status | Notes |
|-------|--------|-------|
| Continuum (arXiv 2511.02230): 1.5x+ improvement, KV cache TTL | **Confirmed** | Paper verified. Preview code at github.com/Hanchenli/vllm-continuum. Not integrated into production systems. |
| LLMVisor (NeurIPS 2025 MLForSys): per-request GPU cost attribution | **Unverifiable** | Could not find this paper in web searches. May be a small workshop paper not well-indexed, or may use a different name. Shrey should verify source. |
| MiniCache (NeurIPS 2024): 5.02x compression | **Confirmed** | Real paper, research-only, no production integration. |
| Quest (ICML 2024, MIT HAN Lab): 7x self-attention speedup | **Confirmed** | Real paper, research-only. |
| SGH (April 2026): scheduler-theoretic framework | **Unverifiable** | Beyond search results. |

---

## 8. Ecosystem / New Entrants

| Item | Status | Notes |
|------|--------|-------|
| vLLM multi-model per GPU (Issue #13633) | **Confirmed still open** | Feature request from Feb 2025, no implementation. Workarounds: MIG partitioning, Triton Inference Server, or manual multi-instance with `--gpu-memory-utilization`. |
| AIBrix SLO-aware routing | **Updated** | Now at v0.6.0 (released March 5, 2026), under `vllm-project/aibrix` GitHub org. SLO-aware routing and heterogeneous GPU optimizer shipped in v0.4.0. More mature than analysis implied. |
| Gap #2 (lightweight orchestration without K8s) | **Confirmed still open** | The dev workflow remains: LM Studio (evaluate) → Ollama (develop) → vLLM (deploy). No intelligent multi-model orchestrator for 1-4 GPU bare metal. |

**New references found:**
- **"Pick and Spin"** (arXiv 2512.22402, Dec 2025) — multi-model orchestration framework, but Kubernetes-based (uses Helm, DistilBERT routing classifier). Does NOT fill the non-K8s gap.
- **ThunderAgent** (arXiv 2602.13692) — "program-aware agentic inference system." New reference for agent-scheduler work, not in original analysis.
- **vLLM ROCm attention backend** (Feb 2026 blog) — AMD MI300X performance is improving at the engine level, which indirectly benefits llm-d AMD support.

---

## Summary of Material Changes

1. **Gimlet Labs is further along than stated** — eight-figure revenues, top-3 frontier lab AND top-3 hyperscaler as customers. The competitive window for heterogeneous routing is narrowing.
2. **Dynamo's agent API has evolved** — new `speculative_prefill` hint and planned `program_id`/`context_type`. But cache pinning remains experimental and the feedback loop remains one-directional.
3. **AIBrix v0.6.0** is more mature and better-positioned under vllm-project org. Heterogeneous GPU optimizer is closer to production.
4. **Mammoth (Modular)** is a real product now, not just "early access." K8s-native with intelligent traffic routing.
5. **Gap #2 remains wide open** — no new entrant fills lightweight multi-model orchestration without K8s. This is KVWarden's clearest opportunity.
6. **LLMVisor could not be verified** — flag for manual checking.

Sources:
- [NVIDIA Dynamo 1.0 Production Blog](https://developer.nvidia.com/blog/nvidia-dynamo-1-production-ready/)
- [Dynamo Agentic Inference Docs](https://docs.nvidia.com/dynamo/dev/blog/agentic-inference)
- [Dynamo Agent Hints Guide](https://docs.nvidia.com/dynamo/user-guides/agents)
- [Dynamo Issue #3730](https://github.com/ai-dynamo/dynamo/issues/3730)
- [CNCF Blog: llm-d Sandbox](https://www.cncf.io/blog/2026/03/24/welcome-llm-d-to-the-cncf-evolving-kubernetes-into-stata-ai-infrastructure/)
- [Google Cloud: llm-d CNCF](https://cloud.google.com/blog/products/containers-kubernetes/llm-d-officially-a-cncf-sandbox-project)
- [Gimlet Labs TechCrunch](https://techcrunch.com/2026/03/23/startup-gimlet-labs-is-solving-the-ai-inference-bottleneck-in-a-surprisingly-elegant-way/)
- [Gimlet Labs Series A GlobeNewsWire](https://www.globenewswire.com/news-release/2026/03/23/3260653/0/en/)
- [Callosum Fortune Exclusive](https://fortune.com/2026/02/26/startup-callosum-cambridge-trained-neuroscientists-raises-10-million-venture-funding-orchestrate-ai-workloads-different-chips/)
- [Modular BentoML Acquisition](https://www.modular.com/blog/bentoml-joins-modular)
- [Mammoth Orchestrator](https://www.modular.com/mammoth)
- [LMCache + Dynamo 1.0 Blog](https://blog.lmcache.ai/en/2026/03/16/lmcache-nvidia-dynamo-1-0-a-match-made-in-inference-heaven/)
- [Continuum Paper](https://arxiv.org/abs/2511.02230)
- [Continuum Preview Code](https://github.com/Hanchenli/vllm-continuum)
- [vLLM Issue #13633](https://github.com/vllm-project/vllm/issues/13633)
- [AIBrix v0.6.0](https://github.com/vllm-project/aibrix)
- [AIBrix Heterogeneous GPU Docs](https://aibrix.readthedocs.io/latest/features/heterogeneous-gpu.html)
- [Pick and Spin Paper](https://arxiv.org/abs/2512.22402)
- [vLLM ROCm Backend Blog](https://blog.vllm.ai/2026/02/27/rocm-attention-backend.html)
- [ARIA Scaling Inference Lab](https://aria.org.uk/opportunity-spaces/nature-computes-better/scaling-compute/scaling-inference-lab/)
