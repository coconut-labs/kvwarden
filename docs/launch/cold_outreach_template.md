# Cold outreach template — customer discovery interviews

**Purpose:** 20-minute calls with ML engineers and infra leads running multi-tenant LLM inference on shared GPUs. Goal is to validate (or kill) the ICP hypothesis: *small ML teams serving 2-8 models on shared GPUs*. Not a sales call. Not a pitch.

---

## How to customize per recipient

- **Change:** the first sentence's hook — mirror something specific you saw in their job post, GitHub profile, blog, or talk. A name-drop of their stack beats a generic "saw your team is hiring."
- **Keep constant:** the hero number (1,585 → 61.5 ms), the engine/model (vLLM 0.19.1, Llama-3.1-8B), the ask (20 min, no deck), and the honest framing that you're researching whether the problem shows up in their traffic.
- **Do NOT say:** "I'd love to demo," "we've built the future of," "beta access," or anything implying a sales motion. No calendar links in the first email — let them propose.

---

## The email (5 sentences)

**Subject:** quick question on multi-tenant vLLM at [company]

Hi [name],

I'm a solo engineer researching how small ML teams handle tenant fairness when a few models share one GPU — I just shipped a reproducible measurement where, on vLLM 0.19.1 serving Llama-3.1-8B on one A100, a quiet user's p99 TTFT drops from 1,585 ms to 61.5 ms once a per-tenant rate limit sits at the admission gate. I'm trying to figure out whether that starvation pattern shows up in real production traffic, or whether I've only found a benchmark artifact. You look like someone who'd actually know: you [one-sentence specific observation from their work — e.g., "run inference for multiple customer models at [company]" or "maintain [open-source router]"]. Could I steal 20 minutes to ask about your setup — no deck, no pitch, just questions? If the answer is no that's fine, and if the answer is yes I'll send a Google Meet for whatever slot works.

Thanks,
Shrey Patel
patelshrey77@gmail.com — https://kvwarden.org

---

## Target-list scaffolding — 10 patterns to look for

Don't hunt for specific names yet — build the list by scanning these patterns. Aim for ~30 candidates, then trim to the 10 you have the most specific hook for.

1. **Startups mentioning vLLM or SGLang in their job posts.** Search LinkedIn or Wellfound for "vLLM" / "SGLang" in the last 90 days. Multi-tenant signal = they list "multi-model" or "customer-specific" serving.
2. **Open-source maintainers of multi-model routers or gateways.** GitHub search for repos tagged `llm-gateway`, `model-router`, `inference-proxy` with ≥50 stars and commits in the last 30 days.
3. **ML infra teams at Series A/B companies serving multiple customers' models.** Think fine-tuning-as-a-service, model-hosting, voice / agent platforms — anyone where each customer has "their" model.
4. **Founding engineers at AI-product startups that moved off OpenAI.** Blog posts titled "why we moved off OpenAI" or "our self-hosted LLM stack" tend to name the engineer.
5. **Authors of public inference benchmarks or cost comparisons.** If someone published a "we benchmarked vLLM at X RPS" writeup in the last 6 months, they have real traffic and opinions.
6. **Contributors to vLLM / SGLang / TensorRT-LLM scheduler code.** Pull the last 50 merged PRs on each repo — contributors who touched scheduler or admission logic are the ones who'll push back on your hypothesis, which is what you want.
7. **ML platform leads at mid-size AI labs (not FAANG).** 50-500 person companies where one person owns the serving stack. Too small and they use OpenAI; too big and they have a dedicated infra team with their own opinions.
8. **Founders of voice-AI, agent-platform, or copilot companies.** Multi-tenant is structural for them — every customer gets their own fine-tune or agent policy.
9. **Developer-experience engineers at LLM API resellers.** OpenRouter-adjacent companies, Together-adjacent, Fireworks-adjacent. They have tenant fairness as a first-class concern.
10. **Authors on LinkedIn / Twitter of "how we run N models on one GPU" posts.** Tactical blog posts from the last 12 months. The author already self-identified as someone who thinks about this problem.

---

## After they reply

- If they agree: send a Google Meet link for the slot they proposed. No prep doc. Show up with three open questions: *what does tenant isolation look like in your traffic today? what's the failure mode that actually pages you? if I gave you a knob for per-tenant rate limits at admission, what would you want it to do?*
- If they ghost: one follow-up after 7 days. One more after 14. Then drop it.
- If they say "sounds interesting but too busy": ask if there's someone on their team who owns serving. Warm intro is worth 10x a cold one.

---

## Tracking

Keep one spreadsheet. Columns: name, company, role, hook (the specific thing you mentioned), date sent, date replied, call date, notes, follow-up status. Review weekly. A 15% reply rate is good; below 5% means the hook isn't specific enough.
