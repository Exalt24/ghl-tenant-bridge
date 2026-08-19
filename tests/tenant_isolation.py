"""GHL multi-tenant isolation test.

The claim being tested: a location-scoped token for tenant B must NOT be able to
read tenant A's data. Includes a POSITIVE CONTROL (A's own token CAN read A's
contact), because without it a broken request would "prove" isolation by failing
for the wrong reason. That is the whole lesson of
[[feedback_check_what_exists_before_declaring_impossible]].
"""
import json
import re
import urllib.request
import urllib.error
from pathlib import Path

SEC = Path(r"C:\Projects\Professional\Operations\.secrets\ghl_sandbox_pit.txt").read_text(encoding="utf-8")
A_TOK = re.search(r"TENANT_A_PIT=(\S+)", SEC).group(1)
B_TOK = re.search(r"TENANT_B_PIT=(\S+)", SEC).group(1)
AGENCY_TOK = SEC.splitlines()[0].strip()

A_LOC = "AnJG67dOYPmbxTOG4cU1"
B_LOC = "1VrYtmTq0g67GfzDixqN"
BASE = "https://services.leadconnectorhq.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def call(method, path, token, body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Version", "2021-07-28")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", UA)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "ERR " + type(e).__name__ + " " + str(e)[:200]


def show(label, status, body, limit=260):
    print("  [%s] %s" % (str(status), label))
    print("        " + body.replace("\n", " ")[:limit])


print("=" * 78)
print("STEP 1  Write a contact into TENANT A using A's own token")
print("=" * 78)
marker = "ISOLATION-CANARY-A"
st, bd = call("POST", "/contacts/", A_TOK, {
    "firstName": "Canary", "lastName": "Alpha",
    "email": "canary.alpha@example.com",
    "locationId": A_LOC,
    "tags": [marker],
})
show("POST /contacts/ as A", st, bd)
contact_id = None
try:
    j = json.loads(bd)
    contact_id = (j.get("contact") or {}).get("id") or j.get("id")
except Exception:
    pass
print("        contact_id = " + str(contact_id))
print()

print("=" * 78)
print("STEP 2  POSITIVE CONTROL: A's token must be able to READ it back")
print("=" * 78)
st, bd = call("GET", "/contacts/?locationId=" + A_LOC, A_TOK)
show("GET /contacts/ as A, scoped to A", st, bd, 400)
control_ok = (st == 200 and marker in bd) or (st == 200 and "canary" in bd.lower())
print("        CONTROL PASSES (A can see its own data): " + str(control_ok))
if contact_id:
    st2, bd2 = call("GET", "/contacts/" + contact_id, A_TOK)
    show("GET /contacts/{id} as A", st2, bd2, 220)
    control_ok = control_ok or st2 == 200
print()

print("=" * 78)
print("STEP 3  THE TEST: B's token tries to read A's data")
print("=" * 78)
st_b1, bd_b1 = call("GET", "/contacts/?locationId=" + A_LOC, B_TOK)
show("GET /contacts/?locationId=A  using B's token", st_b1, bd_b1, 300)
if contact_id:
    st_b2, bd_b2 = call("GET", "/contacts/" + contact_id, B_TOK)
    show("GET /contacts/{A's contact id}  using B's token", st_b2, bd_b2, 300)
else:
    st_b2, bd_b2 = None, "skipped, no contact id"
print()

print("=" * 78)
print("STEP 4  Sanity: B's token reading B's OWN (empty) tenant")
print("=" * 78)
st_b3, bd_b3 = call("GET", "/contacts/?locationId=" + B_LOC, B_TOK)
show("GET /contacts/?locationId=B  using B's token", st_b3, bd_b3, 300)
print()

print("=" * 78)
print("STEP 5  Agency token vs sub-account data (expected: user type mismatch)")
print("=" * 78)
st_ag, bd_ag = call("GET", "/contacts/?locationId=" + A_LOC, AGENCY_TOK)
show("GET /contacts/?locationId=A  using AGENCY token", st_ag, bd_ag, 220)
print()

print("=" * 78)
print("VERDICT")
print("=" * 78)
leaked = False
for st_, bd_ in ((st_b1, bd_b1), (st_b2, bd_b2)):
    if st_ == 200 and ("canary" in (bd_ or "").lower() or marker in (bd_ or "")):
        leaked = True
if not control_ok:
    print("  INCONCLUSIVE: the positive control did not pass, so a failure for B")
    print("  proves nothing. Fix A's write/read first.")
elif leaked:
    print("  LEAK: tenant B's token READ tenant A's contact. That is a real finding.")
else:
    print("  ISOLATED: A can read its own data, B cannot read A's. Isolation holds,")
    print("  and the control proves the negative result is meaningful.")
