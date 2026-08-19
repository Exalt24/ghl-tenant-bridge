# GHL Integration Contract

Every claim in this document was measured against the live GoHighLevel API or read
from HighLevel's official docs on 2026-08-19. Where something is unverified it says
so. Nothing here is inferred from a blog post.

## 1. The urgent one: legacy webhook signatures die 2026-09-01

HighLevel's Webhook Integration Guide states:

> The legacy header `X-WH-Signature` will be deprecated on **September 1, 2026**.
> After that date, webhooks will be signed only with `X-GHL-Signature`.

| Header | Algorithm | Status |
|---|---|---|
| `X-GHL-Signature` | Ed25519 | Current. Prefer whenever present. |
| `X-WH-Signature` | RSA-SHA256 | Legacy. **Stops being sent 2026-09-01.** |

**Any integration that verifies only `X-WH-Signature` stops validating on that
date.** The failure is quiet: either every webhook starts getting rejected, or
worse, a permissive implementation starts accepting unverified payloads.

This bridge is dual-path and prefers Ed25519, so it is already correct on both
sides of the cutover. It also **fails closed**: a legacy signature presented after
the sunset is rejected outright (`reason: "legacy-after-sunset"`), because at that
point the only thing that can be sending one is something spoofing a retired
scheme.

The cutover is exposed as an operational number on `GET /api/webhooks/ghl`
(`daysUntilLegacySunset`), so it is monitored rather than remembered.

Both public keys ship in `src/lib/ghl-signature.ts` and are checked byte-for-byte
against HighLevel's published keys by `tests/verify_public_keys.py`. That test
exists because the RSA key body was fabricated on a first draft; a wrong public key
does not crash, it silently fails every verification.

## 2. Tenancy model

A GHL **sub-account is a "location"**. The location id is the tenant key.

```
GHL agency (companyId)
 └── sub-account / location  (locationId)   <-- one tenant
```

Our mapping is `ghl_locations.location_id -> orgs.id`, and it is authoritative.
**The webhook payload never chooses its own tenant.** `org_id` and `location_id`
written to `ghl_contacts` come from our mapping row, not from the body. Without
that rule, a valid signature from any GHL agency would be a cross-tenant write.

An unmapped location returns **202 ignored**, not 500. The delivery was authentic
and well-formed; it simply is not ours, and a 500 would make GHL retry forever.

## 2b. THE TWO INBOUND PATHS, MEASURED SIDE BY SIDE

Both paths were driven for real on 2026-08-19/20: a workflow was built in the
sandbox UI, published, triggered via
`POST /contacts/{contactId}/workflow/{workflowId}` (HTTP 201), and the delivery was
captured. What follows is measured, not inferred.

### The workflow Custom Webhook action IS UNSIGNED. Confirmed by capture.

GHL delivered from `104.154.217.87` (Google Cloud) with `user-agent: axios/0.21.4`
and sent EXACTLY six headers:

```
accept            application/json, text/plain, */*
content-length    97
content-type      application/json
host              <our endpoint>
user-agent        axios/0.21.4
x-bridge-secret   <the secret WE configured on the action>
```

No `X-GHL-Signature`. No `X-WH-Signature`. No timestamp. No HMAC. Nothing.
Every header was printed rather than grepping for the two expected ones,
specifically so an unfamiliar signing scheme could not slip past and produce a
false "unsigned" conclusion. There was nothing to find.

**Custom headers DO arrive intact**, so a shared secret in a header is genuinely
the only authentication available on this path. That is what `workflow-auth.ts`
implements, and it is why that module fails closed when no secret is configured.

### The workflow payload is minimal, which has real consequences

The body was only:

```json
{"id":"...","name":"Canary Alpha","email":"canary.alpha@example.com","phone":""}
```

**No `webhookId`, no `timestamp`, no `locationId`.** Therefore, on this path:
- There is **no idempotency key**, so a retry would double-apply. The receiver
  REFUSES events with no stable id rather than inventing one silently; a workflow
  integration must synthesise a key from payload content and say that it did.
