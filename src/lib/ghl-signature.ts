/**
 * GoHighLevel outbound-webhook signature verification.
 *
 * SOURCE: HighLevel's official Webhook Integration Guide, read 2026-08-19 at
 * https://marketplace.gohighlevel.com/docs/webhook/WebhookIntegrationGuide/
 * Not inferred, not from a blog post. Both public keys below are published there
 * and both were confirmed to load in Node crypto (RSA modulusLength 4096;
 * asymmetricKeyType 'ed25519').
 *
 *   HEADER              ALGORITHM      STATUS
 *   X-GHL-Signature     Ed25519        current, prefer when present
 *   X-WH-Signature      RSA-SHA256     LEGACY
 *
 * >>> DEPRECATION, DATED: HighLevel deprecates X-WH-Signature on 2026-09-01.
 * After that date webhooks are signed ONLY with X-GHL-Signature. Any integration
 * verifying just the legacy header stops validating on that date. <<<
 *
 * That is why this module is dual-path and prefers Ed25519: it is already correct
 * on both sides of the cutover, and it fails CLOSED rather than silently
 * accepting unverified payloads once the legacy header disappears.
 */

import crypto from "crypto";

/** Legacy RSA-SHA256 public key for X-WH-Signature (deprecated 2026-09-01). */
export const GHL_LEGACY_RSA_PUBLIC_KEY = `-----BEGIN PUBLIC KEY-----
MIICIjANBgkqhkiG9w0BAQEFAAOCAg8AMIICCgKCAgEAokvo/r9tVgcfZ5DysOSC
Frm602qYV0MaAiNnX9O8KxMbiyRKWeL9JpCpVpt4XHIcBOK4u3cLSqJGOLaPuXw6
dO0t6Q/ZVdAV5Phz+ZtzPL16iCGeK9po6D6JHBpbi989mmzMryUnQJezlYJ3DVfB
csedpinheNnyYeFXolrJvcsjDtfAeRx5ByHQmTnSdFUzuAnC9/GepgLT9SM4nCpv
uxmZMxrJt5Rw+VUaQ9B8JSvbMPpez4peKaJPZHBbU3OdeCVx5klVXXZQGNHOs8gF
3kvoV5rTnXV0IknLBXlcKKAQLZcY/Q9rG6Ifi9c+5vqlvHPCUJFT5XUGG5RKgOKU
J062fRtN+rLYZUV+BjafxQauvC8wSWeYja63VSUruvmNj8xkx2zE/Juc+yjLjTXp
IocmaiFeAO6fUtNjDeFVkhf5LNb59vECyrHD2SQIrhgXpO4Q3dVNA5rw576PwTzN
h/AMfHKIjE4xQA1SZuYJmNnmVZLIZBlQAF9Ntd03rfadZ+yDiOXCCs9FkHibELhC
HULgCsnuDJHcrGNd5/Ddm5hxGQ0ASitgHeMZ0kcIOwKDOzOU53lDza6/Y09T7sYJ
PQe7z0cvj7aE4B+Ax1ZoZGPzpJlZtGXCsu9aTEGEnKzmsFqwcSsnw3JB31IGKAyk
T1hhTiaCeIY/OwwwNUY2yvcCAwEAAQ==
-----END PUBLIC KEY-----`;

/** Current Ed25519 public key for X-GHL-Signature. */
export const GHL_ED25519_PUBLIC_KEY = `-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAi2HR1srL4o18O8BRa7gVJY7G7bupbN3H9AwJrHCDiOg=
-----END PUBLIC KEY-----`;

/** 2026-09-01T00:00:00Z, the date the legacy header stops being sent. */
export const LEGACY_SIGNATURE_SUNSET = new Date("2026-09-01T00:00:00Z");

export type SignatureAlg = "ed25519" | "rsa-legacy";

export interface VerifyResult {
  ok: boolean;
  alg?: SignatureAlg;
  /** Machine-readable reason, safe to log. Never contains the payload. */
  reason?:
    | "no-signature-header"
    | "signature-na"
    | "ed25519-verify-failed"
    | "rsa-verify-failed"
    | "legacy-after-sunset"
    | "malformed-signature";
}

