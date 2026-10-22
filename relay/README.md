# relay

A Python and Flask service that takes a GoHighLevel lead delivery and fans it out to the sinks a tenant enabled. Built: the store, the fan-out, the worker, the ingress routes including the signed direct HighLevel route, and four sinks (Slack, Make, Meta Conversions API, Google Sheets). The ad spend pulls, the Fathom receiver and the payment reconciliation are listed in the register below as NOT BUILT YET.

Delivery is at least once, with a stored outcome per event and sink, and an idempotent effect only where the vendor offers one. It is NOT exactly once. Slack and Make document no idempotency key, so a send that ends in a 5xx or a timeout may post twice, and the Slack footer carries the first 8 characters of the event id so a duplicate is recognisable.

## Register

No real vendor account was used for anything here. Every vendor is a local fake written from a saved copy of the vendor's own documentation, and the fixtures are tagged `doc` in `contracts/<vendor>/SOURCE.md`. Nothing is `observed`.

| integration | what ran | what did not | provenance |
|---|---|---|---|
| Slack | The sink against a local fake with chaos modes (rate limit, 5xx, 5xx after the message was posted, hang, forbidden, revoked hook, odd 200). Escaping, truncation to 2800 characters after escaping, header and block limits, the footer. | No real Slack workspace. No real delivery. Which status accompanies each error name is ASSUMED (the changelog page that lists it was not retrieved). | doc: docs.slack.dev pages retrieved 2026-10-22, see `contracts/slack/SOURCE.md`. Nothing is observed. |
| Make | The sink against a local fake: any 2xx is queued, queue full retries, 429 retries, 410 and 403 pause the sink, 5xx and timeouts are ambiguous, the optional api key header. | No real Make account. Whether a scenario can deduplicate on the relay's `Idempotency-Key` header was not tested. The status for a wrong api key is ASSUMED. | doc: help.make.com/webhooks and apps.make.com/gateway retrieved 2026-10-22, see `contracts/make/SOURCE.md`. Nothing is observed. |
| Meta Conversions API | The sink against a local fake that refuses what Meta's pages forbid: an unhashed email or phone, a hashed fbc, an event_time that is not an integer or is over 7 days old, a missing action_source, a website event with no user agent. The fake keeps one copy of an event_id and event_name. Tested: the hash vectors printed on Meta's page, fbc built in milliseconds with the fbclid case kept, UTM and URL sanitising, event ids, skipped and dead rules, retry, pause and throttle paths, one lead through the worker, and a seeded property test over random worlds. | No real Pixel, token or Events Manager. The body of a success and which status each error code arrives with are ASSUMED (the CAPI errors page was a 404 in the evidence). `test_event_code` is not a sandbox: Meta says those events still flow into Events Manager. For two server copies the page says the first is kept with no window; its 48 hour window is for a Pixel and server pair. The page side half of deduplication is not built. Mapping a won opportunity to Purchase is ASSUMED. | doc: developers.facebook.com pages retrieved 2026-10-22, see `contracts/meta/SOURCE.md`. Nothing is observed. |
| Google Sheets | The sink against a local fake that verifies the service account JWT signature with the public key, and the append rules: a required valueInputOption, RAW keeping `=1+2` as text, a table found by adjacent cells, a row at the table's first column. Tested: JWT claims and encoding, token cache, append, a table that does not start at column A, one append in flight per spreadsheet (Postgres advisory lock, a lock row on SQLite), a read-back before a retry, exactly one row after a crash and after a late landing append. | No real spreadsheet or service account. The meaning of 401, 403 and 404 is ASSUMED (the troubleshooting page lists 400, 500 and 503). How Sheets orders concurrent appends was not found, the concurrent-edits page was not retrieved. The grant type rests on RFC 7523, base64url and RS256 on RFC 7515 and 7518 (not retrieved). A table that does not start at column A is noticed only after a row has been written there. | doc: developers.google.com pages and RFC 7523 retrieved 2026-10-22, see `contracts/google/SOURCE.md`. Nothing is observed. |
| Meta Insights | NOT BUILT YET | Everything. | Planned from the saved Meta pages, not started. |
| Windsor.ai | NOT BUILT YET | Everything. | Planned from the saved Windsor pages, not started. |
| Fathom | NOT BUILT YET | Everything. | Planned from the saved Fathom pages, not started. |
| Reconciliation | NOT BUILT YET | Everything. | Not started. |
| Docker | The `Dockerfile` (python:3.13-slim, non-root, shell wrapped gunicorn command so `PORT` expands). CI builds the image and boots it, asserting `/health`. | No orchestrated deployment. | CI run, see the CI badge on the repository. |
| Railway | Nothing. Not deployed to Railway. There is deliberately no `railway.toml` and no Procfile. | A deploy, a healthcheck probe, a cron service. | The deploy notes below come from Railway pages saved 2026-10-22. |

