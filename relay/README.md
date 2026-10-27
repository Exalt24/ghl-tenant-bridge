# relay

![Flow of a lead from HighLevel through the relay to Slack, Make, Meta Conversions API and Google Sheets, with the ad spend pulls, Fathom and reconciliation beside it](docs/flow.svg)

A Python and Flask service that takes a HighLevel lead delivery and fans it out to the sinks a tenant enabled (Slack, Make, Meta Conversions API, Google Sheets). It also pulls daily ad spend from Meta Insights and Windsor.ai, receives and pulls Fathom meetings, and reconciles a payments CSV against a ledger.

No real vendor account was used for any vendor except one check of the Google Sheets values calls with a user token (see the register). Every vendor is a local fake written from a saved copy of the vendor's own documentation, so this page claims what the fakes and the tests show and nothing more. Delivery is at least once with a stored outcome per event and sink, and an idempotent effect only where the vendor offers one. It is NOT exactly once: Slack and Make document no idempotency key, so a send that ends in a 5xx or a timeout may post twice (its six tries span 4.5 hours or more), and the Slack footer carries the first 8 characters of the event id so a duplicate is recognisable. A sink can instead be told to hold such a send with `hold_unresolved: true` (off by default): an ambiguous answer, or a row whose earlier try may have posted because the worker died before its final write, is stored dead with a `held:` reason and an alert, and nothing sends it again until an operator has looked at the destination and replays it (runbook, "A held send"). Its tests are in `tests/test_hold_unresolved.py` and run on SQLite and Postgres; no real Slack or Make account was used.

**At a glance** (details and dates under Counts): 2922 tests pass on SQLite and Postgres 17 (a full local run on 2026-10-24, in two parts), 669 mutants each break one guard (the record of each run is in [docs/measurements.md](docs/measurements.md)) with a named failing test each, and a container smoke test sends one lead through the built image to a fake Slack.

## Register

Each row says what ran against a fake and what never ran against a real vendor. Fixtures are tagged `doc` or `observed` in `contracts/<vendor>/SOURCE.md`, and only Google has `observed` ones. `python tools/check_readme_claims.py` fails when this page calls a doc only integration verified or live, and CI runs it.

