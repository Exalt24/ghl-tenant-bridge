# CLAUDE.md

How Claude Code works on this repo. Two packages: the Next.js app at the root and the Cloudflare Worker in `edge/`.

## Before building

- Read the integration contract in `docs/INTEGRATION_CONTRACT.md` and the vendor's own page for any API you touch. A vendor page beats a blog post, and a field you have not seen in a real response does not go in the code.
- Plan first for anything over one file: write what changes and what test proves it, then build. A change with no test that can fail is not finished.
- Research before asking. When a question is still open, bring two options and a recommendation, not the bare question.

## Rules that are not negotiable

- Verify a webhook signature over the exact raw bytes before parsing anything. The legacy `X-WH-Signature` header is refused: HighLevel stopped sending it on 2026-09-01.
- Idempotency is a database constraint, not a check-then-write. Deduplicate on the delivery id with a primary key.
- Secrets live in `wrangler secret put` or an untracked `.env`, never in a file under git. A test greps `edge/` for account ids and key material.
- Test config must never be able to weaken verification in production: the test key override only works when `ENVIRONMENT` is exactly `test`, and `wrangler.toml` never sets it (a test asserts that).
- A Workers module exports only handlers. Constants and helpers live in `edge/src/core.ts`; `edge/src/worker.ts` is the entry. The real runtime rejected a worker that exported a constant, which the Node tests could not see.

## Tests: run all of them before saying done

- App: `npm run typecheck`, then `npm run test:logic`. The suites that touch a live service run a positive control first, so a broken request cannot pass for a successful refusal.
- Edge, from `edge/`: `npm run test:all` runs the typecheck, the offline suite (real Ed25519, real SQLite behind the D1 API, a seeded property test), the mutation check (each mutant deletes one guard and the suite must go red) and the end-to-end run in real workerd.
- A new guard gets a mutant in `edge/test/mutants.mjs` in the same change. A survivor means the guard is untested, so fix the test, not the mutant.
- A claim in the README is a number from a run in this repo, with its date.

## Git

Plain commit messages in the repo's own voice: what changed and why. No trailers, no bullet lists.