Also true, and not repeated per row: no real HighLevel delivery has reached this service. `POST /webhooks/ghl` checks an Ed25519 signature, and it is tested with keys generated in the test and never with HighLevel's own private key.

## What is built

```
GHL --> Cloudflare Worker (edge/) --POST /ingest, Idempotency-Key--> relay web
GHL workflow Custom Webhook ------POST /hooks/<tenant>/workflow----> relay web
                                                         |
              ONE transaction: event row + one delivery row per enabled sink
                                                         |
 worker: claim ONE row (database clock, lease, token) --> sink.send --> conditional final write
```

- `relay/store/`: SQLite and Postgres with the same semantics. Idempotency is a primary key. A claim is one `UPDATE ... RETURNING` with a lease on the database clock (`FOR UPDATE SKIP LOCKED` on Postgres), and the final write only lands when the claim token still matches and the row is still pending. State change, attempts row, flow control and alerts are one transaction.
- `relay/fanout.py`: outcome classes DONE, RETRY, AMBIGUOUS, DEAD, SKIPPED (plus PAUSE for a tenant level fault). A RETRY spends no attempt budget, a lead dies of age (72 hours) instead. An AMBIGUOUS try spends the budget of 6, because every one can duplicate. A catch-all turns a sink bug into DEAD on the first occurrence.
- `relay/worker.py`: `python -m relay.worker`, `--once` (drain and exit), `--purge` (null payloads older than 30 days, the row stays so dedupe holds). One thread per sink with no barrier between sinks, a mandatory timeout per sink, a per sink minimum interval kept as a time in the database rather than a sleep, and a circuit breaker (5 consecutive failures open it, half open after 60 seconds).
- `relay/app.py`: `GET /health`, `POST /ingest`, `POST /hooks/<tenant>/workflow`, `POST /webhooks/ghl`, `POST /admin/replay`, `POST /admin/backfill`.
- `relay/ghl.py`: event keys per type, `normalise_lead` (flat or enveloped), the Ed25519 verify function.
- `relay/attribution.py`: attribution from the nested Get Contact shape and from flat workflow keys, hashing and normalisation, the fbc, UTM and URL sanitising.
- `relay/sinks/capi.py` and `relay/sinks/sheets.py`: the two sinks added in tier 2. A sink takes its settings from the config as `env` (names of environment variables for identifiers and key material) and `options` (literals such as `sheet` or `test_event_code`); a pixel id, spreadsheet id or key in `options` is refused by name.
- `relay/redact.py`: log masking by value and by query parameter name, in the log record factory so any logger is covered.

### Routes

