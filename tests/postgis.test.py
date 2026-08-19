#!/usr/bin/env python3
"""
PostGIS spatial-matching tests against a REAL Postgres.

Runs the actual migrations (0001 + 0002) on a throwaway database in a local
postgis container, seeds a deliberately awkward geometry, and asserts on the
results. Nothing here is mocked and nothing is asserted from a doc.

THE TEST THAT MATTERS is test_bbox_over_selects_at_the_corner. It places a
property at 95% of both bbox edges, which the bounding box accepts and a true
50 km circle must reject. If ST_DWithin ever agrees with the bbox on that row,
the whole reason this migration exists has evaporated and this test fails.

Every other assertion is a control on that one: units, argument order, index
usage, and tenant isolation. A green suite without the corner case would be
theatre.

Usage:  python tests/postgis.test.py
Needs:  docker, and a running container named `pgis` (postgis/postgis:17-3.5).
        Start with:
          docker run --rm -d --name pgis -e POSTGRES_PASSWORD=pw \
            -p 55432:5432 postgis/postgis:17-3.5
"""

import math
import os
import subprocess
import sys

CONTAINER = "pgis"
DB = "asc_geo_test"
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Storm centre: Dallas. Radius under test.
W_LAT, W_LON = 32.776700, -96.797000
RADIUS_KM = 50.0

# The bbox edges swdi.ts would compute for this point, in degrees.
D_LAT = RADIUS_KM / 111.32
D_LON = RADIUS_KM / (111.32 * max(math.cos(math.radians(W_LAT)), 0.01))

# A property at 95% of BOTH edges: comfortably inside the box, and about
# sqrt(47.5^2 + 47.5^2) = 67 km away, so well outside a 50 km circle.
CORNER_LAT = W_LAT + D_LAT * 0.95
CORNER_LON = W_LON + D_LON * 0.95

# Straightforward controls.
NEAR_LAT, NEAR_LON = W_LAT + (40.0 / 111.32), W_LON      # 40 km due north: in both
FAR_LAT, FAR_LON = W_LAT + (200.0 / 111.32), W_LON       # 200 km north: in neither

ORG_A = "11111111-1111-1111-1111-111111111111"
ORG_B = "22222222-2222-2222-2222-222222222222"
USER_A = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
USER_B = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
LOC_A, LOC_B = "loc_a", "loc_b"
STORM = "99999999-9999-9999-9999-999999999999"

failures = []
checks = [0]


def sh(args, check=True):
    r = subprocess.run(args, capture_output=True, text=True)
    if check and r.returncode != 0:
        print("COMMAND FAILED: %s" % " ".join(args))
        print(r.stdout[-2000:])
        print(r.stderr[-2000:])
        sys.exit(1)
    return r


# psql echoes a command tag for every non-SELECT statement, so a query prefixed with
# `set local ...` returns "SET\nSET\n<value>". These are the tags to discard so an
# assertion compares against the actual result.
_TAGS = ("SET", "BEGIN", "COMMIT", "ROLLBACK", "CREATE", "DROP", "GRANT", "REVOKE",
         "INSERT", "UPDATE", "DELETE", "ALTER", "DO", "ANALYZE")


def _value(out):
    """Last meaningful line of psql output, with command tags stripped."""
    lines = [ln.strip() for ln in (out or "").split("\n") if ln.strip()]
    lines = [ln for ln in lines if not ln.split()[0].upper() in _TAGS]
    return lines[-1] if lines else ""


def psql(sql, db=DB, check=True):
    """Run SQL. .value holds the last real result line, tags stripped."""
    r = sh(["docker", "exec", "-i", CONTAINER, "psql", "-U", "postgres",
            "-d", db, "-X", "-A", "-t", "-F", "|", "-v", "ON_ERROR_STOP=1",
            "-c", sql], check=check)
    r.value = _value(r.stdout)
    r.values = [ln.strip() for ln in (r.stdout or "").split("\n")
                if ln.strip() and ln.strip().split()[0].upper() not in _TAGS]
    return r


def psql_file(local_path, db=DB):
    remote = "/tmp/" + os.path.basename(local_path)
    sh(["docker", "cp", local_path, "%s:%s" % (CONTAINER, remote)])
    return sh(["docker", "exec", "-i", CONTAINER, "psql", "-U", "postgres",
               "-d", db, "-X", "-q", "-v", "ON_ERROR_STOP=1", "-f", remote])