| integration | what ran | what did not | provenance |
|---|---|---|---|
| Slack | The sink against a fake with chaos modes (rate limit, 5xx, 5xx after the message was posted, hang, forbidden, revoked hook, odd 200). Escaping, a cut to 2800 characters after escaping, header and block limits, the footer. The Fathom meeting message goes through the same sink. | No real Slack workspace, no real delivery. Which status accompanies each error name is ASSUMED (the page that lists it was not retrieved). | doc: docs.slack.dev pages retrieved 2026-10-22, see `contracts/slack/SOURCE.md`. |
| Make | The sink against a fake: any 2xx is queued, queue full retries, 429 retries, 410 and 403 pause the sink, 5xx and timeouts are ambiguous, the optional api key header. | No real Make account. Whether a scenario can deduplicate on the relay's `Idempotency-Key` header was not tested. The status for a wrong api key and the 5 MB ceiling are ASSUMED. | doc: help.make.com/webhooks and apps.make.com/gateway retrieved 2026-10-22, see `contracts/make/SOURCE.md`. |
| Meta Conversions API | The sink against a fake that refuses what Meta's pages forbid: an unhashed email or phone, a hashed fbc, an event_time that is not an integer or is over 7 days old, a missing action_source, a website event with no user agent. The hash vectors printed on Meta's page, fbc in milliseconds, UTM and URL sanitising, skip, dead, pause and throttle paths, and a seeded property test. A lead needs a click id unless `capi_require_click_id` is false, and a workflow body needs a `type` such as `Lead`: without it the delivery is skipped with an alert (docs/routes.md). | No real Pixel, token or Events Manager. The body of a success and the status of each error code are ASSUMED. `test_event_code` is not a sandbox: those events still reach Events Manager. For two server copies the page gives no dedupe window. The page side half of deduplication is not built. | doc: developers.facebook.com pages retrieved 2026-10-22, see `contracts/meta/SOURCE.md`. |
| Google Sheets | The sink against a fake that verifies the service account JWT signature with the public key and the append rules: RAW keeps `=1+2` as text, a table found by adjacent cells, a row at the table's first column, and an append that searches column A so a column empty in every row cannot split the table. One append in flight per spreadsheet, a read-back before a retry, one row after a crash and after a late landing append. On 2026-10-23 the append and read-back calls were also run once against the real Sheets API with a user OAuth token on a scratch spreadsheet (trashed the same day). What it showed, and what was corrected to match, is in `contracts/google/SOURCE.md`. | No real service account sign-in: the signed JWT never went to Google, and the relay's own HTTP client never ran against real Google. A private sheet not shared with the account (403) and a 401 healed by a refreshed token are ASSUMED, as is how Sheets orders concurrent appends. Base64url and RS256 rest on RFC 7515 and 7518, which were not retrieved. | doc: developers.google.com pages and RFC 7523 retrieved 2026-10-22. Observed 2026-10-23 with a user token, not a service account: `contracts/google/*.observed.json`, see `contracts/google/SOURCE.md`. |
| Meta Insights | The pull against a fake that serves string spend in pages, an empty page that still carries next, error 190, throttle codes with the usage header in minutes, and error 100 subcode 1487534. Decimal to integer minor units with an ISO 4217 table, a 28 day re-pull upserted on the primary key, all or nothing writes, a next link to another host refused. | No real ad account or token. That spend is in `account_currency` is the likely reading and Meta does not state it (ASSUMED). The ISO 4217 table was written from the standard and not retrieved (ASSUMED), and so is the status of each error code. | doc: developers.facebook.com pages retrieved 2026-10-22, see `contracts/meta/SOURCE.md`. |
| Windsor.ai | The pull against a fake that takes the key only as a query parameter, serves spend as a JSON number and answers 429 with and without `Retry-After`. Spend goes through `Decimal(str(x))`. A test proves the key reaches no log line, exception message or database column. | No real Windsor account. The page names no header for the 429 reset, so `Retry-After` is ASSUMED, and so is the currency of `spend`. | doc: windsor.ai/api-documentation retrieved 2026-10-22, see `contracts/windsor/SOURCE.md`. |
| Fathom | The receiver against signatures built by code that does not import the relay: raw bytes, every `v1` entry in constant time, a 5 minute window both ways, dedupe on `webhook-id`, the meeting stored in its event's transaction, a 2 MB cap, optional Slack fan out. The puller against a fake with `next_cursor`, 429 with `Retry-After` and a limiter of 60 calls per 60 seconds (30 for heavy calls). | No real Fathom webhook was received, so no real signature was checked: the page's example signature cannot be reproduced because the page does not print its body. The retry schedule is unpublished, so nothing beyond a repeated `webhook-id` is ASSUMED about retries. | doc: developers.fathom.ai pages retrieved 2026-10-22, see `contracts/fathom/SOURCE.md`. |
| Reconciliation | A pure function and a CSV loader with a column map that reads UTF-8 (with or without a byte order mark) and Windows-1252 and names the file and byte offset of anything else. Property tests for shuffled rows, repeated rows, split payments, rounding and a currency mismatch. Amounts are decimal strings turned into minor units, a float or an unknown currency is refused. | No real payments export. The loader takes any CSV and no payment provider's column names appear in the package. | Written for this repo from no vendor page, so there is no fixture. |
| Docker | The `Dockerfile` (python:3.13-slim, non root). CI builds the image and `tools/container_smoke.py` boots it on a read only root with tmpfs `/tmp` and `/data`, checks `/health` and the user, sends one lead to a fake Slack inside the container and checks a repeat posts nothing more. | No orchestrated deployment. | The CI run on the repository's Actions page. |
| Railway | Nothing. It is not deployed to Railway, and there is deliberately no `railway.toml` and no Procfile. | A deploy, a healthcheck probe, a cron service. | Railway pages saved 2026-10-22, see the Railway notes in [docs/runbook.md](docs/runbook.md). |

Also true of every row: no real HighLevel delivery has reached this service. `POST /webhooks/ghl` checks an Ed25519 signature that is tested with keys generated in the test and never with HighLevel's own private key.

## How it works

- `relay/store/`: SQLite and Postgres with the same semantics. Idempotency is a primary key. A claim is one `UPDATE ... RETURNING` with a lease on the database clock (`FOR UPDATE SKIP LOCKED` on Postgres), and the final write only lands when the claim token still matches. State, attempts row, flow control and alerts are one transaction.
- `relay/app.py`: the routes. A failure before the durable commit is a 503, never a 200. The request path only stores.
- `relay/worker.py`: `python -m relay.worker`, `--once` (drain and exit) and `--purge` (null payloads older than 30 days, the row stays so dedupe holds). One thread per sink, a timeout per sink, minimum intervals kept as a time in the database and not a sleep, and a circuit breaker.
- `relay/sinks/`: Slack, Make, Meta Conversions API, Google Sheets. `relay/pulls/`: Meta Insights, Windsor.ai and Fathom pulls, `python -m relay.pulls`. `relay/fathom_hook.py`, `relay/reconcile.py`, `relay/money.py`.
- History import, Windows service, cutover demo and health check: [docs/operations.md](docs/operations.md). On 2026-10-24, on one Windows 11 machine, the per-user task was registered and Task Scheduler restarted a killed worker (`python tools/task_scheduler_check.py`); the SYSTEM account was not run.
- Routes, duplicates and outcomes: [docs/routes.md](docs/routes.md). Configuration: [docs/configuration.md](docs/configuration.md). Runbook, retention, scheduling, reconciliation and the Railway notes: [docs/runbook.md](docs/runbook.md).

