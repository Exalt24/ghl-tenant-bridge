-- GHL Tenant Bridge — schema
--
-- Design rule: THE DATABASE LAYER IS THE ONLY ONE THAT COUNTS. Server checks and
-- UI gating are convenience; they are wrong eventually. Every tenant-scoped table
-- here is RLS default-deny and carries org_id, so a caller holding their own JWT
-- and hitting the REST API directly still cannot read another tenant's rows.
--
-- Verified GHL facts this schema is built around (measured 2026-08-19, not assumed):
--   * A GHL "sub-account" is a LOCATION. The location id is the tenant key.
--   * Agency-level and location-level tokens are DIFFERENT CLASSES. An agency
--     token gets "Token's user type mismatch!" on sub-account endpoints, so each
--     tenant needs its own location token, stored per row below.
--   * A location token used against the wrong tenant returns 403 "The token does
--     not have access to this location." That is GHL's own boundary; ours has to
--     agree with it, hence the isolation test in tests/.
--   * GHL signs outbound webhooks with X-GHL-Signature (Ed25519, current) and
--     X-WH-Signature (RSA-SHA256, LEGACY, DEPRECATED 2026-09-01).

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------- tenancy core

create table if not exists orgs (
  id          uuid primary key default gen_random_uuid(),
  name        text not null,
  created_at  timestamptz not null default now()
);

create table if not exists org_members (
  org_id   uuid not null references orgs(id) on delete cascade,
  user_id  uuid not null references auth.users(id) on delete cascade,
  role     text not null default 'member' check (role in ('owner','admin','member','homeowner')),
  primary key (org_id, user_id)
);

-- Membership lookup used INSIDE the policies below. SECURITY DEFINER so the
-- policy can read org_members without recursing into org_members' own policy,
-- which is the classic infinite-recursion trap in Supabase RLS.
create or replace function is_org_member(target_org uuid)
returns boolean
language sql
stable
security definer
set search_path = public
as $$
  select exists (
    select 1 from org_members m
    where m.org_id = target_org and m.user_id = auth.uid()
  );
$$;

-- ------------------------------------------------- GHL location -> org mapping

create table if not exists ghl_locations (
  location_id     text primary key,               -- GHL sub-account (location) id
  org_id          uuid not null references orgs(id) on delete cascade,
  company_id      text not null,                  -- GHL agency id
  name            text,
  timezone        text,
  -- Location-scoped Private Integration Token. Encrypted at rest by Supabase
  -- Vault in a real deployment; never expose this column to the anon role.
  pit_secret_ref  text,
  created_at      timestamptz not null default now(),
  unique (location_id, org_id)
);

create index if not exists ghl_locations_org_idx on ghl_locations(org_id);

-- --------------------------------------------------------------- synced records

create table if not exists ghl_contacts (
  id             uuid primary key default gen_random_uuid(),
  org_id         uuid not null references orgs(id) on delete cascade,
  location_id    text not null references ghl_locations(location_id) on delete cascade,
  ghl_contact_id text not null,
  first_name     text,
  last_name      text,
  email          text,
  phone          text,
  tags           text[] not null default '{}',
  raw            jsonb,
  synced_at      timestamptz not null default now(),
  -- One row per contact per tenant. This is also the outbound idempotency key:
  -- a replayed create upserts instead of duplicating.
  unique (location_id, ghl_contact_id)
);

create index if not exists ghl_contacts_org_idx on ghl_contacts(org_id);

-- ------------------------------------------- webhook idempotency + dead letter

-- Inbound event ledger. The UNIQUE constraint is the idempotency mechanism: a
-- replayed delivery loses the insert race and is a provable no-op. This is a
-- database guarantee rather than a cache check, so it survives a cold Redis and
-- two concurrent deliveries hitting different instances.
create table if not exists webhook_events (
  id            uuid primary key default gen_random_uuid(),
  provider      text not null default 'ghl',
  event_id      text not null,
  event_type    text,
  location_id   text,
  org_id        uuid references orgs(id) on delete set null,
  signature_alg text,                             -- 'ed25519' | 'rsa-legacy'
  received_at   timestamptz not null default now(),
  processed_at  timestamptz,
  attempts      int not null default 0,
  status        text not null default 'received'
                check (status in ('received','processed','failed','dead')),
  last_error    text,
  payload       jsonb,
  unique (provider, event_id)
);

create index if not exists webhook_events_status_idx on webhook_events(status, received_at);
create index if not exists webhook_events_org_idx on webhook_events(org_id);

