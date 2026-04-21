# InferGrid name-collision audit

**Verdict: RENAME-URGENT**

A senior commercial user — `infergrid.net`, operated by Samuel J. Bell (FAIR / Meta AI postdoc, Cambridge ML PhD) — has been selling an **identically named** product in the **adjacent LLM-inference-optimization space** since **2025-08-23**, roughly eight months before our `infergrid.org` went live on 2026-04-20. The site is an active landing page with a Calendly founder-call booking (`samueljamesbell`), "© 2025" footer, and copy that reads "InferGrid — Optimize inference costs and maximize performance … Automatically route to cost-optimal model … Stop overpaying for model inference." Under US common-law trademark (Lanham Act §43(a)), rights accrue through use in commerce and not registration — Bell has the senior claim. Neither party has filed a USPTO mark, but the mark, class (software / SaaS, Nice 9 + 42), and customer are all the same. This is the kind of collision that results in a cease-and-desist the moment either of us gets visible traction, and it will come up in Series A diligence.

There is no upside to fighting this. Rename now — before the Show HN / Twitter launch, before the trademark filing window, before any press uses the name.

## Evidence table

| Check | Status | One-line finding | Source |
| --- | --- | --- | --- |
| PyPI `infergrid` | CLEAR | Sole owner: Shrey Patel <patelshrey77@gmail.com>, v0.1.2 live | https://pypi.org/pypi/infergrid/json |
| PyPI `infer-grid` / `infer_grid` | CLEAR | 404 — no package | https://pypi.org/pypi/infer-grid/json |
| npm `infergrid`, `infer-grid`, `infer_grid` | CLEAR | 404 — no package | https://registry.npmjs.org/infergrid |
| npm `@infergrid/*` scope | CLEAR | 0 results in scope search | https://registry.npmjs.org/-/v1/search?text=scope:infergrid |
| DockerHub `infergrid/*` namespace | CLEAR | Namespace empty, no images | https://hub.docker.com/v2/repositories/infergrid/ |
| DockerHub keyword search | CLEAR | 0 results for "infergrid" | https://hub.docker.com/v2/search/repositories/?query=infergrid |
| GitHub repos named "infergrid" | CLEAR | Only `coconut-labs/infergrid` and `coconut-labs/infergrid-root` | https://github.com/search?q=infergrid&type=repositories |
| GitHub user `@infergrid` | WARN | Account exists, created 2026-03-28, 0 repos / 0 followers / no bio — a squatter or auto-registered shell | https://github.com/infergrid |
| GitHub code-search hits | CLEAR | All hits are coincidental camelCase `inferGrid` matches in unrelated projects (G2 charts, puzzle solver, etc.) | https://github.com/search?q=infergrid&type=code |
| **infergrid.net — branded product** | **COLLISION** | **Active landing page, "InferGrid — Peak Performance at Lowest Cost", booking Calendly founder calls, © 2025. Same market (LLM inference cost optimization). Domain created 2025-08-23, predates ours by ~8 months. Founder: Samuel J. Bell (FAIR/Meta, Cambridge ML PhD)** | https://infergrid.net |
| infergridplay.com | WARN | Separate strategy-grid game, also using "InferGrid" brand, created 2024-12-08. Different vertical so lower direct collision, but adds SERP noise. | https://infergridplay.com |
| infergrid.com | WARN | Registered 2024-12-31 via Domain Cost Club, redirects to `/lander` (parked monetization page) — domainer holding for sale | whois infergrid.com |
| infergrid.org | CLEAR | Held by us via Cloudflare Registrar since 2026-04-20 | whois infergrid.org |
| infergrid.ai | WARN | Registered 2026-03-28 at Namecheap, parked on Namecheap default IP (192.64.119.27). Unknown third-party holder — same registration date as the squat GH `@infergrid` account. Verify manually whether Shrey owns it; if not, treat as speculator/squatter. | whois infergrid.ai |
| infergrid.io | WARN | Registered 2026-03-28 at Namecheap, parked (162.255.119.58). Same unknown-third-party caveat as .ai. | whois infergrid.io |
| infergrid.dev | CLEAR | Not registered — DNS does not resolve, no whois record in the Google Registry | dig infergrid.dev |
| infergrid.net | COLLISION | Owned by the competitor above. | whois infergrid.net |
| USPTO — "infergrid" mark | CLEAR | No live or dead records on Justia / USPTO report aggregators. No one has filed yet. First to file gets the mark. | https://trademarks.justia.com + https://uspto.report/TM/ |
| USPTO phonetic neighbors (informational) | WARN | `INFOGRID` (class 42 — utility software, InfoGrid Inc.), `INVIGRID` (cloud security), `INFIGRID` (IT services India), `INTERGRID`, `INFRAGRID`. Visual neighbors exist but are different enough that USPTO likely would not reject `INFERGRID` on 2(d) grounds alone. | https://www.invigrid.com, https://infogridit.com |
| Twitter/X `@infergrid`, `@infer_grid`, `@infergrid_ai`, `@infergridhq`, `@infergridai` | UNVERIFIED | x.com is an SPA and returns HTTP 200 for every path — check these by hand in a browser. Assume at least one is cybersquatted given the .com / .ai / .io pattern. | https://x.com/infergrid |
| Google SERP first 3 pages | COLLISION | Bell's infergrid.net is a top result alongside InfoGrid, InviGrid, InfiGrid Solutions. The SERP is already crowded with grid-suffix AI/utility brands. | Google "infergrid" |
| Acronym/jargon collisions (medical/finance/defense) | CLEAR | No standard industry acronym maps to "infergrid" or "IG"-prefix collisions in medical/finance/defense that would be confusing. "IFG" = Impaired Fasting Glucose but unrelated as a wordmark. | https://www.acronymfinder.com/IFG.html |

