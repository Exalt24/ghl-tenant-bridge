#!/bin/sh
# Every suite. Re-run after ANY edit: a file changed since its last clean run is
# unverified, regardless of whether it passed before the edit.
fail=0

run() {
  label="$1"
  shift
  printf "%-38s " "$label"
  if "$@" >/dev/null 2>&1; then
    echo PASS
  else
    echo FAIL
    fail=1
  fi
}

run "signature (19 checks)"      node tests/signature.test.mjs
run "workflow-auth (22 checks)"  node tests/workflow-auth.test.mjs
run "noaa live (17 checks)"      node tests/noaa.test.mjs
run "swdi live (23 checks)"      node tests/swdi.test.mjs
run "geo live (31 checks)"       node tests/geo.test.mjs
run "outbox logic (56 checks)"   node tests/outbox.test.mjs
run "published keys"             python tests/verify_public_keys.py

printf "%-38s " "tenant isolation (live GHL)"
if python tests/tenant_isolation.py 2>&1 | grep -q "ISOLATED"; then
  echo PASS
else
  echo FAIL
  fail=1
fi

# Browser storage suite. Needs playwright plus a chromium binary, so it SKIPS loudly
# rather than failing a machine that has neither installed.
printf "%-38s " "outbox browser (33 checks)"
if node -e "require('C:/Users/Dax/AppData/Roaming/npm/node_modules/playwright/index.js')" >/dev/null 2>&1; then
  if node tests/outbox.browser.mjs >/dev/null 2>&1; then
    echo PASS
  else
    echo FAIL
    fail=1
  fi
else
  echo "SKIP (playwright not resolvable at the global path)"
fi

# The full offline cycle: capture with no connection, reconnect, drain to empty. Needs
# a running dev server with the demo sink enabled, so it SKIPS loudly when one is not
# up rather than failing a clone.
printf "%-38s " "offline drain e2e (10 checks)"
if curl -s -o /dev/null --max-time 3 "http://127.0.0.1:3111/api/demo/queue" 2>/dev/null; then
  if node tests/offline-drain.e2e.mjs >/dev/null 2>&1; then
    echo PASS
  else
    echo FAIL
    fail=1
  fi
else
  echo "SKIP (no server: DEMO_SINK=1 npx next start -p 3111)"
fi

# PostGIS spatial matching. Needs a local postgis container, so it SKIPS loudly when
# docker or the container is absent rather than failing a clone that simply has no
# docker. The test itself exits 2 for "no container" and 1 for a real assertion
# failure, so the two cases stay distinguishable.
printf "%-38s " "postgis spatial (25 checks)"
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx pgis; then
  if python tests/postgis.test.py >/dev/null 2>&1; then
    echo PASS
  else
    echo FAIL
    fail=1
  fi
else
  echo "SKIP (no pgis container; docker run --rm -d --name pgis -e POSTGRES_PASSWORD=pw -p 55432:5432 postgis/postgis:17-3.5)"
fi

# OAuth claim verification. SKIPS rather than fails when the token files are absent,
# because they hold live credentials and are gitignored, so a fresh clone legitimately
# will not have them. A skip is reported loudly so it cannot be mistaken for a pass.
printf "%-38s " "oauth claims (17 checks)"
if [ -f tests/token_response.json ] && [ -f tests/location_token.json ]; then
  if python tests/verify_oauth_claims.py 2>&1 | grep -q "ALL CLAIMS VERIFIED"; then
    echo PASS
  else
    echo FAIL
    fail=1
  fi
else
  echo "SKIP (no token files; re-run an install to regenerate)"
fi

# The hosted Supabase path. SKIPPED unless a hosted_env.json is present, because it
# needs a real project, two seeded orgs and two users; a missing config is not a
# failure, it just means this machine has no project wired.
printf "%-38s " "hosted-supabase.e2e"
if [ -f "$HOSTED_ENV" ] || [ -n "$HOSTED_ENV" ] && [ -f "$HOSTED_ENV" ]; then
  if node tests/hosted-supabase.e2e.mjs > /tmp/hosted.log 2>&1; then
    echo PASS
  else
    echo FAIL
    fail=1
  fi
else
  echo "SKIP (set HOSTED_ENV to a hosted_env.json to run)"
fi

echo ""
if [ "$fail" = 0 ]; then
  echo "ALL SUITES PASS"
else
  echo "SOME SUITES FAILED"
  exit 1
fi
