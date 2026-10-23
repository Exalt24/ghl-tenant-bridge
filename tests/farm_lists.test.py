#!/usr/bin/env python3
"""
Farm-list intake tests against a REAL PostGIS.

Applies the real migrations (shim, 0001, 0002 and 0005_farm_lists.sql) to a throwaway database in the
local postgis container and asserts on what ingest_farm_list and plot_overlaps do with deliberately
broken lists. Nothing is mocked and no expected value is copied from the code under test: the hectare
figures are computed in Python from the spherical area formula, and every defect shape is generated
and crossed with every geometry type.

THE TESTS THAT MATTER
  * test_defect_table: each defect (latitude out of range, longitude out of range, unclosed ring,
    bow-tie, swapped coordinates, outside the country box) crossed with each geometry type, asserting the
    exact status and reason code of every cell. A cell with no defect is the positive control.
  * test_overlaps: a plot that half overlaps a region reports about 50 percent, and a plot that only
    touches a region along an edge reports nothing. Without the second, "touching" would read as overlap.
  * test_tenant_isolation: tenant B can neither read nor write tenant A's lists, with a positive control
    that A can.

Usage:  python tests/farm_lists.test.py
Needs:  docker and a running container named `pgis` (postgis/postgis:17-3.5):
          docker run --rm -d --name pgis -e POSTGRES_PASSWORD=pw postgis/postgis:17-3.5
Set FARM_LISTS_MIGRATION to run against a mutated copy of 0005_farm_lists.sql (see farm_lists_mutants.py).
"""
import copy
import itertools
import json
import math
import os
import subprocess
import sys

CONTAINER = os.environ.get("FARM_LISTS_CONTAINER", "pgis")
DB = os.environ.get("FARM_LISTS_DB", "farm_lists_test")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
MIGRATION = os.environ.get("FARM_LISTS_MIGRATION") or os.path.join(ROOT, "supabase", "migrations", "0005_farm_lists.sql")

ORG_A = "11111111-1111-1111-1111-111111111111"
ORG_B = "22222222-2222-2222-2222-222222222222"
USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"

failures = []
checks = [0]


def sh(args, check=True, stdin=None):
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace", input=stdin, timeout=300)
    if check and r.returncode != 0:
        print("COMMAND FAILED: %s" % " ".join(args))
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
        sys.exit(1)
    return r


def psql(sql, db=DB, check=True):
    """Run SQL as the superuser. Returns the CompletedProcess with .out (the stdout lines, command tags dropped) and .err."""
    r = sh(["docker", "exec", "-i", CONTAINER, "psql", "-U", "postgres", "-d", db, "-X", "-A", "-t", "-F", "|", "-v", "ON_ERROR_STOP=1"], check=check, stdin=sql)
    tags = ("SET", "BEGIN", "COMMIT", "ROLLBACK", "CREATE", "DROP", "GRANT", "INSERT", "UPDATE", "DELETE", "ALTER", "DO")
    r.out = [ln.strip() for ln in (r.stdout or "").split("\n") if ln.strip() and ln.strip().split()[0].upper() not in tags]
    r.err = r.stderr or ""
    return r


def as_user(user, sql, check=True):
    """Run SQL as the `authenticated` role with auth.uid() = user, inside one rolled-back transaction."""
    return psql("begin; set local role authenticated; set local test.user_id = '%s'; %s; rollback;" % (user, sql), check=check)


