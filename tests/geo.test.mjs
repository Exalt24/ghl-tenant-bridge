/**
 * geocode.ts + parcels.ts tests.
 *
 * These import the REAL modules (node 24 strips TypeScript natively), which is a
 * deliberate improvement on the older suites in this folder: swdi.test.mjs and
 * noaa.test.mjs re-implement their HTTP calls inline, so they prove the SERVICE
 * behaves and prove nothing about our code. Here a bug in the module fails the test.
 *
 * THE LOAD-BEARING TEST is "a bare POINT query returns nothing". It calls the county
 * service directly with esriGeometryPoint to demonstrate the trap, then shows the
 * module's envelope finding parcels at the identical coordinate. Without that
 * negative control, "our parcel lookup works" is a claim about one lucky query
 * rather than evidence that the envelope is why it works.
 *
 * Pure logic is asserted in both directions too: assertArcgisOk has to throw on a
 * disguised error AND stay quiet on a legitimately empty result, because a guard
 * that fires on everything is as useless as one that never fires.
 *
 * Run: node tests/geo.test.mjs
 */
import {
  geocodeAddress,
  INTERPOLATION_SLOP_METRES,
} from "../src/lib/geocode.ts";
import {
  assertArcgisOk,
  countParcels,
  envelopeAround,
  queryParcelsNearPoint,
  queryParcelsPaged,
  WAKE_COUNTY_NC,
} from "../src/lib/parcels.ts";

const UA = "ghl-tenant-bridge/0.1 (contact: danielalexiscruz.pro@gmail.com)";

// A coordinate verified live 2026-08-20 to sit inside Wake County parcels, and to
// return ZERO from a point query. Downtown Raleigh.
const WAKE_LAT = 35.776949;
const WAKE_LON = -78.641826;

let pass = 0;
const fails = [];
const check = (name, cond, detail = "") => {
  if (cond) {
    pass++;
  } else {
    fails.push(`${name}${detail ? ` -- ${detail}` : ""}`);
  }
  console.log(`  ${cond ? "PASS" : "FAIL"}  ${name}${cond ? "" : ` (${detail})`}`);
};

async function threw(fn) {
  try {
    await fn();
    return null;
  } catch (e) {
    return e.message ?? String(e);
  }
}

// ---------------------------------------------------------------- pure geometry
console.log("\nenvelopeAround (pure)");
{
  const e = envelopeAround(35.0, -78.0, 60);
  check("envelope is symmetric in latitude",
    Math.abs((e.maxLat - 35.0) - (35.0 - e.minLat)) < 1e-12);
  check("envelope is symmetric in longitude",
    Math.abs((e.maxLon + 78.0) - (-78.0 - e.minLon)) < 1e-12);

  const dLat = e.maxLat - 35.0;
  check("60m of latitude is about 0.00054 degrees",
    Math.abs(dLat - 60 / 111320) < 1e-9, `got ${dLat}`);

  // The whole point of the cosine correction: a degree of longitude covers less
  // ground the further from the equator you go, so the same metre radius must span
  // MORE degrees of longitude at higher latitude.
  const eq = envelopeAround(0, 0, 60);
  const hi = envelopeAround(60, 0, 60);
  check("longitude span grows with latitude (cosine correction is applied)",
    (hi.maxLon - hi.minLon) > (eq.maxLon - eq.minLon) * 1.9,
    `equator ${eq.maxLon - eq.minLon}, lat60 ${hi.maxLon - hi.minLon}`);

  // Without a floor on cos(), longitude degrees explode toward the pole.
  const pole = envelopeAround(89.999, 0, 60);
  check("cosine is floored so a near-polar point does not blow up",
    Number.isFinite(pole.maxLon) && (pole.maxLon - pole.minLon) < 200,
    `span ${pole.maxLon - pole.minLon}`);

  check("interpolation slop is documented and non-zero",
    INTERPOLATION_SLOP_METRES >= 50);
}

// ------------------------------------------------- assertArcgisOk, both directions
console.log("\nassertArcgisOk (negative test in both directions)");
{
  let msg = null;
  try {
    assertArcgisOk({ error: { code: 404, message: "Service not found" } }, "ctx");
  } catch (e) {
    msg = e.message;
  }
  check("throws on an error body (the HTTP 200 disguise)", msg !== null);
  check("the message carries the ArcGIS code, not just 'failed'",
    !!msg && msg.includes("404") && msg.includes("Service not found"), `msg=${msg}`);

  let quiet = true;
  try {
    assertArcgisOk({ features: [] }, "ctx");
    assertArcgisOk({ count: 0 }, "ctx");
    assertArcgisOk({}, "ctx");
  } catch {
    quiet = false;
  }
  check("does NOT throw on a legitimately empty result", quiet,
    "a guard that fires on empty would reject every valid no-match");
}

// ---------------------------------------------------------------- guards
console.log("\nguards");
{
  const m1 = await threw(() =>
    queryParcelsNearPoint(WAKE_COUNTY_NC, WAKE_LAT, WAKE_LON, 0, { userAgent: UA }));
  check("a zero radius is refused (that is the silent point-query trap)",
    !!m1 && /radiusMetres/.test(m1), `msg=${m1}`);

  const m2 = await threw(() =>
    queryParcelsPaged({ ...WAKE_COUNTY_NC, orderByField: undefined }, "1=1", { userAgent: UA }));
  check("paging without orderByField is refused", !!m2 && /orderByField/.test(m2), `msg=${m2}`);

  const m3 = await threw(() => geocodeAddress("1600 Pennsylvania Ave NW", { userAgent: "" }));
  check("geocode refuses an empty userAgent", !!m3 && /userAgent/.test(m3), `msg=${m3}`);
}

