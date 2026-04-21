// Telemetry receiver for KVWarden opt-in install stats.
//
// Privacy contract:
// - The CLI sends a small, bounded JSON body — nothing else.
// - We validate strictly: unknown keys are rejected, string fields are
//   length-capped, the event name is enum-checked, and ts must be sane.
// - We deliberately do NOT log, forward, or even read CF-Connecting-IP.
//   Cloudflare's platform keeps its own edge logs; we do not add a second
//   layer. See telemetry-worker/README.md for retention policy.
// - CORS is wide-open because the client is the CLI, not a browser. If
//   someone spoofs events from a real browser that's fine: the fields
//   carry no PII and the rate limiter caps abuse per install_id.

type Env = {
  DB: D1Database;
  RL: KVNamespace; // rate-limit counter: key = rl:<install_id>, TTL 24h
};

const ALLOWED_KEYS = [
  'install_id',
  'version',
  'python_version',
  'platform',
  'gpu_class',
  'event',
  'ts',
];

const ALLOWED_EVENTS = new Set([
  'install_first_run',
  'serve_started',
  'doctor_ran',
]);

const ALLOWED_PLATFORMS = new Set(['linux', 'darwin', 'win32', 'other']);
const ALLOWED_GPU_CLASS = new Set(['h100', 'a100', 'rtx4090', 'other', 'none']);

// Rate limit: a single install_id may send at most 100 events in 24h.
// Beyond that we silently drop (still return 200 so the client sees no
// signal; that's by design — we never want the Worker response to leak
// into CLI UX).
const RATE_LIMIT_PER_DAY = 100;

const UUID_RE =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

const SEMVER_ISH_RE = /^[0-9a-zA-Z.+_-]{1,32}$/;

function corsHeaders(): HeadersInit {
  return {
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'POST, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type',
    'Access-Control-Max-Age': '86400',
  };
}

function json(obj: unknown, status = 200): Response {
  return new Response(JSON.stringify(obj), {
    status,
    headers: { 'Content-Type': 'application/json', ...corsHeaders() },
  });
}

type Payload = {
  install_id: string;
  version: string;
  python_version: string;
  platform: string;
  gpu_class: string;
  event: string;
  ts: number;
};

function validate(body: unknown): Payload | { error: string } {
  if (!body || typeof body !== 'object' || Array.isArray(body)) {
    return { error: 'body must be a JSON object' };
  }
  const obj = body as Record<string, unknown>;
  const keys = Object.keys(obj).sort();
  if (JSON.stringify(keys) !== JSON.stringify([...ALLOWED_KEYS].sort())) {
    return { error: 'unknown or missing fields' };
  }
  if (typeof obj.install_id !== 'string' || !UUID_RE.test(obj.install_id)) {
    return { error: 'bad install_id' };
  }
  if (typeof obj.version !== 'string' || !SEMVER_ISH_RE.test(obj.version)) {
    return { error: 'bad version' };
  }
  if (
    typeof obj.python_version !== 'string' ||
    !/^3\.(?:\d{1,2})$/.test(obj.python_version)
  ) {
    return { error: 'bad python_version' };
  }
  if (
    typeof obj.platform !== 'string' ||
    !ALLOWED_PLATFORMS.has(obj.platform)
  ) {
    return { error: 'bad platform' };
  }
  if (
    typeof obj.gpu_class !== 'string' ||
    !ALLOWED_GPU_CLASS.has(obj.gpu_class)
  ) {
    return { error: 'bad gpu_class' };
  }
  if (typeof obj.event !== 'string' || !ALLOWED_EVENTS.has(obj.event)) {
    return { error: 'bad event' };
  }
  if (typeof obj.ts !== 'number' || !Number.isFinite(obj.ts)) {
    return { error: 'bad ts' };
  }
  // Must be a plausible unix second, within ~2 years of "now".
  const now = Math.floor(Date.now() / 1000);
  if (obj.ts < now - 2 * 365 * 24 * 3600 || obj.ts > now + 24 * 3600) {
    return { error: 'ts out of range' };
  }
  return obj as Payload;
}

async function underRateLimit(env: Env, installId: string): Promise<boolean> {
  const key = `rl:${installId}`;
  const raw = await env.RL.get(key);
  const n = raw ? parseInt(raw, 10) : 0;
  if (n >= RATE_LIMIT_PER_DAY) return false;
  await env.RL.put(key, String(n + 1), { expirationTtl: 86400 });
  return true;
}

async function insert(env: Env, p: Payload): Promise<void> {
  const serverTs = Math.floor(Date.now() / 1000);
  await env.DB.prepare(
    `INSERT INTO events
       (install_id, version, python_version, platform, gpu_class, event, ts, server_ts)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
  )
    .bind(
      p.install_id,
      p.version,
      p.python_version,
      p.platform,
      p.gpu_class,
      p.event,
      p.ts,
      serverTs,
    )
    .run();
}

// Scheduled handler: enforce 90-day retention. Configured via wrangler.toml
// [triggers] crons. Keeps the privacy-doc claim honest.
async function scheduled(env: Env): Promise<void> {
  const cutoff = Math.floor(Date.now() / 1000) - 90 * 24 * 3600;
  await env.DB.prepare('DELETE FROM events WHERE server_ts < ?')
    .bind(cutoff)
    .run();
}

export default {
  async fetch(req: Request, env: Env): Promise<Response> {
    if (req.method === 'OPTIONS') {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }

    const url = new URL(req.url);
    if (url.pathname === '/health' && req.method === 'GET') {
      return json({ ok: true });
    }
    if (url.pathname !== '/event' || req.method !== 'POST') {
      return json({ error: 'not found' }, 404);
    }

    let body: unknown;
    try {
      body = await req.json();
    } catch {
      return json({ error: 'invalid json' }, 400);
    }
    const v = validate(body);
    if ('error' in v) {
      return json(v, 400);
    }
    // Intentionally ignore req.headers.get('CF-Connecting-IP'). We never
    // want the IP to end up in D1 or any logs we write.
    if (!(await underRateLimit(env, v.install_id))) {
      return json({ ok: true, dropped: true });
    }
    try {
      await insert(env, v);
    } catch {
      return json({ error: 'db error' }, 500);
    }
    return json({ ok: true });
  },

  async scheduled(
    _event: ScheduledEvent,
    env: Env,
    ctx: ExecutionContext,
  ): Promise<void> {
    ctx.waitUntil(scheduled(env));
  },
};