def check(label, got, want):
    checks[0] += 1
    ok = got == want
    print("  %-58s %s" % (label, "PASS" if ok else "FAIL (got %r, want %r)" % (got, want)))
    if not ok:
        failures.append(label)
    return ok


def check_true(label, cond, detail=""):
    checks[0] += 1
    print("  %-58s %s" % (label, "PASS" if cond else "FAIL %s" % detail))
    if not cond:
        failures.append(label)
    return cond


# --------------------------------------------------------------------- setup
def setup():
    r = sh(["docker", "ps", "--format", "{{.Names}}"], check=False)
    if CONTAINER not in r.stdout.split():
        print("Container %r is not running. Start it with:" % CONTAINER)
        print("  docker run --rm -d --name pgis -e POSTGRES_PASSWORD=pw "
              "-p 55432:5432 postgis/postgis:17-3.5")
        sys.exit(2)

    psql("drop database if exists %s;" % DB, db="postgres")
    psql("create database %s;" % DB, db="postgres")

    # The container image pre-installs postgis into public in the DEFAULT db, but a
    # freshly created database has no extensions, so 0002 controls its own schema.
    psql_file(os.path.join(HERE, "supabase_shim.sql"))
    psql_file(os.path.join(ROOT, "supabase", "migrations", "0001_schema.sql"))
    psql_file(os.path.join(ROOT, "supabase", "migrations", "0002_postgis.sql"))

    seed = """
    insert into auth.users (id, email) values
      ('{ua}','a@test'), ('{ub}','b@test');
    insert into orgs (id, name) values ('{oa}','Org A'), ('{ob}','Org B');
    insert into org_members (org_id, user_id, role) values
      ('{oa}','{ua}','admin'), ('{ob}','{ub}','admin');
    insert into ghl_locations (location_id, org_id, company_id, name) values
      ('{la}','{oa}','comp_a','A loc'), ('{lb}','{ob}','comp_b','B loc');

    insert into weather_events (id, source, external_id, event_type, occurred_at,
                               latitude, longitude, magnitude)
      values ('{storm}','noaa','test-hail-1','Hail', now(), {wlat}, {wlon}, 1.75);

    insert into property_events
      (org_id, location_id, kind, occurred_at, address, latitude, longitude)
    values
      ('{oa}','{la}','inspection', now(), 'NEAR 40km',   {nlat}, {nlon}),
      ('{oa}','{la}','inspection', now(), 'CORNER 67km', {clat}, {clon}),
      ('{oa}','{la}','inspection', now(), 'FAR 200km',   {flat}, {flon}),
      ('{ob}','{lb}','inspection', now(), 'ORG B 40km',  {nlat}, {nlon}),
      ('{oa}','{la}','inspection', now(), 'NO COORDS',   null,   null);
    """.format(ua=USER_A, ub=USER_B, oa=ORG_A, ob=ORG_B, la=LOC_A, lb=LOC_B,
               storm=STORM, wlat=W_LAT, wlon=W_LON,
               nlat=NEAR_LAT, nlon=NEAR_LON, clat=CORNER_LAT, clon=CORNER_LON,
               flat=FAR_LAT, flon=FAR_LON)
    psql(seed)


# --------------------------------------------------------------------- tests
def test_migration_shape():
    print("\nmigration shape")
    got = psql("select n.nspname from pg_extension e join pg_namespace n "
               "on n.oid=e.extnamespace where e.extname='postgis';").value
    check("postgis installed into the extensions schema", got, "extensions")

    for tbl in ("property_events", "weather_events"):
        got = psql("select attgenerated from pg_attribute where attrelid="
                   "'%s'::regclass and attname='geog';" % tbl).value
        check("%s.geog is a STORED generated column" % tbl, got, "s")

    got = psql("select count(*) from pg_index i join pg_class c on c.oid=i.indexrelid "
               "where c.relname in ('property_events_geog_idx','weather_events_geog_idx');"
               ).value
    check("both GiST indexes exist", got, "2")