- The **5-minute replay window cannot fire**, because there is no timestamp.
- **Tenant cannot be resolved from the body.** It has to come from the endpoint or
  a per-tenant secret, not the payload.

So the two paths differ far more than "signed versus unsigned".

### Cost, and why the app path wins anyway

Premium workflow actions bill **$0.01 per execution** from the agency wallet, with
**100 free executions per sub-account** once premium actions are enabled. Cheap,
but the app path is better on every axis at once:

| | Marketplace app webhooks | Workflow Custom Webhook |
|---|---|---|
| Cost | free | $0.01 per execution |
| Signed | Ed25519 (+ legacy RSA until 2026-09-01) | **no** |
| Retries | 12, exponential backoff with jitter | none |
| Delivery logs | 30 days, manual replay | none |
| Payload | `webhookId`, `timestamp`, `locationId` | id/name/email/phone |
| Events | 58 catalogued types | anything triggerable |

**RECOMMENDED ARCHITECTURE: Marketplace app webhooks as the primary path; the
premium Custom Webhook action only as a fallback for what the app catalogue does
not cover.** In practice that fallback is FORM SUBMISSIONS, because there is no
`FormSubmit` or `SurveySubmit` among the 58 events. That is the one reason the
unsigned path cannot be dropped, only demoted.

Two related notes: there is also an **Inbound Webhook premium trigger** (external
into GHL, the opposite direction), and HighLevel runs an experiment **waiving
premium action charges for selected Marketplace apps**, so a published app may
avoid the fee entirely.


## 3. Token classes, which is the part that surprises people

**Agency-level and location-level tokens are different CLASSES, not different
scope sets.** Measured:

| Call | Token | Result |
|---|---|---|
| `GET /locations/search` | agency PIT | `200` |
| `GET /contacts/?locationId=X` | agency PIT | `401 "The token is not authorized for this scope."` |
| `GET /users/?locationId=X` | agency PIT | `401 "Token's user type mismatch!"` |
| `GET /contacts/?locationId=A` | location PIT for A | `200` |
| `GET /contacts/?locationId=A` | location PIT for **B** | `403 "The token does not have access to this location."` |

No amount of granting scopes fixes the agency token. Each tenant needs its own
location PIT, which is why `ghl_locations.pit_secret_ref` is per row.

**Three distinct failure messages, three distinct causes:**
- `"not authorized for this scope"` — scope missing from the token
- `"Token's user type mismatch!"` — wrong token *class*
- `"The token does not have access to this location."` — right class, wrong tenant

That third one is GHL's own tenant boundary firing, and our RLS has to agree with
it. `tests/tenant_isolation.py` asserts exactly that, **with a positive control**:
tenant A writes and reads its own contact (proving the harness works), then tenant
B is refused on both the list and the direct fetch. Without the control, a broken
request would "prove" isolation by failing for the wrong reason.

## 3b. OAuth 2.0, and the location-token exchange

Researched from HighLevel's docs and then **verified against the live API** on
2026-08-19, because the two doc versions contradicted each other on one path.

**Flow.** Authorization-code grant. The authorize step lives on the marketplace
host, not the API host:

```
GET https://marketplace.gohighlevel.com/oauth/chooselocation
      ?response_type=code&client_id=...&redirect_uri=...&scope=<space separated>
POST https://services.leadconnectorhq.com/oauth/token
      Content-Type: application/x-www-form-urlencoded
      client_id, client_secret, grant_type=authorization_code, code, redirect_uri,
      [user_type=Company|Location]
```

The token response carries `userType`, `companyId`, and `locationId` (sub-account
installs only), which is how you know which class of token you were handed.

**THE VERSION HEADER IS MANDATORY. Measured:**

| Request | Result |
|---|---|
| no `Version` header | `401 "version header was not found."` |
| `Version: 2021-07-28` | `200` |
| `Version: 2023-02-21` | `200` |
| `Version: v3` | `200` |
| `Version: bogus-version` | `400 "Invalid API version segment 'bogus-version' at position 0"` |

