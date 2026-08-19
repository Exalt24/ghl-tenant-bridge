# GHL Tenant Bridge

A multi-tenant GoHighLevel to Supabase bridge, built to a production standard in a
GHL Marketplace sandbox. Two isolated tenants, signature-verified webhooks,
database-level idempotency, RLS default-deny, and NOAA storm-alert ingestion.

Everything claimed here is measured or cited. Where something is unimplemented it
is listed as a gap, not glossed.

## Why it exists

Built 2026-08-19 to work out how GoHighLevel's multi-tenant integration surface
actually behaves, by driving it rather than reading about it. The measured findings
below are the real output; the code is what proves them. It is a capability demo,
not a product, and it says so wherever something is unimplemented.

## The finding worth reading first

**HighLevel deprecates the legacy `X-WH-Signature` webhook header on 2026-09-01.**
After that date webhooks are signed only with `X-GHL-Signature` (Ed25519). Any
integration verifying just the legacy RSA header stops validating on that date,
quietly.

This bridge is dual-path, prefers Ed25519, and **fails closed** on a legacy
signature presented after the sunset. `GET /api/webhooks/ghl` reports
`daysUntilLegacySunset` so the cutover is an ops number, not a memory.

Source: [HighLevel Webhook Integration Guide](https://marketplace.gohighlevel.com/docs/webhook/WebhookIntegrationGuide/)

## What is in here

| Path | What it is |
|---|---|
| `supabase/migrations/0001_schema.sql` | Tenancy, mapping, event ledger, weather, seats. RLS default-deny + FORCE. |
| `src/lib/ghl-signature.ts` | Dual Ed25519 / legacy-RSA verification, sunset-aware, fails closed. |
| `src/lib/noaa.ts` | Live NWS alert ingestion, keyless, roofing-relevant event filter. |
| `src/lib/swdi.ts` | HISTORICAL hail lookup via NCEI SWDI. Point geometry, sizes in inches, back to 2010. |
| `src/app/api/webhooks/ghl/route.ts` | The receiver: verify, replay window, tenant resolve, idempotency, apply, dead-letter. |
| `docs/DEMO_PORTABILITY.md` | What is free and permanent vs what expires or costs money. Read before reusing this for a gig. |
| `docs/INTEGRATION_CONTRACT.md` | The contract. Token classes, limits, failure matrix, known gaps. |
| `src/lib/ghl-oauth.ts` | OAuth install flow, rotating refresh, agency-to-location exchange. PROVEN live. |
| `src/lib/workflow-auth.ts` | Auth for the UNSIGNED workflow-webhook path, with a downgrade guard. |
| `tests/` | Seven suites, all passing. See below. |

## Tests

```bash
sh   tests/run_all.sh              # everything
node tests/signature.test.mjs      # 19 checks, real crypto, both directions
node tests/workflow-auth.test.mjs  # 22 checks, constant-time, downgrade guard
node tests/noaa.test.mjs           # 17 checks, hits the live NWS API
node tests/swdi.test.mjs           # 23 checks, live SWDI, bbox proven both ways
python tests/verify_public_keys.py  # shipped keys == HighLevel's published keys
python tests/tenant_isolation.py    # live GHL cross-tenant isolation, with a control
python tests/verify_oauth_claims.py # 17 checks, DECODES the JWTs, re-runs isolation
```

All pass as of 2026-08-20. `verify_oauth_claims.py` needs the token files, which are
gitignored because they hold live credentials; regenerate them by re-running an install.

`signature.test.mjs` generates real keypairs and asserts the negative half: a
tampered body fails, a wrong key fails, garbage fails, the literal string `"N/A"`
is treated as missing rather than compared, a bad Ed25519 signature is **not**
rescued by a valid legacy one, and a valid legacy signature is rejected after the
sunset. A verifier that only proves "valid input passes" is half a test.

`verify_public_keys.py` exists because the RSA key body was fabricated in a first
draft. A wrong public key does not crash, it silently fails every verification, so
the shipped artifact is diffed against the scraped published key.

`tenant_isolation.py` runs a **positive control** before the negative test: tenant
A writes and reads back its own contact, proving the harness works, and only then
is tenant B refused. Without the control, a broken request would "prove" isolation
by failing for the wrong reason.

## Free vs paid, and why it matters for reuse

**The Marketplace app webhook path is the PRIMARY path, deliberately.** It is free,
Ed25519-signed, retried 12 times with backoff, and carries 30 days of delivery logs.

The workflow "Custom Webhook" action is a **premium** action ($0.01 per execution,
100 free per sub-account, toggled per sub-account) and is used only as a fallback
for **form submissions**, which have no event in the 58-event catalogue.

The sandbox also runs on **trial access to Enterprise features** and expires about
6 months after creation. So nothing load-bearing is built on it: five of the six
test suites need no GHL account at all. Full classification in
`docs/DEMO_PORTABILITY.md`.

## Measured facts about GHL

- A sub-account **is** a location; the location id is the tenant key.
- Agency and location tokens are different **classes**, and there are FOUR distinct
  refusal messages meaning four different things (see section 3g). An agency PIT
  gets `"Token's user type mismatch!"`; an OAuth Company token gets
  `"This authClass type is not allowed to access this scope."`
- **Target User and token class are INDEPENDENT axes.** A Sub-Account-target app
  installed from Agency View still returns a **Company** token with no locationId,
  so `/oauth/locationToken` is mandatory even there. That exchange also silently
  ADDS `oauth.readonly oauth.write` to the minted token.
- A location token on the wrong tenant gets
  `403 "The token does not have access to this location."`
- Sandbox: **25 req/10s**, **10,000/day**, per location, non-multiplying, and a
  **2-location hard cap** per sandbox agency.
- PITs **can** create sub-accounts (an earlier 401 turned out to be a bad payload,
  not a permission wall).
- The contact **list index is eventually consistent**: immediately after a 201
  create, the list endpoint returned `total: 0` while a fetch by id returned the
  record. Never treat an empty list right after a write as proof of anything.
- A fresh sandbox has **zero snapshots**, so snapshot-based provisioning is
  unavailable there.

## Known gaps

- Webhook **subscription registration has no API at all** (all 83 official specs
  parsed: 852 paths, zero containing 'hook'). It is UI-only on both paths. Both
  paths are handled in code.
- **The workflow path is CONFIRMED UNSIGNED** by live capture: six headers, none of
  them a signature, and no `webhookId`/`timestamp`/`locationId` in the body. See
  `docs/INTEGRATION_CONTRACT.md` section 2b. Prefer Marketplace app webhooks, which
  are free, Ed25519-signed, retried and logged.
- **OAuth install flow is IMPLEMENTED AND PROVEN END TO END** (2026-08-20): real install, real `?code=`, `/oauth/token` -> 200, `/oauth/locationToken` -> 201, then a tenant-scoped read that returns the canary contact and a 403 on the neighbouring tenant. 17 verification checks, 0 failed. See `docs/INTEGRATION_CONTRACT.md` section 3g.
- ~~Historical weather is not implemented~~ **CLOSED**: `src/lib/swdi.ts` uses the
  NCEI SWDI web service (keyless) for point-level hail history back to at least
  2010, with sizes in inches. See `docs/INTEGRATION_CONTRACT.md` section 8b.

## Setup

```bash
cp .env.example .env.local        # then fill in
psql "$SUPABASE_DB_URL" -f supabase/migrations/0001_schema.sql
```

Required env: `NEXT_PUBLIC_SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`,
`NWS_USER_AGENT` (NWS returns **403** on an empty User-Agent, measured).

Location PITs are per tenant and belong in a secret store, never in this repo.
