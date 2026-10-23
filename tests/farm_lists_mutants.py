#!/usr/bin/env python3
# LIFESPAN: permanent
"""
Mutation check for 0005_farm_lists.sql: each mutant removes ONE guard in a temp copy of the migration and
tests/farm_lists.test.py (pointed at the copy with FARM_LISTS_MIGRATION, in its own database) must go red.
A mutant whose anchor is not found exactly once is STALE and fails the run, so a reworded line cannot turn
this into a quiet pass.

Usage:  python tests/farm_lists_mutants.py [name filter]      (needs the pgis container, see farm_lists.test.py)
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MIGRATION = os.path.join(ROOT, "supabase", "migrations", "0005_farm_lists.sql")

MUTANTS = [
    ("an unclosed ring gets a generic reason", "when p_postgis_reason ilike '%closed linestring%'        then 'unclosed_ring'", "when p_postgis_reason ilike '%closed linestring%'        then 'invalid_geometry'"),
    ("the latitude range is not checked", "if extensions.st_ymin(v_geom) < -90 or extensions.st_ymax(v_geom) > 90 then", "if false then"),
    ("the longitude range is not checked", "elsif extensions.st_xmin(v_geom) < -180 or extensions.st_xmax(v_geom) > 180 then", "elsif false then"),
    ("a swapped plot is called outside the box", "case when extensions.st_intersects(extensions.st_flipcoordinates(v_geom), v_box) then 'coordinates_swapped' else 'outside_country_box' end", "'outside_country_box'"),
    ("the country box is never applied", "elsif v_box is not null and not extensions.st_intersects(v_geom, v_box) then", "elsif false then"),
    ("any repair is accepted whatever it does to the area", "abs(v_fix_area - v_orig_area) / v_orig_area < 0.01", "true"),
    ("a collapsed polygon is accepted", "if v_area_ha < 0.0001 then", "if false then"),
    ("a duplicate plot_ref is accepted", "if v_ref = any (v_refs) then", "if false then"),
    ("the same file is ingested again as a new list", "if v_list is not null then\n    return v_list;", "if false then\n    return v_list;"),
    ("the area ceiling flag is gone", "elsif v_area_ha > p_max_ha then", "elsif false then"),
    ("a point is no longer flagged point_only", "v_point := true; v_reason := 'point_only';", "v_point := false;"),
    ("any member of any org reads every plot", "create policy farm_plots_select on public.farm_plots for select using (public.is_org_member(org_id));", "create policy farm_plots_select on public.farm_plots for select using (true);"),
    ("any member can ingest into any org", "create policy farm_lists_insert on public.farm_lists for insert with check (public.is_org_member(org_id));", "create policy farm_lists_insert on public.farm_lists for insert with check (true);"),
    ("a plot can be attached to another org's list", "foreign key (list_id, org_id) references public.farm_lists (id, org_id) on delete cascade,", "foreign key (list_id) references public.farm_lists (id) on delete cascade,"),
    ("touching along an edge counts as overlap", "and (p.point_only or extensions.st_area(extensions.st_intersection(p.geom, r.geom)::extensions.geography) > 0)", "and true"),
    ("overlap hectares are computed in degrees", "round((extensions.st_area(extensions.st_intersection(p.geom, r.geom)::extensions.geography) / 10000.0)::numeric, 4)", "round((extensions.st_area(extensions.st_intersection(p.geom, r.geom)) / 10000.0)::numeric, 4)"),
    ("overlaps ignore the layer", "on r.layer = p_layer and extensions.st_intersects(p.geom, r.geom)", "on extensions.st_intersects(p.geom, r.geom)"),
]


def main():
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    base = open(MIGRATION, encoding="utf-8", newline="").read()
    bad = 0
    ran = 0
    for i, (name, old, new) in enumerate(MUTANTS):
        if only and not any(o.lower() in name.lower() for o in only):
            continue
        if base.count(old) != 1:
            print("STALE    %s (anchor found %d times)" % (name, base.count(old)))
            bad += 1
            continue
        ran += 1
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "0005_farm_lists.sql")
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(base.replace(old, new))
            env = dict(os.environ, FARM_LISTS_MIGRATION=path, FARM_LISTS_DB="farm_lists_mut_%d" % i)
            res = subprocess.run([sys.executable, "-I", os.path.join(HERE, "farm_lists.test.py")], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=500)
            survived = res.returncode == 0
            print("%s %s" % ("SURVIVED" if survived else "caught  ", name))
            bad += survived
    print("FARM LIST MUTANTS: %s (%d run, %d problem(s))" % ("ALL CAUGHT" if not bad else "PROBLEMS", ran, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