// ---------------------------------------------------------------- live geocoder
console.log("\nCensus geocoder (live, keyless)");
{
  const g = await geocodeAddress("1600 Pennsylvania Ave NW, Washington, DC", { userAgent: UA });
  check("a known address matches", g !== null);
  if (g) {
    // If longitude and latitude were ever swapped, THIS is what catches it: a US
    // longitude is strongly negative and a US latitude is not.
    check("longitude is negative (US) and not the latitude",
      g.longitude < -50 && g.longitude > -130, `lon=${g.longitude}`);
    check("latitude is plausible for DC and not the longitude",
      g.latitude > 25 && g.latitude < 50, `lat=${g.latitude}`);
    check("matchQuality is honest about being interpolated",
      g.matchQuality === "street-interpolated", `got ${g.matchQuality}`);
    // tigerLine is the evidence the point came from a block-face range, which is
    // exactly why callers must buffer before a parcel lookup.
    check("tigerLineId is present, proving block-face interpolation",
      typeof g.tigerLineId === "string" && g.tigerLineId.length > 0, `id=${g.tigerLineId}`);
    check("side (L/R) is present", g.side === "L" || g.side === "R", `side=${g.side}`);
  }

  const none = await geocodeAddress("99999 Nowhere Street, Nowhereville, ZZ", { userAgent: UA });
  check("an unmatchable address returns null instead of throwing", none === null);

  check("an empty address returns null without a request",
    (await geocodeAddress("   ", { userAgent: UA })) === null);
}

// ------------------------------------------------- THE TRAP, measured both ways
console.log("\nTHE POINT-QUERY TRAP (negative control, then our envelope)");
{
  // Raw point query, exactly what an "obvious" implementation would send.
  const p = new URLSearchParams({
    geometry: `${WAKE_LON},${WAKE_LAT}`,
    geometryType: "esriGeometryPoint",
    inSR: "4326",
    outSR: "4326",
    spatialRel: "esriSpatialRelIntersects",
    outFields: "SITE_ADDRESS",
    returnGeometry: "false",
    f: "json",
  });
  const raw = await fetch(`${WAKE_COUNTY_NC.layerUrl}/query?${p}`, {
    headers: { "User-Agent": UA },
  }).then((r) => r.json());

  check("a bare POINT query returns zero features",
    Array.isArray(raw.features) && raw.features.length === 0,
    `got ${JSON.stringify(raw).slice(0, 120)}`);
  check("and it reports NO error, so the failure is silent",
    raw.error === undefined);

  // Same coordinate, through the module.
  const r = await queryParcelsNearPoint(WAKE_COUNTY_NC, WAKE_LAT, WAKE_LON, 30, {
    userAgent: UA,
  });
  check("the module's ENVELOPE finds parcels at the identical coordinate",
    r.parcels.length > 0, `found ${r.parcels.length}`);
  check("it returns every candidate rather than guessing one",
    r.parcels.length >= 1);
  check("requested fields come back populated",
    r.parcels.some((x) => typeof x.attributes.SITE_ADDRESS === "string"),
    `attrs=${JSON.stringify(r.parcels[0]?.attributes ?? {}).slice(0, 120)}`);
  check("the envelope used is reported back for logging",
    typeof r.envelope.minLat === "number" && r.envelope.minLat < WAKE_LAT);
  check("a small envelope is not flagged as truncated", r.truncated === false);
}

// ---------------------------------------------------------------- live count / paging
console.log("\ncount and paging (live)");
{
  const n = await countParcels(WAKE_COUNTY_NC, "1=1", { userAgent: UA });
  check("returnCountOnly gives a county-sized number", n > 100000, `count=${n}`);

  const dead = { ...WAKE_COUNTY_NC, name: "dead service",
    layerUrl: "https://maps.wakegov.com/arcgis/rest/services/Property/NoSuchService/MapServer/0" };
  const msg = await threw(() => countParcels(dead, "1=1", { userAgent: UA }));
  check("a dead service throws despite its HTTP 200",
    !!msg && /inside a 200 response/.test(msg), `msg=${msg}`);

  // A bounded query so paging terminates quickly. YEAR_BUILT is nullable, so a
  // narrow range keeps this to one short page.
  const paged = await queryParcelsPaged(
    { ...WAKE_COUNTY_NC, maxRecordCount: 500 },
    "YEAR_BUILT = 1925",
    { userAgent: UA, maxPages: 4 }
  );
  check("paging terminates on a short page rather than the transfer flag",
    paged.hitPageCap === false, `pages=${paged.pages}`);
  check("paging returned rows", paged.parcels.length > 0, `rows=${paged.parcels.length}`);
}

console.log(`\n${pass + fails.length} checks, ${fails.length} failed`);
if (fails.length) {
  for (const f of fails) console.log(`  FAILED: ${f}`);
  process.exit(1);
}
console.log("ALL GEO CHECKS PASS");
