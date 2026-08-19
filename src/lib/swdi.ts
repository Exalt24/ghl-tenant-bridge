/**
 * NCEI Severe Weather Data Inventory (SWDI) — HISTORICAL hail and storm lookup.
 *
 * This closes the half of "historical AND live weather ingestion" that
 * api.weather.gov cannot do. The live NWS API only serves ACTIVE alerts; expired
 * ones vanish, so it can never answer "was there hail over this roof in 2024".
 *
 * ============================ MEASURED, NOT ASSUMED ==========================
 * All of the following was verified against the live service on 2026-08-19.
 *
 * Base: https://www.ncei.noaa.gov/swdiws/json/{dataset}/{start}:{end}
 *   (the older www.ncdc.noaa.gov/swdiws host 301-redirects here)
 * Auth: NONE. No key, no account. A User-Agent is sent as good practice.
 * Dates: YYYYMMDD:YYYYMMDD, end-exclusive-ish (a 1-day window works).
 *
 * POINT-LEVEL GEOMETRY, which is the whole reason this is usable for roofing:
 *   {"PROB":"100","SHAPE":"POINT (-100.6303 34.2159)","WSR_ID":"KLBB",
 *    "CELL_ID":"G5","ZTIME":"2026-06-01T00:00:26Z","SEVPROB":"10","MAXSIZE":"0.5"}
 * MAXSIZE is max hail size in INCHES. County-level data is nearly useless for
 * verifying a single roof; this is radar-derived per storm cell.
 *
 * bbox WORKS and was proven in BOTH directions:
 *   bbox=-101.0,34.0,-100.3,34.5 on 2026-06-01 -> count 1  (0.5" hail)
 *   bbox=-97.9,30.1,-97.6,30.4  on 2026-06-01 -> count 0
 * so a zero genuinely means "no hail there", not "the filter was ignored".
 * Format is bbox=minLon,minLat,maxLon,maxLat.
 *
 * ARCHIVE DEPTH, checked nationally per year (a small-bbox zero proves nothing,
 * so coverage was probed without a bbox):
 *   2010-06-01 -> 6023    2013 -> 8414   2015 -> 5791   2018 -> 1396
 *   2020-06-01 -> 1296    2022 -> 8790   2024 -> 7931   2026 -> 11565
 * So usable history reaches back at least to 2010.
 *
 * FRESHNESS: 2026-08-15 returned 4062 records, 2026-08-18 returned 0, so the
 * archive lands within roughly a few days of real time. Do not assume today.
 *
 * DATASETS confirmed working:
 *   nx3hail       hail signatures, MAXSIZE in inches          (the important one)
 *   nx3tvs        tornado vortex signatures                   (133 on 2026-06-01)
 *   nx3structure  storm cell structure                        (131172 on 2026-06-01)
 * Confirmed BROKEN as of 2026-08-19 (HTTP 500, not 404, so likely server-side):
 *   plsr          preliminary local storm reports
 *   warn          NWS storm warnings
 * Do not build on plsr or warn without re-checking them.
 *
 * ========================== HONEST LIMITATIONS ===============================
 * 1. These are RADAR-DERIVED SIGNATURES, not ground truth. A signature is the
 *    algorithm's estimate that hail of ~MAXSIZE was aloft in that cell. It is
 *    evidence for an inspection, not proof of damage, and an insurer may treat it
 *    as indicative rather than conclusive.
 * 2. Radar coverage has gaps and degrades with distance from the station and at
 *    low altitudes, so absence of a signature is weaker evidence than presence.
 * 3. Commercial hail-verification vendors exist precisely because they post-process
 *    this kind of data into per-address reports. This module is the free, honest
 *    version of the same idea.
 */

const SWDI_BASE = "https://www.ncei.noaa.gov/swdiws/json";
const USER_AGENT = process.env.NWS_USER_AGENT ?? "ghl-tenant-bridge (contact: set NWS_USER_AGENT)";

export type SwdiDataset = "nx3hail" | "nx3tvs" | "nx3structure";

/** Datasets measured as returning HTTP 500 on 2026-08-19. Guarded so a caller
 *  gets a clear error instead of a confusing parse failure. */
const KNOWN_BROKEN = new Set(["plsr", "warn"]);

export interface HailSignature {
  /** Max hail size in inches, radar-estimated. */
  maxSizeInches: number | null;
  /** Probability of hail, 0-100. */
  probability: number | null;
  /** Probability of SEVERE hail, 0-100. Roofers care about this one. */
  severeProbability: number | null;
  observedAt: string;
  latitude: number | null;
  longitude: number | null;
  radarStation: string | null;
  cellId: string | null;
}

