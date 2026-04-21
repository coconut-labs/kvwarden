# KVWarden telemetry privacy policy

**Opt-in. Default off. Anonymous. Deletable.**

KVWarden can send a tiny amount of install/usage data back to the
maintainers to help prioritise work (e.g. "are any Windows users hitting
this?", "how many A100 vs H100 installs are live after launch?"). This
document explains exactly what that means.

## What's collected

If and only if you explicitly type `y` at the first-run prompt (or later
run `kvwarden telemetry on`), each CLI invocation sends a single JSON
object with these fields and nothing else:

| Field            | Example                                | Notes                                          |
|------------------|----------------------------------------|------------------------------------------------|
| `install_id`     | `f47ac10b-58cc-4372-a567-0e02b2c3d479` | uuid4 minted on your machine on first run       |
| `version`        | `0.1.2`                                | KVWarden's own version                         |
| `python_version` | `3.12`                                 | major.minor only                                |
| `platform`       | `linux`                                | one of `linux` / `darwin` / `win32` / `other`  |
| `gpu_class`      | `a100`                                 | from `nvidia-smi`; bucketed to `h100`/`a100`/`rtx4090`/`other`/`none` |
| `event`          | `serve_started`                        | one of `install_first_run` / `serve_started` / `doctor_ran` |
| `ts`             | `1_713_600_000`                        | unix seconds                                    |

That's the entire payload. The receiving Cloudflare Worker validates this
schema strictly and rejects any request that carries extra fields, wrong
types, or out-of-range values.

## What's NOT collected

- **No prompts, completions, or any other request/response body content.**
- **No model IDs or model names.** We cannot see what you are running.
- **No tenant IDs** (the per-tenant headers KVWarden uses internally
  never leave your machine).
- **No IP addresses.** The Worker does not read, log, or store
  `CF-Connecting-IP`. Cloudflare's platform keeps its own edge access
  logs; those are subject to [Cloudflare's policies](https://www.cloudflare.com/privacypolicy/)
  and are not combined with the event data.
- **No hostnames, usernames, directory paths, filenames, or env vars.**
- **No YAML config contents, tokens, or API keys.**
- **No analytics SDKs, tracking pixels, or third-party tooling.** The
  receiver is a ~150-line Cloudflare Worker that we maintain.

## Why we collect it

- Size the community after launch (installs, not downloads).
- See which Python / OS / GPU combos actually show up, so we don't spend
  bug-triage effort on irrelevant targets.
- Measure the ratio between `install_first_run` and `serve_started` —
  a proxy for how many people get past install.

That's it. No advertising, no user profiling, no data sale or sharing.

## How to opt out (or back in)

Three equivalent ways, in order of strength:

1. **Environment variable** (overrides everything):
   ```
   export KVWARDEN_TELEMETRY=0
   ```
   When set to `0`, `false`, `no`, or `off`, no telemetry is ever read,
   written, or transmitted — the subsystem short-circuits before touching
   disk.

2. **CLI toggle**:
   ```
   kvwarden telemetry off       # disable
   kvwarden telemetry on        # re-enable
   kvwarden telemetry status    # show current state + config file path
   ```

3. **Delete the config file** at `~/.config/kvwarden/telemetry.json`
   (honors `XDG_CONFIG_HOME`). On the next run you'll get the first-run
   prompt again.

We will **never** re-prompt you once you've made a choice. The file is
written once and read on subsequent runs.

## Retention

Rows in the D1 event store are deleted after **90 days** by a daily
scheduled Worker (`[triggers] crons` in `telemetry-worker/wrangler.toml`).
If that scheduled job is ever removed, this document will be updated in
the same commit.

## Deletion on request

Find your install ID with `kvwarden telemetry status` and email
<patelshrey77@gmail.com> — we will delete all rows matching that
`install_id` within 7 days. There is nothing else to tie events back to
you, so this is a complete deletion.

## Source you can audit

- Client: [`src/kvwarden/_telemetry.py`](../../src/kvwarden/_telemetry.py) (~200 LOC)
- Receiver: [`telemetry-worker/src/index.ts`](../../telemetry-worker/src/index.ts)
- Schema: [`telemetry-worker/schema.sql`](../../telemetry-worker/schema.sql)

If you spot a gap between this document and the code, that's a bug —
please open an issue.
