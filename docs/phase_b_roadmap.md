# Phase B Roadmap — Four Weeks After Gate 0

**Document date:** 2026-04-18
**Author:** Shrey Patel
**Scope:** Weeks 1-4 after Gate 0 completes, covering arXiv preprint, landing page refresh, HN/Reddit launch, community signal collection, Gates 1-3 GPU experiments, risks, and week-by-week execution.
**GPU budget:** ~$60 total across Gates 1-3 (assumes H100 SXM at ~$3.5/hour on RunPod; verify pricing before each provision).

---

## 0. Precondition: Gate 0 outcome branches

Phase B rests on Gate 0 producing real two-model data from `kvwarden serve` on an A100. The multi-model benchmark harness has never run on GPU. Two outcomes shape everything downstream.

- **Gate 0 passes cleanly** (both models load, admission controller stays up, baseline TTFT and throughput recorded): proceed with the Systems+Measurement preprint plan below.
- **Gate 0 reveals a blocker** (engine adapter bug, GPU OOM, admission controller regression): freeze the preprint outline at the measurement-only fallback (Section 1, variant B) and spend Week 1 fixing the blocker before committing Gate 1 spend.

Non-negotiable: do not begin preprint drafting until Gate 0 data is in `results/` and verified.

---

## 1. arXiv preprint (Weeks 1-4)

### Positioning decision (committed)

**Systems + measurement paper** — not measurement-only. The differentiator is KVWarden's admission controller, validated against vLLM baselines using Gate 1 data. This is higher risk (depends on Gate 1 showing 2-4x p99 TTFT improvement) but meaningfully stronger contribution.

**Fallback (variant B):** if Gate 1 noise prevents a clean claim, pivot to measurement-only: "An Empirical Study of the Scheduling Cliff in Production LLM Engines." Lower contribution, zero Gate 1 dependency.

### Title (primary)

**"Staying Below the Cliff: Middleware Admission Control for LLM Inference on 1-4 GPUs"**

Alternative: **"The Scheduling Cliff Is Real: Measurement and Middleware Mitigation in vLLM and SGLang"**

### arXiv category

Primary: **cs.DC** (Distributed, Parallel, and Cluster Computing). Cross-list: **cs.OS** (systems). Do not list cs.LG — this is a systems paper, not ML.

### Abstract (draft)

> We show that modern LLM inference engines (vLLM 0.19, SGLang) exhibit a sharp scheduling cliff: doubling concurrency from 128 to 256 yields only +2% throughput but +718% time-to-first-token (TTFT) on A100 and H100 hardware, with GPU utilization held above 95%. The cliff is hardware- and engine-independent — SGLang trails vLLM by <5% in throughput but delivers 2.2x better TTFT at saturation. The bottleneck is scheduling quality, not compute.
>
> We introduce KVWarden, a middleware layer that sits in front of vLLM or SGLang and enforces admission control at the HTTP boundary, keeping requests from entering the engine scheduler past its cliff. KVWarden requires no Kubernetes, installs via `pip`, and manages multi-model lifecycle with frequency+recency eviction. On a three-model workload (Llama 8B, Qwen 7B, Phi-3) we measure [Gate 1: Nx p99 TTFT improvement under saturation] at [Gate 1: Y ms] proxy overhead.
>
> Between Ollama (no scheduling intelligence) and Dynamo/llm-d (Kubernetes-mandatory datacenter tools), a missing middleware layer can capture most of the latency-SLO benefit for small-cluster deployments. We release KVWarden open-source with a reproducibility artifact.

### Section outline (target: ~10 pages, ACM/IEEE format)

