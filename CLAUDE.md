# CLAUDE.md

How Claude Code works on this repo. Three packages: the Next.js app at the root, the Cloudflare Worker in `edge/` and the Python relay in `relay/`.

## Before building

- Read the integration contract in `docs/INTEGRATION_CONTRACT.md` and the vendor's own page for any API you touch. A vendor page beats a blog post, and a field you have not seen in a real response does not go in the code.
- Plan first for anything over one file: write what changes and what test proves it, then build. A change with no test that can fail is not finished.
- Research before asking. When a question is still open, bring two options and a recommendation, not the bare question.

## Rules that are not negotiable

- Verify a webhook signature over the exact raw bytes before parsing anything. The legacy `X-WH-Signature` header is refused: HighLevel stopped sending it on 2026-09-01.
- Idempotency is a database constraint, not a check-then-write. Deduplicate on the delivery id with a primary key.
- Secrets live in `wrangler secret put` or an untracked `.env`, never in a file under git. A test greps `edge/` for account ids and key material.
- Test config must never be able to weaken verification in production: the test key override only works when `ENVIRONMENT` is exactly `test`, and `wrangler.toml` never sets it (a test asserts that).
- Relay secrets only come from environment variables named in the JSON config, never a literal in a file, and never into a log line or a database column (a test seeds secrets and greps both). Nothing is acknowledged before the durable commit: a failure before it is a 503. A sink takes identifiers and key material (a pixel id, a spreadsheet id, a service account) only through the `env` map of its config, never `options`, and a Sheets append is looked up by event id before any retry that may have already written. Idempotency in relay is a primary key, the claim is one conditional statement on the database clock, and delivery is at least once with a stored outcome per event and sink, never "exactly once".
- Relay money and pulls: an amount is parsed from text with `Decimal` into integer minor units through the ISO 4217 table in `relay/relay/money.py`, never a float, and an unknown currency is refused. A pull writes all or nothing on its primary key, a refused credential stops and alerts instead of retrying, a pull URL is a secret (Windsor's key is a query parameter) so no message, exception or log line carries a URL or a vendor error body. A Fathom webhook is verified over the raw bytes, every `v1` signature in constant time, inside a 5 minute window both ways, and the meeting is stored in the same transaction as its event. Reconciliation names no payment provider's columns.
- A Workers module exports only handlers. Constants and helpers live in `edge/src/core.ts`; `edge/src/worker.ts` is the entry. The real runtime rejected a worker that exported a constant, which the Node tests could not see.

## Tests: run all of them before saying done

- App: `npm run typecheck`, then `npm run test:logic`. The suites that touch a live service run a positive control first, so a broken request cannot pass for a successful refusal.
- Edge, from `edge/`: `npm run test:all` runs the typecheck, the offline suite (real Ed25519, real SQLite behind the D1 API, a seeded property test), the mutation check (each mutant deletes one guard and the suite must go red) and the end-to-end run in real workerd.
- A new guard gets a mutant in `edge/test/mutants.mjs` in the same change. A survivor means the guard is untested, so fix the test, not the mutant.
- Relay, from `relay/` (Python 3.13, `python -m venv .venv`, `pip install -r requirements-dev.txt`): `python -m pytest -q` runs every test on SQLite and on Postgres. Postgres needs `DATABASE_URL_TEST` (`docker run -d --name relay-pg -e POSTGRES_PASSWORD=pw -p 55432:5432 postgres:17`, then `postgresql://postgres:pw@127.0.0.1:55432/postgres`); with it unset the Postgres half is skipped and says so, and CI sets `RELAY_REQUIRE_POSTGRES=1` so it can never skip there. `python tests/mutants.py` copies the package, breaks one guard per mutant and requires the suite to go red. `python tools/check_readme_claims.py` fails when the relay README calls a doc only integration verified or live, and `python tools/container_smoke.py --build` boots the image and sends one lead through it to a fake Slack.
- A new relay guard gets a mutant in `relay/tests/mutants.py` in the same change, and every test that touches a vendor fake runs a positive control first (the fixtures in `relay/tests/conftest.py` do it). Vendor fakes are hand written from saved vendor pages, fixtures are tagged `doc` or `observed` in `relay/contracts/<vendor>/SOURCE.md`. Only Google Sheets has `observed` ones (2026-10-23, a user OAuth token on a scratch sheet, not a service account), and an observed answer is not a verified integration: the README never says "verified" or "live" for any relay integration.
- A claim in the README is a number from a run in this repo, with its date.

## Git

Plain commit messages in the repo's own voice: what changed and why. No trailers, no bullet lists.