`v3` is the current version (released 2026-06-11, sunset TBD) and marks the move
from date strings to named identifiers. `2021-07-28` still works and has **no
published retirement date**, but it is in a maintenance window: critical fixes
only, no new features.

**THE LOCATION-TOKEN PATH CONFLICT, RESOLVED.** The docs disagree because the
answer is version-dependent. Measured with POST + `companyId` + `locationId`:

| Path | `Version: 2021-07-28` | `Version: v3` |
|---|---|---|
| `/oauth/locationToken` | 401 → **route exists** | 401 → **route exists** |
| `/oauth/location-token` | **404 → route does not exist** | 401 → route exists |

**Use `/oauth/locationToken` (camelCase). It is the only form that works on both
versions**, so it survives a version bump either way. A 404 versus a 401 is what
separates "no such route" from "route exists, wrong credentials", which is why the
probe used both and treated 404 as the signal.

Body: `companyId` and `locationId`, form-encoded. Returns a token with
`userType: Location`.

**A PIT cannot mint location tokens.** Our agency PIT returns
`401 "The token is not authorized for this scope."` on that endpoint, so this
exchange requires an OAuth **agency** access token. That is consistent with the
token-class finding in section 3: the exchange is precisely the documented way an
agency install reaches sub-account endpoints.

**Token lifetimes** (from the docs, not independently measured here): access token
about 24 hours; refresh token up to 1 year or until used; **refresh tokens ROTATE**,
so the new one must be persisted on every refresh or the next refresh fails. The
docs do **not** state what happens when a refresh fails, so treat re-running the
install flow as the recovery path and note that it is inference.

**Scopes** are named `<resource>.readonly` / `<resource>.write`, and each scope row
in the docs carries an Access Type of Sub-Account, Agency, or both. The level is a
property of the scope row, not a different scope string, so there is no prefix or
suffix to add.

**Private apps** complete OAuth with no app review, capped at **5 agencies** (one
agency counts once regardless of how many sub-accounts, bulk installs included),
with new installs blocked at 6+. Effective 2025-11-18 for apps created on or after
that date.

### 3c. TARGET USER decides which scopes can EXIST. Measured in the portal, 2026-08-20.

This is the deepest finding of the OAuth work and it is not in the docs. When you
create a Marketplace app you pick a **Target User**, and that choice silently
controls the entire scope catalogue available to the app:

| Target User | Scopes unavailable | Selectable | Example |
|---|---|---|---|
| **Agency** | **146** "Sub-Account-only scopes are not applicable" | 24, in 11 groups | `contacts` NOT selectable |
| **Sub-Account** | **8** "Agency-only scopes are not applicable" | ~155 | `contacts.readonly` / `contacts.write` selectable, badged "Sub-Account" |

The full catalogue is 163 scopes, captured from
`GET backend.leadconnectorhq.com/oauth/static/scopes?isMarketplace=true`.
`contacts.readonly` and `contacts.write` exist in it, so the Agency restriction is
not a naming difference: those scopes are genuinely filtered out and clicking
"Show unavailable" adds nothing.

**Consequence, and it corrects the naive reading of the docs:** the agency to
location token exchange does NOT solve the token-class problem on its own. An
agency-target app can mint a Location token, but that token can never carry
`contacts` scopes because the app was never permitted to request them. **A real
deployment therefore needs TWO apps: a Sub-Account-target app for tenant data, and
an Agency-target app for provisioning.** Verified by creating both.

### 3d. A PRIVATE app's install link uses version_id, not client_id

The portal generates:

```
https://marketplace.gohighlevel.com/v2/oauth/chooselocation
  ?response_type=code&redirect_uri=<enc>&scope=<space separated>
  &version_id=<appId>
```

Note `/v2/` and **`version_id`**, where the docs describe `/oauth/chooselocation`
with `client_id`. Both forms appear to exist; the portal hands you the version_id
form for a private app.