def test_generated_column_is_correct():
    print("\ngenerated column correctness")
    # ST_X must return the LONGITUDE. If the arg order were swapped this is the
    # test that catches it, and a swap produces plausible-looking output, not an error.
    got = psql("select round(extensions.st_x(geog::extensions.geometry)::numeric,4) "
               "from weather_events where id='%s';" % STORM).value
    check("ST_X of the storm returns its longitude (lon-first arg order)",
          got, "%.4f" % W_LON)

    got = psql("select round(extensions.st_y(geog::extensions.geometry)::numeric,4) "
               "from weather_events where id='%s';" % STORM).value
    check("ST_Y returns its latitude", got, "%.4f" % W_LAT)

    # STRICT st_makepoint means a row with a missing coordinate gets NULL geog and
    # is simply never matched, instead of exploding or matching everything.
    got = psql("select geog is null from property_events "
               "where address='NO COORDS';").value
    check("a row with no coordinates yields NULL geog", got, "t")

    # The column is generated: writing to it must be refused outright.
    r = psql("update property_events set geog = null where address='NEAR 40km';",
             check=False)
    check_true("geog is not writable (generated always)",
               r.returncode != 0 and "can only be updated to DEFAULT" in (r.stderr or ""),
               "expected a rejection, got rc=%s %s" % (r.returncode, (r.stderr or "")[:120]))


def test_units_are_metres():
    print("\nunits")
    got = psql("select round((extensions.st_distance(p.geog,w.geog)/1000.0)::numeric,2) "
               "from property_events p, weather_events w "
               "where w.id='%s' and p.address='NEAR 40km';" % STORM).value
    d = float(got)
    # The seed placed this row 40 km north using 111.32 km/degree, the same constant
    # bboxAround() uses. True spheroidal distance says 39.9 km. That 0.25% gap IS the
    # argument for geography over degree maths, so it is asserted rather than rounded away.
    check_true("the '40 km' property measures %.2f km, and the ~0.1 km gap is the "
               "111.32 km/degree approximation error" % d,
               39.8 <= d <= 40.1, "measured %.2f km" % d)

    got = psql("select extensions.st_dwithin(p.geog,w.geog, 50000) "
               "from property_events p, weather_events w "
               "where w.id='%s' and p.address='NEAR 40km';" % STORM).value
    check("ST_DWithin matches at 50000 (metres)", got, "t")

    got = psql("select extensions.st_dwithin(p.geog,w.geog, 50) "
               "from property_events p, weather_events w "
               "where w.id='%s' and p.address='NEAR 40km';" % STORM).value
    check("ST_DWithin does NOT match at 50 (proves metres, not km)", got, "f")


def test_bbox_over_selects_at_the_corner():
    """The point of the whole migration."""
    print("\nTHE CORNER CASE  (bbox over-selects, ST_DWithin does not)")

    got = psql("select round((extensions.st_distance(p.geog,w.geog)/1000.0)::numeric,1) "
               "from property_events p, weather_events w "
               "where w.id='%s' and p.address='CORNER 67km';" % STORM).value
    dist = float(got)
    check_true("the corner property is genuinely beyond 50 km (%.1f km)" % dist,
               dist > RADIUS_KM, "distance was %.1f" % dist)

    bbox_rows = psql("set local test.user_id='%s'; select p.address from property_events p "
                     "join properties_in_bbox_of_weather_event('%s',%s) b "
                     "on b.property_event_id=p.id order by 1;"
                     % (USER_A, STORM, RADIUS_KM)).values
    check_true("the BBOX accepts the corner property (it over-selects)",
               "CORNER 67km" in bbox_rows, "bbox returned %r" % bbox_rows)

    circ_rows = psql("set local test.user_id='%s'; select p.address from property_events p "
                     "join properties_near_weather_event('%s',%s) n "
                     "on n.property_event_id=p.id order by 1;"
                     % (USER_A, STORM, RADIUS_KM)).values
    check_true("ST_DWithin REJECTS the corner property",
               "CORNER 67km" not in circ_rows, "circle returned %r" % circ_rows)
    check_true("ST_DWithin still keeps the genuine 40 km hit",
               "NEAR 40km" in circ_rows, "circle returned %r" % circ_rows)
    check_true("neither method returns the 200 km property",
               "FAR 200km" not in bbox_rows and "FAR 200km" not in circ_rows,
               "bbox=%r circle=%r" % (bbox_rows, circ_rows))
    check_true("so the bbox returns strictly more rows than the circle",
               len(bbox_rows) > len(circ_rows),
               "bbox=%d circle=%d" % (len(bbox_rows), len(circ_rows)))


