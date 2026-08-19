/**
 * NOAA / National Weather Service ingestion.
 *
 * WHY THIS EXISTS IN A ROOFING PLATFORM: roofing intelligence is storm-damage
 * intelligence. A hail event over a ZIP is the trigger for an inspection, a
 * claim, and a sales conversation. Tying weather to properties is the actual
 * product thesis, not a nice-to-have.
 *
 * VERIFIED LIVE 2026-08-19, not written from memory:
 *   GET https://api.weather.gov/alerts/active?area=TX      -> 200, 10 features
 *   GET https://api.weather.gov/points/30.2672,-97.7431    -> 200
 *   No API key. No account. GeoJSON FeatureCollection.
 *   Real property keys on a feature: id, event, areaDesc, onset, effective,
 *   ends, expires, severity-ish fields (category, certainty, response),
 *   affectedZones, geocode, headline, description, instruction, parameters.
 *
 * TWO DISTINCT NOAA SURFACES, and conflating them is the usual mistake:
 *   1. api.weather.gov  -> CURRENT and forecast alerts. Live, keyless, what this
 *      module uses. Alerts EXPIRE and disappear, so live polling only ever gives
 *      you "what is happening now".
 *   2. NCEI Storm Events -> HISTORICAL archive (hail size, wind speed, dates
 *      going back decades). That is a separate bulk dataset, and it is what you
 *      need to answer "was there hail over this roof in the last 3 years". It is
 *      NOT available from api.weather.gov.
 * A roofing platform needs BOTH halves: live alerts to trigger outreach, and
 * historical hail to answer "was there hail over this roof in the last 3 years".
 * This module covers the live half and states the boundary explicitly rather
 * than implying it covers everything. The historical half is in swdi.ts.
 */

const NWS_BASE = "https://api.weather.gov";

/** NWS requires a User-Agent identifying the app and a contact. Requests without
 *  one are rejected or throttled; this is documented policy, not folklore. */
const USER_AGENT = process.env.NWS_USER_AGENT ?? "ghl-tenant-bridge (contact: set NWS_USER_AGENT)";

export interface NwsAlert {
  externalId: string;
  eventType: string | null;
  onset: string | null;
  ends: string | null;
  areaDesc: string | null;
  headline: string | null;
  severity: string | null;
  /** SAME/UGC codes, used to match an alert to counties/zones cheaply. */
  geocodes: string[];
  raw: unknown;
}

async function nwsFetch(path: string): Promise<unknown> {
  const res = await fetch(NWS_BASE + path, {
    headers: { "User-Agent": USER_AGENT, Accept: "application/geo+json" },
  });
  if (!res.ok) {
    throw new Error(`NWS ${path} -> ${res.status}`);
  }
  return res.json();
}

/** Storm-damage-relevant alert types for roofing. Heat advisories are noise here;
 *  hail and wind are the ones that put a roofer on a roof. */
export const ROOFING_RELEVANT_EVENTS = [
  "Severe Thunderstorm Warning",
  "Tornado Warning",
  "Tornado Watch",
  "Severe Weather Statement",
  "High Wind Warning",
  "High Wind Watch",
  "Wind Advisory",
  "Winter Storm Warning",
  "Ice Storm Warning",
  "Hurricane Warning",
  "Tropical Storm Warning",
];

export interface FetchOptions {
  /** Two-letter state/marine area, e.g. "TX". */
  area?: string;
  /** Restrict to roofing-relevant event types. */
  roofingOnly?: boolean;
}

export async function fetchActiveAlerts(opts: FetchOptions = {}): Promise<NwsAlert[]> {
  const qs = opts.area ? `?area=${encodeURIComponent(opts.area)}` : "";
  const data = (await nwsFetch(`/alerts/active${qs}`)) as {
    features?: Array<{ properties?: Record<string, unknown> }>;
  };

  const out: NwsAlert[] = [];
  for (const f of data.features ?? []) {
    const p = f.properties ?? {};
    const eventType = (p.event as string) ?? null;

    if (opts.roofingOnly && (!eventType || !ROOFING_RELEVANT_EVENTS.includes(eventType))) {
      continue;
    }

    const geocode = (p.geocode as Record<string, string[]> | undefined) ?? {};
    const geocodes = [...(geocode.SAME ?? []), ...(geocode.UGC ?? [])];

    out.push({
      // The NWS id is stable per alert and is our idempotency key. Alerts are
      // re-issued with a new id and a `references` array pointing at the old one,
      // so upserting on id gives revision history rather than lost updates.
      externalId: (p.id as string) ?? (p["@id"] as string) ?? "",
      eventType,
      onset: (p.onset as string) ?? (p.effective as string) ?? null,
      ends: (p.ends as string) ?? (p.expires as string) ?? null,
      areaDesc: (p.areaDesc as string) ?? null,
      headline: (p.headline as string) ?? null,
      severity: (p.severity as string) ?? (p.certainty as string) ?? null,
      geocodes,
      raw: p,
    });
  }
  return out.filter((a) => a.externalId);
}

/** Resolve a lat/lon to the NWS grid + county/zone, so a property address can be
 *  matched to alerts without a geospatial join on every poll. */
export async function resolvePoint(lat: number, lon: number): Promise<{
  county: string | null;
  forecastZone: string | null;
  state: string | null;
}> {
  // NWS rejects excessive precision; 4 decimals is ~11m, plenty for a roof.
  const data = (await nwsFetch(`/points/${lat.toFixed(4)},${lon.toFixed(4)}`)) as {
    properties?: Record<string, unknown>;
  };
  const p = data.properties ?? {};
  const rel = (p.relativeLocation as { properties?: Record<string, unknown> } | undefined)
    ?.properties;
  return {
    county: (p.county as string) ?? null,
    forecastZone: (p.forecastZone as string) ?? null,
    state: (rel?.state as string) ?? null,
  };
}

/**
 * Upsert alerts into the shared weather_events table.
 *
 * NOTE weather is deliberately NOT tenant-scoped: a storm over a county is a
 * public fact and storing it once per tenant would duplicate it N times and make
 * "did it hail here" a per-tenant question with per-tenant answers. Tenant
 * linkage happens in property_events, which IS org-scoped and RLS-protected.
 */
export async function ingestAlerts(
  db: {
    from: (t: string) => {
      upsert: (rows: unknown[], opts: { onConflict: string }) => Promise<{ error: unknown }>;
    };
  },
  alerts: NwsAlert[]
): Promise<{ ingested: number }> {
  if (alerts.length === 0) return { ingested: 0 };

  const rows = alerts.map((a) => ({
    source: "noaa",
    external_id: a.externalId,
    event_type: a.eventType,
    occurred_at: a.onset ?? new Date().toISOString(),
    state: null,
    county: a.areaDesc,
    detail: { headline: a.headline, severity: a.severity, geocodes: a.geocodes, raw: a.raw },
  }));

  const { error } = await db
    .from("weather_events")
    .upsert(rows, { onConflict: "source,external_id" });
  if (error) {
    throw new Error("weather upsert failed: " + JSON.stringify(error));
  }
  return { ingested: rows.length };
}
