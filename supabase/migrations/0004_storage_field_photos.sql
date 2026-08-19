-- 0004_storage_field_photos.sql
--
-- The private bucket the offline queue uploads photo bytes into, plus its RLS.
--
-- WHY SQL AND NOT THE DASHBOARD
--   A bucket created by hand in the dashboard is not in the repo, so a fresh
--   environment silently lacks it and every upload fails at runtime with a storage
--   error that reads like a bug in the client. `storage.buckets` is an ordinary table,
--   so the bucket belongs in a migration like everything else.
--
-- THE PATH CONVENTION IS THE SECURITY BOUNDARY
--   objectPathFor() in outbox-transport-supabase.ts writes `<org_id>/<item_id>.jpg`.
--   So the first path segment IS the tenant, and the policies below authorise on it
--   via storage.foldername(name)[1]. If that convention ever changes, these policies
--   must change with it, which is why the coupling is stated here rather than implied.

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'field-photos',
  'field-photos',
  false,                       -- private. A roof photo is customer property.
  10485760,                    -- 10 MB. The client downscales long before this.
  array['image/jpeg']          -- jpeg only: the capture input forces iOS to transcode
                               -- HEIC, so anything else here means a client bug.
)
on conflict (id) do update
  set public = false,
      file_size_limit = excluded.file_size_limit,
      allowed_mime_types = excluded.allowed_mime_types;

-- Uploads. WITH CHECK on insert, and the tenant is the first path segment.
drop policy if exists field_photos_insert on storage.objects;
create policy field_photos_insert on storage.objects
  for insert to authenticated
  with check (
    bucket_id = 'field-photos'
    and public.is_org_member((storage.foldername(name))[1]::uuid)
  );

-- Reads, scoped the same way.
drop policy if exists field_photos_select on storage.objects;
create policy field_photos_select on storage.objects
  for select to authenticated
  using (
    bucket_id = 'field-photos'
    and public.is_org_member((storage.foldername(name))[1]::uuid)
  );

-- Overwrite on retry. uploadBytes() passes upsert:true so an interrupted flush
-- rewrites the same deterministic object instead of orphaning a partial one, and that
-- is an UPDATE on storage.objects, not an INSERT. Without this policy a retry after a
-- half-written upload fails forever.
drop policy if exists field_photos_update on storage.objects;
create policy field_photos_update on storage.objects
  for update to authenticated
  using (
    bucket_id = 'field-photos'
    and public.is_org_member((storage.foldername(name))[1]::uuid)
  )
  with check (
    bucket_id = 'field-photos'
    and public.is_org_member((storage.foldername(name))[1]::uuid)
  );
