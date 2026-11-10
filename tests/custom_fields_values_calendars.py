"""Live check in the HighLevel sandbox: a custom field, a custom value and a calendar with a
booking, all through the API (docs/custom-fields-values-calendars.md).

Opt in on purpose, like tests/tenant_isolation.py: it makes live authenticated calls, so it never
finds a token file by itself.

  GHL_SANDBOX_PIT_FILE   path to the sandbox token file (TENANT_B_PIT=... on a line of its own)
  GHL_CFVC_TRANSCRIPT    optional path; every call is written there as JSON, without headers
  GHL_CFVC_LOCATION      optional location id; default is the sandbox sub-account ASC Test Tenant B

Safe to re-run: every object is looked up by name first. The only thing it removes is the
appointment it books itself.
"""
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ghl_setup as g  # noqa: E402

TENANT_B = "1VrYtmTq0g67GfzDixqN"


def main():
    src = os.environ.get("GHL_SANDBOX_PIT_FILE", "")
    if not src or not Path(src).is_file():
        print("SKIP: set GHL_SANDBOX_PIT_FILE to a sandbox token file to run this suite (it makes live calls to a HighLevel sandbox)")
        return 2
    token = re.search(r"TENANT_B_PIT=(\S+)", Path(src).read_text(encoding="utf-8")).group(1)
    loc = os.environ.get("GHL_CFVC_LOCATION", TENANT_B)
    c = g.Client(token)

    st, body = c.request("GET", "/users/?locationId=%s" % loc)
    users = [u for u in g.expect(st, body, (200,), "list users").get("users", []) if not u.get("deleted")]
    if not users:
        print("FAIL: the sub-account has no user to own the calendar")
        return 1
    user_id = users[0]["id"]

    out = os.environ.get("GHL_CFVC_TRANSCRIPT")
    result = {}
    try:
        result = g.run_all(c, loc, user_id)
    finally:
        # written even when a check fails, so the failing answer can be read
        if out:
            Path(out).write_text(json.dumps({"result": result, "calls": c.transcript}, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    blocked = [k for k, v in result.items() if "blocked" in v]
    if blocked:
        print("BLOCKED parts: " + ", ".join(blocked))
        return 3
    print("CUSTOM FIELD, CUSTOM VALUE AND CALENDAR BOOKING PROVEN")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (g.CheckFailed, g.ScopeBlocked) as e:
        print("FAIL: %s" % e)
        sys.exit(1)
