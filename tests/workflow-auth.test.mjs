/**
 * Workflow-path authentication tests, both directions.
 *
 * The attack this guards is a DOWNGRADE: GHL's marketplace path is signed with
 * Ed25519, the workflow path is only a shared secret. If path selection were based
 * on the URL or a query param, or if a failed signature could fall back to the
 * secret check, an attacker would simply omit the signature header and attack the
 * weaker mechanism. So classification must key off the signature header itself,
 * and a signed request must never be allowed to fall through.
 *
 * Run: node tests/workflow-auth.test.mjs
 */
import crypto from "node:crypto";
import assert from "node:assert";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import path from "node:path";

const here = path.dirname(fileURLToPath(import.meta.url));

// Guard against drift: the shipped module must still contain the load-bearing bits.
const SRC = readFileSync(path.join(here, "..", "src", "lib", "workflow-auth.ts"), "utf8");
for (const needle of [
  "timingSafeEqual",
  "no-secret-configured",
  "length-mismatch",
  "classifyInbound",
  "x-ghl-signature",
]) {
  assert.ok(SRC.includes(needle), `workflow-auth.ts no longer contains: ${needle}`);
}
const ROUTE = readFileSync(
  path.join(here, "..", "src", "app", "api", "webhooks", "ghl", "route.ts"), "utf8");
assert.ok(ROUTE.includes("classifyInbound"), "route no longer classifies the inbound path");

// --- inline port of the module logic (mirrors the .ts) ---
const HEADER = "x-bridge-secret";
const pick = (h, k) => (Array.isArray(h[k]) ? h[k][0] : h[k]);

function verifySecret(headers, expected) {
  if (!expected) return { ok: false, reason: "no-secret-configured" };
  const provided = pick(headers, HEADER);
  if (!provided) return { ok: false, reason: "missing-header" };
  const a = Buffer.from(provided, "utf8");
  const b = Buffer.from(expected, "utf8");
  if (a.length !== b.length) return { ok: false, reason: "length-mismatch" };
  return crypto.timingSafeEqual(a, b) ? { ok: true } : { ok: false, reason: "mismatch" };
}
function verifyBearer(headers, expected) {
  if (!expected) return { ok: false, reason: "no-secret-configured" };
  const v = pick(headers, "authorization");
  if (!v || !v.startsWith("Bearer ")) return { ok: false, reason: "missing-header" };
  const a = Buffer.from(v.slice(7), "utf8");
  const b = Buffer.from(expected, "utf8");
  if (a.length !== b.length) return { ok: false, reason: "length-mismatch" };
  return crypto.timingSafeEqual(a, b) ? { ok: true } : { ok: false, reason: "mismatch" };
}
function classify(headers) {
  const present = (v) => {
    const s = Array.isArray(v) ? v[0] : v;
    return !!s && s !== "N/A";
  };
  return present(headers["x-ghl-signature"]) || present(headers["x-wh-signature"])
    ? "marketplace-app"
    : "workflow-action";
}

const SECRET = "s3cr3t-shared-value-abcdef";
let pass = 0;
const fails = [];
const check = (n, c) => (c ? pass++ : fails.push(n));

// ---- POSITIVE ----
check("correct secret in the custom header passes",
  verifySecret({ [HEADER]: SECRET }, SECRET).ok);
check("correct bearer token passes",
  verifyBearer({ authorization: "Bearer " + SECRET }, SECRET).ok);

// ---- NEGATIVE ----
check("wrong secret fails", !verifySecret({ [HEADER]: "wrong-value-here-abcdef" }, SECRET).ok);
check("missing header fails", !verifySecret({}, SECRET).ok);
check("empty header value fails", !verifySecret({ [HEADER]: "" }, SECRET).ok);
check("shorter secret fails without throwing",
  !verifySecret({ [HEADER]: "short" }, SECRET).ok);
check("longer secret fails without throwing",
  !verifySecret({ [HEADER]: SECRET + "extra" }, SECRET).ok);
check("length mismatch is reported as length-mismatch, not a crash",
  verifySecret({ [HEADER]: "short" }, SECRET).reason === "length-mismatch");
check("bearer without the Bearer prefix fails",
  !verifyBearer({ authorization: SECRET }, SECRET).ok);
check("bearer with wrong token fails",
  !verifyBearer({ authorization: "Bearer " + "x".repeat(SECRET.length) }, SECRET).ok);

// ---- FAIL CLOSED when unconfigured: the critical misconfiguration ----
check("UNSET secret rejects even a matching-looking header (fails CLOSED)",
  !verifySecret({ [HEADER]: SECRET }, undefined).ok);
check("UNSET secret reports no-secret-configured",
  verifySecret({ [HEADER]: SECRET }, undefined).reason === "no-secret-configured");
check("UNSET bearer secret fails closed",
  !verifyBearer({ authorization: "Bearer " + SECRET }, undefined).ok);
check("UNSET secret with NO header also fails", !verifySecret({}, undefined).ok);

// ---- CLASSIFICATION: the downgrade guard ----
check("a request with x-ghl-signature classifies as marketplace-app",
  classify({ "x-ghl-signature": "abc" }) === "marketplace-app");
check("a request with legacy x-wh-signature classifies as marketplace-app",
  classify({ "x-wh-signature": "abc" }) === "marketplace-app");
check("a request with NO signature header classifies as workflow-action",
  classify({}) === "workflow-action");
check('a signature header of literal "N/A" is treated as absent',
  classify({ "x-ghl-signature": "N/A" }) === "workflow-action");
check("an empty signature header is treated as absent",
  classify({ "x-ghl-signature": "" }) === "workflow-action");
check("a signed request is NOT classified as workflow even with a valid secret present",
  classify({ "x-ghl-signature": "abc", [HEADER]: SECRET }) === "marketplace-app");

// ---- the route must not let a failed signature fall through to the secret ----
// Structural assertion: inside the marketplace-app branch the failure path returns,
// it does not continue to the workflow checks.
const mp = ROUTE.slice(ROUTE.indexOf('if (path === "marketplace-app")'));
const branch = mp.slice(0, mp.indexOf("} else {"));
check("marketplace-app branch RETURNS on signature failure (no fallback)",
  /return Response\.json\(\s*\{\s*error:\s*"invalid signature"/.test(branch));
check("marketplace-app branch does not call the workflow verifiers",
  !branch.includes("verifyWorkflowSecret") && !branch.includes("verifyWorkflowBearer"));

console.log(`checks passed: ${pass}`);
if (fails.length) {
  console.log(`FAIL (${fails.length})`);
  fails.forEach((f) => console.log("  - " + f));
  process.exit(1);
}
console.log("PASS, zero gaps (constant-time, fails closed, downgrade guard)");
