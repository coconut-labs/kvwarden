# 1:1 onboarding playbook

30-minute Zoom. Goal by minute 30: kvwarden is running against their real traffic and they've seen one graph that proves it. Not a demo, not a deck — their engine, their tenants, a dashboard they can keep.

If you can't get there in 30 min, that's fine — schedule a 15-min followup. What you want to avoid is ending the call with "I'll send you some docs and we'll pick this up later."

---

## Pre-call (ask them to have ready ~10 min before)

Send this in the calendar invite body:

- URL of their vLLM / SGLang / TRT-LLM endpoint (or willingness to screenshare the box)
- A scratch YAML listing the tenants they want to shape — just tenant names and rough per-tenant RPS / token budgets. No schema needed, pseudocode is fine.
- SSH access to the inference box **or** readiness to screenshare with a terminal open. Either is fine.
- Admin perms on the box (sudo or container exec) — the install writes one systemd unit or runs as a sidecar container.
- 5 minutes of "real-ish" load they can replay or tail, so we can watch a before/after graph. If they don't have load, we'll synthesize it.

If they don't have the endpoint URL by T-minus-5, start the call anyway — you can spend min 0-10 helping them locate it, that's still useful.

---

## Minute 0-5: intro

- Who you are, one sentence: "I'm the founder. I built kvwarden because I watched my last team's inference bill get eaten by one noisy tenant starving everyone else."
- What 30 minutes buys them: "By the end of this call, kvwarden is running against your engine with your tenants and you'll see whether it helps. If it doesn't, that's useful data for me and you've lost 30 minutes."
- One question: "Before we go — what does a bad day look like in your current setup?" (This is the real discovery question. Write the answer down verbatim — it goes into `discovery-notes.md`.)

---

## Minute 5-15: diagnose

- Have them run `kvwarden doctor` against their endpoint. This checks version, reachability, tenant detection heuristics, and flags anything weird (wrong vllm version, blocked port, whatever).
- Pick the closest config template from `configs/` — most users land on one of:
  - `configs/quickstart_fairness.yaml` — single engine, handful of tenants, no SLA tiers yet. Start here for most first-timers.
  - `configs/gate2_fairness_token_bucket.yaml` — tenants with clear per-tenant RPS budgets; the reference fairness config from the launch hero.
  - `configs/gate2_fairness_drr.yaml` — tenants with very different token-size profiles (short chat vs long RAG). DRR handles mixed sizes better than token-bucket.
  - `configs/gate2_multi_tenant.yaml` — 6+ tenants, want hierarchical fairness.
- Edit the template live with them — replace the example tenants with their real ones. Don't over-engineer; one per-tenant cap is enough for day one. They can tighten later.

If `kvwarden doctor` flags something the playbook doesn't cover, capture it in `discovery-notes.md` as "Install blocker hit: yes" and either fix it in-call or promise a patch by next morning.

---

## Minute 15-25: stand up and watch

- Start kvwarden as a sidecar to their engine (same box, same network namespace is easiest).
- Point their client traffic at kvwarden's port instead of the engine's. If they're nervous, run both side-by-side with 10% mirrored traffic first.
- Run a brief `reproduce-hero` equivalent — the `docs/reproduce_hero.md` script against their setup, scaled down if their hardware is smaller. Or just watch their real traffic tail for 3-5 minutes.
- Pull up the Grafana dashboard (JSON in `docs/grafana/`) — the one they want to see is per-tenant p95 latency with the fairness toggle. When the graph moves, they'll notice before you point it out.

If the graph doesn't move: their workload may not be saturated. That's fine — note it, promise a followup once they have real load, move on.

---

## Minute 25-30: capture and close

- Lock the next check-in date. Default: 7-day check-in email + 14-day Zoom. Put it on the calendar live.
- Ask for permission to quote them if they said something quotable. Exact phrasing: "Can I quote the thing you said about FIFO starvation, with your name and company?" Most say yes.
- Confirm the testimonial ask explicitly — getting a "yes, you can ask me again in 30 days" is a real answer.
- Thank them, end the call on time. Going over feels polite but the next call is in 30 min and you need write-up time.

---

## Post-call (next 24 hours)

- Write the `docs/ops/discovery-notes.md` entry. Do this first, while the call is warm.
- Update the matching row in `docs/ops/installs.csv` — set `onboarded_at`, `install_status`, `config_used`.
- Send the followup email with whatever you promised (dashboard JSON, docs link, patch). Under-promise during the call, over-deliver in the email.
- If a doc gap surfaced, file an issue. Don't fix it at 11pm.

---

## Red flags — abort the call politely

Not every waitlist signup is a fit. The following mean kvwarden is the wrong tool and the founder should route them elsewhere and move on. Don't try to retrofit the pitch:

- **"I want an OpenRouter-style hosted SaaS where I pay per token and don't run my own GPUs."** Route to OpenRouter / Together / Fireworks. kvwarden is self-hosted middleware; there's no hosted tier and won't be for the foreseeable future.
- **"I want routing by cost across providers (GPT-4 vs Claude vs open-source)."** Route to LiteLLM or Portkey. kvwarden is about fairness inside a single engine, not cross-provider routing.
- **"We have 100+ tenants on a multi-node Kubernetes cluster with autoscaling."** Route to llm-d or NVIDIA Dynamo. kvwarden's sweet spot is 1-10 boxes and <20 tenants; above that the datacenter tools are the right answer and you'll be fighting their strengths.
- **"I just want a better load balancer across replicas."** Route to a plain nginx/envoy setup or the engine's built-in LB. kvwarden is per-tenant fairness, not replica balancing.

Polite exit script: "Honestly, the tool for that is [X], not kvwarden. We're deliberately narrow — single-to-few-box fairness for teams with a handful of tenants. Happy to intro you to the [X] folks if useful." This protects both sides: they don't waste a week installing the wrong thing, and you don't get a bad 7-day check-in from someone who was never a fit.

---

The goal is 10 of these in W1. If you can't get to 10 because of inbound volume, run them sequentially and queue the rest for W2 — don't rush the quality of the first ones to chase the count.