1. **Introduction (1.0p)** — cliff framing, middleware-vs-K8s gap, contributions.
2. **Background (1.5p)** — vLLM/SGLang continuous batching, PagedAttention, existing orchestration.
3. **Measurement Study (2.0p)** — Phase 1 data on A100 PCIe, A100 SXM, H100 SXM; engine convergence.
4. **System Design (1.5p)** — WorkloadRouter, AdmissionController, CacheManager, TenantManager; OpenAI-API compatibility.
5. **Evaluation (2.5p)** — Gate 1 admission ON/OFF, eviction comparison, proxy overhead; Gate 2 heterogeneous 70B+8B.
6. **Related Work (0.75p)** — Dynamo, llm-d, Mammoth, AIBrix, Ollama, Gimlet, Continuum, Pick-and-Spin.
7. **Discussion & Conclusion (0.75p)** — generalizability, limits, LMCache integration, length prediction.

### Data-to-claim map

All Phase 1 claims cite `results/results_llama31-8b_20260416_120938/`:
- Scheduling cliff (vLLM) → `benchmarks/baseline/`
- Cliff universality across engines/hw → `summary.md`
- Engine convergence <5% and SGLang 2.2x TTFT → `summary.md`
- GPU util >95% → `gpu_metrics_*.csv`

Phase B additions:
- Admission control improves TTFT + multi-model eviction beats LRU → **Gate 1** (`results/gate1/`)
- 70B+8B heterogeneous serving → **Gate 2** (`results/gate2/`)
- Proxy overhead → **Gate 1 or Gate 3** single-request latency diff

### Authors

**Shrey Patel, sole author** as default. "Jay" referenced in Phase B task brief does not appear in any repo material — flag to master for clarification. If Jay contributed to Phase 1 profiling work, add as second author; if only advisory, move to Acknowledgements. Do not list unless confirmed.

### Timeline

- **Week 1:** Gate 0 complete, results curated. Lock preprint outline. Draft Sections 1-2 (intro + background).
- **Week 2:** Draft Sections 3-4 (measurement + system design). Landing-page refresh in parallel.
- **Week 3:** Gate 1-2 complete. Draft Section 5 (evaluation) with real numbers. Beta-reader pass to 2-3 systems-savvy reviewers (solicit via email; no fabricated names).
- **Week 4:** Revise, polish, submit to arXiv. Coordinate with HN launch.

### Risk (framing)

The preprint must match the data, not the pitch. The landing page, pitch, and README still contain expectations phrased as claims. Before submission: (a) every numeric in the preprint is traceable to a `results/` dir, (b) nothing depends on the refuted "29% engine gap" or "81% GPU waste" framings.

---

## 2. Landing page refresh (Week 2, ~1 day)

Current page: `/Users/shrey/Personal Projects/kvwarden-root-pages/kvwarden-root/landing_page/index.html`. It is already strong — immersive intro, scheduling-cliff hook, competitive table. The refresh is tightening, not a rebuild.

### Required changes

1. **Fix the "95% GPU waste" contradiction.** The TenantManager card reads `95% / GPU waste per static allocation`, which collides with Phase 1's "GPU utilization 95-99%" finding. These measure different things (static-partitioning waste vs. active-GPU utilization) but share the number in opposite directions — this will be called out on HN. Replace with `~20% VRAM lost to duplicate KV pre-allocation when running two vLLM instances with --gpu-memory-utilization 0.4` — consistent with `docs/demo_script.md` Scene 1 and `docs/pitch.md`.
2. **Stat block:** keep `5,353 tok/s`, `10,545 tok/s`, `2.2x` SGLang advantage, `8x` cliff. After Gate 1, add a fifth stat: measured admission-control TTFT reduction.
3. **Above-the-fold figure:** swap in `docs/figures/scheduling_cliff_detail.png`. It is the single chart that sells the whole thesis in one frame.
4. **Call-to-action priority:** GitHub star **first**, waitlist **second**. Stars are a public signal; emails are private. The current page already links both — reorder so GitHub is the primary button below the hero.
5. **Add preprint link** once published (Week 4).

### Performance / design