def test_tenant_isolation_through_the_rpc():
    print("\ntenant isolation through the function (positive control first)")
    a = psql("set local role authenticated; set local test.user_id='%s'; "
             "select count(*) from properties_near_weather_event('%s',%s);"
             % (USER_A, STORM, RADIUS_KM)).value
    check_true("POSITIVE CONTROL: org A sees its own property (%s row/s)" % a,
               a not in ("0", ""), "org A saw %r, so the harness itself is broken" % a)

    b = psql("set local role authenticated; set local test.user_id='%s'; "
             "select count(*) from properties_near_weather_event('%s',%s);"
             % (USER_B, STORM, RADIUS_KM)).value
    check_true("org B does NOT see org A's property", b == "1",
               "org B saw %r rows; it owns exactly 1 in-radius property" % b)

    cross = psql("set local role authenticated; set local test.user_id='%s'; "
                 "select count(*) from properties_near_weather_event('%s',%s) "
                 "where org_id='%s';" % (USER_B, STORM, RADIUS_KM, ORG_A)).value
    check("org B sees ZERO rows belonging to org A", cross, "0")

    anon = psql("set local role anon; select count(*) from "
                "properties_near_weather_event('%s',%s);" % (STORM, RADIUS_KM),
                check=False)
    check_true("anon sees nothing (or is refused outright)",
               anon.returncode != 0 or anon.value in ("0", ""),
               "anon got rc=%s out=%r" % (anon.returncode, anon.value))


def test_index_is_used_and_distance_is_not_indexable():
    print("\nindex behaviour")
    got = psql("select case when prosupport = 0 then 'NONE' else "
               "prosupport::regproc::text end from pg_proc p "
               "join pg_namespace n on n.oid=p.pronamespace "
               "where n.nspname='extensions' and p.proname='st_dwithin' "
               "and pg_get_function_identity_arguments(p.oid) like "
               "'geog1 extensions.geography, geog2 extensions.geography, tolerance%';"
               ).value
    check("ST_DWithin has an index support function", got,
          "extensions.postgis_index_supportfn")

    r = psql("select distinct case when prosupport = 0 then 'NONE' else "
             "prosupport::regproc::text end from pg_proc p "
             "join pg_namespace n on n.oid=p.pronamespace "
             "where n.nspname='extensions' and p.proname='st_distance' "
             "and pg_get_function_identity_arguments(p.oid) like "
             "'geog1 extensions.geography, geog2 extensions.geography%';")
    check_true("EVERY geography ST_Distance overload has NO index support, so it "
               "must never be a WHERE filter (got %r)" % r.values,
               r.values == ["NONE"], "overload supports: %r" % r.values)

    # Force the planner's hand: on a 5-row table a seq scan is genuinely cheaper, so
    # this proves the index is USABLE and that ST_DWithin rewrites to an indexable
    # bbox operator. It does not claim the planner would choose it at this size.
    plan = psql("set local enable_seqscan=off; explain (costs off) "
                "select id from property_events where extensions.st_dwithin(geog, "
                "extensions.st_setsrid(extensions.st_makepoint(%s,%s),4326)::"
                "extensions.geography, 50000);" % (W_LON, W_LAT)).stdout
    check_true("plan uses property_events_geog_idx", "property_events_geog_idx" in plan,
               "plan was: %s" % plan.replace("\n", " ")[:200])
    check_true("ST_DWithin rewrote into an indexable && / _st_expand condition",
               "_st_expand" in plan, "plan was: %s" % plan.replace("\n", " ")[:200])


def main():
    print("PostGIS spatial matching, against a real Postgres")
    print("storm at (%.4f, %.4f), radius %.0f km" % (W_LAT, W_LON, RADIUS_KM))
    print("bbox edges: +-%.4f lat, +-%.4f lon" % (D_LAT, D_LON))
    print("corner property at (%.4f, %.4f)" % (CORNER_LAT, CORNER_LON))
    setup()
    test_migration_shape()
    test_generated_column_is_correct()
    test_units_are_metres()
    test_bbox_over_selects_at_the_corner()
    test_tenant_isolation_through_the_rpc()
    test_index_is_used_and_distance_is_not_indexable()

    print("\n%d checks, %d failed" % (checks[0], len(failures)))
    if failures:
        for f in failures:
            print("  FAILED: %s" % f)
        sys.exit(1)
    print("ALL POSTGIS CHECKS PASS")


if __name__ == "__main__":
    main()
