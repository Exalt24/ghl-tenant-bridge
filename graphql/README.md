# graphql

Hasura GraphQL Engine in front of this repository's PostGIS schema: the storm radius match from the app, asked as a GraphQL query, with a per-organisation permission rule.

`smoke.py` runs it for real. It starts a PostGIS container with the repository's own migrations, starts Hasura v2.50.5, loads `metadata.json`, asks for properties within 50 km of a storm and checks the answer, then breaks the setup three ways and requires the checks to notice.

## Run it

You need Docker and Python 3. Nothing is installed and no account is used.

```
python graphql/smoke.py
```

It prints one line per check and `SMOKE PASS` at the end, then removes the containers and volumes. `python graphql/check_noop_init.py` is a second check: it shows the stack's first-start script cannot stop the database container when the file mode is the Linux one (a Windows bind mount hides that), with a control that does stop it. `--keep` leaves the stack up on `http://127.0.0.1:8080` with the admin secret from `docker-compose.yml` (a local development value).

## Measured

One full pass on 2026-10-24 against `hasura/graphql-engine:v2.50.5` and `postgis/postgis:17-3.5`:

- 8 of 8 checks pass. A 50 km radius from Dallas returns the point 40 km away and excludes the point 67 km away that a bounding box would have included. A 100 km radius returns both, so an empty 50 km answer is a real exclusion and not an empty result.
- Organisation A sees its own four rows, organisation B sees its one, and a request without the organisation header is refused.
- 3 of 3 mutants are caught: the tenant filter removed (6 checks fail), the geography column left out of the permission (4 fail), and PostGIS's schema taken off the search path (4 fail).

## What I found while building it

Hasura writes unqualified casts such as `$1::geography` into its SQL. PostGIS is installed in the `extensions` schema here (migration `0002_postgis.sql`, as on Supabase), so with the default search path Hasura builds the schema and exposes `_st_d_within` on the column, and then every spatial query fails with `type "geography" does not exist`. `search_path.sql` sets `"$user", public, extensions` on the database, which fixes it, and the third mutant proves that line is what makes the checks pass. The Hasura pages I read say nothing about where PostGIS must be installed, so this is a measured result, not a documented one.

The `postgis/postgis` image installs PostGIS into `public` in its own init scripts and then calls `postgis_lib_version()`, which fails after the migration has moved the extension. `noop.sh` replaces those two scripts.

## Limits

- Hasura connects as the `postgres` superuser, which bypasses row level security. The organisation boundary here is Hasura's permission rule (`org_id = X-Hasura-Org-Id`), not the database policies the Supabase path relies on. The policies call `auth.uid()`, which Hasura does not set.
- Requests in the smoke test authenticate with the admin secret and role headers. A real deployment would use JWT or a webhook, and Hasura's own docs say the admin secret is mandatory with either.
- The `tenant` role reads every row of `weather_events` (its filter is empty), because storm records are public facts shared by all tenants; `property_events` is the tenant data and is filtered by organisation.
- The matcher function `properties_near_weather_event` in `0002_postgis.sql` returns a table type, and Hasura's custom function docs say the return type must be a tracked table, so it is not exposed here. The same page describes a workaround (track an empty table with the same columns), which this example does not use. The query uses `_st_d_within` on `property_events.geog` directly.
- The schema comes from `tests/supabase_shim.sql` plus the first two migrations, which stand in for a Supabase project. Migrations 0003 and 0004 (field captures, storage) are not applied.
- Nothing here is deployed.
