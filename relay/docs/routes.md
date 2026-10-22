# Routes and delivery rules

Back to the [relay README](../README.md).

## Routes

- `GET /health`: 200 with the database state, the number of events, deliveries by state and the count of suppressed duplicates.
- `POST /ingest`: bearer `RELAY_INGEST_TOKEN` (or `RELAY_INGEST_TOKEN_PREVIOUS` during a rotation), `Idempotency-Key` required, and a `webhookId` in the body must equal it. The tenant comes from the body `locationId` through the config. An unmapped location, or a tenant with no enabled sink, answers 503 and raises an alert, so the Worker keeps the delivery queued while the config is fixed.
- `POST /hooks/<tenant>/workflow`: header `X-Relay-Secret`. No signature is documented for the Custom Webhook action, so a shared secret is the design. The body needs an `event_key` (put a per submission token in the workflow body) unless the tenant sets `dedupe_mode: "body_hash"`. Any failure before the durable commit is a 503, never a 200 or 202, so set the workflow action to retry on error. A Worker route for unsigned workflow posts is the named follow up, not built.
- `POST /webhooks/ghl`: the direct marketplace webhook. `X-GHL-Signature` (Ed25519) must verify over the raw bytes received OR over the compact re-serialisation of the body; which one a real delivery needs is not known. A request with only `X-WH-Signature` is refused (announced for deprecation on 2026-09-01, its removal was not observed). There is no timestamp window unless a tenant sets `ghl_timestamp_window` (not older than 30 days, not more than 5 minutes ahead, ASSUMED). The key is the `webhookId` when present, otherwise a hash of the type, the entity id, the stamp and the raw body. The size cap is `ghl_webhook_max_body_bytes` (256 KB by default).
- `POST /webhooks/fathom/<tenant>`: a Fathom webhook, Standard Webhooks. The signed content is `webhook-id`, `webhook-timestamp` and the raw body joined by periods, the key is the base64 decoded part of the secret after `whsec_`, the signature is HMAC-SHA256 in base64, and every `v1,<signature>` entry of the header is compared in constant time. The timestamp must be within 5 minutes in both directions (per tenant `tolerance_s`). The event id is the `webhook-id`, so Fathom's retries are suppressed duplicates. The meeting (title, summary, action items, never the transcript or the invitees) is stored in the same transaction as the event. The size cap defaults to 2 MB per tenant. An unknown tenant answers like a bad signature.
- `POST /admin/replay` and `POST /admin/backfill`: their own `RELAY_ADMIN_TOKEN`, never the ingest token. Replay resets only dead rows unless `sink` and `force: true` are given.

## Duplicates

A native `webhookId` dedupes for as long as the row exists. A derived key (workflow key, body hash, synthesised) dedupes only against an event with the same key inside `dedupe_window_s` (default 3600), and an older match becomes a new event with id `<key>|<n>`. Every suppressed duplicate is written to `suppressed` and counted in `/health`. The sink set is frozen when the event is ingested, and `/admin/backfill` adds rows for a sink enabled later.

## Outcomes

DONE, RETRY, AMBIGUOUS, DEAD, SKIPPED, and PAUSE for a tenant level fault. A RETRY spends no attempt budget, so a lead dies of age (72 hours) and not of a vendor's rate limit. An AMBIGUOUS try spends the budget of 6, because every one can duplicate. A catch-all turns a sink bug into DEAD on the first occurrence.

## Pulls

`python -m relay.pulls meta_insights|windsor|fathom --tenant <name>` runs one pull and exits with a code a scheduler can read: 0 stored, 1 the data could not be trusted (alert, nothing written), 2 configuration refused, 3 throttled for longer than the run waits (nothing written, the next run pulls the same window again), 4 the vendor refused the credentials (alert, nothing written). `--days N` changes the window for one run. A pull is all or nothing: rows are gathered in memory and written in one transaction. The Meta and Windsor pulls re-pull the last 28 days by default and upsert on `(source, account, campaign_id, day)`.