- `POST /ingest`: bearer `RELAY_INGEST_TOKEN` (or `RELAY_INGEST_TOKEN_PREVIOUS` during a rotation), `Idempotency-Key` required, and a `webhookId` in the body must equal it. The tenant comes from the body `locationId` through the config. An unmapped location, or a tenant with no enabled sink, answers 503 and raises an alert, so the Worker keeps the delivery queued while the config is fixed.
- `POST /hooks/<tenant>/workflow`: header `X-Relay-Secret`. No signature is documented for the Custom Webhook action, so a shared secret is the design. The body needs an `event_key` (put a per-submission token in the workflow body) unless the tenant sets `dedupe_mode: "body_hash"`. Any failure before the durable commit is a 503, never a 200 or 202, so set the workflow action to retry on error. A Worker route for unsigned workflow posts is the named follow-up, not built.
- `POST /webhooks/ghl`: the direct marketplace webhook. `X-GHL-Signature` (Ed25519) must verify over the raw bytes received OR over the compact re-serialisation of the body; which one a live delivery needs is not known. A request with only `X-WH-Signature` is refused (announced for deprecation on 2026-09-01, its removal was not observed). There is no timestamp window unless a tenant sets `ghl_timestamp_window` (not older than 30 days, not more than 5 minutes ahead, ASSUMED). The key is the `webhookId` when present, otherwise a hash of the type, the entity id, the stamp and the raw body. The size cap is `ghl_webhook_max_body_bytes` (256 KB by default).
- `POST /admin/replay` and `POST /admin/backfill`: their own `RELAY_ADMIN_TOKEN`, never the ingest token. Replay resets only dead rows unless `sink` and `force: true` are given.

Duplicates: a native `webhookId` dedupes for as long as the row exists. A derived key (workflow key, body hash, synthesised) dedupes only against an event with the same key inside `dedupe_window_s` (default 3600), and an older match becomes a new event with id `<key>|<n>`. Every suppressed duplicate is written to `suppressed` and counted in `/health`. The sink set is frozen when the event is ingested, and `/admin/backfill` adds rows for a sink enabled later.

## Configuration

`RELAY_CONFIG` is a JSON file path or an inline JSON string, holding names only (see `config.example.json`). A literal secret in it is refused by name.

| variable | meaning |
|---|---|
| `RELAY_ENV` | `dev`, `test` or `prod`. Unset means prod. |
| `RELAY_LIVE` | Only the exact value `1` allows a non-local https sink URL. Without it a real vendor URL is refused when the config loads. |
| `DATABASE_URL` | Postgres. Without it SQLite is used at `RELAY_DB_PATH`. |
| `RELAY_DB_PATH`, `RELAY_INLINE_WORKER` | SQLite in prod is refused unless both are set (an absolute path on a mounted volume, and the worker running as a thread in the same container). A separate `worker --once` service needs Postgres. |
| `RELAY_INGEST_TOKEN`, `RELAY_INGEST_TOKEN_PREVIOUS`, `RELAY_ADMIN_TOKEN` | At least 16 characters. The admin token must differ from the ingest tokens. |
| `ALERT_WEBHOOK` | A Slack webhook for relay alerts, one digest per sink per 15 minutes. Without it the digest goes to the log as `RELAY_ALERT`. |
| `RELAY_WORKER_INTERVAL_S` | Idle wait between passes, default 5. |
| `RELAY_TEST_GHL_PUBLIC_KEY` | Test key for signature tests, honoured only when `RELAY_ENV` is `test`. A test asserts nothing shipped sets it. |

## Run it

```
cd relay
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt     # or .venv/bin/python on Linux and macOS
docker run -d --name relay-pg -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:17
DATABASE_URL_TEST=postgresql://postgres:pw@127.0.0.1:55432/postgres python -m pytest -q
DATABASE_URL_TEST=postgresql://postgres:pw@127.0.0.1:55432/postgres python tests/mutants.py
docker rm -f relay-pg
```

Without `DATABASE_URL_TEST` the Postgres half is skipped and the run prints that it skipped. CI sets `RELAY_REQUIRE_POSTGRES=1`, which turns the skip into a failure.

The mutation harness copies the package, breaks one guard per mutant, and counts it caught only when a named test fails for the mutated behaviour and that same test passes on the unmutated copy. A tree that no longer imports, a fixture error or a missing test file is reported as BROKEN, not caught. `--shard i/n` runs every n-th mutant, which is how CI splits the list across five runners.

### Counts

