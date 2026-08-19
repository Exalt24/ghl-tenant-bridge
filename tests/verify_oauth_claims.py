"""Verify every load-bearing claim about the OAuth result, instead of trusting a
read-through of console output.

Specifically: DECODE both JWTs rather than eyeballing base64, re-read the raw token
responses from disk, and RE-RUN the isolation check to confirm it reproduces rather
than having been a one-off.
"""
import base64
import json
import urllib.parse
import urllib.request
import urllib.error
from pathlib import Path

HERE = Path(__file__).parent
BASE = "https://services.leadconnectorhq.com"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
TENANT_A = "AnJG67dOYPmbxTOG4cU1"
TENANT_B = "1VrYtmTq0g67GfzDixqN"


def jwt_payload(tok):
    parts = tok.split(".")
    if len(parts) < 2:
        return {"_error": "not a JWT, only %d segments" % len(parts)}
    p = parts[1]
    p += "=" * (-len(p) % 4)
    try:
        return json.loads(base64.urlsafe_b64decode(p).decode("utf-8", "replace"))
    except Exception as e:
        return {"_error": str(e)}


def call(path, token, method="GET", form=None):
    data = urllib.parse.urlencode(form).encode() if form else None
    req = urllib.request.Request(BASE + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + token)
    req.add_header("Version", "2021-07-28")
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", UA)
    if data:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return None, "ERR " + type(e).__name__


fails = []
checks = 0


def check(name, cond, detail=""):
    global checks
    checks += 1
    if cond:
        print("  PASS  " + name)
    else:
        print("  FAIL  " + name + ("  :: " + detail if detail else ""))
        fails.append(name)


company = json.loads((HERE / "token_response.json").read_text(encoding="utf-8"))
print("=" * 74)
print("CLAIM 1: the OAuth install returned a COMPANY token, not Location")
print("=" * 74)
print("  raw userType field   = " + repr(company.get("userType")))
print("  locationId present?  = " + repr("locationId" in company))
check("userType is Company", company.get("userType") == "Company", str(company.get("userType")))
check("no locationId in the response", "locationId" not in company or not company.get("locationId"))
cp = jwt_payload(company["access_token"])
print("  DECODED access_token payload:")
for k in ("authClass", "authClassId", "primaryAuthClassId", "source", "sourceId", "channel"):
    if k in cp:
        print("    %-19s = %s" % (k, cp[k]))
check("JWT authClass is Company", cp.get("authClass") == "Company", str(cp.get("authClass")))
check("JWT authClassId is the companyId", cp.get("authClassId") == company.get("companyId"),
      str(cp.get("authClassId")))
check("JWT channel is OAUTH", cp.get("channel") == "OAUTH", str(cp.get("channel")))
print("  app-level scopes on the token: " + repr(company.get("scope")))

lt_path = HERE / "location_token.json"
if not lt_path.exists():
    print("\nlocation_token.json missing, cannot verify claims 2-4")
else:
    loc = json.loads(lt_path.read_text(encoding="utf-8"))
    print()
    print("=" * 74)
    print("CLAIM 2: /oauth/locationToken minted a LOCATION token for tenant A")
    print("=" * 74)
    print("  raw userType   = " + repr(loc.get("userType")))
    print("  raw locationId = " + repr(loc.get("locationId")))
    check("userType is Location", loc.get("userType") == "Location", str(loc.get("userType")))
    check("locationId is tenant A", loc.get("locationId") == TENANT_A, str(loc.get("locationId")))
    lp = jwt_payload(loc["access_token"])
    print("  DECODED payload:")
    for k in ("authClass", "authClassId", "primaryAuthClassId", "source", "sourceId", "channel"):
        if k in lp:
            print("    %-19s = %s" % (k, lp[k]))
    check("JWT authClass is Location", lp.get("authClass") == "Location", str(lp.get("authClass")))
    check("JWT authClassId is tenant A", lp.get("authClassId") == TENANT_A, str(lp.get("authClassId")))

    print()
    print("=" * 74)
    print("CLAIM 3: the exchange ADDED oauth scopes beyond the app's own")
    print("=" * 74)
    app_scopes = set((company.get("scope") or "").split())
    loc_scopes = set((loc.get("scope") or "").split())
    print("  company token scopes  = " + " ".join(sorted(app_scopes)))
    print("  location token scopes = " + " ".join(sorted(loc_scopes)))
    print("  added by the exchange = " + " ".join(sorted(loc_scopes - app_scopes)))
    check("location token gained scopes the company token lacked",
          len(loc_scopes - app_scopes) > 0, str(loc_scopes - app_scopes))
    check("the added scopes are the oauth ones",
          (loc_scopes - app_scopes) == {"oauth.write", "oauth.readonly"},
          str(loc_scopes - app_scopes))

    print()
    print("=" * 74)
    print("CLAIM 4: isolation holds on the OAuth-minted token (RE-RUN, must reproduce)")
    print("=" * 74)
    lt = loc["access_token"]
    sA, bA = call("/contacts/?locationId=" + TENANT_A, lt)
    sB, bB = call("/contacts/?locationId=" + TENANT_B, lt)
    print("  own tenant A     -> HTTP %s  %s" % (sA, bA[:90].replace("\n", " ")))
    print("  foreign tenant B -> HTTP %s  %s" % (sB, bB[:90].replace("\n", " ")))
    check("reads its own tenant (200)", sA == 200, str(sA))
    check("POSITIVE CONTROL: the read returned the canary contact",
          "Msp3QvFpSDhrxB2A3mL2" in bA, "canary not found, so the 200 proves little")
    check("refused on the foreign tenant (403)", sB == 403, str(sB))
    check("refusal message is the tenant-boundary one",
          "does not have access to this location" in bB, bB[:80])

    print()
    print("=" * 74)
    print("CLAIM 5: the COMPANY token alone cannot read contacts")
    print("=" * 74)
    sC, bC = call("/contacts/?locationId=" + TENANT_A, company["access_token"])
    print("  company token on contacts -> HTTP %s" % sC)
    print("    %s" % bC[:200].replace("\n", " "))
    check("company token refused on contacts", sC in (401, 403), str(sC))
    check("error mentions authClass, a DIFFERENT message from the scope error",
          "authClass" in bC, bC[:90])

print()
print("=" * 74)
print("%d checks, %d failed" % (checks, len(fails)))
if fails:
    for f in fails:
        print("  FAILED: " + f)
else:
    print("ALL CLAIMS VERIFIED")
