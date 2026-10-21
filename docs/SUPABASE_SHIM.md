### The shim is no longer the last word

`tests/postgis.test.py` runs against local Postgres through that shim, and a shim is a
fidelity approximation: passing there proves the SQL is coherent, not that PostgREST,
Storage and RLS behave as assumed on the hosted product. So the same migrations are
applied to a real Supabase project and the load-bearing facts are re-asserted there.

What that pass found, which local testing could not:

- **`outbox-transport-supabase.ts` upserted `client_uuid` into a table that did not
  exist.** The column appeared in exactly one file in the whole repo. The transport had
  unit tests and had never touched real schema, so `0003_field_captures.sql` and
  `0004_storage_field_photos.sql` are the migrations that make it real.
- **The RLS phantom reproduces through PostgREST**, not just in psql. An anon `PATCH`
  of a row it cannot see returns **HTTP 200 with an empty body**, and the row is
  provably unchanged. That is the entire reason the transport counts returned rows.
- **A second tenant is the test that matters.** Anon is a member of nothing, so an
  anon-only negative test can pass while a real cross-tenant hole stays open. There are
  two seeded orgs and two users, and tenant B is refused on both the table and the
  storage folder, with a positive control proving B still works inside its own org.
- **The corner proof holds on Supabase's own PostGIS**: a point at 95% of both bounding
  box edges sits **67.2 km** from a 50 km storm, and `ST_DWithin` rejects it while the
  box would accept it. Swapping the `ST_MakePoint` arguments moves a Dallas point
  **14,096 km**.