From one full local pass on 2026-10-22, run in three chunks with `RELAY_REQUIRE_POSTGRES=1` against Postgres 17: 789 tests passed and 2 skipped on SQLite and Postgres together (pytest counts each store backed test once per store; the skips are the Postgres row lock test and the SQLite lock row expiry test, each of which has no counterpart on the other store), and the test files hold 1115 lines that start with `assert` (a static count, pytest does not count executed assertions). `python tests/mutants.py` holds 301 mutants (111 from tier 1, 190 added in tier 2). On 2026-10-22 all 8 local shards, `--shard 1/8` to `8/8`, ran and every one of the 301 was caught by a named failing test that passes on the unmutated tree. CI runs the same list in 5 shards. These numbers are the relay's own and are kept apart from the 263 assertions of the 13 root suites and the counts in `edge/README.md`.


## Deploy on Railway (notes, not done)

- Builder: Dockerfile. No `railway.toml`, no Procfile: Railway's config as code is deprecated for new services.
- The Dockerfile command runs through `/bin/sh -c` because the exec form does not expand `${PORT}`, a generic Docker behaviour.
- Healthcheck path `/health`.
- `DATABASE_URL`: a variable reference to a Railway Postgres service, or a Supabase session mode pooler string. Supabase direct connections are IPv6 unless the project has the IPv4 add-on, so use the pooler if the Railway service has no IPv6 route (not established).
- The worker is a second service from the same image, started with `python -m relay.worker`. A cron style service runs `python -m relay.worker --once` and needs Postgres.

## Runbook: re-queue dead rows in the Worker's D1 after a relay outage

The Worker retries a failed forward with backoff and marks a row dead after 6 attempts, or at once on a 4xx other than 408 and 429. What Railway answers for a stopped deployment is ASSUMED, so a stopped relay may produce dead rows in the Worker. Column names below come from `edge/schema.sql`. The relay dedupes on the `webhookId` the Worker sends as `Idempotency-Key`, so re-queueing is safe.

```
wrangler d1 execute ghl-edge --remote --command "UPDATE inbox SET state = 'pending', attempts = 0, next_at = CAST(strftime('%s','now') AS INTEGER) * 1000, claim_until = NULL, last_error = NULL WHERE state = 'dead';"
```

## ASSUMED, and what would settle each

- HighLevel's Ed25519 signature may be over the raw bytes or over `JSON.stringify(body)` (its own sample code does the latter). The verify function accepts either; a live delivery settles which one is needed. `X-WH-Signature` was announced for deprecation on 2026-09-01 and its removal was not observed, so a request carrying only that header is refused.
- The per event schemas show no `webhookId`, so a delivery without one gets a synthesised key, per event type (contact and opportunity events are flat, appointment events nest their fields). Confirm on a live delivery.
- The status that accompanies each Slack error name, and Make's status for a wrong api key. One real call each.
- The Make 5 MB payload ceiling is stated on the gateway page only.
- Python 3.13, `python:3.13-slim`, `psycopg[binary]` and `cryptography`'s Ed25519 API were checked at install time on 2026-10-22: `pip index versions` listed every pinned version and the upload dates were all at least 14 days old (Flask 3.1.3, gunicorn 26.2.0, httpx 0.28.1, cryptography 50.0.2, psycopg 3.3.6, pytest 9.1.1), and a one line import test passed.
- Meta: the body of a success (any 2xx is done), which status each error code arrives with, the 80000 series beyond the four codes the rate limit page names, a bare phone number of 11 to 15 digits with no `+` already carrying a country code, and `OpportunityStatusUpdate` with `status: "won"` as the Purchase. One real call would settle each.
- Google: what 401, 403 and 404 mean on a values call, how long a slow append can take to land (the relay waits 60 seconds before it believes a read-back that found nothing), and how concurrent appends are ordered.
- HighLevel: whether a live delivery carries a `webhookId`, and whether it is flat or wrapped in `data` (the relay reads both).
- Retention: an event payload is nulled after 30 days by `worker --purge`, which nothing schedules yet.

## Known limits

- The outer repeat of the claimable predicate in the Postgres claim is belt and braces. Under READ COMMITTED with `SKIP LOCKED` removing it changes no behaviour, so it has no mutant.
- The 500 lead test and the seeded property test use simulated database time, so they prove the logic, not wall clock throughput.
