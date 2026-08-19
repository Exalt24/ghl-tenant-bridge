/**
 * Authentication for the GHL WORKFLOW "Custom Webhook" action path.
 *
 * ============================== WHY THIS EXISTS ==============================
 * GHL has exactly TWO ways to push an event at you, and they have completely
 * different security properties. Verified 2026-08-19:
 *
 *  1. MARKETPLACE APP webhook subscriptions (configured in the Developer Portal).
 *     SIGNED with X-GHL-Signature (Ed25519) / legacy X-WH-Signature (RSA-SHA256).
 *     Payloads carry `timestamp` and `webhookId`. Gets GHL's delivery
 *     infrastructure: 30-day logs, manual replay, and 12 retries with exponential
 *     backoff and jitter. Handled by src/lib/ghl-signature.ts.
 *
 *  2. WORKFLOW "Custom Webhook" ACTION (configured inside a sub-account).
 *     **UNSIGNED. CONFIRMED BY LIVE CAPTURE on 2026-08-20**, not inferred.
 *     A workflow was built and published in the sandbox, triggered via
 *     POST /contacts/{contactId}/workflow/{workflowId} (HTTP 201), and the
 *     delivery captured. GHL sent EXACTLY six headers, from 104.154.217.87
 *     (Google Cloud), user-agent axios/0.21.4:
 *
 *       accept, content-length, content-type, host, user-agent,
 *       x-bridge-secret   <-- the header WE configured, nothing of GHL's
 *
 *     No X-GHL-Signature. No X-WH-Signature. No timestamp. No HMAC. Every header
 *     was dumped rather than grepped for the two expected ones, precisely so an
 *     unfamiliar scheme could not produce a false "unsigned" conclusion.
 *
 *     THE PAYLOAD IS ALSO MINIMAL: {id, name, email, phone} only. No webhookId,
 *     no timestamp, no locationId. So on this path there is NO idempotency key
 *     (a retry would double-apply), the 5-minute replay window CANNOT fire, and
 *     the tenant cannot be resolved from the body. The receiver therefore refuses
 *     events lacking a stable id rather than inventing one silently.
 *
 *     Custom headers DO arrive intact, so a shared secret in a header is the only
 *     authentication available here, exactly as the official Custom Webhook
 *     article recommends:
 *       "If your provider asks for a custom header (e.g. X-Signature: <secret>),
 *        you can choose No auth, then add the header under Headers."
 *     It also gets NONE of the delivery infrastructure above, and it is a PREMIUM
 *     action ($0.01/execution). See docs/DEMO_PORTABILITY.md.
 *
 * WHY YOU CANNOT AVOID THIS PATH: the marketplace app event catalogue has 58
 * events (independently counted from the official docs repo) and there is **NO
 * form-submission event**. No FormSubmit, no SurveySubmit. So anything triggered
 * by a form submission has to come through a workflow action, which means the
 * unsigned path is mandatory rather than a shortcut.
 *
 * BLOG WARNING: two community posts claim GHL signs webhooks with HMAC-SHA256.
 * HMAC-SHA256 appears in no official GHL doc; their real schemes are asymmetric
 * (Ed25519 / RSA-SHA256). Those posts describe generic webhook practice, not GHL.
 * Do not implement against them.
 */

import crypto from "crypto";

export type WorkflowAuthReason =
  | "no-secret-configured"
  | "missing-header"
  | "length-mismatch"
  | "mismatch";

export interface WorkflowAuthResult {
  ok: boolean;
  reason?: WorkflowAuthReason;
}

/** Header we ask the workflow action to send. Configurable so it can be rotated
 *  or renamed per tenant without a code change. */
export const WORKFLOW_SECRET_HEADER =
  process.env.GHL_WORKFLOW_SECRET_HEADER?.toLowerCase() ?? "x-bridge-secret";

/**
 * Constant-time shared-secret check.
 *
 * `crypto.timingSafeEqual` THROWS when the two buffers differ in length, and a
 * naive try/catch around it leaks length via timing anyway. So length is compared
 * first and explicitly, and only equal-length buffers reach the constant-time
 * compare. Both branches return the same shape so a caller cannot distinguish
 * them by anything but the reason string, which is never sent to the client.
 */
