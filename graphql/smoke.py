#!/usr/bin/env python3
# LIFESPAN: permanent
"""Hasura over the repository's own PostGIS schema, run against a real Hasura and a real PostGIS container.

    python graphql/smoke.py            start the stack, seed it, run the checks, run the mutants, tear everything down
    python graphql/smoke.py --keep     leave the stack running afterwards (http://127.0.0.1:8080, admin secret in docker-compose.yml)

What it proves, in the order it runs:
  1. The three migrations (shim, 0001, 0002) applied, so the tables Hasura tracks are the real ones.
  2. Hasura tracks property_events and weather_events from graphql/metadata.json with no inconsistent object.
  3. A spatial filter (_st_d_within on the generated geography column) answers in metres: the point 40 km away is inside a 50 km radius, the
     corner point about 67 km away is not, and it is inside a 100 km radius (a control, so an empty result cannot pass for a correct one).
  4. The `tenant` role only sees its own organisation's property events, and a request without the organisation header is refused.
  5. Each mutant (the permission filter removed, the geography column left out of the permission, PostGIS's schema taken off the search path) makes at least one check fail.
Standard library only; needs docker. The admin secret in the compose file is a local development value.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
COMPOSE = ["docker", "compose", "-f", os.path.join(HERE, "docker-compose.yml")]
URL = "http://127.0.0.1:8080"
ADMIN = {"x-hasura-admin-secret": "local-dev-only"}

ORG_A = "aaaaaaaa-0000-4000-8000-00000000000a"
ORG_B = "bbbbbbbb-0000-4000-8000-00000000000b"
USER_A = "aaaaaaaa-1111-4000-8000-00000000000a"
USER_B = "bbbbbbbb-1111-4000-8000-00000000000b"

# Dallas, the same storm centre tests/postgis.test.py uses. 111.32 km per degree of latitude, as swdi.ts does.
W_LAT, W_LON = 32.7767, -96.797
NEAR = (W_LAT + 40.0 / 111.32, W_LON)                      # 40 km due north
CORNER = (W_LAT + 47.5 / 111.32, W_LON + 47.5 / (111.32 * 0.8406))   # about 67 km away, inside the 50 km bounding box
FAR = (W_LAT + 200.0 / 111.32, W_LON)                      # 200 km north

SEED = """
insert into auth.users (id, email) values ('{ua}','a@test'), ('{ub}','b@test');
insert into orgs (id, name) values ('{oa}','Org A'), ('{ob}','Org B');
insert into ghl_locations (location_id, org_id, company_id, name) values ('la','{oa}','comp_a','A loc'), ('lb','{ob}','comp_b','B loc');
insert into weather_events (source, external_id, event_type, occurred_at, latitude, longitude, magnitude)
  values ('noaa','smoke-hail-1','Hail', now(), {wlat}, {wlon}, 1.75);
insert into property_events (org_id, location_id, kind, occurred_at, address, latitude, longitude) values
  ('{oa}','la','inspection', now(), 'NEAR 40km',   {nlat}, {nlon}),
  ('{oa}','la','inspection', now(), 'CORNER 67km', {clat}, {clon}),
  ('{oa}','la','inspection', now(), 'FAR 200km',   {flat}, {flon}),
  ('{ob}','lb','inspection', now(), 'ORG B 40km',  {nlat}, {nlon}),
  ('{oa}','la','inspection', now(), 'NO COORDS',   null,   null);
