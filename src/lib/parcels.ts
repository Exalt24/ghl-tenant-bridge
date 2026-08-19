/**
 * parcels.ts -- county parcel lookup against public ArcGIS FeatureServer /
 * MapServer endpoints.
 *
 * WHY COUNTY ENDPOINTS
 * --------------------
 * There is no national parcel API that is both free and queryable. The national
 * layer in ArcGIS Living Atlas is raster TILES (capabilities: Map,TilesOnly,
 * Tilemap) so nothing can be queried out of it. Regrid covers every county but its
 * terms draw an undefined line between caching "parcel records" and caching "the
 * Data". ATTOM imposes a 24-hour cache ceiling AND forbids using its content "to
 * create, enhance or structure any database", which is disqualifying for anything
 * with a persistent property store.
 *
 * County and state ArcGIS services are authoritative, free, keyless, and their only
 * real cost is that every one has a different schema. So this module is deliberately
 * CONFIGURED per source rather than pretending a single national shape exists.
 *
 * FOUR BEHAVIOURS MEASURED LIVE 2026-08-20 AGAINST WAKE COUNTY NC, ALL OF WHICH
 * PRODUCE SILENT WRONG ANSWERS IF IGNORED
 * ---------------------------------------------------------------------------
 * 1. A POINT QUERY RETURNS ZERO FEATURES AND NO ERROR.
 *    geometry=-78.641826,35.776949 with esriGeometryPoint + Intersects returned
 *    {"features": []}. The SAME coordinate as a ~20m envelope returned 4 parcels
 *    including "330 S SALISBURY ST". This is the one that matters most: a
 *    point-in-polygon design reports "no parcel for this address" across an entire
 *    dataset and nothing in the response says anything is wrong. Hence
 *    queryParcelsNearPoint() only ever sends an ENVELOPE.
 *
 * 2. A DEAD SERVICE RETURNS HTTP 200.
 *    A nonexistent service path returned status 200 with a body of
 *    {"error":{"code":404,"message":"Service not found"}}. So res.ok is not a
 *    health check; the BODY has to be inspected. assertArcgisOk() below exists
 *    solely for that.
 *
 * 3. AN UNBOUNDED QUERY CAPS SILENTLY-ISH.
 *    where=1=1 returned exactly 2000 features with exceededTransferLimit: true.
 *    The flag is the only signal, so pagination keys off it and every paged query
 *    sets orderByFields, because without a deterministic order paging can repeat
 *    or skip rows.
 *
 * 4. A SMALL ENVELOPE RETURNS SEVERAL PARCELS, NOT ONE.
 *    That 20m box returned 4. Downtown parcels are small and a geocoded point is
 *    interpolated to the block face anyway, so the caller must CHOOSE. This module
 *    returns all candidates and refuses to guess; picking one is a decision with
 *    consequences and it belongs to the caller.
 */

/** Degrees of latitude per metre. Good to well under a metre at any latitude, since
 *  latitude degrees barely vary. */
const DEG_LAT_PER_M = 1 / 111_320;

export interface ParcelSource {
  /** Human label, used in errors so a failure names the county. */
  name: string;
  /** Full layer query URL, e.g. https://host/arcgis/rest/services/X/MapServer/0 */
  layerUrl: string;
  /** Fields to request. Every county names these differently, which is the whole
   *  reason this is configuration and not a constant. */
  outFields: string[];
  /** Server page size. Wake reports maxRecordCount 2000. */
  maxRecordCount?: number;
  /** A field guaranteed to exist, used for a stable paging order. */
  orderByField?: string;
}

/** Verified live 2026-08-20: 437,674 parcels, ArcGIS 11.5, native wkid 2264 (NC
 *  State Plane feet), maxRecordCount 2000, supports JSON/geoJSON/PBF. Kept as a
 *  known-good source so the tests exercise a real service rather than a mock. */
export const WAKE_COUNTY_NC: ParcelSource = {
  name: "Wake County NC",
  layerUrl: "https://maps.wakegov.com/arcgis/rest/services/Property/Parcels/MapServer/0",
  outFields: ["PIN_NUM", "SITE_ADDRESS", "YEAR_BUILT", "HEATEDAREA", "DEED_ACRES"],
  maxRecordCount: 2000,
  orderByField: "OBJECTID",
};

export interface Parcel {
  attributes: Record<string, unknown>;
}

export interface ParcelQueryResult {
  parcels: Parcel[];
  /** True when the server capped the page. The caller has NOT seen everything. */
  truncated: boolean;
  /** The envelope actually sent, so a caller can log why it matched what it did. */
  envelope: { minLon: number; minLat: number; maxLon: number; maxLat: number };
}

/**
 * Throws when an ArcGIS response carries an error, INCLUDING the case where the
 * HTTP status is 200. Measured: a missing service is 200 + {"error":{"code":404}}.
 */
export function assertArcgisOk(body: unknown, context: string): void {
  if (body && typeof body === "object" && "error" in body) {
    const e = (body as { error?: { code?: number; message?: string } }).error;
    throw new Error(
      `${context}: ArcGIS returned an error inside a 200 response ` +
        `(code ${e?.code ?? "?"}: ${e?.message ?? "unknown"})`
    );
  }
}

/**
 * Build an envelope of `radiusMetres` around a point.
 *
 * Longitude degrees shrink with cos(latitude), and the cosine is floored so a
 * near-polar coordinate cannot divide by ~zero. Same correction as bboxAround() in
 * swdi.ts, deliberately, because two different bbox maths in one codebase is how
 * they drift apart.
 */
