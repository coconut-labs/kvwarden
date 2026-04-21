-- KVWarden telemetry event store.
--
-- Apply with:
--   wrangler d1 execute kvwarden-telemetry --file=schema.sql
--
-- Column notes:
--   install_id       uuid4 chosen by the client, stable across runs for a
--                    single user install. Not linked to any account.
--   version          KVWarden version string (semver-ish).
--   python_version   "3.11" / "3.12" / ... (major.minor only).
--   platform         linux | darwin | win32 | other
--   gpu_class        h100 | a100 | rtx4090 | other | none
--   event            install_first_run | serve_started | doctor_ran
--   ts               unix seconds, client-supplied (validated server-side)
--   server_ts        unix seconds, when the Worker inserted the row

CREATE TABLE IF NOT EXISTS events (
    install_id      TEXT NOT NULL,
    version         TEXT NOT NULL,
    python_version  TEXT NOT NULL,
    platform        TEXT NOT NULL,
    gpu_class       TEXT NOT NULL,
    event           TEXT NOT NULL,
    ts              INTEGER NOT NULL,
    server_ts       INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_server_ts ON events(server_ts);
CREATE INDEX IF NOT EXISTS idx_events_event     ON events(event);
