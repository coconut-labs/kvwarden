# The Inference Orchestration Gap Map
## What Dynamo, llm-d, and the Ecosystem Still Can't Do — April 2026

---

## Executive Summary

The LLM inference orchestration landscape in April 2026 is powerful at datacenter scale but riddled with concrete gaps in hardware portability, small-scale usability, cost attribution, and agent-aware scheduling. Seven verified gaps remain unsolved by any production system. This report documents each gap with primary-source evidence (GitHub issues, press releases, NVIDIA documentation, CNCF announcements) and maps where existing and emerging solutions fall short.

---

## Landscape Comparison

| System | Scale | K8s Required | Multi-Model | KV Cache Tiering | Cost Attribution | Hardware | Status |
|--------|-------|:------------:|:-----------:|:----------------:|:----------------:|----------|--------|
| **Dynamo v1.0** | Datacenter | Yes | Yes | KVBM (multi-tier) | No | NVIDIA only | Production (18+ deployers) |
| **llm-d v0.5** | Datacenter | Hard req | 1 model/pool | LMCache | No | NVIDIA (+AMD/Intel WIP) | CNCF Sandbox |
| **Mammoth (Modular)** | Datacenter | Yes | Yes | Via backends | No | NVIDIA, AMD | Production |
| **AIBrix v0.6** | Datacenter | Yes | Yes | Distributed | No | NVIDIA (heterogeneous) | Under vllm-project |
| **Ollama v0.12** | Single node | No | LRU eviction | No | No | NVIDIA, AMD, Apple | Stable |
| **LocalAI** | Single node | No | LRU eviction | No | No | Multi | Stable |
| **vLLM** | Per-instance | No | 1 model/instance | No (standalone) | No | Multi | Production |
| **SGLang** | Per-instance | No | 1 model/instance | No (standalone) | No | Multi | Production |
| **Gimlet Labs** | Cloud | Managed | Yes | Yes | Likely | Multi-silicon | Revenue stage ($92M raised) |

---

## Gap Coverage Matrix

| Gap | Dynamo | llm-d | Mammoth | AIBrix | Ollama | Gimlet | KVWarden |
|-----|:------:|:-----:|:-------:|:------:|:------:|:------:|:---------:|
| 1. Cross-hardware routing | -- | -- | -- | Partial | -- | Yes | Planned |
| 2. Lightweight orchestration (no K8s) | -- | -- | -- | -- | Partial | -- | **Core** |
| 3. Agent-scheduler feedback loop | One-way | -- | -- | -- | -- | -- | Planned |
| 4. Per-request GPU cost attribution | -- | -- | -- | -- | -- | -- | **Planned** |
| 5. Attention-aware KV compression | -- | -- | -- | -- | -- | -- | Planned |
| 6. Non-NVIDIA orchestration | -- | WIP | -- | -- | Yes | Yes | Via engines |
| 7. Production stability | Issues | Pre-1.0 | Stable | Maturing | Stable | Unknown | Alpha |

**Legend:** "Core" = primary differentiator. "Planned" = on roadmap. "Partial" = incomplete coverage. "--" = not addressed.

---

## Gap 1: Cross-Hardware Workload-Aware Routing

**Status:** Completely unsolved in open source.

No system intelligently routes inference requests across different accelerator types (NVIDIA GPU, AMD GPU, TPU, Trainium) based on workload characteristics. Academic research confirms performance variations of up to 3.7x between architectures depending on batch size and sequence length.

**Who's closest:**
- **Gimlet Labs** ($92M raised, Menlo Ventures-led Series A) is the only production solution. Their compiler lowers portions of the inference graph onto optimal hardware. They report eight-figure revenues, a top-3 frontier lab and a top-3 hyperscaler as customers. Proprietary cloud — not an open framework.
- **Callosum** (UK, $10.25M from Plural + ARIA grant) distributes AI tasks across chips from NVIDIA, AMD, AWS Trainium, Cerebras, SambaNova. Founded by Cambridge neuroscientists. Very early stage, no published benchmarks.
- **AIBrix v0.6** has an SLO-aware GPU optimizer for heterogeneous deployments, but limited to different NVIDIA GPU models (e.g., A100 vs H100), not cross-vendor.
- **Ray Serve** provides accelerator-aware resource primitives but no intelligence layer for workload-to-hardware matching.

