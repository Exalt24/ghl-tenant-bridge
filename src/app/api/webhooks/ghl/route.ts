/**
 * GHL inbound webhook receiver.
 *
 * The contract this route is written to, which is ASC-Edge's own standard:
 *   "A feature is not finished merely because a webhook was sent. It is finished
 *    when the correct information reaches the correct destination, produces the
 *    intended result, handles failures safely, and can be verified."
 *
 * So the pipeline is explicit and ordered, and every stage can reject:
 *
 *   1. RAW BYTES        read the body before anything parses it
 *   2. SIGNATURE        Ed25519 preferred, RSA legacy until 2026-09-01, fail closed
 *   3. REPLAY WINDOW    reject anything older than 5 minutes
 *   4. TENANT RESOLVE   GHL location_id -> our org_id, unknown location = reject
 *   5. IDEMPOTENCY      unique (provider, event_id) in Postgres, replay = no-op
 *   6. APPLY            write inside the resolved tenant only
 *   7. FAILURE PATH     record the error, bump attempts, dead-letter after N
 *
 * Ordering is deliberate. Verifying before parsing means a spoofed payload never
 * reaches the JSON parser. Resolving the tenant before applying means a payload
 * cannot choose its own destination. Idempotency in the DATABASE rather than a
 * cache means it survives a cold Redis and two concurrent deliveries landing on
 * different instances.
 */

import { createClient } from "@supabase/supabase-js";
import { verifyGhlWebhook } from "@/lib/ghl-signature";
import {
  classifyInbound,
  verifyWorkflowSecret,
  verifyWorkflowBearer,
  isFallbackPath,
} from "@/lib/workflow-auth";

export const runtime = "nodejs"; // needs node:crypto, not the edge runtime

const REPLAY_WINDOW_MS = 5 * 60 * 1000;
const MAX_ATTEMPTS = 5;

/** Service-role client. NEVER import this into anything client-reachable: the
 *  service role bypasses RLS by design, which is precisely why every tenant
 *  decision below is made explicitly in code rather than left to the database. */
function serviceClient() {
  return createClient(
    process.env.NEXT_PUBLIC_SUPABASE_URL!,
    process.env.SUPABASE_SERVICE_ROLE_KEY!,
    { auth: { persistSession: false } }
  );
}

function headerMap(req: Request): Record<string, string> {
  const out: Record<string, string> = {};
  req.headers.forEach((v, k) => {
    out[k.toLowerCase()] = v;
  });
  return out;
}

/** GHL payload shapes vary by event; pull the identifiers defensively. */
function extractIds(body: Record<string, unknown>) {
  const eventId =
    (body.webhookId as string) ??
    (body.id as string) ??
    (body.eventId as string) ??
    null;
  const locationId =
    (body.locationId as string) ??
    (body.location_id as string) ??
    ((body.location as Record<string, unknown> | undefined)?.id as string) ??
    null;
  const eventType = (body.type as string) ?? (body.event as string) ?? null;
  const timestamp =
    (body.timestamp as string) ??
    (body.dateAdded as string) ??
    (body.createdAt as string) ??
    null;
  return { eventId, locationId, eventType, timestamp };
}

