/**
 * POST /api/properties/resolve
 *
 * Address in, coordinates plus candidate parcels out. This is the route that WIRES
 * geocode.ts and parcels.ts together; before it existed both were tested libraries
 * with no caller.
 *
 * The two-step shape is forced by what each source actually does, and both halves
 * were measured rather than assumed:
 *
 *   1. The Census geocoder returns a point INTERPOLATED along a block face, not a
 *      rooftop. The response literally carries a tigerLine id, a side of the street
 *      and an address range. Published error runs 10-50m and roughly half of these
 *      land on a neighbouring parcel.
 *   2. County parcel services return ZERO FEATURES AND NO ERROR for a bare point
 *      query. The same coordinate as a ~20m envelope returns several parcels.
 *
 * Put together: the geocode is approximate, so the parcel lookup MUST buffer, and it
 * will legitimately return more than one candidate. This route therefore returns
 * ALL candidates with the match quality attached and refuses to pick one, because
 * silently choosing the first parcel is how a roof report gets attached to the
 * neighbour's house.
 *
 * Server-side only, deliberately: the Census geocoder sends no CORS headers, so this
 * cannot run from a browser even if someone wanted it to.
 */

import { NextResponse } from "next/server";

import { geocodeAddress, INTERPOLATION_SLOP_METRES } from "@/lib/geocode";
import { queryParcelsNearPoint, WAKE_COUNTY_NC, type ParcelSource } from "@/lib/parcels";

// node runtime, not edge: these are outbound HTTPS calls with generous timeouts.
export const runtime = "nodejs";

const UA =
  process.env.GEO_USER_AGENT ??
  "ghl-tenant-bridge/0.1 (contact: danielalexiscruz.pro@gmail.com)";

/** Only configured counties are queryable. Every county has a different schema, so
 *  this is a registry rather than a pretence that one national endpoint exists. */
const SOURCES: Record<string, ParcelSource> = {
  "wake-nc": WAKE_COUNTY_NC,
};

export async function POST(req: Request) {
  let body: { address?: unknown; county?: unknown; radiusMetres?: unknown };
  try {
    body = await req.json();
  } catch {
    return NextResponse.json({ error: "body must be JSON" }, { status: 400 });
  }

  const address = typeof body.address === "string" ? body.address.trim() : "";
  if (!address) {
    return NextResponse.json({ error: "address is required" }, { status: 400 });
  }

  const countyKey = typeof body.county === "string" ? body.county : "wake-nc";
  const source = SOURCES[countyKey];
  if (!source) {
    return NextResponse.json(
      { error: `unknown county '${countyKey}'`, available: Object.keys(SOURCES) },
      { status: 400 }
    );
  }

  // Default to the interpolation slop rather than an arbitrary number: the buffer
  // exists to cover the geocoder's own error, so that is what sets its size.
  const radiusMetres =
    typeof body.radiusMetres === "number" && body.radiusMetres > 0
      ? body.radiusMetres
      : INTERPOLATION_SLOP_METRES;

  try {
    const geo = await geocodeAddress(address, { userAgent: UA });
    if (!geo) {
      // A miss is a normal outcome, not an error. 200 with matched:false so a caller
      // does not have to distinguish "not in TIGER" from "the service broke".
      return NextResponse.json({ matched: false, address, parcels: [] });
    }

    const parcels = await queryParcelsNearPoint(
      source,
      geo.latitude,
      geo.longitude,
      radiusMetres,
      { userAgent: UA }
    );

    return NextResponse.json({
      matched: true,
      address,
      coordinates: { latitude: geo.latitude, longitude: geo.longitude },
      matchedAddress: geo.matchedAddress,
      // Surfaced, not hidden. A caller deciding whether to trust a parcel match needs
      // to know the point was interpolated along a street segment.
      matchQuality: geo.matchQuality,
      tigerLineId: geo.tigerLineId,
      side: geo.side,
      searchRadiusMetres: radiusMetres,
      county: countyKey,
      // Plural on purpose. A 20m envelope in a dense block returns several parcels and
      // choosing between them is a decision with consequences, so it stays with the
      // caller who has the context.
      candidateCount: parcels.parcels.length,
      parcels: parcels.parcels.map((p) => p.attributes),
      truncated: parcels.truncated,
      envelope: parcels.envelope,
    });
  } catch (e) {
    return NextResponse.json(
      { error: e instanceof Error ? e.message : "resolve failed" },
      { status: 502 }
    );
  }
}