def psql_file(local_path, db=DB):
    remote = "/tmp/" + os.path.basename(local_path)
    sh(["docker", "cp", local_path, "%s:%s" % (CONTAINER, remote)])
    return sh(["docker", "exec", "-i", CONTAINER, "psql", "-U", "postgres", "-d", db, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", remote])


def check(label, got, want):
    checks[0] += 1
    ok = got == want
    print("  %-72s %s" % (label[:72], "PASS" if ok else "FAIL (got %r, want %r)" % (got, want)))
    if not ok:
        failures.append(label)
    return ok


def check_true(label, cond, detail=""):
    checks[0] += 1
    print("  %-72s %s" % (label[:72], "PASS" if cond else "FAIL %s" % detail))
    if not cond:
        failures.append(label)
    return cond


def setup():
    r = sh(["docker", "ps", "--format", "{{.Names}}"], check=False)
    if CONTAINER not in r.stdout.split():
        print("Container %r is not running. Start it with:" % CONTAINER)
        print("  docker run --rm -d --name pgis -e POSTGRES_PASSWORD=pw postgis/postgis:17-3.5")
        sys.exit(2)
    psql("drop database if exists %s;" % DB, db="postgres")
    psql("create database %s;" % DB, db="postgres")
    psql_file(os.path.join(HERE, "supabase_shim.sql"))
    psql_file(os.path.join(ROOT, "supabase", "migrations", "0001_schema.sql"))
    psql_file(os.path.join(ROOT, "supabase", "migrations", "0002_postgis.sql"))
    psql_file(MIGRATION)
    psql("""
      insert into auth.users (id, email) values ('%s', 'a@example.test'), ('%s', 'b@example.test');
      insert into orgs (id, name) values ('%s', 'Org A'), ('%s', 'Org B');
      insert into org_members (org_id, user_id, role) values ('%s', '%s', 'owner'), ('%s', '%s', 'owner');
      -- two reference regions that share an edge: R1 is lon 0..0.02, R2 is lon 0.02..0.04, both lat 0..0.02
      insert into reference_regions (layer, name, geom) values
        ('alerts', 'R1', extensions.st_multi(extensions.st_geomfromtext('POLYGON((0 0,0 0.02,0.02 0.02,0.02 0,0 0))', 4326))),
        ('alerts', 'R2', extensions.st_multi(extensions.st_geomfromtext('POLYGON((0.02 0,0.02 0.02,0.04 0.02,0.04 0,0.02 0))', 4326))),
        ('other',  'R3', extensions.st_multi(extensions.st_geomfromtext('POLYGON((0 0,0 0.02,0.02 0.02,0.02 0,0 0))', 4326)));
    """ % (USER_A, USER_B, ORG_A, ORG_B, ORG_A, USER_A, ORG_B, USER_B))


# ------------------------------------------------------------------ builders
def sq(lon, lat, size=0.0089831):
    """A closed square ring starting at (lon, lat); 0.0089831 degrees is about 1 km at the equator."""
    return [[lon, lat], [lon, lat + size], [lon + size, lat + size], [lon + size, lat], [lon, lat]]


def shape(kind, lon=0.0, lat=0.0, size=0.0089831):
    if kind == "Point":
        return {"type": "Point", "coordinates": [lon + size / 2, lat + size / 2]}
    if kind == "Polygon":
        return {"type": "Polygon", "coordinates": [sq(lon, lat, size)]}
    if kind == "MultiPolygon":
        return {"type": "MultiPolygon", "coordinates": [[sq(lon, lat, size)], [sq(lon + 0.5, lat, size)]]}
    if kind == "HolePolygon":
        hole = sq(lon + size * 0.25, lat + size * 0.25, size * 0.5)[::-1]
        return {"type": "Polygon", "coordinates": [sq(lon, lat, size), hole]}
    raise ValueError(kind)


def feature(ref, geom):
    return {"type": "Feature", "properties": {"plot_ref": ref}, "geometry": geom}


def collection(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def ingest(fc, org=ORG_A, name="t", box=None, max_ha=None, user=None):
    args = "'%s', '%s', $j$%s$j$::jsonb" % (org, name, json.dumps(fc))
    if box is not None:
        args += ", array[%s]::double precision[]" % ",".join(repr(x) for x in box)
    if max_ha is not None:
        args += ", p_max_ha => %s" % max_ha
    sql = "select public.ingest_farm_list(%s);" % args
    r = as_user(user, sql, check=False) if user else psql(sql, check=False)
    return r


def plots(list_id):
    r = psql("select plot_ref, status, coalesce(reason,''), point_only, coalesce(area_ha::text,'') from public.farm_plots where list_id = '%s' order by feature_index;" % list_id)
    return [tuple(x.split("|")) for x in r.out]


def new_list(fc, **kw):
    r = ingest(fc, **kw)
    ids = [x for x in r.out if len(x) == 36]
    return ids[0] if ids else None


# ------------------------------------------------------------------ the generated defect table
def apply_defect(kind, defect):
    """The geometry for one cell of the table plus the expected (status, reason) from the rules written down here, not read from the migration."""
    g = shape(kind)
    box = None
    if defect == "clean":
        return g, box, ("accepted", "point_only" if kind == "Point" else "")
    if defect == "lat_out":
        g = shape(kind, lat=95.0)
        return g, box, ("rejected", "latitude_out_of_range")
    if defect == "lon_out":
        g = shape(kind, lon=190.0)
        return g, box, ("rejected", "longitude_out_of_range")
    if kind == "Point":
        if defect in ("unclosed", "bowtie"):
            return None, box, None            # a point has no ring
    if defect == "unclosed":
        g = copy.deepcopy(g)
        ring = g["coordinates"][0] if kind != "MultiPolygon" else g["coordinates"][0][0]
        ring.pop()
        return g, box, ("rejected", "unclosed_ring")
    if defect == "bowtie":
        bow = [[0, 0], [0.01, 0.01], [0.01, 0], [0, 0.01], [0, 0]]
        if kind == "Polygon":
            g = {"type": "Polygon", "coordinates": [bow]}
        elif kind == "MultiPolygon":
            g = {"type": "MultiPolygon", "coordinates": [[bow], [sq(0.5, 0)]]}
        else:
            g = {"type": "Polygon", "coordinates": [bow, sq(0.0025, 0.0025, 0.002)[::-1]]}
        return g, box, ("rejected", "self_intersection")
    # country box 10 km around the origin; a swapped plot sits at lon 3 lat 5 degrees, which swapped is lon 5 lat 3 and both inside a 0..10 box
    if defect == "swapped":
        g = shape(kind, lon=3.0, lat=5.0)
        return g, [4.5, 2.5, 5.5, 3.5], ("rejected", "coordinates_swapped")
    if defect == "outside":
        g = shape(kind, lon=30.0, lat=30.0)
        return g, [4.5, 2.5, 5.5, 3.5], ("rejected", "outside_country_box")
    raise ValueError(defect)


def test_defect_table():
    print("test_defect_table (CLASS-TABLE: defect x geometry type)")
    for kind, defect in itertools.product(("Point", "Polygon", "MultiPolygon", "HolePolygon"), ("clean", "lat_out", "lon_out", "unclosed", "bowtie", "swapped", "outside")):
        g, box, want = apply_defect(kind, defect)
        if g is None:
            continue
        # a unique file per cell so the content hash never collides across cells
        fc = collection(feature("%s-%s" % (kind, defect), g))
        lid = new_list(fc, name="%s %s" % (kind, defect), box=box)
        rows = plots(lid) if lid else []
        got = (rows[0][1], rows[0][2]) if rows else ("(not ingested)", "")
        check("%-12s %-9s -> %s %s" % (kind, defect, want[0], want[1] or "(no reason)"), got, want)


# ------------------------------------------------------------------ single-case tests
def spherical_ha(lon0, lat0, lon1, lat1):
    """Area of a lon/lat rectangle in hectares on a sphere of mean radius, computed here and not by PostGIS."""
    r = 6371008.8
    return abs(math.radians(lon1 - lon0)) * abs(math.sin(math.radians(lat1)) - math.sin(math.radians(lat0))) * r * r / 10000.0


def test_areas_and_repairs():
    print("test_areas_and_repairs")
    lid = new_list(collection(feature("sq", shape("Polygon")), feature("holey", shape("HolePolygon")), feature("pt", shape("Point", lon=1)), feature("pt", shape("Point", lon=2)),
                              feature("line", {"type": "LineString", "coordinates": [[0, 0], [1, 1]]})), name="areas")
    rows = {}
    for r in plots(lid):
        rows.setdefault(r[0], r)             # the first use of a plot_ref; a later duplicate is checked separately below
    want = spherical_ha(0, 0, 0.0089831, 0.0089831)
    check_true("a clean 1 km square is accepted with an area within 1 percent of the independent figure", rows["sq"][1] == "accepted" and abs(float(rows["sq"][4]) - want) / want < 0.01, str(rows["sq"]))
    check_true("a polygon with a hole has the hole's quarter taken out of its area", abs(float(rows["holey"][4]) / float(rows["sq"][4]) - 0.75) < 0.01, str(rows["holey"]))
    check("a point plot is accepted and flagged point_only, with no area", (rows["pt"][1], rows["pt"][2], rows["pt"][3], rows["pt"][4]), ("accepted", "point_only", "t", ""))
    all_rows = plots(lid)
    check("the second use of a plot_ref in one list is rejected as a duplicate", [r[2] for r in all_rows if r[0] == "pt"], ["point_only", "duplicate_plot_ref"])
    check("a LineString is rejected as unsupported_geometry", rows["line"][1:3], ("rejected", "unsupported_geometry"))
    spike = {"type": "Polygon", "coordinates": [[[0, 0], [0, 0.0089831], [0.0089831, 0.0089831], [0.0089831, 0], [0.0044, 0], [0.0044, -0.0009], [0.0044, 0], [0, 0]]]}
    lid2 = new_list(collection(feature("spike", spike)), name="spike")
    r2 = plots(lid2)[0]
    check_true("a spike that repairs to the same area is stored as repaired, not rejected", r2[1] == "repaired" and abs(float(r2[4]) - want) / want < 0.01, str(r2))
    unequal = {"type": "Polygon", "coordinates": [[[0, 0], [0.02, 0.02], [0.02, 0], [0, 0.01], [0, 0]]]}   # a bow-tie with unequal lobes: its own area is non-zero, and the repaired area is far larger
    lid_u = new_list(collection(feature("unequal", unequal)), name="unequal bowtie")
    check("an unequal bow-tie is rejected, because repairing it would silently change its area", plots(lid_u)[0][1:3], ("rejected", "self_intersection"))
    big = {"type": "Polygon", "coordinates": [sq(10, 10, 0.2)]}
    lid3 = new_list(collection(feature("big", big)), name="big", max_ha=1000)
    r3 = plots(lid3)[0]
    check("a polygon over the area ceiling is accepted and flagged, not rejected", (r3[1], r3[2]), ("accepted", "over_area_ceiling"))
    tiny = {"type": "Polygon", "coordinates": [[[0, 0], [0, 0.0000001], [0.0000001, 0.0000001], [0.0000001, 0], [0, 0]]]}
    lid4 = new_list(collection(feature("tiny", tiny)), name="tiny")
    check("a collapsed polygon of about zero area is rejected as zero_area", plots(lid4)[0][1:3], ("rejected", "zero_area"))
    check_true("a rejected plot keeps its original GeoJSON and has no geometry", psql("select count(*) from public.farm_plots where list_id = '%s' and status = 'rejected' and geom is null and original is not null;" % lid4).out == ["1"])


def test_whole_file_refusals():
    print("test_whole_file_refusals")
    r = psql("select public.ingest_farm_list('%s', 'x', '{\"type\":\"Feature\"}'::jsonb);" % ORG_A, check=False)
    check_true("a file that is not a FeatureCollection is refused as a whole", r.returncode != 0 and "not_a_feature_collection" in r.err)
    r = psql("select public.ingest_farm_list('%s', 'x', '{\"type\":\"FeatureCollection\",\"features\":[]}'::jsonb);" % ORG_A, check=False)
    check_true("a FeatureCollection with zero features is refused, never read as an empty success", r.returncode != 0 and "empty_feature_collection" in r.err)
    n = psql("select count(*) from public.farm_lists where name = 'x';").out
    check("a refused file leaves no list behind", n, ["0"])


def test_idempotent():
    print("test_idempotent")
    fc = collection(feature("only", shape("Polygon", lon=7)))
    a = new_list(fc, name="first")
    before = psql("select (select count(*) from public.farm_lists), (select count(*) from public.farm_plots);").out
    b = new_list(fc, name="second try")
    after = psql("select (select count(*) from public.farm_lists), (select count(*) from public.farm_plots);").out
    check("the same file ingested twice returns the same list id", a, b)
    check("and adds no list and no plot rows", before, after)
    c = new_list(collection(feature("only", shape("Polygon", lon=8))), name="other file")
    check_true("a different file gets a different list", c and c != a)


def test_overlaps():
    print("test_overlaps")
    half = {"type": "Polygon", "coordinates": [sq(0.01, 0.0, 0.02)[0:0] or [[0.01, 0], [0.01, 0.02], [0.03, 0.02], [0.03, 0], [0.01, 0]]]}   # lon 0.01..0.03: half of it is in R1, half in R2
    edge = {"type": "Polygon", "coordinates": [[[0.02, 0.005], [0.02, 0.015], [0.03, 0.015], [0.03, 0.005], [0.02, 0.005]]]}              # lon 0.02..0.03: touches R1 only along an edge, inside R2
    inside_pt = {"type": "Point", "coordinates": [0.005, 0.005]}
    outside_pt = {"type": "Point", "coordinates": [1.0, 1.0]}
    lid = new_list(collection(feature("half", half), feature("edge", edge), feature("pin", inside_pt), feature("far", outside_pt), feature("bad", shape("Polygon", lat=95.0))), name="overlaps")
    rows = [tuple(x.split("|")) for x in psql("select plot_ref, region, coalesce(overlap_ha::text,''), coalesce(overlap_pct::text,'') from public.plot_overlaps('%s', 'alerts') order by 1, 2;" % lid).out]
    by = {(r[0], r[1]): r for r in rows}
    check_true("a plot that is half in R1 reports about 50 percent of itself in R1", ("half", "R1") in by and abs(float(by[("half", "R1")][3]) - 50.0) < 1.0, str(rows))
    check_true("and the other half in R2", ("half", "R2") in by and abs(float(by[("half", "R2")][3]) - 50.0) < 1.0)
    check("a plot that only touches R1 along an edge is NOT reported for R1", ("edge", "R1") in by, False)
    check_true("but it is reported for R2, where it lies fully inside", ("edge", "R2") in by and abs(float(by[("edge", "R2")][3]) - 100.0) < 0.5)
    check_true("a point inside a region is reported as inside, with no hectares or percent", ("pin", "R1") in by and by[("pin", "R1")][2:] == ("", ""))
    check("a point outside every region is not reported", [r for r in rows if r[0] == "far"], [])
    check("a rejected plot is never reported", [r for r in rows if r[0] == "bad"], [])
    other = psql("select count(*) from public.plot_overlaps('%s', 'other');" % lid).out
    check("another layer is a separate answer (only the pin and half fall in R3)", other, ["2"])
    ha = float(by[("half", "R1")][2])
    want = spherical_ha(0.01, 0, 0.02, 0.02)
    check_true("the overlap hectares match an independent spherical figure within 1 percent", abs(ha - want) / want < 0.01, "%s vs %s" % (ha, want))


def test_tenant_isolation():
    print("test_tenant_isolation")
    lid = new_list(collection(feature("a-plot", shape("Polygon", lon=20))), name="a list", org=ORG_A)
    mine = as_user(USER_A, "select count(*) from public.farm_plots where list_id = '%s'" % lid).out
    check("positive control: a member of org A reads the plots of A's list", mine, ["1"])
    theirs = as_user(USER_B, "select count(*) from public.farm_plots where list_id = '%s'" % lid).out
    check("tenant B reads none of A's plots", theirs, ["0"])
    check("tenant B reads none of A's lists", as_user(USER_B, "select count(*) from public.farm_lists where id = '%s'" % lid).out, ["0"])
    check("tenant B gets no overlap rows for A's list", as_user(USER_B, "select count(*) from public.plot_overlaps('%s', 'alerts')" % lid).out, ["0"])
    r = ingest(collection(feature("sneaky", shape("Polygon", lon=21))), org=ORG_A, name="by B", user=USER_B)
    check_true("tenant B cannot ingest a list into org A", r.returncode != 0 and ("row-level security" in r.err or "violates" in r.err), r.err[-200:])
    direct = as_user(USER_B, "insert into public.farm_lists (org_id, name, content_sha256) values ('%s', 'direct by B', 'sha-direct-b')" % ORG_A, check=False)
    check_true("tenant B cannot insert a list row into org A directly either", direct.returncode != 0 and "row-level security" in direct.err, direct.err[-200:])
    mine_direct = as_user(USER_A, "insert into public.farm_lists (org_id, name, content_sha256) values ('%s', 'direct by A', 'sha-direct-a')" % ORG_A, check=False)
    check_true("positive control: A can insert a list row into org A directly", mine_direct.returncode == 0, mine_direct.err[-200:])
    check_true("positive control: A can ingest into org A as a signed-in member", ingest(collection(feature("fine", shape("Polygon", lon=22))), org=ORG_A, name="by A", user=USER_A).returncode == 0)
    check_true("and no sneaky list exists afterwards", psql("select count(*) from public.farm_lists where name = 'by B'").out == ["0"])
    cross = psql("insert into public.farm_plots (list_id, org_id, feature_index, plot_ref, status, original) values ('%s', '%s', 99, 'cross', 'rejected', '{}'::jsonb);" % (lid, ORG_B), check=False)
    check_true("a plot cannot be attached to another org's list, even by the superuser (composite key)", cross.returncode != 0 and "foreign key" in cross.err, cross.err[-200:])


def main():
    setup()
    for t in (test_defect_table, test_areas_and_repairs, test_whole_file_refusals, test_idempotent, test_overlaps, test_tenant_isolation):
        t()
    print("\nRESULT: %s (%d checks)" % ("ALL PASS" if not failures else "%d FAILED: %s" % (len(failures), "; ".join(failures)[:800]), checks[0]))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
