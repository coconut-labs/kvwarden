# KVWarden Telemetry Worker

Tiny Cloudflare Worker that receives opt-in anonymous install stats from
the KVWarden CLI and writes them to D1. Intentionally **not** auto-deployed
from this repo — a human reviews and runs `wrangler deploy`.

## What it does (and doesn't)

- `POST /event` — strict JSON schema, bounded field lengths, enum-checked.
  Writes one row to D1.
- `GET  /health` — returns `{ok: true}`. Useful for a Pingdom check, not
  for the CLI.
- 24-hour rate limiter keyed by `install_id` (KV, 100 events/day). Over
  the limit → silently dropped, 200 OK.
- Scheduled handler deletes rows older than 90 days. Keeps the
  `docs/privacy/telemetry.md` retention claim honest.
- **Never reads or logs `CF-Connecting-IP`.** The Worker has no IP-capture
  code path. Cloudflare's platform keeps its own edge logs; we do not
  stack another.

## One-time setup

```bash
cd telemetry-worker

# 1. Create the D1 database, paste the returned id into wrangler.toml.
wrangler d1 create kvwarden-telemetry

# 2. Create the KV namespace for the rate limiter, paste the id in.
wrangler kv:namespace create RL

# 3. Apply the schema.
wrangler d1 execute kvwarden-telemetry --file=schema.sql

# 4. Deploy.
wrangler deploy
```

The Worker URL (e.g. `https://kvwarden-telemetry.<account>.workers.dev`)
is what you set as `KVWARDEN_TELEMETRY_URL` at Python-package build time.
Without that, the CLI is a no-op even for users who opted in.

## Local dev

```bash
wrangler dev
# In another shell:
curl -X POST http://127.0.0.1:8787/event \
  -H 'Content-Type: application/json' \
  -d '{
    "install_id":"11111111-1111-1111-1111-111111111111",
    "version":"0.1.2",
    "python_version":"3.12",
    "platform":"linux",
    "gpu_class":"a100",
    "event":"install_first_run",
    "ts":1700000000
  }'
```

## Inspecting collected data

```bash
wrangler d1 execute kvwarden-telemetry \
  --command='SELECT event, COUNT(*) FROM events GROUP BY event;'
```

## Retention + deletion requests

90 days, enforced by the `scheduled` handler (`[triggers] crons` in
`wrangler.toml`). For per-`install_id` deletion on request:

```bash
wrangler d1 execute kvwarden-telemetry \
  --command="DELETE FROM events WHERE install_id = '...';"
```

See `docs/privacy/telemetry.md` for the user-facing policy.