- Mobile: test on iPhone SE width (375px) — the immersive canvas intro may need a reduced-motion fallback.
- SEO: the title tag is already strong. Add `<link rel="canonical">` and a `/robots.txt` + `/sitemap.xml`. Verify OG image renders (currently no `og:image` tag — add one using `docs/figures/scheduling_cliff_detail.png` exported to a 1200x630 PNG).
- Lenis smooth-scroll: confirm it respects `prefers-reduced-motion`.

### Content gaps vs README / pitch

- Landing page has no architecture diagram — README has the ASCII one. Consider adding a clean SVG version.
- No mention of the Phase 1 cross-engine finding (engine convergence). Add a two-line callout in the Landscape section: "We measured <5% throughput gap between vLLM and SGLang. The 29% difference you may have read about is gone."

---

## 3. HN + Reddit launch (Week 4)

### HN titles — A/B candidate set

1. **"Show HN: KVWarden – Multi-model LLM serving without Kubernetes"** (safe, matches HN conventions)
2. **"Show HN: I profiled vLLM and SGLang across 3 GPUs – the scheduling cliff is real"** (findings-first, drives discussion to the data)
3. **"Show HN: Middleware that keeps LLM inference below the scheduling cliff (pip install)"** (value-prop-first)

Pick #1 if the preprint is the hero. Pick #2 if Gate 1 data is the hero. Pick #3 if GitHub stars are the primary KPI.

### Timing

- **HN:** Tuesday or Wednesday, 08:30-09:30 PT. This overlaps US morning + EU early afternoon. Avoid Mondays (queue clear from weekend) and Fridays (low engagement).
- **/r/LocalLLaMA:** same day, 2-4 hours after HN post. Different audience; they want install commands and benchmark numbers, not a narrative.
- **X/Twitter:** thread on launch day. Tag @vllm_project, @lmsys_org (SGLang), @LMCacheProject.

### Launch thread content

- Hook: the scheduling-cliff chart.
- One-line pitch: "pip install kvwarden. Smart multi-model serving on 1-4 GPUs. No cluster."
- Three bullets: (1) measured cliff on A100+H100, (2) admission-control result from Gate 1, (3) pip install + daemon in under 2 minutes.
- Links: repo, preprint, landing page.
- Ask: GitHub star + honest feedback.

### Pre-launch outreach

Do not fabricate DMs to "influencers you know." The honest pre-launch list is:
- Post to the vLLM Slack/Discord in the appropriate channel 24h before with "heads up, launching this tomorrow"
- Email the LMCache team — they are a direct collaborator target, not cold outreach
- Email two systems-professors whose courses cite vLLM, offering the repo as teaching material (publicly available list; no fabricated relationships)

### Response playbook (canned lines for repeated questions)

- **Why not K8s?** K8s adds ~30 min of setup on one node. If you already run K8s, use Dynamo or llm-d.
- **Why not Dynamo?** K8s-mandatory and NVIDIA-only. Different scale.
- **Is this just Ollama?** Ollama uses LRU, no admission control. KVWarden targets SLO-sensitive mixed traffic.
- **Isn't vLLM going to add multi-model?** Issue #13633 open since Feb 2025. When it ships, KVWarden's differentiation stays in admission control + tiered cache.
- **What about LMCache?** Planned dependency; LMCache handles KV tier movement, KVWarden handles orchestration.

---

## 4. Community signal → fundraise vs academic decision

### Decision window

**Decide 4 weeks after HN launch** (end of Week 8 counting from Phase B start). Two weeks is too short to read signal; six weeks gives competitors a head start.

### Signal thresholds (30 days post-launch)

Green (fundraise) / Yellow / Red (shelve-or-academic):
- GitHub stars: >1,500 / 400-1,500 / <400
- Waitlist signups: >250 / 75-250 / <75
- Unsolicited design-partner inbounds: >=3 concrete / 1-2 vague / 0
- VC cold-outreach: >=5 partners / 1-4 / 0
- Active external contributors: >=2 / 1 / 0

