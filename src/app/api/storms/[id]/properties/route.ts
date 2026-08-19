/**
 * GET /api/storms/[id]/properties?radiusKm=50
 *
 * Properties within a true spherical radius of a hail event. This is the route that
 * WIRES the PostGIS work in 0002_postgis.sql; before it existed the migration's RPC
 * had no caller anywhere in the app.
 *
 * TWO DECISIONS WORTH READING
 * ---------------------------
 * 1. It calls the ANON-KEY client, not the service role. properties_near_weather_event
 *    is SECURITY INVOKER precisely so the caller's RLS applies, and using the
 *    service-role key here would bypass every policy and cheerfully return other
 *    orgs' properties. The route would look identical in a single-tenant test.
 *
 * 2. The radius is in KILOMETRES at this boundary and converted once, inside the SQL
 *    function. Passing 50 straight into ST_DWithin on a geography column means 50
 *    METRES and returns almost nothing, which is a silent wrong answer rather than
 *    an error. Keeping the unit conversion in exactly one place is the guard.
 *
 * The measured reason this route exists at all: a bounding box reaches ~1.41x its
 * radius at the corners, so box-matching claims a property 67km away was hit by a
 * 50km storm. SWDI only accepts a box for the FETCH, so the box stays there and the
 * MATCH runs on ST_DWithin. See tests/postgis.test.py, which keeps the box as a
 * negative control.
 */

import { NextResponse } from "next/server";

import { browserClient, supabaseConfigured } from "@/lib/supabase";

export const runtime = "nodejs";

const MAX_RADIUS_KM = 200;

export async function GET(
  req: Request,
  ctx: { params: Promise<{ id: string }> }
) {
  const { id } = await ctx.params;

  if (!supabaseConfigured()) {
    return NextResponse.json(
      { error: "Supabase is not configured. Copy .env.example to .env.local." },
      { status: 503 }
    );
  }

  const url = new URL(req.url);
  const raw = url.searchParams.get("radiusKm");
  const radiusKm = raw === null ? 50 : Number(raw);

  if (!Number.isFinite(radiusKm) || radiusKm <= 0) {
    return NextResponse.json(
      { error: "radiusKm must be a positive number of KILOMETRES" },
      { status: 400 }
    );
  }
  if (radiusKm > MAX_RADIUS_KM) {
    // A cap rather than an unbounded scan. Without it a caller can ask for a radius
    // that matches every property a tenant owns and turn an indexed lookup into a
    // full table scan.
    return NextResponse.json(
      { error: `radiusKm exceeds the ${MAX_RADIUS_KM} km cap` },
      { status: 400 }
    );
  }

  // Anon key on purpose. RLS scopes the result to whoever is calling.
  const supabase = browserClient();
  const { data, error } = await supabase.rpc("properties_near_weather_event", {
    p_weather_event_id: id,
    p_radius_km: radiusKm,
  });

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 502 });
  }

  const rows = Array.isArray(data) ? data : [];
  return NextResponse.json({
    weatherEventId: id,
    radiusKm,
    // Named so nobody reads this as a bbox result. The distinction is the entire
    // reason the PostGIS migration exists.
    matchMethod: "ST_DWithin on geography (true spheroidal metres)",
    count: rows.length,
    properties: rows,
  });
}
