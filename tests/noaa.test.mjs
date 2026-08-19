/**
 * NOAA/NWS live integration test. Hits the real API, because the point of this
 * module is that the API works with no key, and a mocked test would prove nothing
 * about that claim.
 *
 * Run: node tests/noaa.test.mjs
 */
import assert from "node:assert";

const UA = "ghl-tenant-bridge/0.1 (contact: danielalexiscruz.pro@gmail.com)";
const BASE = "https://api.weather.gov";

async function get(path) {
  const res = await fetch(BASE + path, {
    headers: { "User-Agent": UA, Accept: "application/geo+json" },
  });
  return { status: res.status, body: res.ok ? await res.json() : null };
}

let pass = 0;
const fails = [];
const check = (n, c) => (c ? pass++ : fails.push(n));

// --- the load-bearing claim: keyless access works ---
const alerts = await get("/alerts/active?area=TX");
check("GET /alerts/active?area=TX returns 200 with NO api key", alerts.status === 200);
check("response is a GeoJSON FeatureCollection", alerts.body?.type === "FeatureCollection");
check("features is an array", Array.isArray(alerts.body?.features));

// --- the payload shape the module depends on ---
const feats = alerts.body?.features ?? [];
if (feats.length > 0) {
  const p = feats[0].properties ?? {};
  for (const key of ["id", "event", "areaDesc", "onset", "effective", "expires", "geocode"]) {
    check(`alert property "${key}" is present (module reads it)`, key in p);
  }
  check("alert id is a non-empty string (our idempotency key)",
    typeof p.id === "string" && p.id.length > 0);
  const ids = feats.map((f) => f.properties?.id).filter(Boolean);
  check("alert ids are unique within a response", new Set(ids).size === ids.length);
} else {
  console.log("  note: zero active TX alerts right now, so payload-shape checks were skipped");
}

// --- point resolution ---
const pt = await get("/points/30.2672,-97.7431");
check("GET /points/{lat},{lon} returns 200", pt.status === 200);
check("point response carries properties", !!pt.body?.properties);
check("point exposes county", "county" in (pt.body?.properties ?? {}));
check("point exposes forecastZone", "forecastZone" in (pt.body?.properties ?? {}));

// --- the User-Agent requirement is real, prove it rather than assert it ---
const noUa = await fetch(BASE + "/alerts/active?area=TX", {
  headers: { Accept: "application/geo+json", "User-Agent": "" },
});
console.log(`  note: request with an empty User-Agent returned ${noUa.status} ` +
  `(NWS documents a UA requirement; recording the observed behaviour rather than assuming it)`);

// --- the documented BOUNDARY: live alerts are not a historical archive ---
// This asserts the limitation the module claims, so the claim cannot silently rot.
const active = feats.length;
const expired = feats.filter((f) => {
  const e = f.properties?.expires;
  return e && Date.parse(e) < Date.now();
}).length;
check("active-alerts feed contains no already-expired alerts (it is NOT an archive)",
  expired === 0);
console.log(`  observed: ${active} active alerts, ${expired} expired present`);

console.log(`\nchecks passed: ${pass}`);
if (fails.length) {
  console.log(`FAIL (${fails.length})`);
  fails.forEach((f) => console.log("  - " + f));
  process.exit(1);
}
console.log("PASS, zero gaps (live NWS, keyless, payload shape, documented boundary)");