## Run it

```
cd relay
python -m venv .venv
.venv/Scripts/python -m pip install -r requirements-dev.txt     # or .venv/bin/python on Linux and macOS
docker run -d --name relay-pg -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:17
DATABASE_URL_TEST=postgresql://postgres:pw@127.0.0.1:55432/postgres python -m pytest -q
DATABASE_URL_TEST=postgresql://postgres:pw@127.0.0.1:55432/postgres python tests/mutants.py
python tools/check_readme_claims.py
python tools/check_readme_counts.py
python tools/container_smoke.py --build
docker rm -f relay-pg
```

The commands the operators use besides `relay.worker` (history import, health check, service install) are in [docs/operations.md](docs/operations.md).

On Windows PowerShell the two `DATABASE_URL_TEST=... python` lines are not valid, so set the variable first and call the venv Python explicitly:

```
$env:DATABASE_URL_TEST = "postgresql://postgres:pw@127.0.0.1:55432/postgres"
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe tests\mutants.py
```

Without `DATABASE_URL_TEST` the Postgres half is skipped and the run prints that it skipped. CI sets `RELAY_REQUIRE_POSTGRES=1`, which turns the skip into a failure. The mutation harness counts a mutant caught only when a named test fails for the mutated behaviour and that same test passes on the unmutated copy ([docs/operations.md](docs/operations.md)).

## Counts

From one full local run on 2026-10-24: 2922 tests passed and 2 skipped on SQLite and on Postgres 17, on Windows 11, with `RELAY_REQUIRE_POSTGRES=1` set. The 45 test files hold 2180 lines whose first word is `assert` (a static count with indented lines, not executed assertions). The 44th file, `tests/test_demo_cutover.py`, was added after the full run and was run on its own on 2026-10-24: it passed in 31 seconds. The 45th file, `tests/test_hold_unresolved.py`, was added on 2026-10-25 and also falls outside the full run above.

`python tests/mutants.py` holds 669 mutants. Each run recorded in [docs/measurements.md](docs/measurements.md) caught every mutant it ran with a named failing test, and the earlier mutants were not all run again after the last change; CI runs the whole list in 8 shards. These numbers are the relay's own and are kept apart from the 324 assertions of the root suites and the counts in `edge/README.md`.

## ASSUMED, and what would settle each

- HighLevel: whether its Ed25519 signature is over the raw bytes or over `JSON.stringify(body)` (the relay accepts either), whether a delivery carries a `webhookId` and is flat or wrapped in `data`. One real delivery settles all three. `X-WH-Signature` was announced for deprecation on 2026-09-01 and its removal was not observed.
- Slack and Make: the status for each Slack error name and for a wrong Make api key, and the Make 5 MB ceiling. One real call each.
- Meta: the body of a success (any 2xx is done), the status of each error code, a bare 11 to 15 digit phone already carrying a country code, `OpportunityStatusUpdate` with `status: "won"` as the Purchase, that Insights `spend` is in `account_currency`, and the ISO 4217 table. One real call each, and one account in a non USD currency for the last two.
- Google: the service account sign-in, what a private sheet not shared with the account answers, how long a slow append takes to land (the relay waits 60 seconds before it believes an empty read-back), and how concurrent appends are ordered.
- Windsor.ai: that a 429 carries `Retry-After`, and the currency of `spend`.
- Fathom: nothing about retries beyond "the same `webhook-id` comes again". One real webhook settles the signature check.
- Versions: pinned versions were checked at install time on 2026-10-22 ([docs/operations.md](docs/operations.md)).

## Known limits

- The 500 lead test and the seeded property tests use simulated database time, so they prove the logic and not wall clock throughput.
- Two payments with the same id, amount, currency, date and bucket cannot be told from a repeated row, so reconciliation reports them as a duplicate.
- A pull writes all or nothing, so one malformed row in a window stops that window until the row or the code is fixed.
- A replay is a deliberate re-send: replaying a Sheets row that already landed appends it again.
- A transcript is never stored by the relay, but the Fathom body is read in memory up to the route's cap.

## License

MIT, see [LICENSE](LICENSE).