**WHERE THE CLIENT CREDENTIALS ACTUALLY LIVE:** left nav **MANAGE -> Secrets ->
Client Keys**. NOT under Advanced Settings -> Auth, and the app record's
`clientKeys: []` does not mean none exist, it just does not inline them. A **draft
private app does have keys**, so nothing needs publishing first.

Shapes, confirmed against a real pair: `client_id` is `<appId>-<8 chars>` and
`client_secret` is a plain UUID, matching HighLevel's own doc example.

**The secret is shown ONCE.** The docs: "copy and securely store your Client Secret
immediately. After clicking OK, you will not be able to view or copy the secret again
from the UI." Capture it at generation or generate a new pair.

*A previous revision of this document claimed client keys were gated behind leaving
draft status. That was wrong and is corrected here. It came from treating a failed
UI probe (a MANAGE nav click that did not expand) as evidence of absence.*

The chooselocation page renders blank without a HighLevel session on the marketplace
domain, which is a different cookie scope from app.gohighlevel.com.

**Two other portal facts worth knowing:** the redirect URL field **rejects
`http://localhost`** and requires HTTPS, and saving Auth settings is atomic — a
`422 {"message":["redirectUris must contain at least 1 elements","scopes must
contain at least 1 elements"]}` means neither was persisted, so redirect and scopes
must be set in the same pass.

### 3e. Token-endpoint validation ORDER, and why you cannot smoke-test credentials

`POST /oauth/token` validates the **authorization code BEFORE the client
credentials**. Measured with three requests that differ only in the credentials:

| Request | Response |
|---|---|
| real client_id + real secret + bogus code | `401 {"error":"UnAuthorized!","error_description":"Authorization code not found"}` |
| bogus client_id + real secret + bogus code | **identical** |
| real client_id + bogus secret + bogus code | **identical** |

**Consequence:** there is no way to verify a client_id/client_secret pair in
isolation. An invalid client and an invalid code are indistinguishable, so a
"credentials smoke test" is impossible and any such check would be false comfort.
Credentials can only be confirmed by completing a real authorization and exchanging
a genuine code.

Also confirmed: the documented `/oauth/chooselocation?client_id=...` form
**does work** (the page loads and asks for a HighLevel login), so both it and the
portal's `/v2/oauth/chooselocation?version_id=...` form are live. The
earlier note in 3d should be read as "the portal hands you the version_id form",
not "client_id does not work".

### 3f. Deleting an app version

HighLevel's own versioning doc: **"Drafts can't be deleted immediately. After you
click Delete, it may take some time for the deletion to complete."** So deletion is
asynchronous, and a draft that still appears in the dashboard right after deleting is
expected rather than a failed delete.

Version limits worth knowing before creating more: **only one Draft or Disapproved
version at a time**, `(In Review + Disapproved + Draft) = 1`, and a maximum of **3
total versions per app**. A new draft comes from **"Clone as Draft"** on the latest
Live version, and you cannot create another draft until the current one is resolved.

**Where the credentials live, since this cost real time:** left nav **MANAGE ->
Secrets -> Client Keys**. Not under Advanced Settings -> Auth.

### 3g. THE FULL OAUTH CHAIN, PROVEN END TO END 2026-08-20

Everything below was executed live and then re-verified by a 17-check script that
DECODES the JWTs rather than reading console output (`tests/verify_oauth_claims.py`).
17 checks, 0 failed.

```
install URL (client_id AND version_id)
  -> "Install"  ->  "Proceed to Install"  ->  select sub-account  ->  "Continue"
  ->  "Allow & Install"
  -> redirect: https://<redirect_uri>?code=6ff158ab...
  -> POST /oauth/token          -> 200  userType Company
  -> POST /oauth/locationToken  -> 201  userType Location
  -> GET  /contacts/?locationId=A  -> 200 (returns the canary contact)
  -> GET  /contacts/?locationId=B  -> 403 "does not have access to this location"
```