**Verification:** Gimlet's traction confirmed via [TechCrunch](https://techcrunch.com/2026/03/23/startup-gimlet-labs-is-solving-the-ai-inference-bottleneck-in-a-surprisingly-elegant-way/), [GlobeNewsWire](https://www.globenewswire.com/news-release/2026/03/23/3260653/0/en/). Callosum confirmed via [Fortune](https://fortune.com/2026/02/26/startup-callosum-cambridge-trained-neuroscientists-raises-10-million-venture-funding-orchestrate-ai-workloads-different-chips/). AIBrix heterogeneous GPU feature confirmed in [docs](https://aibrix.readthedocs.io/latest/features/heterogeneous-gpu.html).

---

## Gap 2: Lightweight Multi-Model Orchestration (1-4 GPUs, No K8s)

**Status:** Wide open. No solution exists.

A stark gap exists between datacenter-scale orchestration (Dynamo, llm-d, Mammoth — all requiring Kubernetes) and the tools available to developers running 1-4 GPUs on bare metal. No tool provides intelligent multi-model memory management, KV cache tiering, and multi-tenant isolation in a lightweight, standalone package.

**Current state of small-scale tools:**
- **Ollama** offers the best UX (zero to Llama 3.3:70B in 5 minutes), with exact per-layer GPU memory measurement and quantized KV cache. But: no workload-aware routing between models, model switching takes 10-30s, no KV cache tiering, no multi-tenant isolation.
- **LocalAI** provides multi-model serving with LRU eviction and P2P federation. But: no intelligent scheduling, no KV cache management, known GPU memory bugs.
- **vLLM/SGLang** each serve exactly one model per instance. vLLM Issue #13633 (multi-model per GPU) remains open with no implementation. The workaround — multiple instances with `--gpu-memory-utilization 0.4` — wastes memory through duplicate KV cache pre-allocation.

**The missing product:** A lightweight daemon that manages multiple model instances on a single node, implements intelligent loading/eviction based on request patterns (not just LRU), provides KV cache tiering across GPU/CPU/SSD, and offers per-tenant isolation — all without Kubernetes. The developer workflow today is: LM Studio (evaluate) → Ollama (develop) → vLLM (deploy). The orchestration layer between Ollama and datacenter tools doesn't exist.