""".format(ua=USER_A, ub=USER_B, oa=ORG_A, ob=ORG_B, wlat=W_LAT, wlon=W_LON, nlat=NEAR[0], nlon=NEAR[1], clat=CORNER[0], clon=CORNER[1], flat=FAR[0], flon=FAR[1])


def run(cmd, stdin=None, check=True):
    res = subprocess.run(cmd, input=stdin, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    if check and res.returncode != 0:
        raise RuntimeError("%s failed: %s" % (" ".join(cmd[:4]), (res.stderr or res.stdout)[-600:]))
    return res


def post(path, body, headers):
    req = urllib.request.Request(URL + path, data=json.dumps(body).encode(), headers=dict({"content-type": "application/json"}, **headers), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as err:
        try:
            return err.code, json.loads(err.read())
        except ValueError:
            return err.code, {}


def wait_for(what, probe, seconds=150):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if probe():
                return
        except (OSError, RuntimeError, ValueError):
            pass
        time.sleep(0.3)
    raise RuntimeError("timed out waiting for " + what)


def hasura_ready():
    with urllib.request.urlopen(URL + "/healthz", timeout=3) as resp:
        return resp.status == 200


def apply_metadata(meta):
    status, out = post("/v1/metadata", meta, ADMIN)
    if status != 200:
        raise RuntimeError("metadata refused: %s %s" % (status, json.dumps(out)[:300]))
    status, out = post("/v1/metadata", {"type": "get_inconsistent_metadata", "args": {}}, ADMIN)
    if status != 200 or out.get("inconsistent_objects"):
        raise RuntimeError("inconsistent metadata: " + json.dumps(out)[:400])


def gql(query, variables=None, role="tenant", org=ORG_A, secret=True):
    headers = dict(ADMIN) if secret else {}
    if role:
        headers["x-hasura-role"] = role
    if org:
        headers["x-hasura-org-id"] = org
    return post("/v1/graphql", {"query": query, "variables": variables or {}}, headers)


WITHIN = """query($from: geography!, $d: Float!) {
  property_events(where: {geog: {_st_d_within: {distance: $d, from: $from}}}, order_by: {address: asc}) { address }
}"""
ALL_OWN = "query { property_events(order_by: {address: asc}) { address } }"
POINT = {"type": "Point", "coordinates": [W_LON, W_LAT]}


def addresses(resp):
    status, body = resp
    if status != 200 or "errors" in body:
        return None
    return [r["address"] for r in body["data"]["property_events"]]


def checks():
    out = []
    out.append(("within 50 km: the 40 km point is in, the 67 km corner and the 200 km point are out", addresses(gql(WITHIN, {"from": POINT, "d": 50000})) == ["NEAR 40km"]))
    out.append(("within 100 km: the corner is reachable, so an empty result above is a real exclusion", addresses(gql(WITHIN, {"from": POINT, "d": 100000})) == ["CORNER 67km", "NEAR 40km"]))
    out.append(("a row with no coordinates never matches a spatial filter, while every row that has coordinates does (5,000 km)", addresses(gql(WITHIN, {"from": POINT, "d": 5_000_000})) == ["CORNER 67km", "FAR 200km", "NEAR 40km"]))
    out.append(("org A sees only its own four rows", addresses(gql(ALL_OWN, org=ORG_A)) == ["CORNER 67km", "FAR 200km", "NEAR 40km", "NO COORDS"]))
    out.append(("org B sees only its own row, and it matches the same radius", addresses(gql(WITHIN, {"from": POINT, "d": 50000}, org=ORG_B)) == ["ORG B 40km"]))
    status, body = gql(ALL_OWN, org=None)
    out.append(("a tenant request without an organisation header is refused", status != 200 or "errors" in body))
    status, body = gql(ALL_OWN, secret=False)
    out.append(("a request with no admin secret and no token is refused", status != 200 or "errors" in body))
    status, body = gql("query { weather_events { event_type magnitude } }")
    out.append(("weather events are shared facts: the tenant reads them", status == 200 and body.get("data", {}).get("weather_events") == [{"event_type": "Hail", "magnitude": 1.75}]))
    return out


def psql(sql):
    run(COMPOSE + ["exec", "-T", "db", "psql", "-U", "postgres", "-d", "relay_geo", "-v", "ON_ERROR_STOP=1", "-q", "-c", sql])


def reconnect_hasura(base):
    """Hasura keeps pooled connections, and a search_path change only reaches NEW ones, so restart it and load the metadata again."""
    run(COMPOSE + ["restart", "hasura"])
    wait_for("hasura after restart", hasura_ready)
    apply_metadata(base)


def mutants(base):
    def with_table(fn):
        meta = json.loads(json.dumps(base))
        for t in meta["args"]["metadata"]["sources"][0]["tables"]:
            fn(t)
        return meta

    def drop_filter(t):
        for p in t.get("select_permissions", []):
            p["permission"]["filter"] = {}

    def drop_geog(t):
        for p in t.get("select_permissions", []):
            p["permission"]["columns"] = [c for c in p["permission"]["columns"] if c != "geog"]

    return [("tenant filter removed", with_table(drop_filter)), ("geography column not exposed", with_table(drop_geog))]


def main(argv):
    keep = "--keep" in argv
    with open(os.path.join(HERE, "metadata.json"), encoding="utf-8") as fh:
        base = json.load(fh)
    failures = []
    try:
        run(COMPOSE + ["down", "-v", "--remove-orphans"], check=False)
        run(COMPOSE + ["up", "-d", "--wait", "--wait-timeout", "240"])
        wait_for("hasura", hasura_ready)
        run(COMPOSE + ["exec", "-T", "db", "psql", "-U", "postgres", "-d", "relay_geo", "-v", "ON_ERROR_STOP=1", "-q"], stdin=SEED)
        apply_metadata(base)
        results = checks()
        for name, ok in results:
            print(("ok   " if ok else "FAIL ") + name)
            if not ok:
                failures.append(name)
        for name, meta in mutants(base):
            apply_metadata(meta)
            broken = [n for n, ok in checks() if not ok]
            print(("ok   " if broken else "FAIL ") + "mutant '%s' makes %d check(s) fail" % (name, len(broken)))
            if not broken:
                failures.append("mutant survived: " + name)
        apply_metadata(base)
        # The search_path mutant: put the database default back, reconnect, and the spatial checks must fail with Hasura's own cast error.
        psql("alter database relay_geo reset search_path")
        reconnect_hasura(base)
        broken = [n for n, ok in checks() if not ok]
        print(("ok   " if broken else "FAIL ") + "mutant 'PostGIS schema not on the search_path' makes %d check(s) fail" % len(broken))
        if not broken:
            failures.append("mutant survived: search_path")
        psql('alter database relay_geo set search_path = "$user", public, extensions')
        reconnect_hasura(base)
        if [n for n, ok in checks() if not ok]:
            failures.append("checks did not recover after the search_path was restored")
    finally:
        if not keep:
            run(COMPOSE + ["down", "-v", "--remove-orphans"], check=False)
    print("SMOKE " + ("FAIL: " + "; ".join(failures) if failures else "PASS"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