### Fundraise branch

**Pre-seed angels first, then institutional pre-seed.**

- Target size: $1.5M-$2.5M on a $10M-$15M cap SAFE. Solo-founder discount applies; do not overshoot.
- 18-month runway for 3-4 engineers + RunPod/Lambda compute + lightweight sales.

**Target investors (public thesis match — not claimed warm relationships):**
- **Decibel Partners** (Jon Sakoda) — infra/dev-tools focus
- **Amplify Partners** (Sarah Catanzaro, Mike Dauber) — published ML-infra thesis
- **Menlo Ventures** (Matt Murphy) — led Gimlet investment
- **Essence VC** (Tim Tully) — explicit developer-infra + inference thesis
- **Felicis Ventures** (Astasia Myers) — published on inference/ML-infra

Approach: 4-5 angel checks ($25-100K each) first for traction and intro graph, then institutional.

### Academic branch

- **First target:** MLSys 2027 (papers due ~Oct 2026) — right audience, 6-month fit.
- **Second target:** NeurIPS 2026 Systems-for-LLMs workshop — lower bar, summer deadline.
- **Third target:** OSDI 2027 (~Dec 2026) — high risk/reward for solo author.
- Skip SOSP (too niche).

### Plan C: signal is "meh"

Honest answer for a solo unfunded founder: **document, open-source, move on.** Keep preprint on arXiv as public record; archive repo as "maintained not actively developed"; apply work as credential for PhD or senior-eng role at an inference company (Together, Anyscale, Modal, Fireworks, NVIDIA). The gap analysis, profiling data, and measurement paper stand on their own.

---

## 5. Gate 1-3 GPU experiments ($60 budget)

All pricing assumes H100 SXM at $3.5/hour on RunPod (spot). Re-verify at provision time.

### Gate 1: Admission-control TTFT + 3-model eviction (~$15, ~4h on 1xH100)

Three runs, same H100 pod:
- **A. Baseline vLLM** (no admission): raw `vllm.entrypoints.openai.api_server` at c=64,128,192,256,384 → `results/gate1/vllm_baseline/`
- **B. KVWarden with admission ON**: `kvwarden serve llama-8b --admission-target-ttft 300` → `results/gate1/kvwarden_admission_on/`
- **C. 3-model eviction**: `bash scripts/run_multi_model_demo.sh --models llama-8b,qwen-7b,phi-3 --workload traffic_shift.yaml` → `results/gate1/eviction_comparison/`

**Success criteria:** (1) TTFT p99 at c=256 with admission ON >=2x better than OFF; (2) throughput loss <10% at c=256; (3) KVWarden eviction beats Ollama LRU cold-start count on `traffic_shift.yaml`.

### Gate 2: Heterogeneous 70B+8B on 2xH100 (~$24, ~3.4h at ~$7/h)

```
kvwarden serve \
  --model Llama-3.1-70B-Instruct:tp=2 \
  --model Llama-3.1-8B-Instruct:tp=1,gpu=auto \
  --gpu-budget 92%
python benchmarks/scripts/run_heterogeneous.py \
  --traffic-mix heterogeneous_mix.yaml --duration 15m \
  --output results/gate2/
```

**Success criteria:** both models stay loaded 15 min without OOM; 70B TTFT p50 <2s, 8B TTFT p50 <250ms at mixed load; router correctly distinguishes 70B vs 8B traffic.

### Gate 3: Ollama head-to-head + noise re-runs (~$16, 1xH100)

Ollama same 3 models (`llama3.1:8b`, `qwen2.5:7b`, `phi3:mini`) on `traffic_shift.yaml` → `results/gate3/ollama/`. Then `python scripts/summarize_results.py --check-noise results/gate1/ results/gate2/` and re-run any cell with std-dev >15% from remaining budget.