#### The finding that matters most

**A Sub-Account-target app, installed from Agency View, returns a COMPANY token.**
`userType: "Company"`, **no `locationId`**, and the decoded JWT says
`authClass: "Company"`, `authClassId: <the agency id>` — even though a specific
sub-account was selected during install.

So **Target User and token class are INDEPENDENT axes.** Target User decides which
scopes can exist for the app (see 3c); it does NOT decide which class of token an
install hands back. Consequently **`POST /oauth/locationToken` is MANDATORY**, in
precisely the configuration where the docs' framing suggests it would be unnecessary.

#### The two tokens, decoded

| | Company token | Location token |
|---|---|---|
| `authClass` | Company | **Location** |
| `authClassId` | `ncTg6Q3fkTB8tVHocj3U` (agency) | `AnJG67dOYPmbxTOG4cU1` (tenant A) |
| `primaryAuthClassId` | same as above | same as above |
| `source` / `sourceId` | `INTEGRATION` / the client_id | identical |
| `channel` | `OAUTH` | `OAUTH` |
| `scope` | `contacts.readonly contacts.write` | **the same PLUS `oauth.readonly oauth.write`** |
| `expires_in` | 86399 (~24h) | 86400 |

**The exchange ADDS scopes.** GHL grants `oauth.readonly` and `oauth.write` on the
minted Location token without them ever being requested by the app. Do not assume the
location token's scope set equals the app's.

#### A FOURTH distinct refusal message

Section 3 lists three. There is a fourth, and it is the token-CLASS rejection on an
OAuth token:

| Message | Cause |
|---|---|
| `"The token is not authorized for this scope."` | scope missing from the token |
| `"Token's user type mismatch!"` | agency **PIT** on a sub-account endpoint |
| `"The token does not have access to this location."` | right class, wrong tenant |
| **`"This authClass type is not allowed to access this scope. Please verify your IAM configuration if this is not the case."`** | **OAuth Company token on a sub-account scope** |

Four different ways GHL says no, each meaning something different. Treating them as
interchangeable is how the earlier token-class confusion happened.

#### Isolation holds identically on OAuth as on a PIT

Re-run to confirm it reproduces: tenant A returned **200 containing the canary
contact `Msp3QvFpSDhrxB2A3mL2`** (a positive control, so the 200 is not an empty-list
pass), tenant B returned **403**. Same boundary, same message as the PIT test.

#### The install URL riddle: BOTH identifiers are required

| URL | Result |
|---|---|
| `client_id` only | `error.noversionid` |
| `version_id` only | blank page; `400 CastError: Cast to ObjectId failed for value "" at path "appId"` on `/marketplace/app/installationDetails?appId=&versionId=...` |
| **`client_id` + `version_id`** | **renders and installs** |

Found by capturing the failing XHR, not by reasoning about it. An earlier guess that
the blank page was reCAPTCHA or draft status was wrong: the page mounts (146KB of
HTML) and then renders nothing because `appId` is empty.

#### Five gates in the consent flow, not two

`Install` -> **`Proceed to Install`** (a private-app trust warning) -> **`Select
Sub-Account`** (a CHECKBOX; clicking the row label does nothing) -> `Continue` ->
**`Allow & Install`**.

Also: the marketplace-domain HighLevel login requires clicking **"Send code to
email"** before any OTP is sent. Waiting for a code that was never requested is a
silent stall.

## 4. Idempotency

The unique index on `webhook_events (provider, event_id)` **is** the idempotency
mechanism. A replayed delivery loses the insert race and returns
`{"status":"duplicate"}` with a 200.

This is a database guarantee, not a cache check, which means it survives a cold
Redis and two concurrent deliveries landing on different instances. An event with
no stable id is **rejected**, because without an idempotency key a retry would
double-apply.

## 5. Failure handling