## Cost-to-rename estimate

**Assets to rewire**

- PyPI: deprecate `infergrid` (0.1.2, current package — leave as a redirect stub pointing users at the new name; push 0.0.1 of the new name). ~1 hour.
- GitHub: rename `coconut-labs/infergrid` and `coconut-labs/infergrid-root` — GitHub sets up permanent redirects, no break. ~15 min each.
- Domain: buy `<newname>.org` (and .com/.ai/.dev for defense) via Cloudflare Registrar. 1 hour + $40–$120 depending on TLD pool.
- Cloudflare Worker: rename `infergrid-waitlist.shrey77-wrk.workers.dev` → `<newname>-waitlist.*.workers.dev`. ~30 min.
- Landing page: global find-replace on `infergrid.org` repo — brand text, OG/twitter meta, favicon if it has the wordmark, copy. ~2 hours.
- Docs + README + pitch.md + demo_script.md + launch drafts (`docs/launch/`): ~150 occurrences in the tree, use `git grep -l infergrid | xargs sed -i` with manual review. ~2–3 hours.
- Python package namespace: `src/infergrid/` → `src/<newname>/`, update imports, tests, `pyproject.toml`. ~2 hours.
- Config files (`configs/*.yaml` reference engine names with the grid brand). ~30 min.
- Twitter/X, LinkedIn, any pre-registered handles: re-reserve new handles. ~30 min.
- Internal artifacts (PROGRESS.md, memory notes, PR history): leave as-is — historical record.

**Breaking users**

Near-zero. PyPI has 0.1.2 live but no meaningful install base yet (the launch hasn't happened). The CF Worker waitlist is still unwired (`WAITLIST_API = ''`). This is the cheapest possible moment to rename. Every day after Show HN this cost multiplies.

**Total engineering time**: ~1 working day (8 hours) of focused work.
**Total cash cost**: ~$50–$120 for defensive domain purchases. No legal cost if we rename before launch; a late rename after a C&D could run $5–20k in outside counsel.

## Recommended alternatives

All verified with PyPI + npm + `dig` for .com/.ai/.dev on 2026-04-21. None hit the USPTO wordmark database via Justia lookups. Priority order reflects founder brief — "shared-GPU fairness" / "multi-tenant LLM serving."

| Name | Thesis fit | PyPI | npm | .com | .ai | .dev | USPTO wordmark | Pronounceable |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| **tenantkv** | Multi-tenant + KV cache — the exact contribution | Free | Free | Unregistered | Unregistered | Unregistered | No hit | "tenant-K-V", clean three-syllable |
| **gpufair** | Tenant-fair on a GPU — literal thesis | Free | Free | Unregistered | Unregistered | Unregistered | No hit | "GPU-fair", two-syllable, reads well |
| **gpucommons** | Shared GPU as a commons — evokes community + sharing | Free | Free | Unregistered | Unregistered | Unregistered | No hit | "GPU-commons", confident brand voice |
| **coslice** | Co-tenants sharing a GPU slice — subtle, ownable | Free | Free | Unregistered | Unregistered | Unregistered | No hit | "co-slice", two syllables, distinctive |
| **tenantgrid** | Closest semantic bridge from InferGrid if founder wants continuity | Free | Free | Unregistered | Unregistered | Unregistered | No hit | "tenant-grid", clean |
| **parceltok** | KV-parcels routed per tenant token-stream — technical, memorable | Free | Free | Unregistered | Unregistered | Unregistered | No hit | "parcel-tok", novel |
| **quorumllm** | Fair share of quorum across LLM tenants; flags conflict with Consensys' QUORUM (blockchain, class 9) | Free | Free | Unregistered | Unregistered | Unregistered | QUORUM (blockchain) is near-neighbor — weaker | "quorum-L-L-M" |

**Dropped candidates**: `kvshare` (used as the exact name in an arXiv paper on LLM multi-tenant KV cache, direct academic collision); `slicefair` (taken by an equity-split SaaS); `fairshare` (`.com` and `.ai` both taken by financial SaaS); `fairserve`, `sliceserve`, `equiserve`, `fairtokens`, `multigpu`, `sharegrid`, `plexshare`, `tierra`, `fairlane`, `tokenfair`, `quantgrid`, `partshare`, `quorumai` (.com all taken or registered speculators).

**Tagline candidates tuned to each**:

- tenantkv — *"Multi-tenant KV cache that pays for itself on day one."*
- gpufair — *"One GPU. Ten tenants. Fair share of every token."*
- gpucommons — *"The shared GPU your tenants deserve."*
- coslice — *"Your GPU, co-sliced fairly."*

## Recommended action (3 lines)

1. Pause Show HN and the public launch drafts in `docs/launch/` — every day the old name ships it costs more to unwind.
2. Pick the replacement (my personal lean is **gpufair** or **tenantkv** — both are literal, both are free across PyPI / npm / domains, both ride the exact thesis). Buy `<name>.org` + `.com` + `.ai` + `.dev` today; reserve PyPI + npm + GitHub org same day.
3. Reach out to Samuel Bell at `infergrid.net` after the rename is committed — a friendly "we're moving off the name, heads-up" email is cheap insurance against future confusion and is how I'd want the situation handled if the roles were reversed.
