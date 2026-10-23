-- ============================================================================
-- 0005_farm_lists.sql  --  farm lists of GPS points and polygons, validated and intersected
-- ============================================================================
--
-- WHY THIS EXISTS
-- ---------------
-- A farm list is a customer-supplied file of plots, each a GPS point or a polygon. Before anything is
-- run against a geospatial dataset the list has to be made trustworthy, and "trustworthy" is a set of
-- checks that PostGIS will NOT do on its own: it accepts a ring that is not closed, a latitude of 200,
-- and a point whose latitude and longitude were typed the wrong way round. Each of those would flow
-- silently into an intersection and give a confident wrong answer, so each one gets a stored REASON CODE
-- here instead of a free-text message, and a rejected plot is kept (with its original GeoJSON) rather
-- than dropped.
--
-- WHAT WAS MEASURED BEFORE THE RULES WERE WRITTEN (postgis/postgis:17-3.5, PostGIS 3.5, 2026-10-24)
--   * ST_GeomFromGeoJSON accepts an unclosed ring and a 3-position ring without complaint; only
--     ST_IsValidReason names them. So the reason code is DERIVED from ST_IsValidReason, not guessed.
--   * A bow-tie ring has a shoelace ST_Area of 0 while ST_MakeValid returns two triangles of area 0.5, so
--     "repair and compare areas" would silently turn a mistake into two plots. A self-intersection is
--     therefore rejected, and only a defect whose repaired polygon keeps the original area (a spike) is
--     marked repaired.
--   * ST_Area on geography of a 1 km by 1 km square at the equator is 99.33 hectares, the hectare figure
--     the tests compare against.
--   * The intersection of two squares that only share an edge is a LineString of area 0, so "touching" is
--     not "overlapping" and plot_overlaps reports it as nothing.
--
-- Everything is SECURITY INVOKER and search_path is pinned to '', the same as 0002: RLS applies to the
-- caller, and every name is qualified.

-- A composite key so a plot can only be attached to a list of its OWN org.
create table if not exists public.farm_lists (
  id             uuid primary key default gen_random_uuid(),
  org_id         uuid not null references public.orgs(id) on delete cascade,
  name           text not null,
  content_sha256 text not null,
  plot_count     integer not null default 0,
  created_at     timestamptz not null default now(),
  unique (org_id, content_sha256),
  unique (id, org_id)
);

create table if not exists public.farm_plots (
  id            uuid primary key default gen_random_uuid(),
  list_id       uuid not null,
  org_id        uuid not null,
  feature_index integer not null,
  plot_ref      text not null,
  geom          extensions.geometry(Geometry, 4326),
  status        text not null check (status in ('accepted', 'repaired', 'rejected')),
  reason        text,
  point_only    boolean not null default false,
  area_ha       numeric,
  original      jsonb not null,
  foreign key (list_id, org_id) references public.farm_lists (id, org_id) on delete cascade,
  unique (list_id, feature_index)
);

-- Public reference layers (for example a deforestation-alert area). Every signed-in user reads them,
-- nobody writes them through the API.
create table if not exists public.reference_regions (
  id    uuid primary key default gen_random_uuid(),
  layer text not null,
  name  text not null,
  geom  extensions.geometry(MultiPolygon, 4326) not null
);

create index if not exists farm_plots_geom_idx on public.farm_plots using gist (geom);
create index if not exists reference_regions_geom_idx on public.reference_regions using gist (geom);
create index if not exists farm_plots_list_idx on public.farm_plots (list_id);

alter table public.farm_lists enable row level security;
alter table public.farm_plots enable row level security;
alter table public.reference_regions enable row level security;
alter table public.farm_lists force row level security;
alter table public.farm_plots force row level security;
alter table public.reference_regions force row level security;

drop policy if exists farm_lists_select on public.farm_lists;
create policy farm_lists_select on public.farm_lists for select using (public.is_org_member(org_id));
drop policy if exists farm_lists_insert on public.farm_lists;
create policy farm_lists_insert on public.farm_lists for insert with check (public.is_org_member(org_id));
-- farm_lists is updated once by the ingest function (the plot count), so it has an update policy with both clauses.
drop policy if exists farm_lists_update on public.farm_lists;
create policy farm_lists_update on public.farm_lists for update using (public.is_org_member(org_id)) with check (public.is_org_member(org_id));

drop policy if exists farm_plots_select on public.farm_plots;
create policy farm_plots_select on public.farm_plots for select using (public.is_org_member(org_id));
drop policy if exists farm_plots_insert on public.farm_plots;
create policy farm_plots_insert on public.farm_plots for insert with check (public.is_org_member(org_id));