**Success criteria:** fair Ollama comparison on same workload; no Gate 1/2 cell above 15% std-dev; all runs reproducible from recorded scripts.

---

## 6. Risks & mitigations

| # | Risk (prob.) | Tripwire | Mitigation |
|---|---|---|---|
| 1 | Gate 1 admission shows <2x TTFT improvement (Medium) | End of Week 2 | Pivot to measurement-only preprint (variant B); keep repo public reframed as "measurement + candidate middleware" |
| 2 | vLLM ships native multi-model (Issue #13633) in Phase B (Low-med) | GitHub notification | Differentiation shifts to admission control + tiered cache; emphasize HTTP-boundary layer |
| 3 | Gimlet or Modular announces bare-metal product (Low) | TechCrunch / launch post | Accelerate HN launch by 1 week; OSS nature is the moat |
| 4 | Solo-founder burnout (Med-high) | Missed W2/W3 milestone by >3 days | Drop Gate 2 (70B), ship measurement-only; protect Gate 0 → Gate 1 → HN critical path |
| 5 | GPU cost overrun (Medium) | Spend past $45 with Gate 2 incomplete | Stop provisioning; use Phase 1 + Gate 1 only; defer Gate 2 |

Additional monitors: (a) HN launch post goes to second page in <30 min = signal red; (b) arXiv submission bounces on format = add 24h buffer; (c) landing-page waitlist service fails = the Cloudflare Worker at `waitlist-api/` must be verified working before launch.

---

## 7. Week-by-week Gantt

Current date: 2026-04-18 (Saturday). Week 1 starts Monday 2026-04-20.

```
Week 1 (Apr 20-26, 2026) — GATE 0 + FOUNDATION
  Mon-Tue  [Gate 0: 2-model kvwarden serve on A100 — $4.50]
  Wed      [Curate Gate 0 results, decide variant A/B]
  Thu-Fri  [Lock preprint outline, draft Sec 1-2]
  Sat      [Landing page: fix 95% contradiction, OG image]
  Sun      [Buffer + memory update]

Week 2 (Apr 27 - May 3, 2026) — DRAFT + GATE 1
  Mon-Tue  [Gate 1: admission + eviction on 1xH100 — $15]
  Wed      [Gate 1 data curation, Sec 3-4 draft]
  Thu-Fri  [Preprint Sec 3-4, landing page polish]
  Sat      [Beta reader #1 sends draft, LMCache email]
  Sun      [Buffer]

Week 3 (May 4-10, 2026) — GATE 2 + EVALUATION
  Mon-Tue  [Gate 2: 70B+8B on 2xH100 — $24]
  Wed-Thu  [Sec 5 evaluation with real numbers]
  Fri      [Beta readers #2-3, start revision]
  Sat-Sun  [Revision pass 1, demo video if time]

Week 4 (May 11-17, 2026) — LAUNCH
  Mon      [Gate 3: Ollama comparison + noise re-runs — $16]
  Tue      [Final revision, arXiv submit]
  Wed 09:00 PT  [HN Show post + Twitter thread + /r/LocalLLaMA]
  Thu-Fri  [Respond to comments, issues, triage]
  Sat-Sun  [Metrics snapshot — day 4 signal check]
```

Critical path: Gate 0 → Gate 1 data → Sec 5 draft → arXiv submit → HN post. Everything else is parallel track.

---

## Appendix A: Immediate actions (now, Apr 18-19 weekend)

1. Verify RunPod H100 SXM current price (update budget line in Sec 5 if changed).
2. Email Jay (if the reference is real) to confirm authorship intent — or confirm with master that it's a stray reference.
3. Fix the `PROGRESS.md` known inaccuracies noted in memory (follow-up PR planned separately).
4. Confirm waitlist Cloudflare Worker is live and tested end-to-end.
5. Pre-reserve the arXiv endorsement path (cs.DC requires endorsement for first-time authors).
