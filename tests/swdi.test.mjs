/**
 * NCEI SWDI historical hail tests. Hits the live service.
 *
 * The load-bearing claims are (a) it needs no key, (b) it returns POINT geometry
 * with hail SIZE, (c) the bbox filter actually filters, and (d) the archive goes
 * back years. (c) and (d) are asserted in BOTH directions, because a zero result
 * from a bbox or an old date is worthless as evidence unless the same query shape
 * is shown returning data when data exists. That is exactly how "no hail here"
 * would otherwise be indistinguishable from "the parameter was ignored".
 *
 * Run: node tests/swdi.test.mjs
 */
const BASE = "https://www.ncei.noaa.gov/swdiws/json";
const UA = "ghl-tenant-bridge/0.1 (contact: danielalexiscruz.pro@gmail.com)";

async function swdi(path) {
  const res = await fetch(BASE + path, { headers: { "User-Agent": UA } });
  if (!res.ok) return { status: res.status, count: null, result: [] };
  const body = await res.json();
  return {
    status: res.status,
    count: body.summary?.count ?? (body.result ?? []).length,
    result: body.result ?? [],
  };
}

let pass = 0;
const fails = [];
const check = (n, c) => (c ? pass++ : fails.push(n));

// ---- (a) keyless access, and a control that the service answers at all ----
const nat = await swdi("/nx3hail/20260601:20260602");
check("national hail query returns 200 with NO api key", nat.status === 200);
check("national hail query returns data (service is alive)", (nat.count ?? 0) > 0);
console.log(`  national 2026-06-01 hail signatures: ${nat.count}`);

// ---- (b) the fields the module depends on ----
if (nat.result.length) {
  const r = nat.result[0];
  for (const k of ["MAXSIZE", "PROB", "SEVPROB", "ZTIME", "SHAPE", "WSR_ID", "CELL_ID"]) {
    check(`hail record carries "${k}"`, k in r);
  }
  check("SHAPE is WKT POINT geometry, not a county name",
    typeof r.SHAPE === "string" && /^POINT\s*\(/i.test(r.SHAPE));
  const m = String(r.SHAPE).match(/POINT\s*\(\s*(-?[\d.]+)\s+(-?[\d.]+)\s*\)/i);
  check("POINT parses to two finite coordinates",
    !!m && Number.isFinite(Number(m[1])) && Number.isFinite(Number(m[2])));
  if (m) {
    const lon = Number(m[1]);
    const lat = Number(m[2]);
    check("WKT order is (lon lat): lon in [-180,180], lat in [-90,90]",
      lon >= -180 && lon <= 180 && lat >= -90 && lat <= 90);
    check("coordinates are plausibly CONUS", lat > 20 && lat < 55 && lon > -130 && lon < -60);
  }
  const sizes = nat.result.map((x) => Number(x.MAXSIZE)).filter(Number.isFinite);
  check("MAXSIZE parses as a number (inches)", sizes.length > 0);
  check("MAXSIZE values are in a sane hail range (0 to 8 inches)",
    sizes.every((s) => s >= 0 && s <= 8));
  console.log(`  MAXSIZE range observed: ${Math.min(...sizes)} to ${Math.max(...sizes)} inches`);
}

// ---- (c) bbox filters, PROVEN BOTH WAYS on the same date ----
const hit = await swdi("/nx3hail/20260601:20260602?bbox=-101.0,34.0,-100.3,34.5");
const miss = await swdi("/nx3hail/20260601:20260602?bbox=-97.9,30.1,-97.6,30.4");
check("bbox POSITIVE control returns data (Lubbock area had hail)", (hit.count ?? 0) > 0);
check("bbox NEGATIVE control returns zero (Austin area did not)", miss.count === 0);
check("bbox actually narrows the result vs national", (hit.count ?? 0) < (nat.count ?? 1e9));
console.log(`  bbox hit=${hit.count}  bbox miss=${miss.count}  (so a zero MEANS zero)`);

// every returned point must fall inside the requested box
if (hit.result.length) {
  const inside = hit.result.every((r) => {
    const m = String(r.SHAPE).match(/POINT\s*\(\s*(-?[\d.]+)\s+(-?[\d.]+)\s*\)/i);
    if (!m) return false;
    const lon = Number(m[1]);
    const lat = Number(m[2]);
    return lon >= -101.0 && lon <= -100.3 && lat >= 34.0 && lat <= 34.5;
  });
  check("every point returned really is inside the requested bbox", inside);
}

// ---- (d) archive depth, nationally so a small box cannot fake a gap ----
const years = ["2010", "2015", "2020", "2024"];
const counts = {};
for (const y of years) {
  const r = await swdi(`/nx3hail/${y}0601:${y}0602`);
  counts[y] = r.count;
  check(`archive has data for ${y} (national query)`, (r.count ?? 0) > 0);
}
console.log(`  archive depth: ${years.map((y) => `${y}=${counts[y]}`).join("  ")}`);

// ---- the datasets measured as BROKEN must still be broken, or the note is stale ----
for (const ds of ["plsr", "warn"]) {
  const r = await swdi(`/${ds}/20260601:20260602`);
  if (r.status === 200) {
    console.log(`  NOTE: /${ds} now returns 200 (was HTTP 500 on 2026-08-19). ` +
      `Update the KNOWN_BROKEN guard in src/lib/swdi.ts.`);
  } else {
    console.log(`  confirmed still broken: /${ds} -> HTTP ${r.status}`);
  }
}

console.log(`\nchecks passed: ${pass}`);
if (fails.length) {
  console.log(`FAIL (${fails.length})`);
  fails.forEach((f) => console.log("  - " + f));
  process.exit(1);
}
console.log("PASS, zero gaps (live SWDI, keyless, point geometry, bbox proven both ways, archive depth)");
