-- SQLite schema. Same tables and semantics as schema_postgres.sql, timestamps are UTC text 'YYYY-MM-DD HH:MM:SS.mmm'
-- so they sort as text. Every time in this file comes from the database clock, never from the Python process.

-- One row per accepted event. event_id is the idempotency constraint. dedupe_key is the key before any '|n' suffix,
-- seq counts how many events this key has produced (0 for the first). payload is nulled by `worker --purge` after 30
-- days and the row stays, so a late retry still dedupes.
CREATE TABLE IF NOT EXISTS events (
  event_id     TEXT PRIMARY KEY,
  tenant       TEXT NOT NULL,
  source       TEXT NOT NULL,
  dedupe_basis TEXT NOT NULL,
  dedupe_key   TEXT NOT NULL,
  seq          INTEGER NOT NULL DEFAULT 0,
  received_at  TEXT NOT NULL,
  payload      TEXT
);
CREATE INDEX IF NOT EXISTS events_key ON events (dedupe_key, seq);
CREATE INDEX IF NOT EXISTS events_received ON events (received_at);

-- One row per (event, sink). The sink set is frozen when the event is ingested. State stays 'pending' while a worker
-- holds the row: claim_until is the lease, claim_token proves who holds it, and the final write is conditional on it.
-- attempts counts tries that were not a plain RETRY, retries counts the RETRY tries (they spend no budget). claims counts
-- every claim, so a claim with no final write (a dead worker) shows as claims > attempts + retries. maybe_acted_at is the
-- time of the last try that MAY have reached the vendor (an ambiguous send, a read-back that found nothing).
CREATE TABLE IF NOT EXISTS deliveries (
  event_id     TEXT NOT NULL REFERENCES events (event_id),
  sink         TEXT NOT NULL,
  tenant       TEXT NOT NULL,
  state        TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done', 'dead', 'skipped')),
  attempts     INTEGER NOT NULL DEFAULT 0,
  retries      INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  next_at      TEXT NOT NULL,
  claim_until  TEXT,
  claim_token  TEXT,
  last_status  INTEGER,
  last_error   TEXT,
  effect_ref   TEXT,
  finished_at  TEXT,
  claims       INTEGER NOT NULL DEFAULT 0,
  maybe_acted_at TEXT,
  PRIMARY KEY (event_id, sink)
);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries (sink, state, next_at);

-- One row per try. Written in the same transaction as the state change. note holds an error class name, never
-- str(exception) and never a URL.
CREATE TABLE IF NOT EXISTS attempts (
  event_id TEXT NOT NULL,
  sink     TEXT NOT NULL,
  n        INTEGER NOT NULL,
  at       TEXT NOT NULL,
  status   INTEGER,
  ok       INTEGER NOT NULL,
  note     TEXT,
  PRIMARY KEY (event_id, sink, n)
);

-- Every duplicate that was dropped, with why. Counted in /health.
CREATE TABLE IF NOT EXISTS suppressed (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL,
  at       TEXT NOT NULL,
  basis    TEXT NOT NULL,
  tenant   TEXT NOT NULL
);

-- Per (tenant, sink) flow control: a pause for a tenant level fault, the circuit breaker, and the earliest next send
-- (the minimum interval and a vendor wide rate limit). Rows wait; nothing sleeps.
CREATE TABLE IF NOT EXISTS sink_state (
  tenant               TEXT NOT NULL,
  sink                 TEXT NOT NULL,
  paused_until         TEXT,
  reason               TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  breaker_until        TEXT,
  next_send_at         TEXT,
  PRIMARY KEY (tenant, sink)
);

-- One append in flight per spreadsheet across processes. Postgres uses an advisory lock instead and has no such table.
-- A holder that dies leaves a row that simply expires: `until` is on the database clock.
CREATE TABLE IF NOT EXISTS append_locks (
  lock_key TEXT PRIMARY KEY,
  holder   TEXT NOT NULL,
  until    TEXT NOT NULL
);

-- Alerts wait here until the digest for their sink is due (one post per sink per 15 minutes).
CREATE TABLE IF NOT EXISTS alerts (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  sink     TEXT NOT NULL,
  tenant   TEXT,
  kind     TEXT NOT NULL,
  reason   TEXT NOT NULL,
  event_id TEXT,
  at       TEXT NOT NULL,
  sent_at  TEXT
);
CREATE INDEX IF NOT EXISTS alerts_unsent ON alerts (sink, sent_at);
CREATE TABLE IF NOT EXISTS alert_state (
  sink         TEXT PRIMARY KEY,
  last_sent_at TEXT
);

-- Created now, filled by later tiers (ad spend pulls and the Fathom receiver).
CREATE TABLE IF NOT EXISTS spend_daily (
  source      TEXT NOT NULL,
  account     TEXT NOT NULL,
  campaign_id TEXT NOT NULL,
  day         TEXT NOT NULL,
  spend_minor INTEGER NOT NULL,
  currency    TEXT NOT NULL,
  impressions INTEGER,
  clicks      INTEGER,
  pulled_at   TEXT NOT NULL,
  PRIMARY KEY (source, account, campaign_id, day)
);
CREATE TABLE IF NOT EXISTS fathom_meetings (
  recording_id TEXT PRIMARY KEY,
  tenant       TEXT NOT NULL,
  title        TEXT,
  summary      TEXT,
  action_items TEXT,
  received_at  TEXT NOT NULL
);