drop policy if exists reference_regions_select on public.reference_regions;
create policy reference_regions_select on public.reference_regions for select to authenticated using (true);

-- ------------------------------------------------------------- reason codes
-- PostGIS words its own validity failures; this maps them to stable codes so a caller never parses prose.
create or replace function public.farm_plot_reason(p_postgis_reason text)
returns text
language sql
immutable
set search_path = ''
as $$
  select case
    when p_postgis_reason ilike 'Too few points%'            then 'too_few_points'
    when p_postgis_reason ilike '%Invalid number of points%' then 'too_few_points'
    when p_postgis_reason ilike '%closed linestring%'        then 'unclosed_ring'
    when p_postgis_reason ilike '%Self-intersection%'        then 'self_intersection'
    when p_postgis_reason ilike 'Hole lies outside shell%'   then 'hole_outside_shell'
    when p_postgis_reason ilike 'Holes are nested%'          then 'nested_holes'
    when p_postgis_reason ilike '%Interior is disconnected%' then 'disconnected_interior'
    else 'invalid_geometry'
  end;
$$;

-- ------------------------------------------------------------------ ingest
-- p_country_box is an optional {min_lon, min_lat, max_lon, max_lat} for the country the list claims; with
-- it, a plot that falls outside the box only when its coordinates are swapped is reported as
-- coordinates_swapped, and one outside it either way as outside_country_box.
-- p_max_ha flags (does not reject) a polygon above that area.
create or replace function public.ingest_farm_list(
  p_org         uuid,
  p_name        text,
  p_geojson     jsonb,
  p_country_box double precision[] default null,
  p_max_ha      numeric default 1000
)
returns uuid
language plpgsql
security invoker
set search_path = ''
as $$
declare
  v_sha       text;
  v_list      uuid;
  v_feat      jsonb;
  v_idx       integer := 0;
  v_ref       text;
  v_refs      text[] := '{}';
  v_gtype     text;
  v_geom      extensions.geometry;
  v_fixed     extensions.geometry;
  v_status    text;
  v_reason    text;
  v_point     boolean;
  v_area_ha   numeric;
  v_box       extensions.geometry;
  v_valid_why text;
  v_orig_area double precision;
  v_fix_area  double precision;
