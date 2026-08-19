-- ============================================================================
-- supabase_shim.sql  --  TEST HARNESS ONLY. Never run this against a real project.
-- ============================================================================
--
-- 0001_schema.sql targets Supabase, so it depends on things a vanilla Postgres
-- container does not have: the `auth` schema, `auth.users`, `auth.uid()`, and the
-- `anon` / `authenticated` / `service_role` roles. This file creates the smallest
-- possible stand-in for those so the migrations can be applied and, more
-- importantly, so RLS can actually be EXERCISED locally.
--
-- The one design decision worth reading: auth.uid() here reads a session GUC
-- instead of a JWT. That makes the current user switchable inside a transaction
-- with `set local test.user_id = '<uuid>'`, which is what lets a test prove that
-- tenant B cannot see tenant A's rows THROUGH the same function, rather than
-- asserting isolation from a single logged-in perspective. A test that only ever
-- queries as one tenant cannot fail for the reason that matters.
--
-- Fidelity limits, stated rather than glossed:
--   * Real Supabase mints `auth.uid()` from a verified JWT. This trusts a session
--     variable. Fine for exercising policy LOGIC, useless for testing token
--     handling.
--   * This container has no supautils, no PostgREST, and no Supabase role grants
--     beyond what is created here. So it validates the SQL and the policies, and
--     it does NOT validate Supabase's permission model or the REST surface.
--   * Passing here is necessary, not sufficient. The client-SDK negative tests
--     against a real project remain the authority for anon-key behaviour.

-- ---------------------------------------------------------------- roles
do $$
begin
  if not exists (select 1 from pg_roles where rolname = 'anon') then
    create role anon nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'authenticated') then
    create role authenticated nologin noinherit;
  end if;
  if not exists (select 1 from pg_roles where rolname = 'service_role') then
    create role service_role nologin noinherit bypassrls;
  end if;
end $$;

-- ---------------------------------------------------------------- auth schema
create schema if not exists auth;

create table if not exists auth.users (
  id    uuid primary key default gen_random_uuid(),
  email text
);

-- Switchable current user. `true` on current_setting means "return null if unset"
-- rather than raising, so an unauthenticated query behaves like anon instead of
-- erroring, which is what the anon-sees-nothing test needs.
create or replace function auth.uid()
returns uuid
language sql
stable
as $$
  select nullif(current_setting('test.user_id', true), '')::uuid;
$$;

grant usage on schema auth to anon, authenticated, service_role;
grant select on auth.users to authenticated, service_role;

-- ------------------------------------------------- Supabase default privileges
--
-- 0001_schema.sql contains NO table grants. It relies on the Supabase platform,
-- which sets default privileges so that tables created in `public` are reachable
-- by `anon` and `authenticated`, with RLS then deciding which ROWS they see.
-- A vanilla Postgres has no such defaults, so without this block every query as
-- `authenticated` dies with "permission denied for table weather_events" and no
-- policy is ever evaluated.
--
-- Worth stating plainly, because it is a genuine property of the schema: 0001's
-- access model is platform grants PLUS its own RLS. On a non-Supabase Postgres the
-- tables are simply unreachable, which fails CLOSED. That is the safe direction,
-- but it does mean the policies cannot be exercised at all without this shim.
--
-- ALTER DEFAULT PRIVILEGES is used rather than GRANT ON ALL TABLES because this
-- file runs BEFORE the migrations, so the tables do not exist yet. Defaults set by
-- the `postgres` role apply to everything `postgres` creates afterwards.
--
-- Note what is deliberately NOT granted: no BYPASSRLS on anon or authenticated, so
-- the negative tests still mean something. service_role has BYPASSRLS above, which
-- mirrors Supabase and is exactly why the client-SDK tests use the anon key instead.

grant usage on schema public to anon, authenticated, service_role;

alter default privileges in schema public
  grant select, insert, update, delete on tables to anon, authenticated, service_role;
alter default privileges in schema public
  grant usage, select on sequences to anon, authenticated, service_role;
alter default privileges in schema public
  grant execute on functions to anon, authenticated, service_role;
