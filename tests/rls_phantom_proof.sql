-- Does an RLS-rejected write really return SUCCESS with zero rows?
-- The whole phantom guard rests on this, so measure it instead of citing it.
\set ON_ERROR_STOP off

create table if not exists phantom_demo (
  id      int primary key,
  org_id  int not null,
  note    text
);
alter table phantom_demo enable row level security;
alter table phantom_demo force row level security;

truncate phantom_demo;
insert into phantom_demo values (1, 100, 'org 100 row'), (2, 200, 'org 200 row');

-- Caller may only touch org 100. Note USING for read/update visibility and
-- WITH CHECK for what may be written.
drop policy if exists only_org_100 on phantom_demo;
create policy only_org_100 on phantom_demo
  for all
  using (org_id = 100)
  with check (org_id = 100);

do $$ begin
  if not exists (select 1 from pg_roles where rolname='app_user') then
    create role app_user nologin;
  end if;
end $$;
grant select, insert, update, delete on phantom_demo to app_user;

set role app_user;

-- 1. UPDATE a row the policy HIDES. This is the phantom case.
update phantom_demo set note = 'hijacked' where id = 2;
select '1. UPDATE on a hidden row -> the command tag above is the whole answer' as note;

-- 2. DELETE a row the policy hides.
delete from phantom_demo where id = 2;
select '2. DELETE on a hidden row -> same, zero rows, no error' as note;

-- 3. The control: the same UPDATE on a VISIBLE row must report 1.
update phantom_demo set note = 'legitimately updated' where id = 1;
select '3. CONTROL: UPDATE on a visible row -> must say UPDATE 1' as note;

-- 4. And the contrast worth knowing: an INSERT that violates WITH CHECK DOES raise.
insert into phantom_demo values (3, 999, 'wrong org');
select '4. INSERT violating WITH CHECK -> raises 42501, unlike update/delete' as note;

-- 5. RETURNING is what makes the difference detectable from a client.
update phantom_demo set note = 'x' where id = 2 returning id;
select '5. UPDATE ... RETURNING on a hidden row -> zero rows returned' as note;

update phantom_demo set note = 'y' where id = 1 returning id;
select '6. UPDATE ... RETURNING on a visible row -> returns the id' as note;

reset role;
select 'final state, as superuser:' as note;
select * from phantom_demo order by id;
