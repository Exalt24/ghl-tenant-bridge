## Measured facts

Everything here was verified on this machine, not read in a doc, except the browser and iOS facts (Safari and the seven day eviction), which are cited from vendor documentation.

### A bounding box is the wrong shape for a radius

Its corners reach about 1.41x the radius, so a property **67 km from a 50 km storm sits
inside the box**. SWDI only accepts a box, so the fetch uses one and the *match* uses
`ST_DWithin` over a generated `geography(Point,4326)` column with a GiST index. The box
stays in the repo as a **negative control**, so the over-selection is measured rather
than asserted.

### Postgres and PostGIS

- `geography` distances and the `ST_DWithin` tolerance are **metres**; `geometry(4326)`
  is **degrees**. Proven both directions.
- `ST_MakePoint` is **(longitude, latitude)**. The columns here are named
  latitude/longitude, so the natural typing order is the wrong one, and a swap produces
  plausible small numbers rather than an error.
- `ST_DWithin` carries `postgis_index_supportfn` and rewrites to `&& _st_expand(...)`.
  **`ST_Distance` carries none** and must never appear in a `WHERE` clause.
- **The RLS phantom.** An `INSERT` violating a `WITH CHECK` clause raises. An `UPDATE`
  or `DELETE` filtered by a `USING` clause **returns success with zero rows**: `UPDATE
  0`, no error, while the same statement on a visible row says `UPDATE 1`. So a client
  treating "it did not throw" as proof deletes its local copy of a record that was
  never stored. `RETURNING` is what makes it detectable. Re-runnable proof:
  `tests/rls_phantom_proof.sql`.
- Creating the `extensions` schema leaves it **without the usage grants** Supabase
  normally provides, which locks every RLS-scoped caller out of PostGIS. Found because
  the suite runs as `authenticated` rather than as owner.

### Geocoding and parcels

- The **Census geocoder** is keyless and returns a **block-face interpolation, not a
  rooftop**: the response carries a `tigerLine` id, a side of the street, and an
  address range. So the parcel lookup must buffer.
- A county ArcGIS **point query returns zero features and no error**, while the same
  coordinate as a ~20 m envelope returns several parcels. A point-in-polygon design
  reports "no parcel for this address" across an entire dataset with nothing to warn you.
- **A dead ArcGIS service answers HTTP 200** with `{"error":{"code":404}}` in the body,
  so the status line is not a health check.
- An unbounded query stops at **2000 rows** with only `exceededTransferLimit` to say so.

### Browsers and offline

- **Background Sync is supported in no version of Safari or iOS Safari**, and iOS has no
  Periodic Background Sync or Background Fetch either. Nothing runs after the app
  closes on the phones field crews carry, so the flush lives in the page.
- **`FileSystemFileHandle.createWritable()` requires Safari/iOS 26** (Chrome 86, Firefox
  111), while OPFS directories have existed in Safari for years. The obvious
  availability check therefore passes on an older iPhone and the write then throws. The
  adapter feature-detects on a real handle and falls back to IndexedDB, and reads
  consult **both** backends so bytes queued before a browser update are not declared
  missing.
- iOS evicts an origin's storage after seven days without interaction, taking IndexedDB,
  OPFS and the Cache API **together**. `navigator.storage.persist()` is the mitigation,
  it is requested on every mount, and its answer is surfaced rather than assumed.

### GoHighLevel

- **Target User and token class are independent axes.** A Sub-Account-target app
  installed from Agency View still returns a **Company** token with no `locationId`, so
  `/oauth/locationToken` is mandatory even there.
- **The workflow Custom Webhook action is unsigned.** Confirmed by capturing a real
  delivery: six headers, none a signature, and no `webhookId`, `timestamp` or
  `locationId` in the body. So no idempotency key and no replay window on that path,
  and it is a premium action. The Marketplace app subscription path is free,
  Ed25519-signed, retried 12 times and logged for 30 days.
- **There is no `FormSubmit` event** in the 58-event app catalogue, so form submissions
  must come through a workflow action, which is exactly the unsigned path.
- **Webhook subscription registration has no API at all**: all 83 official specs
  parsed: 852 paths, zero containing "hook". It is UI-only on both paths.
- Four distinct refusal messages mean four different things: missing scope, wrong token
  class on a PIT, right class but wrong tenant, and an OAuth Company token hitting a
  sub-account scope.
- Sandbox limits: **25 req/10s**, 10,000/day, per location, and a **2-location hard cap**
  per sandbox agency.