/**
 * GHL sends the literal string "N/A" as the signature in some configurations,
 * which must be treated as absent rather than compared. Their own sample code
 * guards this explicitly.
 */
function isMissing(sig: string | null | undefined): boolean {
  return !sig || sig === "N/A";
}

function verifyEd25519(payload: Buffer, signatureB64: string, pem: string): boolean {
  try {
    const sig = Buffer.from(signatureB64, "base64");
    // Ed25519 takes a null digest: the algorithm hashes internally.
    return crypto.verify(null, payload, crypto.createPublicKey(pem), sig);
  } catch {
    return false;
  }
}

function verifyRsaSha256(payload: Buffer, signatureB64: string, pem: string): boolean {
  try {
    const verifier = crypto.createVerify("SHA256");
    verifier.update(payload);
    verifier.end();
    return verifier.verify(pem, signatureB64, "base64");
  } catch {
    return false;
  }
}

/**
 * Verify a GHL webhook.
 *
 * @param rawBody MUST be the exact bytes received. Re-serialising parsed JSON
 *   changes key order and whitespace and the signature will not match; this is
 *   the most common way webhook verification is broken in practice.
 * @param headers lower-cased header map.
 * @param now injectable for testing the sunset behaviour.
 */
export interface VerifyOptions {
  /** Injectable clock, so the 2026-09-01 sunset behaviour is testable. */
  now?: Date;
  /** Test-only key overrides. Production passes neither, so the published keys
   *  are used and there is no way to weaken verification via config. */
  ed25519PublicKey?: string;
  legacyRsaPublicKey?: string;
}

export function verifyGhlWebhook(
  rawBody: Buffer | string,
  headers: Record<string, string | string[] | undefined>,
  opts: VerifyOptions | Date = {}
): VerifyResult {
  // Accept a bare Date for backwards compatibility with the earlier signature.
  const options: VerifyOptions = opts instanceof Date ? { now: opts } : opts;
  const now = options.now ?? new Date();
  const edKey = options.ed25519PublicKey ?? GHL_ED25519_PUBLIC_KEY;
  const rsaKey = options.legacyRsaPublicKey ?? GHL_LEGACY_RSA_PUBLIC_KEY;

  const payload = Buffer.isBuffer(rawBody) ? rawBody : Buffer.from(rawBody, "utf8");

  const pick = (name: string): string | undefined => {
    const v = headers[name];
    return Array.isArray(v) ? v[0] : v;
  };

  const ghlSig = pick("x-ghl-signature");
  const legacySig = pick("x-wh-signature");

  // Prefer Ed25519 whenever present. HighLevel's recommended flow, and it is the
  // only header that exists after the sunset.
  if (!isMissing(ghlSig)) {
    return verifyEd25519(payload, ghlSig as string, edKey)
      ? { ok: true, alg: "ed25519" }
      : { ok: false, alg: "ed25519", reason: "ed25519-verify-failed" };
  }

  if (!isMissing(legacySig)) {
    // Fail CLOSED past the sunset. If the legacy header shows up after GHL stopped
    // sending it, something is spoofing us with a retired scheme, so accepting it
    // would be the actual vulnerability.
    if (now >= LEGACY_SIGNATURE_SUNSET) {
      return { ok: false, alg: "rsa-legacy", reason: "legacy-after-sunset" };
    }
    return verifyRsaSha256(payload, legacySig as string, rsaKey)
      ? { ok: true, alg: "rsa-legacy" }
      : { ok: false, alg: "rsa-legacy", reason: "rsa-verify-failed" };
  }

  if (ghlSig === "N/A" || legacySig === "N/A") {
    return { ok: false, reason: "signature-na" };
  }
  return { ok: false, reason: "no-signature-header" };
}

/** Days until the legacy scheme dies. Surfaced on the health endpoint so the
 *  cutover is visible in ops rather than remembered. */
export function daysUntilLegacySunset(now: Date = new Date()): number {
  return Math.ceil((LEGACY_SIGNATURE_SUNSET.getTime() - now.getTime()) / 86_400_000);
}