export function envelopeAround(lat: number, lon: number, radiusMetres: number) {
  const dLat = radiusMetres * DEG_LAT_PER_M;
  const cos = Math.max(Math.cos((lat * Math.PI) / 180), 0.01);
  const dLon = dLat / cos;
  return {
    minLon: lon - dLon,
    minLat: lat - dLat,
    maxLon: lon + dLon,
    maxLat: lat + dLat,
  };
}

/**
 * Parcels intersecting a small envelope around a point.
 *
 * ALWAYS an envelope, never a bare point. A point query against these services
 * returns zero features with no error (measured), so the "obvious" implementation
 * fails silently and universally.
 *
 * Returns every candidate. It does not pick one, because a geocoded point is
 * interpolated to the block face and a 20m box in a dense area legitimately
 * contains several parcels; choosing among them is the caller's call.
 */
export async function queryParcelsNearPoint(
  source: ParcelSource,
  lat: number,
  lon: number,
  radiusMetres: number,
  opts: { userAgent: string; timeoutMs?: number }
): Promise<ParcelQueryResult> {
  if (radiusMetres <= 0) {
    throw new Error("queryParcelsNearPoint: radiusMetres must be > 0, a point query silently matches nothing");
  }

  const env = envelopeAround(lat, lon, radiusMetres);
  const params = new URLSearchParams({
    geometry: `${env.minLon},${env.minLat},${env.maxLon},${env.maxLat}`,
    geometryType: "esriGeometryEnvelope",
    inSR: "4326",
    outSR: "4326",
    spatialRel: "esriSpatialRelIntersects",
    outFields: source.outFields.join(","),
    returnGeometry: "false",
    f: "json",
  });
  if (source.orderByField) params.set("orderByFields", source.orderByField);

  const res = await fetch(`${source.layerUrl}/query?${params.toString()}`, {
    headers: { "User-Agent": opts.userAgent, Accept: "application/json" },
    signal: AbortSignal.timeout(opts.timeoutMs ?? 45_000),
  });
  if (!res.ok) throw new Error(`${source.name}: HTTP ${res.status}`);

  const body = (await res.json()) as {
    features?: Array<{ attributes?: Record<string, unknown> }>;
    exceededTransferLimit?: boolean;
    error?: unknown;
  };
  // Before touching features: a 200 can be a 404 in disguise.
  assertArcgisOk(body, source.name);

  const features = body.features ?? [];
  return {
    parcels: features.map((f) => ({ attributes: f.attributes ?? {} })),
    truncated: body.exceededTransferLimit === true,
    envelope: env,
  };
}

/**
 * Count matching rows without transferring them.
 *
 * returnCountOnly is the cheap way to size a query before deciding whether to page
 * it. Verified against Wake: returns { count: 437674 } for where=1=1.
 */
export async function countParcels(
  source: ParcelSource,
  where: string,
  opts: { userAgent: string; timeoutMs?: number }
): Promise<number> {
  const params = new URLSearchParams({ where, returnCountOnly: "true", f: "json" });
  const res = await fetch(`${source.layerUrl}/query?${params.toString()}`, {
    headers: { "User-Agent": opts.userAgent, Accept: "application/json" },
    signal: AbortSignal.timeout(opts.timeoutMs ?? 45_000),
  });
  if (!res.ok) throw new Error(`${source.name}: HTTP ${res.status}`);
  const body = (await res.json()) as { count?: number; error?: unknown };
  assertArcgisOk(body, source.name);
  if (typeof body.count !== "number") {
    throw new Error(`${source.name}: returnCountOnly gave no numeric count`);
  }
  return body.count;
}

/**
 * Page through a where-clause query until the server stops truncating.
 *
 * orderByFields is mandatory here. Without a deterministic sort, resultOffset
 * paging over an unordered set can return the same row twice and miss another
 * entirely, and nothing in the response would reveal it.
 *
 * maxPages is a hard stop so a bad offset parameter cannot loop forever. When it
 * trips, the result says so rather than quietly looking complete, because a
 * truncated result that reads as complete is the failure mode this whole file is
 * written against.
 */
export async function queryParcelsPaged(
  source: ParcelSource,
  where: string,
  opts: { userAgent: string; timeoutMs?: number; maxPages?: number }
): Promise<{ parcels: Parcel[]; pages: number; hitPageCap: boolean }> {
  if (!source.orderByField) {
    throw new Error(
      `${source.name}: paging needs orderByField, otherwise offsets can repeat or skip rows`
    );
  }
  const pageSize = source.maxRecordCount ?? 1000;
  const maxPages = opts.maxPages ?? 50;
  const parcels: Parcel[] = [];
  let pages = 0;

  for (; pages < maxPages; pages++) {
    const params = new URLSearchParams({
      where,
      outFields: source.outFields.join(","),
      orderByFields: source.orderByField,
      resultOffset: String(pages * pageSize),
      resultRecordCount: String(pageSize),
      returnGeometry: "false",
      f: "json",
    });
    const res = await fetch(`${source.layerUrl}/query?${params.toString()}`, {
      headers: { "User-Agent": opts.userAgent, Accept: "application/json" },
      signal: AbortSignal.timeout(opts.timeoutMs ?? 45_000),
    });
    if (!res.ok) throw new Error(`${source.name}: HTTP ${res.status} on page ${pages}`);
    const body = (await res.json()) as {
      features?: Array<{ attributes?: Record<string, unknown> }>;
      exceededTransferLimit?: boolean;
      error?: unknown;
    };
    assertArcgisOk(body, `${source.name} page ${pages}`);

    const features = body.features ?? [];
    parcels.push(...features.map((f) => ({ attributes: f.attributes ?? {} })));

    // A short page means the end. The transfer-limit flag alone is not enough,
    // because the last full page can still carry it.
    if (features.length < pageSize) {
      pages++;
      return { parcels, pages, hitPageCap: false };
    }
  }
  return { parcels, pages, hitPageCap: true };
}
