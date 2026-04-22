# Token rotation checkpoint — 2026-04-21 post-migration

Migration from `infergrid` → `kvwarden` is done on both main and LP repos. Every credential that was pasted into a chat session during the migration window is listed below with a preference: **deprecate (revoke, do not replace)** unless another running service depends on the credential, in which case **rotate (replace with a new value)**.

**Founder preference:** deprecate everything that isn't actively needed by a running inter-service dependency.

## Must rotate (live inter-service dependency)

| Credential | Used by | How to rotate |
|---|---|---|
| **Resend API key** (`re_185s...`) | The `kvwarden-waitlist` Worker's `RESEND_API_KEY` secret. Worker calls Resend on every `/api/notify` to send emails from `hello@kvwarden.org`. Without a valid key, waitlist-announcement emails fail. | https://resend.com/api-keys → Create → set name `kvwarden-waitlist-YYYYMMDD`, sending scope, domain `kvwarden.org`. Then: `export CLOUDFLARE_API_TOKEN=<new-cf-token>; cd landing_page/waitlist-api; echo <new-resend-key> \| npx wrangler secret put RESEND_API_KEY`. Revoke the old key in Resend dashboard after verifying a test notify works. |

## Safe to deprecate now (no running service depends)

| Credential | Why deprecate | Action |
|---|---|---|
| **Cloudflare account token** (`cfat_uUsZ...`) | Was used only to inventory the account and run the migration. The account token has narrower scopes than the user token anyway. | Revoke: https://dash.cloudflare.com/profile/api-tokens → find the token → Delete. |
| **Cloudflare user token** (`cfut_euNy...`) | Was used for the DNS + Workers + D1 migration. Everything is now deployed; the platform serves traffic without the token. Next config change generates a fresh token. | Same dashboard URL → Delete. If you need future CF changes via API, create a new scoped token at that time. |
| **Cloudflare user token — older** (`cfut_L7eP...`) | The first CF token pasted in session, noted in `docs/runbooks/secrets.md` as having `expires_on` Apr 19–21, so likely already dead. | Confirm it's gone from the token list; Delete if present. |

## Keep but rotate opportunistically

| Credential | Why keep | Rotation trigger |
|---|---|---|
| **HuggingFace token** (`HF_TOKEN`) | Read access to gated Meta Llama models. Needed any time we spin a bench pod that downloads Llama-3.1-Instruct. Low blast radius (read-only). | Rotate when a bench agent stalls and may have leaked the token in a log, or on 90-day cadence regardless. |
| **RunPod API key** (`RUNPOD_API_KEY`) | Used to provision GPU pods for gate benches. No pods currently running; balance ≈ $94. Blast radius bounded by remaining balance. | Rotate if chat transcripts are shared externally or if balance starts draining unexpectedly. |
| **PyPI tokens** (project-scoped for `infergrid`, and account-scoped `PYPI_KVWARDEN_TOKEN` for `kvwarden` first upload 2026-04-22) | Used to publish new package versions. Not actively in a running service. Supply-chain risk if leaked (malicious version could be pushed). | **kvwarden: 0.0.1 stub shipped 2026-04-22 — narrow the account-scoped token to project-scoped (`kvwarden`) and revoke the account-scoped one BEFORE the 0.1.0 release.** Old `infergrid` token should be revoked after the final `infergrid` 0.1.3 redirect-stub release. |
| **Docker Hub PAT** (`DOCKER_HUB_TOKEN` for user `kvwarden`, reserved 2026-04-22) | Will be used when we push the first kvwarden container image. Namespace reservation only today — no images yet. Same supply-chain risk as PyPI if leaked (malicious image could be pushed to a reserved namespace). | Rotate after the first image push by narrowing the PAT to read/write on a single repo (e.g. `kvwarden/kvwarden`) instead of account-wide. Rotate also if the value is shared externally. |

## Deprecation order (recommended sequence, all doable in one sitting)

1. **Verify the waitlist still works** before touching anything — POST `/api/subscribe` with a test email; confirm the row lands in D1. This guards against a rotation breaking production.
2. **Rotate Resend first** — generate new key, set via `wrangler secret put`, verify a test notify succeeds, then revoke the old.
3. **Delete both CF tokens** in the dashboard — `cfat_` and both `cfut_` values.
4. **PyPI / RunPod / HF** — rotate when you hit a use case that needs them. Don't deprecate proactively; they're still safe at rest.
5. **Delete `~/.infergrid/creds.md` and `~/.infergrid/secrets.env`** if you want zero local persistence of the pasted values. Recreate from scratch with fresh values when needed. (Optional — local file at mode 600 is fine to keep.)

## What won't break when you revoke the CF tokens

Post-migration, the running services are:

- `kvwarden.org` LP served by Worker `infergrid-root` → **no token needed** to serve traffic
- `kvwarden-waitlist.shrey77-wrk.workers.dev` Worker with RESEND/ADMIN secrets set → **no token needed** to receive signups / send notifications (secrets live in CF's side, not in the token)
- `infergrid.org → kvwarden.org` 301 via Worker `infergrid-redirect` → **no token needed** to serve
- D1 database `infergrid-waitlist` (UUID `d78fc056-...`, bound to the waitlist Worker) → **no token needed** for reads/writes by the Worker
- DNS records on both zones → **no token needed**

The tokens are only required the next time you want to CHANGE one of these. Which for normal operation is rarely.

## What WILL need a fresh token

- Deploying a code update to either Worker (`wrangler deploy`)
- Adding/modifying DNS
- Rotating Worker secrets
- Creating new D1 databases
- Renaming Workers (`infergrid-root` → `kvwarden-root` cosmetic cleanup)

Generate a fresh scoped token when any of these come up.

---

## Credential inventory for audit

All values are stored at `~/.infergrid/creds.md` (mode 600, outside any git tree). This list is just the names and scopes; never paste actual values here.

- `CLOUDFLARE_API_TOKEN` (user token, `cfut_euNy...`)
- `CLOUDFLARE_ACCOUNT_ID` (`81376...`) — public ID, not a secret
- `CLOUDFLARE_ZONE_ID_KVWARDEN`, `CLOUDFLARE_ZONE_ID_INFERGRID` — public IDs
- `HF_TOKEN` (HuggingFace, bench use)
- `RUNPOD_API_KEY` (RunPod, bench use)
- `RESEND_API_KEY` (Resend, LIVE — Worker depends on it)
- `KVWARDEN_ADMIN_KEY` (generated locally, set on Worker, stored locally so admin endpoints can be called)
- `DOCKER_HUB_TOKEN` (Docker Hub PAT for user `kvwarden`, reserved 2026-04-22; no images pushed yet)