function parsePoint(shape: string | undefined): { lat: number | null; lon: number | null } {
  // "POINT (-100.630308512923 34.2159779249994)"  -> lon first, then lat (WKT order)
  if (!shape) return { lat: null, lon: null };
  const m = shape.match(/POINT\s*\(\s*(-?[\d.]+)\s+(-?[\d.]+)\s*\)/i);
  if (!m) return { lat: null, lon: null };
  return { lon: Number(m[1]), lat: Number(m[2]) };
}

function num(v: unknown): number | null {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function yyyymmdd(d: Date): string {
  return d.toISOString().slice(0, 10).replace(/-/g, "");
}

/**
 * Build a bounding box around a point.
 *
 * Longitude degrees shrink with latitude, so a naive square box is materially
 * wrong at US latitudes: at 40 degrees north a degree of longitude is about 85km
 * against 111km for latitude. Correcting by cos(lat) keeps the box the radius the
 * caller actually asked for instead of silently over-wide east to west.
 */
export function bboxAround(lat: number, lon: number, radiusKm: number): string {
  const dLat = radiusKm / 111.32;
  const cos = Math.cos((lat * Math.PI) / 180);
  const dLon = radiusKm / (111.32 * Math.max(cos, 0.01));
  const minLon = (lon - dLon).toFixed(4);
  const minLat = (lat - dLat).toFixed(4);
  const maxLon = (lon + dLon).toFixed(4);
  const maxLat = (lat + dLat).toFixed(4);
  return `${minLon},${minLat},${maxLon},${maxLat}`;
}

async function swdiFetch(
  dataset: string,
  start: Date,
  end: Date,
  bbox?: string
): Promise<{ result: Array<Record<string, unknown>>; count: number }> {
  if (KNOWN_BROKEN.has(dataset)) {
    throw new Error(
      `SWDI dataset "${dataset}" returned HTTP 500 when last measured (2026-08-19). ` +
        `Re-verify before using it.`
    );
  }
  const qs = bbox ? `?bbox=${bbox}` : "";
  const url = `${SWDI_BASE}/${dataset}/${yyyymmdd(start)}:${yyyymmdd(end)}${qs}`;

  const res = await fetch(url, { headers: { "User-Agent": USER_AGENT } });
  if (!res.ok) {
    throw new Error(`SWDI ${dataset} -> ${res.status}`);
  }
  const body = (await res.json()) as {
    result?: Array<Record<string, unknown>>;
    summary?: { count?: number };
  };
  return {
    result: body.result ?? [],
    count: body.summary?.count ?? (body.result ?? []).length,
  };
}

/**
 * Historical hail lookup for one property.
 *
 * @param radiusKm how close a signature must be to count. Hail swaths are narrow,
 *   so a wide radius produces false positives; 5-10km is a defensible default and
 *   the caller should state whatever it picks in any report shown to a homeowner.
 */
export async function hailHistoryForPoint(
  lat: number,
  lon: number,
  start: Date,
  end: Date,
  radiusKm = 8
): Promise<HailSignature[]> {
  const { result } = await swdiFetch("nx3hail", start, end, bboxAround(lat, lon, radiusKm));

  return result
    .map((r) => {
      const { lat: pLat, lon: pLon } = parsePoint(r.SHAPE as string | undefined);
      return {
        maxSizeInches: num(r.MAXSIZE),
        probability: num(r.PROB),
        severeProbability: num(r.SEVPROB),
        observedAt: (r.ZTIME as string) ?? "",
        latitude: pLat,
        longitude: pLon,
        radarStation: (r.WSR_ID as string) ?? null,
        cellId: (r.CELL_ID as string) ?? null,
      };
    })
    .sort((a, b) => (b.maxSizeInches ?? 0) - (a.maxSizeInches ?? 0));
}

/** The headline a roofer actually wants: worst hail over this address in a window. */
export function worstHail(signatures: HailSignature[]): HailSignature | null {
  return signatures.length ? signatures[0] : null;
}

/**
 * Roofing-damage threshold. 1 inch is the commonly cited size at which asphalt
 * shingles begin to sustain damage, so it is a useful triage cut. Stated as a
 * DEFAULT and not a fact about any particular roof, because material, age and
 * impact angle all matter and this module cannot see any of them.
 */
export const SHINGLE_DAMAGE_THRESHOLD_INCHES = 1.0;

export function likelyDamaging(
  signatures: HailSignature[],
  thresholdInches = SHINGLE_DAMAGE_THRESHOLD_INCHES
): boolean {
  return signatures.some((s) => (s.maxSizeInches ?? 0) >= thresholdInches);
}