export function verifyWorkflowSecret(
  headers: Record<string, string | string[] | undefined>,
  expected: string | undefined = process.env.GHL_WORKFLOW_SECRET
): WorkflowAuthResult {
  if (!expected) {
    // FAIL CLOSED. An unset secret must never mean "allow everything", which is
    // the classic misconfiguration that turns an optional check into no check.
    return { ok: false, reason: "no-secret-configured" };
  }

  const rawHeader = headers[WORKFLOW_SECRET_HEADER];
  const provided = Array.isArray(rawHeader) ? rawHeader[0] : rawHeader;
  if (!provided) {
    return { ok: false, reason: "missing-header" };
  }

  const a = Buffer.from(provided, "utf8");
  const b = Buffer.from(expected, "utf8");
  if (a.length !== b.length) {
    return { ok: false, reason: "length-mismatch" };
  }
  return crypto.timingSafeEqual(a, b) ? { ok: true } : { ok: false, reason: "mismatch" };
}

/**
 * A workflow action can send an `Authorization: Bearer <token>` instead, which is
 * one of the five documented auth modes on that action (Bearer, API Key, Basic,
 * OAuth2, or No-auth-plus-custom-headers). Same constant-time treatment.
 */
export function verifyWorkflowBearer(
  headers: Record<string, string | string[] | undefined>,
  expected: string | undefined = process.env.GHL_WORKFLOW_BEARER
): WorkflowAuthResult {
  if (!expected) return { ok: false, reason: "no-secret-configured" };
  const rawHeader = headers["authorization"];
  const value = Array.isArray(rawHeader) ? rawHeader[0] : rawHeader;
  if (!value || !value.startsWith("Bearer ")) return { ok: false, reason: "missing-header" };

  const a = Buffer.from(value.slice(7), "utf8");
  const b = Buffer.from(expected, "utf8");
  if (a.length !== b.length) return { ok: false, reason: "length-mismatch" };
  return crypto.timingSafeEqual(a, b) ? { ok: true } : { ok: false, reason: "mismatch" };
}

export type InboundPath = "marketplace-app" | "workflow-action";

/**
 * Which of the two inbound paths is this request?
 *
 * Decided by the presence of a GHL signature header, NOT by the URL or a query
 * param, because those are attacker-controlled. A request carrying a signature
 * header must be verified as a marketplace-app webhook and must NOT be allowed to
 * fall back to the weaker shared-secret check: otherwise anyone could downgrade a
 * signed path to a guessable one just by omitting the signature.
 */
export function classifyInbound(
  headers: Record<string, string | string[] | undefined>
): InboundPath {
  const ghl = headers["x-ghl-signature"];
  const legacy = headers["x-wh-signature"];
  const present = (v: string | string[] | undefined) => {
    const s = Array.isArray(v) ? v[0] : v;
    return !!s && s !== "N/A";
  };
  return present(ghl) || present(legacy) ? "marketplace-app" : "workflow-action";
}

/**
 * WHICH INBOUND PATH IS PRIMARY. This is an architecture decision, recorded here
 * rather than only in a doc, because a preference nothing references is not a
 * preference.
 *
 * 'marketplace-app' is primary because it is FREE, Ed25519-signed, retried 12x with
 * exponential backoff and jitter, and carries 30 days of replayable delivery logs.
 *
 * The workflow action is a PREMIUM feature: $0.01 per execution from the agency
 * wallet, 100 free per sub-account, and toggleable per sub-account, so it can
 * simply be switched off underneath you. It is also unsigned and its payload has no
 * webhookId/timestamp/locationId (measured 2026-08-20). Treat it as a fallback for
 * form submissions only, since no FormSubmit event exists in the 58-event catalogue.
 *
 * See docs/DEMO_PORTABILITY.md.
 */
export const PRIMARY_INBOUND_PATH: InboundPath = "marketplace-app";

/** True when a request arrived on the premium, unsigned, unretried fallback path.
 *  Callers should log this so reliance on a paid trial feature is VISIBLE in ops
 *  rather than discovered when the trial ends. */
export function isFallbackPath(path: InboundPath): boolean {
  return path !== PRIMARY_INBOUND_PATH;
}