| Outcome | HTTP | Why |
|---|---|---|
| bad/missing signature | 401 | no detail in the body, a prober learns nothing |
| malformed JSON | 400 | unparseable |
| missing event or location id | 400 | cannot be made idempotent or routed |
| outside 5-minute replay window | 401 | stale replay |
| unmapped location | 202 | authentic but not ours, stop retrying |
| duplicate event | 200 | already processed, no-op |
| processed | 200 | applied |
| retryable failure | 500 | let GHL retry with its own backoff |
| dead-lettered after 5 attempts | 200 | it will never succeed; now our problem, visible in the ledger |

`webhook_events` has **RLS enabled and zero user policies**, so only the service
role can read it. Payloads can contain another tenant's data before mapping is
resolved, so it must never be user-readable.

## 6. Rate limits (sandbox, measured)

- **25 requests / 10 seconds**
- **10,000 requests / day**
- Applied at the **location** level, and they do **not** multiply if you generate
  more PITs
- Webhooks testable at **low volume** only
- **A sandbox agency is capped at 2 locations**, proven by
  `POST /locations/ -> 400 "Only 2 locations are allowed for this company, as this
  is a sandbox agency."`
- Sandbox accounts live up to **6 months** and their data may be reset or purged

Production PITs use the standard limits for the paid plan. Any backfill or sync
loop must be paced against the location-level ceiling, not a global one.

## 7. RLS design notes

- Every tenant table is `enable row level security` **plus `force row level
  security`**. Without FORCE, the table owner bypasses its own policies, which is a
  silent hole in precisely the audit a client runs.
- `is_org_member()` is `SECURITY DEFINER` so a policy can read `org_members`
  without recursing into that table's own policy.
- Update policies carry **both `USING` and `WITH CHECK`**. A policy with `USING`
  alone passes the read test on a row you own and then lets you rewrite its
  `org_id` into someone else's tenant. That is the subtle cross-tenant write and
  the easiest RLS mistake to ship.
- Seat accounting is an atomic `claim_seat()` function, not a read-modify-write in
  application code, because two invites landing at once on a 5-seat plan must not
  both succeed.

## 8. Weather (NOAA / NWS)

Verified live: `GET https://api.weather.gov/alerts/active?area=TX` returns **200
with no API key**, as a GeoJSON FeatureCollection. `GET /points/{lat},{lon}`
resolves a coordinate to county and forecast zone.

**A `User-Agent` is mandatory.** Measured: an empty UA returns **403**.

**Two different NOAA surfaces, and conflating them is the usual mistake:**
1. `api.weather.gov` — **live/current** alerts. Keyless. Alerts expire and vanish,
   so polling only ever answers "what is happening now". Asserted in
   `tests/noaa.test.mjs`: the active feed contains zero already-expired alerts.
   Note that `/alerts?start=&end=` *accepts* a date range and returns 200, but the
   feature list comes back empty for past windows, so the parameter existing is not
   the same as history being served.
2. **NCEI SWDI** — the **historical** half, and it is implemented in
   `src/lib/swdi.ts`. See section 8b.

### 8b. Historical hail (NCEI SWDI) — IMPLEMENTED, measured 2026-08-19

`https://www.ncei.noaa.gov/swdiws/json/{dataset}/{YYYYMMDD}:{YYYYMMDD}`
(the older `www.ncdc.noaa.gov/swdiws` host 301-redirects here). **No API key, no
account.**

This is the piece that makes "was there damaging hail over this roof" answerable,
because it returns **point geometry with hail size**, not county rollups:

```json
{"PROB":"100","SHAPE":"POINT (-100.6303 34.2159)","WSR_ID":"KLBB",
 "CELL_ID":"G5","ZTIME":"2026-06-01T00:00:26Z","SEVPROB":"10","MAXSIZE":"0.5"}
```

`MAXSIZE` is max hail size in **inches**. Observed range on one national day:
**0.5 to 4.0 inches** across 11,565 signatures.

**`bbox=minLon,minLat,maxLon,maxLat` works, and it was proven in BOTH directions**
so that a zero is meaningful rather than a silently-ignored filter:

