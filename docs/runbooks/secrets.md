# Secrets runbook

What each secret does, where it lives, how to rotate it, and the blast
radius if it leaks. Short — treat as a reference, not a tutorial.

---

## Rotate all three now (one-time, post-launch cleanup)

Three credentials were pasted into a shared session transcript during the
0.1.1 → 0.1.2 ship and the waitlist backend bring-up:

| Secret | Where used | Rotation URL |
|---|---|---|
| PyPI API token (`pypi-AgE...`) | `twine upload` to release new InferGrid versions | https://pypi.org/manage/account/token/ |
| Resend API key (`re_185s...`) | Worker sends waitlist emails via Resend | https://resend.com/api-keys |
| Cloudflare API token (`cfut_L7e...`) | Wrangler deploy + D1 + zone DNS | https://dash.cloudflare.com/profile/api-tokens |

**Do all three in the same sitting.** Rotating one but not the others
leaves the transcript equally risky.

### PyPI — 90 seconds

1. https://pypi.org/manage/account/token/ → **Revoke** the existing
   `infergrid`-scoped token
2. **Add API token** → Name: `infergrid-release-<YYYYMMDD>`, Scope:
   `Project: infergrid`, **Create**
3. Copy the new `pypi-...` value immediately (shown once)
4. Save in your password manager under "InferGrid / PyPI"
5. Next release (0.1.3+): `TWINE_PASSWORD=<new token> twine upload dist/*`

### Resend — 90 seconds

1. https://resend.com/api-keys → click the existing key → **Delete**
2. **Create API Key** → Name: `infergrid-waitlist-<YYYYMMDD>`,
   Permission: **Sending access**, Domain: **All domains** (or
   specifically `infergrid.org`)
3. Copy the new `re_...` value
4. Rotate on the Worker (no redeploy needed — secrets are side-channel):
   ```bash
   export CLOUDFLARE_API_TOKEN=<your CF token>
   cd landing_page/waitlist-api
   echo "<new re_...>" | npx wrangler secret put RESEND_API_KEY
   ```
5. Verify with one test email via `/api/notify` (needs `ADMIN_KEY`
   to also be set; see below).

### Cloudflare — 90 seconds

Note: the `cfut_L7e...` token that was pasted has a
`not_before`/`expires_on` of April 19-21 2026 — it may already be
expired by the time you're reading this. Rotating is still the right
move so the revocation is explicit.

1. https://dash.cloudflare.com/profile/api-tokens → existing token →
   **Roll** (generates a new value, old one dies immediately)
2. Copy the new token
3. Update your local shell profile or wherever you keep it. The Worker
   itself does NOT hold this token — it's used by `wrangler` CLI calls
   you + Claude make.
4. Next wrangler call: `export CLOUDFLARE_API_TOKEN=<new>` +
   `export CLOUDFLARE_ACCOUNT_ID=81376236895f9c9e63cb6360f2315144`.

---

## Secrets that exist on the Worker (set, don't rotate right now)

These live in Cloudflare Workers secret storage via `wrangler secret
put NAME`. They're **not** in git. They persist across redeploys.

| Name | What it gates | Current state |
|---|---|---|
| `RESEND_API_KEY` | `/api/notify` sends email via Resend | **Set** (will be rotated per above) |
| `ADMIN_KEY` | `/api/subscribers`, `/api/stats`, `/api/notify`, `/api/notifications` — all admin endpoints | **Unset** — admin routes return 401 until you set it |

To set `ADMIN_KEY`:

```bash
export CLOUDFLARE_API_TOKEN=<your CF token>
cd landing_page/waitlist-api
# Pick a strong string. 1Password/Bitwarden generates fine.
# Paste it when wrangler prompts; it's sent over TLS to CF and stored server-side.
npx wrangler secret put ADMIN_KEY
```

Once set, call admin endpoints with `Authorization: Bearer <your ADMIN_KEY>`.

---

## Blast radius (if any one secret leaks)

| Secret | What an attacker can do |
|---|---|
| PyPI token | Upload a malicious `infergrid==0.1.3` to PyPI; every `pip install infergrid` pulls it. Fast supply-chain attack. |
| Resend API key | Send email from `hello@infergrid.org`. Phishing risk + we pay for the delivery + domain reputation damage. |
| Cloudflare token (scoped) | Deploy/overwrite Workers, run D1 queries, modify DNS on the attached zone. Can redirect `infergrid.org` to a malicious host. |
| Cloudflare Global API Key (if ever used) | Full account — delete everything, transfer domains, drain balance. **Never paste this in a chat.** |
| `ADMIN_KEY` (if set) | Read the full subscriber list (email leak, GDPR-reportable) and trigger mass notify emails at our Resend cost. |

---

## Prevention for next time

- Use **scoped** Cloudflare API tokens (Workers + D1 + specific zone), never Global API Key
- PyPI tokens should always be **project-scoped** to `infergrid`, never account-scoped
- Resend API keys support **scope: Sending** (no admin power) — use that for Worker use
- Before pasting any secret into a chat or an issue, ask yourself: does the tool I'm giving this to *need* production-scoped access, or is a read-only/limited-scope variant enough?
- Secrets should expire. If your dashboard offers an expiration (CF does), pick 30-90 days.
- Rotate on a 90-day cadence regardless of suspicion.
