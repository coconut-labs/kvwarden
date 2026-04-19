# Gate 0 Launch Post (draft, unshipped)

Target surfaces: HN Show, /r/LocalLLaMA, X thread, Shrey's LinkedIn.

Ship decision after: Gate 0.5 (bench harness fix) + Jay signs off on the scheduling-cliff framing.

---

## HN Show title candidates

1. **"Show HN: InferGrid – we shadow-reviewed our own benchmark and it had been lying for two weeks"**
2. "Show HN: InferGrid – multi-model LLM serving on one box, and the 5 measurement bugs we caught before spending the H100 budget"
3. "Show HN: InferGrid – we profiled vLLM and SGLang and the engines have converged; the real product is the orchestrator"
4. "Show HN: InferGrid – run two LLMs on one A100 without Kubernetes"

Pick #1 for the contrarian/found-and-fixed pull (highest expected engagement now that we have the streaming-bypass + TTFT-v2 stories). #4 is the safe default if we want the pure capability framing. Avoid the original "stays below the scheduling cliff" title until Gate 1 confirms the cliff actually reproduces with honest TTFT measurement — pre-PR-#28 we may have been measuring a network artifact, and the linear dress-rehearsal mock cannot rule that out.

---

## One-line elevator pitch

InferGrid is a pip-installable middleware that keeps vLLM (and SGLang) below the concurrency level where throughput saturates but latency explodes — so two models can share one GPU without a Kubernetes cluster.

---

## Body (post-ready draft, ~450 words)

**Two weeks ago we thought vLLM lagged SGLang by 29% on Llama-3.1-8B. The data said otherwise.**

I've been building InferGrid — a middleware that sits on top of vLLM and SGLang to do three things the big-league orchestrators (Dynamo, llm-d, AIBrix) don't do well: lightweight multi-model serving on 1–4 GPUs, admission control under overload, and bare-metal deployment with zero Kubernetes. The v0 of this thesis was: vLLM leaves 29% throughput on the table vs SGLang, and we can recover it.

I ran the profiling on RunPod A100 SXM and H100 SXM ($18 total). Three concurrency sweeps per engine, 200 requests per level, 2 repeats. Here's what the data actually said:

- **vLLM and SGLang have converged.** At c=128, SGLang hits 5,276 tok/s, vLLM hits 5,334. That's a <2% gap, not 29%.
- **GPUs aren't 81% idle.** They're 95–99% busy. The waste isn't hardware — it's scheduling.
- **There's a clear scheduling cliff.** Going from c=128 → c=256 gains 2% throughput and costs 1,434% TTFT (vLLM A100). Same shape on H100. Same shape on SGLang. **Hardware-independent.**

That last point reshaped the whole pitch. The product is no longer "beat SGLang"; it's "stay below the cliff while multiplexing models on one box."