export async function POST(req: Request) {
  // --- 1. raw bytes -------------------------------------------------------
  // Re-serialising parsed JSON changes key order and whitespace, which breaks
  // every signature scheme. This is the single most common way webhook
  // verification silently fails.
  const raw = Buffer.from(await req.arrayBuffer());
  const headers = headerMap(req);

  // --- 2. authenticate ----------------------------------------------------
  // GHL has TWO inbound paths with different security properties, and which one
  // this is must be decided by the SIGNATURE HEADER, never by the URL or a query
  // param (both attacker-controlled). Otherwise a caller could downgrade the
  // signed marketplace path to the weaker shared-secret path just by omitting the
  // signature header.
  const path = classifyInbound(headers);
  let authAlg: string | undefined;

  if (path === "marketplace-app") {
    const sig = verifyGhlWebhook(raw, headers);
    if (!sig.ok) {
      // 401 with no detail in the body. Telling a prober WHY it failed helps them.
      console.warn("[ghl-webhook] rejected signed request", { reason: sig.reason, alg: sig.alg });
      return Response.json({ error: "invalid signature" }, { status: 401 });
    }
    authAlg = sig.alg;
  } else {
    // Workflow "Custom Webhook" action: no GHL signature is documented on this
    // path, so authentication is a secret WE supply and the workflow sends back.
    // Either a custom header or a bearer token satisfies it; both are compared in
    // constant time and both fail closed when no secret is configured.
    const bySecret = verifyWorkflowSecret(headers);
    const byBearer = bySecret.ok ? bySecret : verifyWorkflowBearer(headers);
    if (!byBearer.ok) {
      console.warn("[ghl-webhook] rejected workflow request", { reason: byBearer.reason });
      return Response.json({ error: "unauthorized" }, { status: 401 });
    }
    authAlg = "workflow-shared-secret";
  }

  // Make reliance on the PREMIUM, unsigned, unretried fallback path VISIBLE in ops.
  // It costs $0.01 per execution, can be toggled off per sub-account, and on a
  // sandbox it runs on trial access. Silent dependence on it is how a demo dies in
  // front of a client, so every fallback delivery is logged as one.
  if (isFallbackPath(path)) {
    console.warn(
      "[ghl-webhook] FALLBACK PATH: unsigned workflow-action delivery (premium, " +
        "no retries, no delivery log). Prefer a Marketplace app subscription where " +
        "the event exists. See docs/DEMO_PORTABILITY.md"
    );
  }

  let body: Record<string, unknown>;
  try {
    body = JSON.parse(raw.toString("utf8"));
  } catch {
    return Response.json({ error: "malformed json" }, { status: 400 });
  }

  const { eventId, locationId, eventType, timestamp } = extractIds(body);

  if (!eventId) {
    // Without a stable id there is no idempotency key, so a retry would double
    // apply. Refuse rather than guess.
    return Response.json({ error: "missing event id" }, { status: 400 });
  }
  if (!locationId) {
    return Response.json({ error: "missing location id" }, { status: 400 });
  }

  // --- 3. replay window ---------------------------------------------------
  if (timestamp) {
    const sentMs = Date.parse(timestamp);
    if (!Number.isNaN(sentMs) && Math.abs(Date.now() - sentMs) > REPLAY_WINDOW_MS) {
      return Response.json({ error: "timestamp outside replay window" }, { status: 401 });
    }
  }

  const db = serviceClient();

  // --- 4. tenant resolve --------------------------------------------------
  // A valid signature proves the message came from GHL. It does NOT say which of
  // OUR tenants it belongs to. An unmapped location is refused: silently creating
  // one would let an unknown sub-account seed data into our system.
  const { data: mapping, error: mapErr } = await db
    .from("ghl_locations")
    .select("location_id, org_id")
    .eq("location_id", locationId)
    .maybeSingle();

  if (mapErr) {
    return Response.json({ error: "tenant lookup failed" }, { status: 500 });
  }
  if (!mapping) {
    // 202 on purpose: the delivery was authentic and well-formed, so GHL should
    // not keep retrying it. It is simply not for us.
    console.warn("[ghl-webhook] unmapped location", { locationId, eventType });
    return Response.json({ status: "ignored", reason: "unmapped location" }, { status: 202 });
  }

  // --- 5. idempotency -----------------------------------------------------
  // The unique index on (provider, event_id) is the mechanism. Losing this insert
  // race IS the duplicate check, so two concurrent deliveries cannot both proceed.
  const { error: insErr } = await db.from("webhook_events").insert({
    provider: "ghl",
    event_id: eventId,
    event_type: eventType,
    location_id: locationId,
    org_id: mapping.org_id,
    signature_alg: authAlg,
    payload: body,
    status: "received",
  });

  if (insErr) {
    // 23505 = unique_violation, i.e. we have already seen this event.
    if ((insErr as { code?: string }).code === "23505") {
      return Response.json({ status: "duplicate", eventId }, { status: 200 });
    }
    return Response.json({ error: "ledger write failed" }, { status: 500 });
  }

  // --- 6. apply + 7. failure path ----------------------------------------
  try {
    await applyEvent(db, mapping.org_id, locationId, eventType, body);
    await db
      .from("webhook_events")
      .update({ status: "processed", processed_at: new Date().toISOString() })
      .eq("provider", "ghl")
      .eq("event_id", eventId);
    return Response.json({ status: "processed", eventId }, { status: 200 });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);

    const { data: row } = await db
      .from("webhook_events")
      .select("attempts")
      .eq("provider", "ghl")
      .eq("event_id", eventId)
      .maybeSingle();

    const attempts = (row?.attempts ?? 0) + 1;
    const dead = attempts >= MAX_ATTEMPTS;

    await db
      .from("webhook_events")
      .update({ attempts, last_error: message.slice(0, 800), status: dead ? "dead" : "failed" })
      .eq("provider", "ghl")
      .eq("event_id", eventId);

    // A dead-lettered event returns 200 so GHL stops retrying a message that will
    // never succeed; it is now OUR problem, visible in the ledger. A retryable
    // failure returns 500 so the provider retries with its own backoff.
    return Response.json(
      { status: dead ? "dead-letter" : "retry", eventId, attempts },
      { status: dead ? 200 : 500 }
    );
  }
}

type Db = ReturnType<typeof serviceClient>;

async function applyEvent(
  db: Db,
  orgId: string,
  locationId: string,
  eventType: string | null,
  body: Record<string, unknown>
): Promise<void> {
  const contact = (body.contact as Record<string, unknown>) ?? body;
  const ghlContactId = (contact.id as string) ?? (contact.contactId as string) ?? null;
  if (!ghlContactId) {
    throw new Error("no contact id in payload for event type " + String(eventType));
  }

  // org_id and location_id come from OUR mapping, never from the payload. If the
  // payload could name its own org, a valid signature from any GHL agency would
  // be a cross-tenant write.
  const { error } = await db.from("ghl_contacts").upsert(
    {
      org_id: orgId,
      location_id: locationId,
      ghl_contact_id: ghlContactId,
      first_name: (contact.firstName as string) ?? null,
      last_name: (contact.lastName as string) ?? null,
      email: (contact.email as string) ?? null,
      phone: (contact.phone as string) ?? null,
      tags: (contact.tags as string[]) ?? [],
      raw: contact,
      synced_at: new Date().toISOString(),
    },
    { onConflict: "location_id,ghl_contact_id" }
  );

  if (error) throw new Error("contact upsert failed: " + error.message);
}

/** Health endpoint. Surfaces the legacy-signature cutover as an operational
 *  number so it is monitored rather than remembered. */
export async function GET() {
  const { daysUntilLegacySunset, LEGACY_SIGNATURE_SUNSET } = await import("@/lib/ghl-signature");
  return Response.json({
    ok: true,
    legacySignatureSunset: LEGACY_SIGNATURE_SUNSET.toISOString(),
    daysUntilLegacySunset: daysUntilLegacySunset(),
    preferredHeader: "X-GHL-Signature",
  });
}
