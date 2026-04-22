# Discovery call notes

## How to use this file

One section per 1:1 onboarding call. Append; don't edit earlier entries. Keep it terse — the point is that six weeks from now you can grep this file for "wants cost routing" or "Mixtral 8x22B" and remember which conversation it was. Always ask permission before quoting anyone; default assumption is the notes stay private.

If a call produces a testimonial-worthy line, copy it verbatim into the "Quote-worthy" field and confirm permission in the followup email. Everything here feeds `docs/ops/installs.csv` — after each call, update the matching row.

Template at the bottom of this file. Copy it, paste, fill.

---

## 2026-04-28 — alex chen (example-ai, small vertical SaaS)
**Role:** solo platform eng, 4-person team, 3 customer tenants + 1 internal eval tenant on 1x A100
**Tenant setup:** 4 tenants on vllm 0.19.x, Llama-3.1-8B-Instruct, peak ~30 RPS aggregate, bursty
**Current pain:** one noisy tenant starves the other three during retrieval bursts; tried vllm request priorities but it's per-request not per-tenant
**Tried before:** nothing real — read the llm-d docs, decided the Kubernetes lift wasn't worth it for a single box
**kvwarden fit:** yes — exactly the shape we built for. Token-bucket config off the shelf.
**Install blocker hit:** no — `quickstart_fairness.yaml` worked; had to bump the per-tenant cap once
**Would pay for support:** maybe — "if this holds for a month we'd consider a $500-1000/mo support line to skip the self-host maintenance"
**Followup:** send a 7-day check-in email 2026-05-05; share the Grafana dashboard JSON
**Quote-worthy:** "FIFO starvation was our single biggest support ticket category last quarter. This fixes it." — asked permission, she said yes with name + company

## 2026-04-29 — priya devops (synthco, RAG startup)
**Role:** eng lead, 6 internal tenants (each an internal product surface) on 2x 4090
**Tenant setup:** sglang 0.4.x, Qwen2.5-14B, each tenant has different token budgets and quality SLAs
**Current pain:** new product surface ships every two weeks and capacity planning is done in a spreadsheet; no fairness guarantee means a shipped-Tuesday feature can flatten a shipped-last-month feature
**Tried before:** homegrown python rate limiter in front of sglang, didn't handle in-flight requests correctly
**kvwarden fit:** partial — sglang adapter is newer than vllm, some config knobs weren't obviously exposed (max_running_requests surfaces through a different YAML key). Worked it out live.
**Install blocker hit:** yes — YAML key confusion for the sglang-specific cap; 5 min to resolve on call. Filed follow-up to add a doc note.
**Would pay for support:** no — internal team, not a commercial fit, but willing to be a design partner for sglang adapter work
**Followup:** open a docs issue for the sglang YAML key naming mismatch; invite them to the 2026-05-15 sglang adapter feedback call
**Quote-worthy:** none requested yet — too early

---

## Template (copy this for each new call)

```markdown
## 2026-MM-DD — firstname lastname (company)
**Role:** ...
**Tenant setup:** N tenants, M GPUs, what models
**Current pain:** (what made them click the waitlist?)
**Tried before:** Dynamo/llm-d/homegrown/nothing
**kvwarden fit:** yes / partial / no — and why
**Install blocker hit:** yes/no — describe
**Would pay for support:** yes/no/maybe — range if yes
**Followup:** what I promised to send them, by when
**Quote-worthy:** any verbatim line I can use (with permission)
```
