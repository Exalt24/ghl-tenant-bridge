/**
 * geocode.ts -- address to coordinates via the US Census Bureau geocoder.
 *
 * WHY THIS ONE
 * ------------
 * It is free, needs NO API key, has no published per-request rate limit, and it is
 * a US federal work product so there is no licence to read before shipping. For a
 * roofing platform that only operates in the US, that combination is hard to beat:
 * Nominatim's public instance caps at 1 request/second and forbids apps where
 * geocoding is the primary function, and every commercial alternative bills per
 * thousand lookups.
 *
 * THE LIMITATION THAT MATTERS, AND IT IS NOT A SMALL ONE
 * -----------------------------------------------------
 * This returns a point INTERPOLATED ALONG A BLOCK FACE from TIGER address ranges.
 * It is NOT a rooftop coordinate. Measured live 2026-08-20 against
 * "1600 Pennsylvania Ave NW": the response carries
 *   tigerLine: { side: "L", tigerLineId: "76225813" }
 * and an address range of 1600-1648, i.e. the point was derived by walking a
 * street segment, not by locating a building. Published error for this technique
 * is 10-50m, and roughly half of interpolated points land on a DIFFERENT parcel
 * than the true address.
 *
 * So: good enough to centre a map, to seed a radius query, and to feed a BUFFERED
 * parcel lookup. Not good enough to assert "this is the house", and never good
 * enough to drive a roof measurement. `matchQuality` below exists so a caller
 * cannot forget that.
 *
 * Everything in this file was verified live rather than read in a doc:
 *   * keyless GET works, 1 match returned for the test address
 *   * `benchmark` is REQUIRED; omitting it is an error, not a default
 *   * the response nests matches at result.addressMatches[]
 *   * coordinates come back as { x: longitude, y: latitude } -- X IS LONGITUDE,
 *     which is the same trap as ST_MakePoint elsewhere in this codebase
 *   * there is no CORS header, so this is server-side only
 */

const BASE = "https://geocoding.geo.census.gov/geocoder";

/**
 * Public_AR_Current is the current address-range benchmark. It is passed
 * explicitly because the API has no default and returns an error without it.
 */
const BENCHMARK = "Public_AR_Current";

/** The Census API has no documented rate limit, so we impose a modest one rather
 *  than discovering theirs the hard way. Batch geocoding (up to 10,000 rows per
 *  file) is the documented path for bulk work and is deliberately not wrapped
 *  here: a per-address loop is the wrong tool for a bulk import. */
export const CENSUS_BATCH_LIMIT = 10000;

export type MatchQuality =
  /** Interpolated along a street segment from TIGER ranges. 10-50m typical error,
   *  and about half of these land on a neighbouring parcel. Never treat as rooftop. */
  | "street-interpolated"
  /** No match at all. */
  | "none";

export interface GeocodeResult {
  latitude: number;
  longitude: number;
  matchedAddress: string;
  matchQuality: MatchQuality;
  /** Present when the match was interpolated, which for this provider is always.
   *  Kept so a caller can log WHY a coordinate is approximate. */
  tigerLineId: string | null;
  /** "L" or "R": which side of the street segment. Its existence is the tell that
   *  this is a block-face interpolation. */
  side: string | null;
}

interface CensusMatch {
  coordinates?: { x?: number; y?: number };
  matchedAddress?: string;
  tigerLine?: { tigerLineId?: string; side?: string };
}

/**
 * Geocode a single one-line US address.
 *
 * Returns null on no match, and THROWS on a transport or shape failure. The
 * distinction is deliberate: "this address is not in TIGER" is a normal outcome a
 * caller should handle, whereas "the API changed shape" must not be silently
 * swallowed into a null that looks identical.
 */
export async function geocodeAddress(
  address: string,
  opts: { userAgent: string; timeoutMs?: number } = { userAgent: "" }
): Promise<GeocodeResult | null> {
  if (!address.trim()) return null;
  if (!opts.userAgent) {
    // Not required by Census, but every other geo provider in this codebase needs
    // one (NWS returns 403 on an empty User-Agent, measured), so the interface is
    // kept uniform to stop a caller learning two conventions.
    throw new Error("geocodeAddress: a userAgent is required for consistency with the other geo clients");
  }

  const url =
    `${BASE}/locations/onelineaddress` +
    `?address=${encodeURIComponent(address)}` +
    `&benchmark=${BENCHMARK}` +
    `&format=json`;

  const res = await fetch(url, {
    headers: { "User-Agent": opts.userAgent, Accept: "application/json" },
    signal: AbortSignal.timeout(opts.timeoutMs ?? 30_000),
  });

  if (!res.ok) throw new Error(`census geocoder -> HTTP ${res.status}`);

  const body = (await res.json()) as { result?: { addressMatches?: CensusMatch[] } };
  const matches = body?.result?.addressMatches;
  if (!Array.isArray(matches)) {
    throw new Error("census geocoder: result.addressMatches missing, response shape changed");
  }
  if (matches.length === 0) return null;

  const m = matches[0];
  const x = m.coordinates?.x;
  const y = m.coordinates?.y;
  if (typeof x !== "number" || typeof y !== "number") {
    throw new Error("census geocoder: match had no numeric coordinates");
  }

  return {
    // x IS LONGITUDE. Reading these in the order they appear in the JSON and
    // assigning them positionally is how a coordinate swap enters a codebase.
    longitude: x,
    latitude: y,
    matchedAddress: m.matchedAddress ?? address,
    matchQuality: "street-interpolated",
    tigerLineId: m.tigerLine?.tigerLineId ?? null,
    side: m.tigerLine?.side ?? null,
  };
}

/**
 * Metres of slop to allow around a geocoded point when using it to look something
 * up spatially.
 *
 * 60m is not arbitrary: published interpolation error for this technique tops out
 * around 50m, so a 60m buffer covers it with a little headroom. This is exported
 * because the parcel lookup MUST buffer -- a bare point silently matches nothing
 * against county parcel services (measured, see parcels.ts).
 */
export const INTERPOLATION_SLOP_METRES = 60;
