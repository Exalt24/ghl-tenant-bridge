-- Hasura writes unqualified casts such as `$1::geography` into its SQL. PostGIS lives in the `extensions` schema here (migration 0002, as on
-- Supabase), so the connecting role has to find it on its search_path or every spatial filter fails with: type "geography" does not exist.
-- Measured against Hasura v2.50.5 on 2026-10-24. Setting it on the database covers every role that connects to it, Hasura's included.
alter database relay_geo set search_path = "$user", public, extensions;
