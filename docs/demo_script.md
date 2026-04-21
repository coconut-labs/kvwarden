# KVWarden Demo Script (2 minutes)

Target: Developer audience. Tone: technical, direct, no fluff.

---

## SCENE 1 -- The Problem (0:00-0:25)

**ON SCREEN:** Terminal with two tmux panes. Left pane runs vLLM serving Llama 8B. Right pane runs a second vLLM instance for Qwen 7B. Both show startup logs.

**NARRATION:**
"This is how you serve two models on one GPU today. Two vLLM processes, each pre-allocating half the GPU memory with `--gpu-memory-utilization 0.4`. You've just lost 20% of your VRAM to duplicate KV cache overhead. And when traffic shifts from one model to the other? Nothing adapts. The idle instance wastes memory while the busy one queues requests."

**ON SCREEN:** htop-style GPU monitor showing both processes pinned at 40% VRAM each. A curl request to the Qwen instance returns a timeout error.

---

## SCENE 2 -- The Fix (0:25-0:50)

**ON SCREEN:** Clean terminal. Type the commands:

```
pip install kvwarden
kvwarden serve llama-8b qwen-7b --gpu-budget 80%
```

**NARRATION:**
"KVWarden replaces both instances with a single orchestration layer. One command. It launches vLLM or SGLang under the hood, manages model loading and eviction, and allocates GPU memory as a shared pool -- not static partitions."

**ON SCREEN:** KVWarden startup log showing:
```
[kvwarden] Loading llama-8b on gpu:0 (42% VRAM)
[kvwarden] Loading qwen-7b on gpu:0 (38% VRAM)
[kvwarden] Admission controller: max_concurrent=120, target_ttft=200ms
[kvwarden] Serving on http://0.0.0.0:8000
```

---

## SCENE 3 -- Automatic Routing (0:50-1:15)

**ON SCREEN:** Split view. Left: a script sending requests. Right: KVWarden dashboard or log output showing routing decisions.

**NARRATION:**
"Requests come in through a single endpoint. KVWarden routes them to the right model automatically. Watch the routing log -- Llama handles the code generation requests, Qwen handles the Chinese language queries. No client-side logic needed."

**ON SCREEN:** Log output scrolling:
```
[router] POST /v1/chat -> llama-8b (queue: 3, ttft_est: 45ms)
[router] POST /v1/chat -> qwen-7b  (queue: 1, ttft_est: 28ms)
[router] POST /v1/chat -> llama-8b (queue: 7, ttft_est: 89ms)
```

**NARRATION:**
"The router tracks queue depth and estimated time-to-first-token for each model. Clients hit one endpoint. KVWarden decides where the request goes."

---

## SCENE 4 -- The Scheduling Cliff (1:15-1:45)

**ON SCREEN:** The scheduling cliff chart (`figures/scheduling_cliff_detail.png`) appears full-screen.

**NARRATION:**
"Here is why this matters. We profiled vLLM and SGLang on A100 and H100 GPUs. At 128 concurrent requests, you get 5,300 tokens per second with 150 millisecond TTFT. Push to 256 concurrent requests -- throughput barely moves, up 2%. But TTFT explodes to 2.3 seconds. That is an 8x degradation. We call this the scheduling cliff."

**ON SCREEN:** Transition to KVWarden admission control visualization. Requests above the threshold are queued or shed. TTFT stays flat.

**NARRATION:**
"KVWarden's admission controller detects the cliff and holds concurrency below the threshold. Excess requests queue at the middleware layer with backpressure signals, instead of piling into the engine's scheduler where they destroy latency for everyone."

---

## SCENE 5 -- Positioning (1:45-2:00)

**ON SCREEN:** Simple competitive grid:

```
                    Requires K8s?    Multi-Model?    Your Scale?
Dynamo (NVIDIA)     Yes              Yes             Datacenter
llm-d (CNCF)       Yes              1 per pool      Datacenter
Ollama              No               LRU only        Hobby
KVWarden           No               Intelligent     1-4 GPUs
```

**NARRATION:**
"Dynamo and llm-d need Kubernetes. Ollama has no scheduling intelligence. KVWarden is the only option for developers who want smart multi-model orchestration on bare metal. One pip install. No cluster required."

**ON SCREEN:** Final frame with GitHub URL and install command:

```
pip install kvwarden
github.com/coconut-labs/kvwarden
```

**END.**