| Query, 2026-06-01 | Count |
|---|---|
| national, no bbox | 11,565 |
| bbox Lubbock TX `-101.0,34.0,-100.3,34.5` | **1** (0.5" hail) |
| bbox Austin TX `-97.9,30.1,-97.6,30.4` | **0** |

`tests/swdi.test.mjs` additionally asserts every returned point actually falls
inside the requested box.

**Archive depth**, probed nationally because a small-bbox zero proves nothing:

| Date | Signatures |
|---|---|
| 2010-06-01 | 6,023 |
| 2015-06-01 | 5,791 |
| 2020-06-01 | 1,296 |
| 2024-06-01 | 7,931 |

So usable history reaches back **at least to 2010**.

**Freshness:** 2026-08-15 returned 4,062 records; 2026-08-18 returned 0. The
archive lands within roughly a few days of real time, so do not query today.

**Datasets confirmed working:** `nx3hail` (hail sizes), `nx3tvs` (tornado vortex
signatures, 133), `nx3structure` (storm cells, 131,172).
**Confirmed BROKEN, HTTP 500 not 404, so likely server-side:** `plsr`, `warn`.
`src/lib/swdi.ts` guards those two so a caller gets a clear error, and the test
prints a note if they ever start working again.

**Honest limitations, which belong in anything shown to a homeowner:**
1. These are **radar-derived signatures, not ground truth**. A signature is an
   algorithm's estimate that hail of about `MAXSIZE` was aloft in that cell. It is
   grounds for an inspection, not proof of damage, and an insurer may treat it as
   indicative only.
2. Radar coverage has gaps and degrades with distance from the station and at low
   altitude, so **absence of a signature is weaker evidence than presence**.
3. The 1-inch shingle-damage threshold in the module is a widely cited default, not
   a fact about any specific roof: material, age and impact angle all matter and
   none of them are visible here.
4. Bounding boxes are corrected by `cos(latitude)`. A naive square box is
   materially wrong at US latitudes, where a degree of longitude is about 85km at
   40 degrees north against 111km for latitude.

Weather rows are deliberately **not** tenant-scoped: a storm over a county is a
public fact, and storing it per tenant would duplicate it N times. Tenant linkage
happens in `property_events`, which is org-scoped and RLS-protected.

## 9. Known gaps, stated rather than hidden

- ~~Webhook subscription registration is not covered~~ **RESOLVED.** There is no
  registration API at all, confirmed exhaustively: all 83 official OpenAPI specs
  parsed, 852 paths, **zero** containing "hook". Subscription is UI-only on both
  paths, and both paths are now handled in code. Whether the workflow path is signed
  is also **settled by live capture** (it is not) — see section 2b.
- ~~Workflows can be created via API~~ **THEY CANNOT**, confirmed four ways: live
  `POST /workflows/` → 404; the spec defines `/workflows/` as **GET-only in both**
  versions; `/snapshots/` has no load-into-location endpoint; and HighLevel's own
  ideas board carries an open 70-voter request for "API to create Workflows".
  A workflow must be built in the UI once, then it CAN be triggered
  programmatically via `POST /contacts/{contactId}/workflow/{workflowId}` (verified,
  HTTP 201).
- **OAuth install flow is IMPLEMENTED** (`src/lib/ghl-oauth.ts`: authorize URL, code
  exchange, rotating-refresh handling, agency→location token exchange on the
  version-safe camelCase path). **Not yet exercised end to end**, because that needs
  a Marketplace app's `client_id`/`client_secret` and **app creation is
  Developer-Portal-only** — there is no app-create path in `marketplace.json`
  (only installations, rebilling config, and billing charges).
- ~~Historical weather is not implemented~~ **CLOSED 2026-08-19**: implemented via
  NCEI SWDI with point-level hail sizes back to at least 2010 (section 8b).
- **The sandbox has zero snapshots**, so snapshot-based sub-account provisioning is
  untested here.