**New reference:** "Pick and Spin" ([arXiv 2512.22402](https://arxiv.org/abs/2512.22402), Dec 2025) addresses multi-model orchestration with adaptive routing — but it's Kubernetes-based (Helm deployment, DistilBERT classifier). Does not fill the non-K8s gap.

**Verification:** vLLM Issue #13633 confirmed [still open](https://github.com/vllm-project/vllm/issues/13633). Developer workflow confirmed via [multiple](https://dev.to/starmorph/local-llm-inference-in-2026-the-complete-guide-to-tools-hardware-open-weight-models-2iho) [guides](https://www.sitepoint.com/the-2026-definitive-guide-to-running-local-llms-in-production/).

---

## Gap 3: Bidirectional Agent-Scheduler Feedback Loop

**Status:** Partially addressed (one-directional only). No closed loop exists.

Dynamo v1.0 ships `nvext.agent_hints` — structured signals from agent harnesses to the inference scheduler:
- `latency_sensitivity`: Router queue priority
- `priority`: Engine queue ordering + KV cache eviction weight
- `osl`: Expected output sequence length for load balancing
- `speculative_prefill`: Proactive KV cache warming after assistant turns (new in v1.0)
- Planned: `program_id`, `context_type` (agent-type-aware scheduling)

Cache pinning (`nvext.cache_control`) provides TTL-based KV retention — but is **explicitly experimental**, works only with SGLang + HiCache, and is described as "ephemeral type only, to match Anthropic's API."

**The gap:** All signaling is agent → scheduler. The scheduler never reports back resource availability, predicted latency, or optimal request patterns to the agent runtime. No system implements the bidirectional closed loop where the scheduler learns from agent behavior and feeds that learning back.

**Closest academic work:**
- **Continuum** ([arXiv 2511.02230](https://arxiv.org/abs/2511.02230)) predicts tool-call durations and pins KV cache with computed TTLs. 1.5x+ improvement on SWE-Bench/BFCL. Preview code at [github.com/Hanchenli/vllm-continuum](https://github.com/Hanchenli/vllm-continuum). Not integrated into production systems.
- **ThunderAgent** ([arXiv 2602.13692](https://arxiv.org/pdf/2602.13692)) — program-aware agentic inference system. New reference.
- No agent framework (LangChain, CrewAI, AutoGen, LlamaIndex) integrates with inference schedulers for plan-level optimization.

**Verification:** Dynamo agent API confirmed via [docs](https://docs.nvidia.com/dynamo/user-guides/agents) and [blog](https://docs.nvidia.com/dynamo/dev/blog/agentic-inference). Cache pinning experimental status confirmed. One-directional signaling confirmed — no bidirectional feedback in any documentation.

---

## Gap 4: Per-Request GPU Cost Attribution

**Status:** Open research problem. No production implementation.

No inference orchestration system provides per-tenant, per-request cost attribution accounting for actual GPU compute and memory consumption in co-batched multi-tenant environments. The entire industry meters costs at the API token layer (Langfuse, Datadog LLM Observability, Bifrost, AWS Bedrock `requestMetadata`).

The gap between "counting tokens per API call" and "attributing actual GPU time per request when 32 requests share one forward pass" remains wide.

**Closest research:** LLMVisor (reportedly NeurIPS 2025 MLForSys Workshop) decomposes batch latency into per-request shares at microsecond scale. **Note: this paper could not be independently verified in web searches — may use a different name or be poorly indexed. Manual verification recommended.**

**Verification:** Token-level cost tracking tools (Langfuse, Datadog) confirmed as the state of the art. No GPU-level attribution found in any production system's documentation.

---

## Gap 5: Attention-Aware KV Cache Compression at the Orchestration Layer

**Status:** Partially solved (codec-based compression exists). Attention-aware compression unsolved.

**LMCache + CacheGen** is the only production system compressing KV cache at the orchestration layer. CacheGen (SIGCOMM 2024) achieves up to 3x smaller representation than quantization. Integrated into vLLM, SGLang, llm-d, and Dynamo. Production adoption at Google Cloud GKE, CoreWeave, and Cohere. 3-10x delay savings reported.

**The gap:** CacheGen compression is codec-based and content-agnostic — it does not selectively compress based on attention patterns or token importance. Engine-internal FP8/FP4 quantization reduces memory within a single instance but doesn't extend to cross-node transfer. When KVBM moves KV blocks between tiers, no compression is applied.

**Research prototypes (unintegrated):**
- **MiniCache** (NeurIPS 2024): 5.02x compression by merging similar KV states across adjacent layers
- **Quest** (ICML 2024, MIT HAN Lab): Query-aware KV cache selection, 7x self-attention speedup
- **EvicPress** (arXiv 2512.14946): Joint compression + eviction using utility function

**Verification:** LMCache integration confirmed via [LMCache blog](https://blog.lmcache.ai/en/2026/03/16/lmcache-nvidia-dynamo-1-0-a-match-made-in-inference-heaven/) and [GitHub](https://github.com/LMCache/LMCache). MiniCache and Quest confirmed as real papers, no production integration.

---

## Gap 6: Non-NVIDIA Inference Orchestration

**Status:** Effectively vapor for orchestration layers. Improving at the engine level.

- **Dynamo** is NVIDIA-only by design. All documentation references Hopper/Blackwell exclusively.
- **llm-d** added AMD and Intel as partners in the CNCF Sandbox announcement. AMD ROCm container images added in v0.5. vLLM's ROCm attention backend is [improving](https://blog.vllm.ai/2026/02/27/rocm-attention-backend.html) at the engine level, which indirectly benefits llm-d.
- **LMCache** was CUDA-only until recently; AMD and ARM support described as early stages.
- All published benchmarks for both Dynamo and llm-d remain NVIDIA-only.

**Verification:** Dynamo NVIDIA-only confirmed in [v1.0 blog](https://developer.nvidia.com/blog/nvidia-dynamo-1-production-ready/). llm-d AMD partnership confirmed in [CNCF blog](https://www.cncf.io/blog/2026/03/24/welcome-llm-d-to-the-cncf-evolving-kubernetes-into-stata-ai-infrastructure/). vLLM ROCm backend confirmed.

---

## Gap 7: Production Stability

**Status:** Improving but significant concerns remain.

**Dynamo v1.0:**
- KV cache memory leak ([Issue #3730](https://github.com/ai-dynamo/dynamo/issues/3730)): 95% of blocks permanently leaked on Qwen3-480B with 8xH20 GPUs. `is_complete()` never returns True. No evidence of fix in v1.0 release notes.
- 18+ production deployers (AstraZeneca, ByteDance, Pinterest, etc.) suggest core paths are stable, but edge cases remain.
- NVIDIA describes the agent API as "a v1 API that we are actively co-designing with the community."

**llm-d v0.5:**
- CNCF Sandbox explicitly means "not yet widely tested in production."
- Six releases in ten months with breaking API changes (Gateway API Inference Extension v1alpha2 → v1).
- New v0.5 features (hierarchical KV offloading, cache-aware LoRA routing, active-active HA) are significant but increase surface area.
- `llm-d-deployer` repo was archived.

**Verification:** Issue #3730 confirmed [on GitHub](https://github.com/ai-dynamo/dynamo/issues/3730). Dynamo deployer list confirmed in [NVIDIA press release](https://nvidianews.nvidia.com/news/dynamo-1-0). llm-d CNCF Sandbox status confirmed.

---

## Where KVWarden Fits

KVWarden is a middleware orchestration layer sitting on vLLM/SGLang, targeting **Gap #2** (lightweight multi-model orchestration without Kubernetes) with extensions into **Gap #4** (per-request cost attribution) and **Gap #5** (KV cache tiering).

### Architecture

```
                    +---------------------+
                    |   WorkloadRouter    | <-- request profiling, SLO-aware routing
                    +--------+------------+
                             |
              +--------------+--------------+
              |              |              |
     +--------v--------+ +--v---+ +--------v--------+
     |  CacheManager   | |Shared| | TenantManager   |
     | GPU -> CPU -> SSD| |State | |Resource budgets |
     +-----------------+ +------+ +-----------------+
              |                            |
     +--------v----------------------------v--------+
     |          vLLM / SGLang Engine                 |
     +----------------------------------------------+
```

### Differentiation

1. **No Kubernetes required** — `pip install kvwarden` + daemon. The only intelligent orchestrator that runs on bare metal with 1-4 GPUs.
2. **Intelligent model lifecycle** — Load, evict, and hot-swap models based on request frequency + recency, not naive LRU.
3. **KV cache tiering** — GPU HBM -> CPU RAM -> SSD via LMCache integration, without datacenter infrastructure.
4. **Per-tenant isolation** — Software-level resource budgets for multi-user environments.

### Current Status

Phase 1: Profiling vLLM/SGLang scheduling overhead to establish baselines. Implementation of WorkloadRouter, CacheManager, and TenantManager follows.

---

## Methodology

All claims in this report were verified against primary sources on April 15, 2026:
- GitHub repositories and issue trackers (ai-dynamo/dynamo, llm-d/llm-d, vllm-project/vllm, vllm-project/aibrix, LMCache/LMCache)
- NVIDIA technical documentation and blog posts
- CNCF announcements and blog posts
- Press releases and funding announcements (TechCrunch, Fortune, GlobeNewsWire)
- arXiv papers and conference proceedings
- Product documentation (Modular, LMCache, AIBrix)

Claims rated as "unverifiable" are explicitly flagged. See companion document `gap_analysis_verification_april2026.md` for per-claim verification status.

---

## Sources

- [NVIDIA Dynamo 1.0 Production Blog](https://developer.nvidia.com/blog/nvidia-dynamo-1-production-ready/)
- [NVIDIA Dynamo Agentic Inference](https://docs.nvidia.com/dynamo/dev/blog/agentic-inference)
- [NVIDIA Dynamo Agent Hints Guide](https://docs.nvidia.com/dynamo/user-guides/agents)
- [Dynamo Issue #3730 — KV Cache Memory Leak](https://github.com/ai-dynamo/dynamo/issues/3730)
- [NVIDIA Press Release — Dynamo 1.0](https://nvidianews.nvidia.com/news/dynamo-1-0)
- [CNCF Blog — llm-d Sandbox](https://www.cncf.io/blog/2026/03/24/welcome-llm-d-to-the-cncf-evolving-kubernetes-into-stata-ai-infrastructure/)
- [Google Cloud Blog — llm-d CNCF](https://cloud.google.com/blog/products/containers-kubernetes/llm-d-officially-a-cncf-sandbox-project)
- [CoreWeave Blog — llm-d](https://www.coreweave.com/blog/the-next-chapter-for-ai-infrastructure-why-llm-ds-move-to-cncf-matters)
- [Gimlet Labs — TechCrunch](https://techcrunch.com/2026/03/23/startup-gimlet-labs-is-solving-the-ai-inference-bottleneck-in-a-surprisingly-elegant-way/)
- [Gimlet Labs — Series A (GlobeNewsWire)](https://www.globenewswire.com/news-release/2026/03/23/3260653/0/en/)
- [Menlo Ventures — Gimlet Investment](https://menlovc.com/perspective/menlos-investment-in-gimlet-the-multi-silicon-inference-cloud/)
- [Callosum — Fortune Exclusive](https://fortune.com/2026/02/26/startup-callosum-cambridge-trained-neuroscientists-raises-10-million-venture-funding-orchestrate-ai-workloads-different-chips/)
- [ARIA Scaling Inference Lab](https://aria.org.uk/opportunity-spaces/nature-computes-better/scaling-compute/scaling-inference-lab/)
- [Modular — BentoML Acquisition](https://www.modular.com/blog/bentoml-joins-modular)
- [Mammoth Orchestrator](https://www.modular.com/mammoth)
- [LMCache + Dynamo 1.0](https://blog.lmcache.ai/en/2026/03/16/lmcache-nvidia-dynamo-1-0-a-match-made-in-inference-heaven/)
- [LMCache GitHub](https://github.com/LMCache/LMCache)
- [Continuum Paper](https://arxiv.org/abs/2511.02230)
- [Continuum Preview Code](https://github.com/Hanchenli/vllm-continuum)
- [vLLM Issue #13633 — Multi-Model per GPU](https://github.com/vllm-project/vllm/issues/13633)
- [AIBrix v0.6 — Heterogeneous GPU](https://aibrix.readthedocs.io/latest/features/heterogeneous-gpu.html)
- [AIBrix GitHub (vllm-project)](https://github.com/vllm-project/aibrix)
- [Pick and Spin Paper](https://arxiv.org/abs/2512.22402)
- [vLLM ROCm Attention Backend](https://blog.vllm.ai/2026/02/27/rocm-attention-backend.html)
- [DigitalOcean — Dynamo 1.0](https://www.digitalocean.com/blog/nvidia-dynamo-1-now-available)
