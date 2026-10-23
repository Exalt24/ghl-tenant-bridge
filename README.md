# GHL Tenant Bridge

[![CI](https://github.com/Exalt24/ghl-tenant-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/Exalt24/ghl-tenant-bridge/actions/workflows/ci.yml)

![Offline capture and drain](docs/screenshots/offline-drain.gif)

*One unedited recording: the signal drops, four photos queue locally, then the queue drains to empty on its own.*

Multi-tenant GoHighLevel to Supabase bridge with PostGIS storm matching and an offline photo capture queue. It also does keyless geocoding and county parcel lookup.

Built by driving the real APIs rather than reading about them. **Every number below was
measured on this machine, and where something is unimplemented it is listed as a gap
rather than glossed.**

- **13 test suites, 263 counted assertions** (the 11 suites that print a count; the
  other two, the published-keys diff and the live tenant isolation check, report pass
  or fail), run against a real Postgres, a real browser and live third-party APIs. The
  per-suite counts are listed under Tests.
- The suites that touch a live service run a **positive control before the negative
  test**, so a broken request cannot pass for a successful refusal.
- **Register, stated plainly:** the database and storage are a real hosted Supabase
  project, and the Next.js app is run locally. Nothing is deployed to a public URL
  and there are no real users, so this is "built, tested, and running against a real
  hosted backend", never "in production".

---

## The offline queue, actually working

A roofer stands on a roof with no signal, takes four photos, and drives away. Nothing
may be lost, and nothing may be sent twice.

In the recording at the top, left to right: signal drops and the strip appears,
four photos queue locally while the count climbs, then signal returns and the queue
drains to empty on its own. The server received nothing during the offline stretch.

`tests/offline-drain.e2e.mjs` asserts exactly that, and checks the server's own counter
afterwards so "the queue emptied" cannot pass by the client simply forgetting:

```
PASS  offline strip shows when the connection drops
PASS  four photos captured with NO connection
PASS  all four are in the queue
PASS  the server received NOTHING while offline
PASS  the queue drained to empty after reconnecting
PASS  the server actually received the uploads      (9 POSTs, 15,568 bytes)
PASS  no orphaned photo bytes left on the device
```

| | |
|---|---|
| ![Empty state](docs/screenshots/capture-empty.png) | ![Queued](docs/screenshots/capture-queued.png) |
| Nothing queued, so nothing is said. The quiet state is the absence of a badge, not a green tick. | Count plus action. Thumbnails are read back from device storage, so they survive a reload. |

### The state that matters

<img src="docs/screenshots/capture-offline.png" alt="Four photos queued with no signal" width="390">

No signal, four photos held on the device, each one named and marked *Waiting*. The
strip is deliberately quiet: a thin line of muted text, not a red banner, because the
app is working exactly as intended and saying so loudly would be a lie about severity.

The same page at desktop width, since a field tool still gets opened on a laptop in an
office:

![Desktop layout](docs/screenshots/capture-desktop.png)

### Why the interface looks like this

The design came from reading real field apps, not from taste. **CompanyCam's actual
mobile UI** is a thin muted strip reading "You seem to be offline." and a calm grey card
reading "3 items waiting to upload." with a one-word action. The first draft here was a
red alert bar, which is precisely the documented **Housecall Pro** failure whose
persistent "OFFLINE Limited Functionality" banner false-fires on good signal and draws
review complaints.

So: **count plus action, never status plus colour**, and the card disappears at zero
(Encircle's decrementing-badge pattern). The copy says what will happen, because
web.dev's guidance is that non-technical audiences misread the word "offline".

Two more borrowed decisions. **Capture never blocks on a save**, because Encircle earned a
3-star review for making users save one picture before taking the next. And **GPS and
time are stamped at capture, not at upload**, because the upload can be hours later.

Accessibility was checked against the spec rather than assumed. `role="status"` carries
an implicit `aria-live="polite"` and `aria-atomic="true"`, and MDN documents it as
**inappropriate for a frequently-updating counter**, since every photo would fire a full
announcement. So the count is plain text and one polite region announces transitions
only. The queue is a plain list with real buttons, never a listbox holding buttons.

---

## The finding worth reading first

**HighLevel deprecated the legacy `X-WH-Signature` webhook header on 2026-09-01.** Since
that date webhooks are signed only with `X-GHL-Signature` (Ed25519). Any integration
verifying just the legacy RSA header stopped validating on that date, quietly.

This bridge is dual-path, prefers Ed25519, and **fails closed** on a legacy signature
presented after the sunset. `GET /api/webhooks/ghl` reports `daysUntilLegacySunset` so
the cutover is an ops number rather than a memory.

Source: [HighLevel Webhook Integration Guide](https://marketplace.gohighlevel.com/docs/webhook/WebhookIntegrationGuide/)

---

## What is in here

| Path | What it is |
|---|---|
| `src/app/capture/page.tsx` | The field capture surface. Downscales, stamps GPS at capture, queues locally. |
| `src/lib/outbox.ts` | The queue. UUIDv7 keys minted at capture, single-flight flush, proof-of-write guard. |
| `src/lib/outbox-storage-browser.ts` | OPFS for bytes, IndexedDB for metadata, with a fallback because Safari lacked `createWritable` until 26. |
| `src/lib/outbox-runtime.ts` | Registers the flush triggers. Page-driven, because iOS has no background path. |
| `src/lib/outbox-transport-supabase.ts` | The Supabase transport. Derives the row count from a PostgREST `.select()`. |
| `src/lib/outbox-transport-http.ts` | Reference transport for a non-Supabase backend. |
| `supabase/migrations/0002_postgis.sql` | Spherical storm-to-property matching. Generated geography columns, GiST, `SECURITY INVOKER` so RLS applies. |
| `src/lib/geocode.ts` | US Census geocoding, keyless. Honest that it returns a block-face interpolation, not a rooftop. |
| `src/lib/parcels.ts` | County ArcGIS parcel lookup. Envelope-only, because a point query silently matches nothing. |
| `src/lib/ghl-signature.ts` | Dual Ed25519 / legacy-RSA verification, sunset-aware, fails closed. |
| `src/lib/ghl-oauth.ts` | OAuth install, rotating refresh, agency-to-location exchange. Proven live. |
| `src/lib/noaa.ts` / `src/lib/swdi.ts` | Live NWS alerts and historical hail via NCEI SWDI, both keyless. |
| `src/app/api/webhooks/ghl/route.ts` | The receiver: verify, replay window, tenant resolve, idempotency, apply, dead-letter. |
| `edge/` | A Cloudflare Worker that verifies GHL's Ed25519 signature, stores each delivery once in D1 and forwards it on a cron with backoff. Its own tests, including real workerd. See [edge/README.md](edge/README.md). |
| `relay/` | A Python and Flask relay: takes a GoHighLevel lead delivery and fans it out to Slack, a Make webhook, Meta Conversions API and Google Sheets, at least once, with a stored outcome per sink, on SQLite or Postgres. It also pulls ad spend from Meta Insights and Windsor.ai, receives Fathom webhooks and reconciles a payments CSV. Built against the vendors' documented contracts and tested with local fakes, no vendor account was used except one check of the Google Sheets calls with a user token, and it is not deployed. Its own README holds the per integration register of what ran and what did not. See [relay/README.md](relay/README.md). |
| `docs/INTEGRATION_CONTRACT.md` | Token classes, limits, failure matrix, known gaps. |
| `docs/DEMO_PORTABILITY.md` | What is free and permanent vs what expires or costs money. |

---

## Measured facts

Every number here was measured on this machine; the full list, with the geocoding, parcel, browser and HighLevel findings, is in [docs/MEASURED_FACTS.md](docs/MEASURED_FACTS.md).

---

## Tests

```bash
sh tests/run_all.sh               # everything

node tests/signature.test.mjs      # 19  real crypto, both directions
node tests/workflow-auth.test.mjs  # 22  constant-time, downgrade guard
node tests/noaa.test.mjs           # 17  live NWS API
node tests/swdi.test.mjs           # 23  live SWDI, bbox proven both ways
node tests/geo.test.mjs            # 31  live Census + live county parcels
node tests/outbox.test.mjs         # 56  state machine, all deps injected
node tests/outbox.browser.mjs      # 33  real Chromium, survives a reload
node tests/offline-drain.e2e.mjs   # 10  capture offline, reconnect, queue empties
python tests/postgis.test.py        # 25  real migrations on real Postgres

(cd edge && npm install && npm run test:all)   # 30 + 18 mutants + 10 in real workerd
python tests/verify_public_keys.py  #     shipped keys == HighLevel's published keys
python tests/tenant_isolation.py    #     live GHL cross-tenant isolation, with a control
python tests/verify_oauth_claims.py # 17  decodes the JWTs, re-runs isolation
node tests/hosted-supabase.e2e.mjs # 10  the SHIPPING transport vs hosted Supabase
```

Four suites need something a fresh clone may not have and **skip loudly** rather than
failing: `verify_oauth_claims.py` needs the gitignored token files, `postgis.test.py`
needs a local `pgis` container, `outbox.browser.mjs` needs Playwright, and
`offline-drain.e2e.mjs` needs a running server.

The counts above are what each suite prints. On 2026-10-07 I re-ran nine of them from a
fresh clone and all passed with the printed counts (signature 19, workflow-auth 22, noaa
17, swdi 23, geo 31, outbox 56, outbox browser 33, postgis 25, offline drain
10, which sum to 236). The remaining 27 assertions are in `verify_oauth_claims.py` and
`hosted-supabase.e2e.mjs`, which need the gitignored token files and the hosted
Supabase project, plus the two suites that report pass or fail only.

**A green test proves nothing until it has been watched go red for the right reason.**
Reverting the coordinate order in `0002_postgis.sql` produces 5 failures, including the
40 km property collapsing to 4.75 km.

Some tests exist because of a specific mistake:

- `verify_public_keys.py` exists because the RSA key body was **fabricated** in a first
  draft. A wrong public key does not crash; it silently fails every verification, so
  the shipped artifact is diffed against the scraped published key.
- `tenant_isolation.py` runs a **positive control first**: tenant A writes and reads
  back its own contact, proving the harness works, and only then is tenant B refused.
  Without it, a broken request would "prove" isolation by failing for the wrong reason.
- `outbox.browser.mjs` **forces the IndexedDB backend**, because Chromium supports
  `createWritable` and would otherwise leave that whole path unexecuted, and that is
  the path most iPhones will actually use.

---

## Running it

```bash
npm install
cp .env.example .env.local          # then fill in

# apply the schema to any Postgres 17 with PostGIS available
psql "$SUPABASE_DB_URL" -f supabase/migrations/0001_schema.sql
psql "$SUPABASE_DB_URL" -f supabase/migrations/0002_postgis.sql

npm run build
DEMO_SINK=1 npm start               # http://localhost:3000/capture
```

Required env: `NEXT_PUBLIC_SUPABASE_URL`, `NEXT_PUBLIC_SUPABASE_ANON_KEY`,
`SUPABASE_SERVICE_ROLE_KEY`, and `NWS_USER_AGENT` (NWS returns **403** on an empty
User-Agent, measured).

`DEMO_SINK=1` enables `/api/demo/queue`, a local no-op sink so the queue can drain
without Supabase credentials. It refuses to run in production unless explicitly set, so
it cannot become an accidental open upload endpoint. With Supabase configured, swap the
capture page's transport for `createSupabaseOutboxTransport`.

### Local Postgres for the PostGIS suite

```bash
docker run --rm -d --name pgis -e POSTGRES_PASSWORD=pw -p 55432:5432 postgis/postgis:17-3.5
python tests/postgis.test.py
```

`tests/supabase_shim.sql` stands in for the `auth` schema and Supabase's default
privileges so RLS can be **exercised** locally. It is a test harness and says so; it
validates policy logic, not Supabase's permission model.

### The shim is no longer the last word

Why the PostGIS suite ran on a shim first and what replaced it: [docs/SUPABASE_SHIM.md](docs/SUPABASE_SHIM.md).

---

## Known gaps

Listed because a README that only lists strengths is not information.

- **Not deployed.** It runs locally. No hosted instance, no users.
- **iOS is documentation, not measurement.** The browser suite is Chromium on Windows.
  It proves the adapter is correct against the standard APIs; the seven-day eviction and
  the smaller iOS quota are cited from WebKit's own docs and have not been reproduced on
  a device here.
- **Webhook subscription registration is UI-only** on both paths, by the vendor's
  design. Both paths are handled in code; neither can be created via API.
- **No live authenticated write to a customer's production account.** The sandbox work
  is real; a production write is not claimed.
- **The workflow path is unsigned** and cannot be made otherwise. Prefer Marketplace app
  webhooks, which are free, signed, retried and logged.
- **Roof geometry is not solved here.** Ridge, hip and valley measurement has no free
  and legal source; the honest routes are per-report vendors or a lidar pipeline that
  nobody has published.
- **Google imagery is off-limits** for this shape of product: its terms forbid tracing
  or digitising building outlines, forbid training models on it, and forbid mixing it
  with a non-Google basemap.

---

## Free vs paid, and why it matters for reuse

What is free and permanent and what expires or costs money: [docs/FREE_VS_PAID.md](docs/FREE_VS_PAID.md).

