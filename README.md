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
| `supabase/migrations/0002_postgis.sql` | Spherical hail-to-property matching. Generated geography columns, GiST, SECURITY INVOKER so RLS applies. |
| `src/lib/geocode.ts` | US Census geocoding, keyless. Honest that it returns a block-face interpolation, not a rooftop. |
| `src/lib/parcels.ts` | County ArcGIS parcel lookup. Envelope-only, because a point query silently matches nothing. |
| `src/lib/outbox.ts` | Offline write queue. UUIDv7 idempotency keys, single-flight flush, never deletes before a confirmed send. |
| `src/lib/outbox-storage-browser.ts` | OPFS for bytes, IndexedDB for metadata, with a fallback because Safari lacked `createWritable` until 26. |
| `tests/` | Eleven suites, all passing. See below. |

## Tests

```bash
sh   tests/run_all.sh              # everything
node tests/signature.test.mjs      # 19 checks, real crypto, both directions
node tests/workflow-auth.test.mjs  # 22 checks, constant-time, downgrade guard
node tests/noaa.test.mjs           # 17 checks, hits the live NWS API
node tests/swdi.test.mjs           # 23 checks, live SWDI, bbox proven both ways
node tests/geo.test.mjs            # 31 checks, live Census + live county parcels
node tests/outbox.test.mjs         # 56 checks, state machine, all deps injected
node tests/outbox.browser.mjs      # 33 checks, real Chromium, survives a reload
python tests/postgis.test.py        # 25 checks, real migrations on real Postgres
python tests/verify_public_keys.py  # shipped keys == HighLevel's published keys
python tests/tenant_isolation.py    # live GHL cross-tenant isolation, with a control
python tests/verify_oauth_claims.py # 17 checks, DECODES the JWTs, re-runs isolation
```

All eleven pass as of 2026-08-20. Three need something the machine may not have and
SKIP loudly rather than failing a fresh clone: `verify_oauth_claims.py` needs the
gitignored token files, `postgis.test.py` needs a local `pgis` container, and
`outbox.browser.mjs` needs playwright.

`postgis.test.py` applies the real migrations to a throwaway database and keeps the
bounding box around as a **negative control**, so the corner over-selection is
measured rather than asserted. It also exercises the radius function as
`authenticated` rather than as the owner, which is how it caught a bug this repo
would otherwise have shipped: creating the `extensions` schema leaves it without the
usage grants Supabase normally provides, locking every RLS-scoped caller out of
PostGIS.

`geo.test.mjs` imports the modules directly rather than re-implementing their HTTP
calls, which the older suites here do. It proves the parcel trap in both directions on
one coordinate: a bare point query returns zero features and no error, the same point
as a small envelope returns four parcels.

`outbox.browser.mjs` writes, reloads the page, and reads back through a freshly
constructed adapter, because surviving the page going away is the only durability
claim worth making. It also forces the IndexedDB backend, since Chromium supports
`createWritable` and would otherwise leave that entire path unexecuted.

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

## Measured facts about the geo and offline stack

All verified on this machine 2026-08-20, not read in a doc.

- **A bounding box is the wrong shape for a radius.** Its corners reach ~1.41x the
  radius, so a property 67 km from a 50 km storm sits inside the box. SWDI only
  accepts a box, so the fetch uses one and the MATCH uses `ST_DWithin`.
- **`ST_MakePoint` is (longitude, latitude).** Our columns are named
  latitude/longitude, so the natural typing order is the wrong one, and a swap
  produces plausible small numbers rather than an error.
- **`geography` distances are metres; `geometry(4326)` distances are degrees.**
  Proven both ways: 400000 matches London-Paris, 400 does not, and the same pair
  matches a `geometry` tolerance of 4.0.
- **`ST_DWithin` is index-aware and `ST_Distance` is not.** DWithin carries
  `postgis_index_supportfn` and rewrites to `&& _st_expand(...)`; Distance carries
  none, so it must never appear in a WHERE clause.
- **A county ArcGIS POINT query returns zero features and no error**, where the
  identical coordinate as a ~20 m envelope returns four parcels.
- **A dead ArcGIS service returns HTTP 200** with `{"error":{"code":404}}` in the
  body, so the status line is not a health check.
- **The Census geocoder returns a block-face interpolation, not a rooftop.** The
  response carries a `tigerLine` id, a side of the street, and an address range.
- **`FileSystemFileHandle.createWritable()` requires Safari/iOS 26** (Chrome 86,
  Firefox 111). OPFS directories have existed in Safari far longer, so the obvious
  availability check passes and the write then throws.
- **Background Sync is supported in no version of Safari or iOS Safari.** There is no
  background flush to build for iPhones.

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
