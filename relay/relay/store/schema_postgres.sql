-- Postgres schema. Same tables and semantics as schema_sqlite.sql, with real timestamps. Every time in this file comes
-- from the database clock (clock_timestamp()), never from the Python process.

CREATE TABLE IF NOT EXISTS events (
  event_id     TEXT PRIMARY KEY,
  tenant       TEXT NOT NULL,
  source       TEXT NOT NULL,
  dedupe_basis TEXT NOT NULL,
  dedupe_key   TEXT NOT NULL,
  seq          INTEGER NOT NULL DEFAULT 0,
  received_at  TIMESTAMPTZ NOT NULL,
  payload      TEXT
);
CREATE INDEX IF NOT EXISTS events_key ON events (dedupe_key, seq);
CREATE INDEX IF NOT EXISTS events_received ON events (received_at);

CREATE TABLE IF NOT EXISTS deliveries (
  event_id     TEXT NOT NULL REFERENCES events (event_id),
  sink         TEXT NOT NULL,
  tenant       TEXT NOT NULL,
  state        TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'done', 'dead', 'skipped')),
  attempts     INTEGER NOT NULL DEFAULT 0,
  retries      INTEGER NOT NULL DEFAULT 0,
  created_at   TIMESTAMPTZ NOT NULL,
  next_at      TIMESTAMPTZ NOT NULL,
  claim_until  TIMESTAMPTZ,
  claim_token  TEXT,
  last_status  INTEGER,
  last_error   TEXT,
  effect_ref   TEXT,
  finished_at  TIMESTAMPTZ,
  claims       INTEGER NOT NULL DEFAULT 0,
  maybe_acted_at TIMESTAMPTZ,
  PRIMARY KEY (event_id, sink)
);
CREATE INDEX IF NOT EXISTS deliveries_due ON deliveries (sink, state, next_at);

CREATE TABLE IF NOT EXISTS attempts (
  event_id TEXT NOT NULL,
  sink     TEXT NOT NULL,
  n        INTEGER NOT NULL,
  at       TIMESTAMPTZ NOT NULL,
  status   INTEGER,
  ok       INTEGER NOT NULL,
  note     TEXT,
  PRIMARY KEY (event_id, sink, n)
);

CREATE TABLE IF NOT EXISTS suppressed (
  id       BIGSERIAL PRIMARY KEY,
  event_id TEXT NOT NULL,
  at       TIMESTAMPTZ NOT NULL,
  basis    TEXT NOT NULL,
  tenant   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sink_state (
  tenant               TEXT NOT NULL,
  sink                 TEXT NOT NULL,
  paused_until         TIMESTAMPTZ,
  reason               TEXT,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  breaker_until        TIMESTAMPTZ,
  next_send_at         TIMESTAMPTZ,
  PRIMARY KEY (tenant, sink)
);

CREATE TABLE IF NOT EXISTS alerts (
  id       BIGSERIAL PRIMARY KEY,
  sink     TEXT NOT NULL,
  tenant   TEXT,
  kind     TEXT NOT NULL,
  reason   TEXT NOT NULL,
  event_id TEXT,
  at       TIMESTAMPTZ NOT NULL,
  sent_at  TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS alerts_unsent ON alerts (sink, sent_at);
CREATE TABLE IF NOT EXISTS alert_state (
  sink         TEXT PRIMARY KEY,
  last_sent_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS spend_daily (
  source      TEXT NOT NULL,
  account     TEXT NOT NULL,
  campaign_id TEXT NOT NULL,
  day         TEXT NOT NULL,
  spend_minor BIGINT NOT NULL,
  currency    TEXT NOT NULL,
  impressions BIGINT,
  clicks      BIGINT,
  pulled_at   TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (source, account, campaign_id, day)
);
CREATE TABLE IF NOT EXISTS fathom_meetings (
  recording_id TEXT PRIMARY KEY,
  tenant       TEXT NOT NULL,
  title        TEXT,
  summary      TEXT,
  action_items TEXT,
  received_at  TIMESTAMPTZ NOT NULL
);

-- Contacts the relay must treat specially at send time. contact_key is `email:<lower case>` or `phone:<E.164>` (relay/contacts.py).
-- sent_history holds messages that went out BEFORE this relay (a history import): the same contact is not sent the same message kind
-- again. suppression_list is do-not-contact: a listed contact is never sent anything, and the refusal is stored on the delivery.
-- Both keys are primary keys, so a repeated import adds nothing.
CREATE TABLE IF NOT EXISTS sent_history (
  contact_key  TEXT NOT NULL,
  message_kind TEXT NOT NULL,
  imported_at  TIMESTAMPTZ NOT NULL,
  PRIMARY KEY (contact_key, message_kind)
);
CREATE TABLE IF NOT EXISTS suppression_list (
  contact_key TEXT PRIMARY KEY,
  reason      TEXT NOT NULL,
  added_at    TIMESTAMPTZ NOT NULL
);

-- One row per named beater (the worker), written on every cycle with the database clock. relay.health_check reads it.
CREATE TABLE IF NOT EXISTS heartbeat (
  name    TEXT PRIMARY KEY,
  beat_at TIMESTAMPTZ NOT NULL
);