-- ------------------------------------------------- structured event records (#6)

create table if not exists property_events (
  id            uuid primary key default gen_random_uuid(),
  org_id        uuid not null references orgs(id) on delete cascade,
  location_id   text not null references ghl_locations(location_id) on delete cascade,
  contact_id    uuid references ghl_contacts(id) on delete set null,
  kind          text not null check (kind in
                  ('photo','document','inspection','weather','activity')),
  occurred_at   timestamptz not null,
  address       text,
  latitude      numeric(9,6),
  longitude     numeric(9,6),
  summary       text,
  detail        jsonb,
  created_at    timestamptz not null default now()
);

create index if not exists property_events_org_kind_idx on property_events(org_id, kind, occurred_at desc);

-- ------------------------------------------------------ NOAA weather cache (#11)

-- Weather is NOT tenant data: a hailstorm over a ZIP is a public fact, so it is
-- deliberately shared and readable by any authenticated user. Tenant linkage
-- happens in property_events, which IS scoped. Keeping the distinction explicit
-- avoids storing the same NOAA event once per tenant.
create table if not exists weather_events (
  id             uuid primary key default gen_random_uuid(),
  source         text not null default 'noaa',
  external_id    text not null,
  event_type     text,                            -- 'Hail', 'Thunderstorm Wind', ...
  occurred_at    timestamptz not null,
  state          text,
  county         text,
  latitude       numeric(9,6),
  longitude      numeric(9,6),
  magnitude      numeric,
  detail         jsonb,
  ingested_at    timestamptz not null default now(),
  unique (source, external_id)
);

create index if not exists weather_events_time_idx on weather_events(occurred_at desc);
create index if not exists weather_events_geo_idx on weather_events(state, county);

-- ----------------------------------------------------- seats / billing (#8, #9)

create table if not exists org_billing (
  org_id                 uuid primary key references orgs(id) on delete cascade,
  stripe_customer_id     text,
  stripe_subscription_id text,
  seats_purchased        int not null default 1 check (seats_purchased >= 0),
  seats_used             int not null default 0 check (seats_used >= 0),
  status                 text not null default 'inactive',
  updated_at             timestamptz not null default now()
);

-- ============================================================================
-- RLS: DEFAULT DENY EVERYWHERE, then narrow grants.
--
-- Enabling RLS without a policy denies all access, which is the state we want to
-- start from. Note FORCE ROW LEVEL SECURITY: without it the table OWNER bypasses
-- its own policies, which is a silent hole in exactly the audit a client runs.
-- ============================================================================

alter table orgs             enable row level security;
alter table org_members      enable row level security;
alter table ghl_locations    enable row level security;
alter table ghl_contacts     enable row level security;
alter table webhook_events   enable row level security;
alter table property_events  enable row level security;
alter table weather_events   enable row level security;
alter table org_billing      enable row level security;

alter table orgs             force row level security;
alter table org_members      force row level security;
alter table ghl_locations    force row level security;
alter table ghl_contacts     force row level security;
alter table webhook_events   force row level security;
alter table property_events  force row level security;
alter table org_billing      force row level security;

-- orgs: visible only to members
create policy orgs_select on orgs
  for select using (is_org_member(id));

-- org_members: you can see the membership rows of orgs you belong to
create policy org_members_select on org_members
  for select using (is_org_member(org_id));

-- ghl_locations: tenant-scoped read. NOTE there is deliberately NO insert/update
-- policy: provisioning a location mapping is a privileged server-side operation
-- and runs with the service role, never with a user JWT.
create policy ghl_locations_select on ghl_locations
  for select using (is_org_member(org_id));

-- ghl_contacts: read scoped to the org. Writes are server-side only (the webhook
-- receiver), so again no insert/update policy for end users.
create policy ghl_contacts_select on ghl_contacts
  for select using (is_org_member(org_id));

-- BOTH USING AND WITH CHECK, on purpose. A policy with USING alone passes the
-- read test on a row you own and then lets you REWRITE its org_id into someone
-- else's tenant. That is the subtle version of a cross-tenant write and it is the
-- single easiest RLS mistake to ship.
create policy ghl_contacts_update on ghl_contacts
  for update
  using (is_org_member(org_id))
  with check (is_org_member(org_id));

-- property_events: same shape
create policy property_events_select on property_events
  for select using (is_org_member(org_id));

create policy property_events_insert on property_events
  for insert with check (is_org_member(org_id));

create policy property_events_update on property_events
  for update
  using (is_org_member(org_id))
  with check (is_org_member(org_id));

-- webhook_events: operational data. NO end-user policy at all, so RLS-enabled +
-- zero policies = nobody but the service role can read it. Payloads can contain
-- another tenant's data before mapping, so this must never be user-readable.

-- org_billing: members read, nobody writes but the server (Stripe webhook)
create policy org_billing_select on org_billing
  for select using (is_org_member(org_id));

-- weather_events: public reference data, readable by any authenticated user.
create policy weather_events_select on weather_events
  for select to authenticated using (true);

-- ============================================================================
-- Seat accounting as an ATOMIC function, not a read-modify-write in app code.
-- Two invites landing at once on a 5-seat plan must not both succeed; doing this
-- in application code is a lost-update race.
-- ============================================================================

create or replace function claim_seat(target_org uuid)
returns boolean
language plpgsql
security definer
set search_path = public
as $$
declare
  ok boolean;
begin
  update org_billing
     set seats_used = seats_used + 1,
         updated_at = now()
   where org_id = target_org
     and seats_used < seats_purchased
  returning true into ok;

  return coalesce(ok, false);
end;
$$;

revoke all on function claim_seat(uuid) from public, anon;
grant execute on function claim_seat(uuid) to authenticated;
