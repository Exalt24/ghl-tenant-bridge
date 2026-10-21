-- Inbox of verified GoHighLevel webhooks. webhook_id is GHL's own delivery id, so the primary key IS the
-- idempotency guarantee: a retry or a replay inserts nothing. State moves pending -> claimed -> done, or
-- dead after MAX_ATTEMPTS. A claimed row whose claim_until has passed is picked up again by the next drain.
CREATE TABLE IF NOT EXISTS inbox (
  webhook_id  TEXT PRIMARY KEY,
  type        TEXT NOT NULL,
  location_id TEXT,
  body        TEXT NOT NULL,
  received_at INTEGER NOT NULL,
  state       TEXT NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'claimed', 'done', 'dead')),
  attempts    INTEGER NOT NULL DEFAULT 0,
  next_at     INTEGER NOT NULL,
  claim_until INTEGER,
  last_error  TEXT
);

-- The drain asks one question every tick: which rows are due. This index answers it without a table scan.
CREATE INDEX IF NOT EXISTS inbox_due ON inbox (state, next_at);

-- One row per forward attempt. Written in the same batch as the state change, so the log cannot disagree with the inbox.
CREATE TABLE IF NOT EXISTS deliveries (
  webhook_id TEXT    NOT NULL,
  attempt    INTEGER NOT NULL,
  at         INTEGER NOT NULL,
  status     INTEGER,
  ok         INTEGER NOT NULL,
  PRIMARY KEY (webhook_id, attempt)
);