begin
  if p_geojson is null or p_geojson->>'type' is distinct from 'FeatureCollection' or jsonb_typeof(p_geojson->'features') is distinct from 'array' then
    raise exception 'not_a_feature_collection' using errcode = '22023';
  end if;
  if jsonb_array_length(p_geojson->'features') = 0 then
    raise exception 'empty_feature_collection' using errcode = '22023';
  end if;
  if p_country_box is not null and array_length(p_country_box, 1) = 4 then
    v_box := extensions.st_makeenvelope(p_country_box[1], p_country_box[2], p_country_box[3], p_country_box[4], 4326);
  end if;

  v_sha := encode(sha256(convert_to(p_geojson::text, 'utf8')), 'hex');   -- built into Postgres 11 and later, so no pgcrypto is needed
  select id into v_list from public.farm_lists where org_id = p_org and content_sha256 = v_sha;
  if v_list is not null then
    return v_list;                                   -- the same file again: same list, no new rows
  end if;

  insert into public.farm_lists (org_id, name, content_sha256) values (p_org, p_name, v_sha) returning id into v_list;

  for v_feat in select value from jsonb_array_elements(p_geojson->'features') loop
    v_idx := v_idx + 1;
    v_ref := coalesce(nullif(v_feat->'properties'->>'plot_ref', ''), nullif(v_feat->'properties'->>'id', ''), nullif(v_feat->>'id', ''), 'feature-' || v_idx);
    v_geom := null; v_status := 'accepted'; v_reason := null; v_point := false; v_area_ha := null;
    v_gtype := v_feat->'geometry'->>'type';

    if v_ref = any (v_refs) then
      v_status := 'rejected'; v_reason := 'duplicate_plot_ref';
    elsif v_gtype is null or v_gtype not in ('Point', 'Polygon', 'MultiPolygon') then
      v_status := 'rejected'; v_reason := 'unsupported_geometry';
    else
      begin
        v_geom := extensions.st_setsrid(extensions.st_geomfromgeojson((v_feat->'geometry')::text), 4326);
      exception when others then
        v_geom := null; v_status := 'rejected'; v_reason := 'invalid_geojson';
      end;
    end if;
    v_refs := v_refs || v_ref;

    if v_status = 'accepted' and v_geom is not null then
      if extensions.st_ymin(v_geom) < -90 or extensions.st_ymax(v_geom) > 90 then
        v_status := 'rejected'; v_reason := 'latitude_out_of_range';
      elsif extensions.st_xmin(v_geom) < -180 or extensions.st_xmax(v_geom) > 180 then
        v_status := 'rejected'; v_reason := 'longitude_out_of_range';
      elsif v_box is not null and not extensions.st_intersects(v_geom, v_box) then
        v_status := 'rejected';
        v_reason := case when extensions.st_intersects(extensions.st_flipcoordinates(v_geom), v_box) then 'coordinates_swapped' else 'outside_country_box' end;
      end if;
    end if;

    if v_status = 'accepted' and v_geom is not null then
      if v_gtype = 'Point' then
        v_point := true; v_reason := 'point_only';
      else
        if not extensions.st_isvalid(v_geom) then
          v_valid_why := extensions.st_isvalidreason(v_geom);
          v_orig_area := extensions.st_area(v_geom);
          v_fixed := extensions.st_collectionextract(extensions.st_makevalid(v_geom), 3);
          v_fix_area := case when v_fixed is null or extensions.st_isempty(v_fixed) then 0 else extensions.st_area(v_fixed) end;
          if v_orig_area > 0 and abs(v_fix_area - v_orig_area) / v_orig_area < 0.01 and extensions.st_geometrytype(v_fixed) in ('ST_Polygon', 'ST_MultiPolygon') then
            v_geom := extensions.st_multi(v_fixed);
            v_status := 'repaired'; v_reason := public.farm_plot_reason(v_valid_why);
          else
            v_status := 'rejected'; v_reason := public.farm_plot_reason(v_valid_why);
            v_geom := null;
          end if;
        end if;
        if v_geom is not null then
          v_area_ha := extensions.st_area(v_geom::extensions.geography) / 10000.0;
          if v_area_ha < 0.0001 then
            v_status := 'rejected'; v_reason := 'zero_area'; v_geom := null; v_area_ha := null;
          elsif v_area_ha > p_max_ha then
            v_reason := coalesce(v_reason || '+', '') || 'over_area_ceiling';
          end if;
        end if;
      end if;
    end if;

    insert into public.farm_plots (list_id, org_id, feature_index, plot_ref, geom, status, reason, point_only, area_ha, original)
    values (v_list, p_org, v_idx, v_ref, case when v_status = 'rejected' then null else v_geom end, v_status, v_reason, v_point, round(v_area_ha, 4), v_feat);
  end loop;

  update public.farm_lists set plot_count = v_idx where id = v_list;
  return v_list;
end;
$$;

comment on function public.ingest_farm_list(uuid, text, jsonb, double precision[], numeric) is
  'Validate a GeoJSON FeatureCollection of Point, Polygon and MultiPolygon plots into farm_plots with a status and a reason code per plot. Idempotent on the file hash. SECURITY INVOKER so RLS applies.';

-- ---------------------------------------------------------------- overlaps
-- Hectares and percent of each plot that overlap each region of a layer, computed on geography (metres).
-- A plot that only touches a region along an edge has an intersection of area 0 and is not reported. A point
-- plot is reported as inside (overlap_ha and overlap_pct null) when the point is in the region.
create or replace function public.plot_overlaps(p_list uuid, p_layer text)
returns table (plot_ref text, region text, overlap_ha numeric, overlap_pct numeric)
language sql
stable
security invoker
set search_path = ''
as $$
  select p.plot_ref,
         r.name,
         case when p.point_only then null else round((extensions.st_area(extensions.st_intersection(p.geom, r.geom)::extensions.geography) / 10000.0)::numeric, 4) end,
         case when p.point_only then null
              else round((100.0 * extensions.st_area(extensions.st_intersection(p.geom, r.geom)::extensions.geography) / nullif(extensions.st_area(p.geom::extensions.geography), 0))::numeric, 2) end
  from public.farm_plots p
  join public.reference_regions r
    on r.layer = p_layer and extensions.st_intersects(p.geom, r.geom)
  where p.list_id = p_list
    and p.status in ('accepted', 'repaired')
    and (p.point_only or extensions.st_area(extensions.st_intersection(p.geom, r.geom)::extensions.geography) > 0)
  order by p.feature_index, r.name;
$$;

comment on function public.plot_overlaps(uuid, text) is
  'Overlap of each accepted or repaired plot with a reference layer, in hectares and percent of the plot, on geography. Edge-touching is not overlap. SECURITY INVOKER.';
