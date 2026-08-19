-- ============================================================================
-- 0002_postgis.sql  --  true spherical distance for hail-to-property matching
-- ============================================================================
--
-- WHY THIS EXISTS
-- ---------------
-- swdi.ts already asks NCEI for hail inside a bounding box, because a bbox is the
-- only shape that API accepts. A bbox is fine as a FETCH filter and wrong as a
-- MATCH filter: the corners of a box reach ~1.41x the radius, so a property that
-- is 70 km from a storm is still "inside" a box drawn for 50 km. Matching on the
-- box means telling a roofer a house was hit when it was not.
--
-- So: fetch with the bbox (the API's constraint), then match with ST_DWithin on
-- geography (true spheroidal metres). tests/postgis.test.py proves the corner
-- case is real rather than asserting it.
--
-- EVERY CLAIM BELOW WAS VERIFIED AGAINST A LIVE POSTGRES, NOT READ IN A DOC.
-- Measured 2026-08-20 on postgis/postgis:17-3.5 (PostgreSQL 17.5, PostGIS 3.5.2):
--   * `create extension postgis with schema extensions` lands in `extensions`.
--   * The generated column below is accepted: pg_attribute.attgenerated = 's'.
--   * ST_MakePoint is (X, Y) = (longitude, latitude). Verified: ST_X of the
--     London row returns -0.128, the longitude. Our columns are named
--     latitude/longitude, so the natural typing order is the WRONG order.
--   * geography distances and the ST_DWithin tolerance are METRES. Verified both
--     directions: London->Paris = 343.9 km; 400000 matches, 400 does not.
--   * geometry(4326) would be DEGREES, not metres. Verified: the same pair
--     matches at a tolerance of 4.0. That is the trap this file avoids.
--   * ST_DWithin carries SUPPORT extensions.postgis_index_supportfn and rewrites
--     to `geog && _st_expand(...)` on a GiST index. ST_Distance carries NONE and
--     cannot use an index, so it must never appear in a WHERE clause.
--
-- PostGIS is NOT relocatable (postgis.control sets relocatable = false), so the
-- schema choice here is effectively permanent: changing it later costs a
-- DROP EXTENSION ... CASCADE and a restore. `extensions` is what Supabase's own
-- docs use, and it keeps spatial_ref_sys out of `public`, where it would be
-- exposed through the REST API and cannot have RLS enabled on it.

create schema if not exists extensions;
create extension if not exists postgis with schema extensions;

-- USAGE grants, and this is not boilerplate: it is a bug this migration would
-- otherwise INTRODUCE. Supabase projects ship an `extensions` schema that already
-- grants usage to anon/authenticated/service_role. But the line above will CREATE
-- that schema on any project where it is absent, and a schema you create yourself
-- has no grants. The matcher below is SECURITY INVOKER by design, so it executes as
-- the CALLER, which means an authenticated user without usage on `extensions` gets
-- "permission denied for schema extensions" the first time they run a radius query.
-- Caught by tests/postgis.test.py, which exercises the function as `authenticated`
-- rather than as the owner. Running the suite as a superuser would have hidden this
-- completely.
do $$
declare r text;
begin
  foreach r in array array['anon','authenticated','service_role'] loop
    if exists (select 1 from pg_roles where rolname = r) then
      execute format('grant usage on schema extensions to %I', r);
    end if;
  end loop;
end $$;

-- ---------------------------------------------------------------- geog columns
--
-- GENERATED ALWAYS ... STORED, not a trigger. Every function in the expression
-- is IMMUTABLE in PostGIS (st_makepoint, st_setsrid, and the geography casts),
-- which is exactly what Postgres requires for a stored generated column. The
-- payoff is that geog cannot drift from latitude/longitude: it is not a copy
-- that some later insert path might forget to maintain.
--
-- The expression is fully schema-qualified on purpose. A generated column's
-- expression is resolved at DDL time using the current search_path, so leaving
-- it bare would make this migration depend on how the connection happens to be
-- configured.
--
-- longitude is cast to float8 explicitly. The columns are numeric(9,6) and
-- st_makepoint takes float8; the implicit cast works, but naming it means the
-- next reader does not have to wonder.
--
-- st_makepoint is STRICT, so a row missing either coordinate gets a NULL geog
-- and simply never matches a radius query. That is the desired behaviour, and it
-- is why geog carries no NOT NULL constraint.

alter table property_events
  add column if not exists geog extensions.geography(Point, 4326)
  generated always as (
    extensions.st_setsrid(
      extensions.st_makepoint(longitude::float8, latitude::float8),   -- LON FIRST
      4326
    )::extensions.geography
  ) stored;

alter table weather_events
  add column if not exists geog extensions.geography(Point, 4326)
  generated always as (
    extensions.st_setsrid(
      extensions.st_makepoint(longitude::float8, latitude::float8),   -- LON FIRST
      4326
    )::extensions.geography
  ) stored;

-- ---------------------------------------------------------------- GiST indexes
-- On the bare column. Wrapping the indexed column in a function or a cast at
-- query time defeats the index silently, which is why the RPC below casts the
-- CONSTANT and never the column.

create index if not exists property_events_geog_idx on property_events using gist (geog);
create index if not exists weather_events_geog_idx  on weather_events  using gist (geog);

-- ---------------------------------------------------------------- the matcher
--
-- SECURITY INVOKER, deliberately. This is the tenant-safety decision in the file.
-- property_events is org-scoped and its RLS policies must apply to the caller, so
-- the function must NOT run as its owner. A SECURITY DEFINER version here would
-- bypass RLS and happily return another org's properties, and it would look
-- correct in every test that only ever queries as one tenant.
--
-- weather_events is intentionally NOT tenant data (a hailstorm over a ZIP is a
-- public fact, see 0001), so the join reads a shared row and scopes only the
-- property side. That asymmetry is the point: one storm row, many tenants.
--
-- search_path is pinned to '' and every name is qualified, including the KNN
-- operator via operator(extensions.<->). An unqualified operator is the piece
-- that gets forgotten and it fails at RUNTIME, not at create time.
--
-- Radius arrives in KM because that is how a human states it, and is converted
-- to metres exactly once, here. Passing 50 to ST_DWithin on geography means 50
-- METRES and would silently return almost nothing.

create or replace function properties_near_weather_event(
  p_weather_event_id uuid,
  p_radius_km        double precision
)
returns table (
  property_event_id uuid,
  org_id            uuid,
  address           text,
  distance_km       double precision
)
language sql
stable
security invoker
set search_path = ''
as $$
  select p.id,
         p.org_id,
         p.address,
         extensions.st_distance(p.geog, w.geog) / 1000.0 as distance_km
  from public.weather_events w
  join public.property_events p
    on extensions.st_dwithin(p.geog, w.geog, p_radius_km * 1000.0)   -- METRES
  where w.id = p_weather_event_id
  order by p.geog operator(extensions.<->) w.geog;
$$;

comment on function properties_near_weather_event(uuid, double precision) is
  'Properties within a true spherical radius of a weather event. SECURITY INVOKER so '
  'property_events RLS scopes the result to the caller''s org. Radius in km, converted '
  'to metres internally. Filters with ST_DWithin (index-aware) and only computes '
  'ST_Distance for reporting, because ST_Distance has no index support function.';

-- ------------------------------------------------- the bbox, kept for contrast
--
-- Not dead code. This is the shape swdi.ts is forced to send to NCEI, and
-- tests/postgis.test.py uses it as the NEGATIVE control: it proves a corner
-- property that the bbox accepts is correctly rejected by ST_DWithin. Without
-- the comparison, "we use PostGIS" is a claim rather than a measurement.

create or replace function properties_in_bbox_of_weather_event(
  p_weather_event_id uuid,
  p_radius_km        double precision
)
returns table (
  property_event_id uuid,
  distance_km       double precision
)
language sql
stable
security invoker
set search_path = ''
as $$
  with w as (
    select geog, latitude::float8 as lat, longitude::float8 as lon
    from public.weather_events where id = p_weather_event_id
  ),
  box as (
    -- same maths as bboxAround() in src/lib/swdi.ts: degrees of longitude shrink
    -- by cos(latitude), and the cos is floored so a near-polar point cannot
    -- divide by ~zero.
    select w.geog,
           w.lat - (p_radius_km / 111.32)                                        as min_lat,
           w.lat + (p_radius_km / 111.32)                                        as max_lat,
           w.lon - (p_radius_km / (111.32 * greatest(cos(radians(w.lat)), 0.01))) as min_lon,
           w.lon + (p_radius_km / (111.32 * greatest(cos(radians(w.lat)), 0.01))) as max_lon
    from w
  )
  select p.id,
         extensions.st_distance(p.geog, box.geog) / 1000.0
  from box
  join public.property_events p
    on p.latitude::float8  between box.min_lat and box.max_lat
   and p.longitude::float8 between box.min_lon and box.max_lon;
$$;

comment on function properties_in_bbox_of_weather_event(uuid, double precision) is
  'The bounding-box match, kept as a negative control for tests/postgis.test.py. A box '
  'reaches ~1.41x the radius at its corners, so this deliberately over-selects. Never '
  'use it to decide whether a property was hit.';
