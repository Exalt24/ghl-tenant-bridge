-- 0003_field_captures.sql
--
-- The landing table for the offline capture queue.
--
-- WHY THIS MIGRATION EXISTS
--   outbox-transport-supabase.ts upserts a row with `client_uuid`, `captured_at` and
--   `object_path`, and no migration ever created a table with those columns. The
--   transport was written, unit-tested against a fake, and never pointed at real
--   schema, so `client_uuid` appeared in exactly one file in the repo. Written is not
--   wired. This closes it.
--
-- THE THREE THINGS THAT ARE LOAD-BEARING HERE
--   1. A UNIQUE **INDEX** on client_uuid, not a unique constraint. PostgREST resolves
--      an `onConflict` target against an index, so an upsert against a constraint-only
--      column fails at request time rather than at deploy time.
--   2. `force row level security`. Without it the table OWNER bypasses its own
--      policies, which is invisible until an audit finds it.
--   3. Both `using` AND `with check` on UPDATE. A using-only policy passes the read
--      test on a row you own and then lets you rewrite its org_id into another tenant.

create table if not exists public.field_captures (
  -- UUIDv7 minted on the device at capture time. This IS the idempotency key end to
  -- end: the same photo retried from the same queue lands on the same row.
  client_uuid    uuid primary key,
  org_id         uuid not null references public.orgs(id) on delete cascade,
  kind           text not null check (kind in ('photo', 'form')),
  captured_at    timestamptz not null,
  -- Deterministic storage path, null for a form-only item that carries no bytes.
  object_path    text,
  -- Stamped on the device at capture, NOT at upload. The upload can be hours later
  -- and from a different place entirely, so an upload-time location would be a lie.
  captured_lat   numeric,
  captured_lon   numeric,
  original_name  text,
  original_bytes bigint,
  received_at    timestamptz not null default now()
);

-- Explicit UNIQUE INDEX for the upsert conflict target. The primary key already
-- creates one, and this is deliberately redundant so the requirement is stated rather
-- than inherited: if client_uuid ever stops being the PK, the upsert must not break.
create unique index if not exists field_captures_client_uuid_idx
  on public.field_captures (client_uuid);

-- Tenant reads are always scoped by org, so the index that serves them leads with it.
create index if not exists field_captures_org_captured_idx
  on public.field_captures (org_id, captured_at desc);

alter table public.field_captures enable row level security;
alter table public.field_captures force row level security;

drop policy if exists field_captures_select on public.field_captures;
create policy field_captures_select on public.field_captures
  for select using (public.is_org_member(org_id));

drop policy if exists field_captures_insert on public.field_captures;
create policy field_captures_insert on public.field_captures
  for insert with check (public.is_org_member(org_id));

-- USING and WITH CHECK both, on purpose. USING alone would let a member rewrite a row
-- they can see into an org they are not a member of.
drop policy if exists field_captures_update on public.field_captures;
create policy field_captures_update on public.field_captures
  for update using (public.is_org_member(org_id))
          with check (public.is_org_member(org_id));

-- No DELETE policy. A field capture is a record of what someone saw at a place and
-- time; removing one is an admin action through the service role, not a client action.