**So this weekend I ran Gate 0 — the first live GPU bring-up of `infergrid serve`.** Two models, Llama-3.1-8B-Instruct and Qwen2.5-7B-Instruct, co-resident on a single A100-SXM4-80GB. Budget: $4.50. Actual: $5.76, because of five distinct dependency and infrastructure regressions that each bit once (transformers 5.x dropped an attribute vLLM 0.8.5 relies on; numpy 2.4 broke numba; vLLM v1 engine OOMs under co-load; pod lacked SSH port mapping; HF_TOKEN didn't propagate into SSH shells — all fixed in the fix branch, all in the repo).

**Result: system passed. Our benchmark harness broke first.**

- 3h52m server uptime, both engines `healthy: true` the whole way
- 181 requests admitted, 0 rejected, 0 timed out by admission
- 55.7 GB / 80 GB VRAM, matching the 0.35+0.35 config exactly
- 10.2 ms router overhead per model
- No OOM, no crash, no need to restart
- The multi-model bench harness then hung on `alternating | concurrency=1` after the first wave, hit aiohttp's 300s timeout, and never recovered. Deferred to Gate 0.5.

**Then Gate 0.5 (local) reproduced and fixed it** — root cause was the engine stopped returning HTTP response headers; the harness had no idle timeout. We added asymmetric timeouts (sock_read=30s), session reuse, late `response.prepare()`, an engine circuit breaker (R4), a per-bench-phase abort (R5), and a per-engine subprocess log capture so the next "engine went silent" incident leaves evidence. Cut the 301s stall to 33s.

**Then Gate 0.6 (real vLLM, ~$3.17) validated the fix end-to-end.** 75 multi-model requests + 2 smoke = 77 admitted, 0 rejected, 0 stalls, 0 OOMs. Engine stderr captured (234 KB). Throughput at c=1/8/32: 84/270/812 tok/s (alternating two models on one A100; lower than single-model baselines because each engine sees half the load).

**Then we caught two measurement bugs that would have silently corrupted Gate 1.** Both surfaced from a Jay-style shadow review of our own diff:

- **TTFT was lying.** The bench was timing the first SSE `data:` line, not the first non-empty content. Whitespace-only frames (which vLLM emits during warm-up) counted as the first token — `bool(" ")` is True in Python. And on chat-completions endpoints (`delta.content` instead of `text`), TTFT silently collapsed to `total_latency_ms`. Three fixes, one discriminator test that fails loud on the next regression.
- **Admission control was a no-op for streaming.** `route_request` released the admission slot in `finally` — but for `stream=True`, the await returned the generator object immediately, so the slot freed microseconds after acquire. Our smoke poller (added during the fix) confirmed: 149 samples during a c=32 phase, peak in_flight = 0 with cap = 16. **The Gate 1 hypothesis (Arm A cap=128 vs Arm B cap=1024) was unmeasurable** — both arms would have admitted everything to the engine. Wrapping the iterator so admission releases when the stream actually ends took the post-fix peak in_flight to 16 + queue_depth 16, exactly as expected.

Five more Gate-1 plumbing blockers fell out when we ran the dress rehearsal end-to-end (CPU-only, mock backend) for the first time — bash precedence in our own script, a `len(models) < 2` gate that would have killed single-model Gate 1, tenant-budget config that was parsed but never wired to the manager, missing yaml fields, and aiohttp's default `TCPConnector.limit_per_host=100` clamping c=256 to 100. All fixed; dress rehearsal now exits OVERALL: PASS.

We could have spent the $7-10 H100 budget on garbage data. Instead the H100 spend goes against an experiment that's actually wired correctly.

Repo: **github.com/coconut-labs/infergrid**
Full reading list:
- `results/gate0_20260418/GATE0_OUTCOME.md` — Gate 0 post-mortem (+ 6-run recovery log)
- `results/gate06_20260419/GATE06_OUTCOME.md` — Gate 0.6 validation
- `results/CORRECTIONS.md` — every misleading number in the artifacts and what it actually means
- `docs/launch/gate1_runbook.md` — the runbook for the H100 spend, including how to read the result (CONFIRM vs DISCONFIRM vs PLUMBING-REGRESSION)

Next: Gate 1 on H100 SXM5 (~$7-10, ~1.8h, 3-layer cost cap at $12). arXiv preprint draft after.

Happy to discuss anywhere — the "engines have converged, the scheduling is the product" framing feels like the least-crowded corner of this market. Curious what you're seeing.

— Shrey

---

## Response playbook (top-5 expected questions)

**Q: Why not just use Dynamo / llm-d?**
A: Both require Kubernetes. For 1–4 GPUs on a single box, K8s is pure overhead. Lightweight orchestration is a real gap in the landscape (see `docs/inference_orchestration_gaps_report.md`).

**Q: Is this just Ollama with extra steps?**
A: Ollama is LRU model-swap + llama.cpp. We're frequency+recency multi-model + vLLM/SGLang + admission control + KV tiering (stub). Different problem: Ollama optimizes for hobbyists on a single prompt; InferGrid for teams sharing a box under concurrent load.

**Q: Why should I care about the scheduling cliff when I can just run at lower concurrency?**
A: Because the engines' default concurrency is at or above the cliff. Most people don't know they're there. Our admission controller holds the line for you.

**Q: When will this be usable?**
A: `infergrid serve --config ...` works today on any A100 (see PRs #19 and #26 for reproduction). Gate 1 H100 admission-TTFT validation is the next paid run. Production-quality deployment is post-Gate-3.

**Q: vLLM 0.8.5 is ancient. Why?**
A: That's what the Phase 1 profiling pinned. Upgrade is on the roadmap once Gate 1 data lands. Newer vLLM avoids the compat issues we hit; we have pins in `requirements-gpu.txt` that cover 0.8.5 specifically.

**Q: Your discriminator test catches the bug, but how do you know YOUR fix is right?**
A: We don't, with certainty. That's why every fix lands with a test that fails LOUD on the unfixed code. The streaming-admission test fails on the pre-fix router with `in_flight == 0` (peak should be > 75% of cap). The TTFT v2 test reports `tokens_out=0` for the chat-shape case on the pre-PR-#31 bench. If a future fix regresses, CI catches it. We can't promise the next bug doesn't exist; we can promise we won't silently re-ship the ones we found.

**Q: "Admission control was a no-op for streaming" sounds like a fundamental design flaw, not a fix-and-move-on bug.**
A: Yes. It means our PR #29 wasn't a one-line patch — it was a full architectural review of where slots are held vs released, plus tests for GC-path leaks and a max-stream-duration fence. Three follow-up PRs (#30, #32, #33) closed adjacent issues the same review surfaced. The whole thread is in the repo with discriminator tests and CORRECTIONS.md C5.

---

## X thread outline (5 tweets, found-and-fixed framing)

1. "Hook: We were about to spend $7-10 on an H100 to test our admission controller. Then we ran our own pre-flight script for the first time. The admission controller had been a no-op for streaming traffic. <link>"
2. "How: route_request released the slot in `finally`, but for streaming the await returned the generator object immediately — slot freed microseconds after acquire. Smoke poller confirmed: 149 samples, peak in_flight=0 with cap=16. PR #29."
3. "Also fixed: TTFT was timing the first SSE frame, not the first non-empty content. `bool(\" \") == True`, so whitespace-only frames counted as the first token. On chat-completions, TTFT silently collapsed to total_latency. PRs #28 + #31."
4. "Five more plumbing blockers fell out when the dress rehearsal ran end-to-end for the first time. Bash precedence in our own script, a single-model gate that would've killed Gate 1, tenant config never wired to the manager, missing yaml fields, aiohttp limit_per_host=100 silently clamping c=256."
5. "Why post about bugs we found in our own work? Because the only way you trust the H100 numbers when they land is if you trust how we found everything else. CORRECTIONS.md is the receipt. Repo: github.com/coconut-labs/infergrid"

---

## Timing

Per Phase B roadmap: HN post target **2026-05-13 09:00 PT**. ~24 days out from 2026-04-19.

**Do not ship until:**
- [x] Gate 0.5 bench fix merged (PRs #21-25)
- [x] Gate 0.6 real-vLLM validation passed (~$3.17, A100-SXM4)
- [x] Streaming-admission and TTFT measurement bugs caught and fixed pre-launch (PRs #28-33)
- [x] Gate 1 dress rehearsal exits PASS end-to-end (PR #34)
- [x] Cost-cap defense in pod bootstrap (PR #35)
- [ ] Gate 1 H100 result in hand (CONFIRM, DISCONFIRM, or PLUMBING-REGRESSION per `docs/launch/gate1_runbook.md`)
- [ ] Jay reviews the new framing (the streaming-bypass beat is now the lead, not the 29% number)
- [ ] PyPI `infergrid` placeholder uploaded (squat-protection before HN visibility)
- [ ] Landing page refresh live + waitlist Cloudflare Worker deployed (currently `window.WAITLIST_API = ''` silently drops submissions)

**Branching the post on Gate 1 outcome:**
- **CONFIRM** (Arm A flat across c=128/256, Arm B explodes): keep title #1 OR pivot to a Gate 1 results-driven title; the streaming-bypass story is the supporting beat
- **DISCONFIRM** (both arms flat): title #1 or #4; lead with the found-and-fixed story, treat the H100 result as "we instrumented honestly and the cliff didn't reproduce — admission's value at this scale is smaller than projected, and that's still publishable"
- **PLUMBING-REGRESSION** (admission gauge stays at 0 on real vLLM despite the unit tests): hold the post; file an issue on the actual regression first
