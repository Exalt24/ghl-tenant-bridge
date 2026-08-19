/**
 * hosted-supabase.e2e.mjs -- the real transport against a real hosted Supabase project.
 *
 * WHY THIS SUITE EXISTS
 *   Every other outbox suite runs against a fake transport or a local Postgres shim. A
 *   shim is a fidelity approximation: it proved the SQL was coherent, never that
 *   PostgREST, Storage and RLS behave as assumed on the hosted product. Worse,
 *   outbox-transport-supabase.ts upserts `client_uuid` and no migration had ever
 *   created a table with that column, so the module had never touched real schema.
 *
 * WHAT IS DELIBERATELY NOT MOCKED
 *   This imports the SHIPPING transport (createSupabaseOutboxTransport) rather than
 *   reimplementing its calls. A test that re-writes the upsert proves the test author
 *   understood the API, not that the shipped code works.
 *
 * THE ASSERTION THAT MATTERS
 *   Tenant B is a member of a DIFFERENT org, so its write must be refused. Anon-only
 *   negative tests are much weaker: anon is a member of nothing, so it can pass while a
 *   real cross-tenant hole stays open.
 *
 * Requires hosted_env.json (URL, anon key, service key, both orgs, both users). Run:
 *   node tests/hosted-supabase.e2e.mjs
 */
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

import { createSupabaseOutboxTransport, objectPathFor } from "../src/lib/outbox-transport-supabase.ts";

const require = createRequire(import.meta.url);
const { createClient } = require("@supabase/supabase-js");

const ENV_PATH = process.env.HOSTED_ENV
  ?? "C:/Users/Dax/AppData/Local/Temp/claude/C--Projects-Professional/491b0370-1067-441c-a628-29b572867fed/scratchpad/hosted_env.json";
const env = JSON.parse(readFileSync(ENV_PATH, "utf8"));

const fails = [];
const check = (name, ok, detail = "") => {
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${name}${ok ? "" : `  (${detail})`}`);
  if (!ok) fails.push(name);
};

const BUCKET = "field-photos";
const TABLE = "field_captures";

async function signedInClient(email) {
  const c = createClient(env.NEXT_PUBLIC_SUPABASE_URL, env.NEXT_PUBLIC_SUPABASE_ANON_KEY, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
  const { data, error } = await c.auth.signInWithPassword({
    email,
    password: env.DEMO_PASSWORD,
  });
  if (error) throw new Error(`sign-in failed for ${email}: ${error.message}`);
  return { client: c, userId: data.user.id };
}

// A tiny but REAL jpeg. A 1x1 PNG mislabelled image/jpeg is what made an earlier run
// report the page broken when the fixture was, so the bucket's jpeg-only mime filter is
// treated as a thing to satisfy honestly rather than route around.
function realJpeg() {
  // Minimal baseline JPEG: SOI, APP0/JFIF, DQT, SOF0, DHT, SOS, EOI (8x8 grey).
  return new Uint8Array([
    0xff, 0xd8, 0xff, 0xe0, 0x00, 0x10, 0x4a, 0x46, 0x49, 0x46, 0x00, 0x01, 0x01, 0x00,
    0x00, 0x01, 0x00, 0x01, 0x00, 0x00, 0xff, 0xdb, 0x00, 0x43, 0x00,
    ...Array(64).fill(0x10),
    0xff, 0xc0, 0x00, 0x0b, 0x08, 0x00, 0x08, 0x00, 0x08, 0x01, 0x01, 0x11, 0x00,
    0xff, 0xc4, 0x00, 0x1f, 0x00, 0x00, 0x01, 0x05, 0x01, 0x01, 0x01, 0x01, 0x01, 0x01,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04, 0x05,
    0x06, 0x07, 0x08, 0x09, 0x0a, 0x0b,
    0xff, 0xc4, 0x00, 0x14, 0x10, 0x01, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0xff, 0xda, 0x00, 0x08, 0x01, 0x01, 0x00, 0x00, 0x3f, 0x00, 0xd2, 0xcf, 0x20,
    0xff, 0xd9,
  ]);
}

// UUIDv7-shaped, unique per run so a rerun does not collide on the unique index.
function uuidv7() {
  const ms = Date.now();
  const hex = ms.toString(16).padStart(12, "0");
  const r = () => Math.floor(Math.random() * 16).toString(16);
  const rand = Array.from({ length: 18 }, r).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-7${rand.slice(0, 3)}-8${rand.slice(3, 6)}-${rand.slice(6, 18)}`;
}

const A = await signedInClient(env.DEMO_EMAIL);
const B = await signedInClient(env.DEMO_EMAIL_B);
console.log(`signed in: A=${A.userId.slice(0, 8)} (org A)  B=${B.userId.slice(0, 8)} (org B)`);

const transportA = createSupabaseOutboxTransport({ client: A.client, bucket: BUCKET, table: TABLE });
const transportB = createSupabaseOutboxTransport({ client: B.client, bucket: BUCKET, table: TABLE });

// ---------------------------------------------------------------- the happy path
const item = {
  id: uuidv7(),
  kind: "photo",
  payload: {
    org_id: env.ORG_A,
    captured_lat: 32.78,
    captured_lon: -96.8,
    original_name: "ridge-line.jpg",
    original_bytes: 0,
  },
  bytesRef: "x",
  capturedAt: Date.now(),
  attempts: 0,
  state: "pending",
  lastError: null,
  nextAttemptAt: 0,
};
const bytes = realJpeg();
item.payload.original_bytes = bytes.byteLength;

console.log("\n=== tenant A: the full path through the SHIPPING transport ===");
let upAck;
try {
  upAck = await transportA.uploadBytes(item, bytes);
  check("uploadBytes returned a verified byte count", upAck.bytesStored === bytes.byteLength,
    `stored=${upAck?.bytesStored} sent=${bytes.byteLength}`);
} catch (e) {
  check("uploadBytes succeeded", false, e.message);
}

try {
  const ack = await transportA.upsertRow(item);
  check("upsertRow reported rowsWritten = 1", ack.rowsWritten === 1, JSON.stringify(ack));
} catch (e) {
  check("upsertRow succeeded", false, e.message);
}

// Idempotency: the same item again must not create a second row.
try {
  const again = await transportA.upsertRow(item);
  check("re-upserting the same client_uuid still reports 1 (idempotent)",
    again.rowsWritten === 1, JSON.stringify(again));
} catch (e) {
  check("second upsert succeeded", false, e.message);
}

{
  const { data, error } = await A.client.from(TABLE).select("client_uuid").eq("client_uuid", item.id);
  check("exactly ONE row exists for that key", !error && data?.length === 1,
    error?.message ?? `rows=${data?.length}`);
}

// The bytes are really in the bucket, at the deterministic path.
{
  const path = objectPathFor(item);
  const { data, error } = await A.client.storage.from(BUCKET).download(path);
  const size = data ? (await data.arrayBuffer()).byteLength : -1;
  check("the photo bytes are downloadable at the deterministic path",
    !error && size === bytes.byteLength, error?.message ?? `size=${size}`);
}

// ---------------------------------------------------------------- cross-tenant
console.log("\n=== tenant B (a member of a DIFFERENT org): must be refused ===");
{
  const { data, error } = await B.client.from(TABLE).select("client_uuid").eq("client_uuid", item.id);
  check("B cannot SEE A's row", !error && data?.length === 0,
    error?.message ?? `rows=${data?.length}`);
}

// B writing into A's org. This is the real hole a using-only policy would leave.
const stolen = { ...item, id: uuidv7() };
let bRefused = false;
try {
  const ack = await transportB.upsertRow(stolen);
  // Either it throws (WITH CHECK raises) or it reports zero rows. Both are a refusal;
  // what must never happen is rowsWritten >= 1.
  bRefused = ack.rowsWritten === 0;
  check("B's write into A's org is refused (0 rows, not an error)", bRefused, JSON.stringify(ack));
} catch (e) {
  bRefused = true;
  check("B's write into A's org is refused (raised)", true, e.message.slice(0, 60));
}
{
  const { data } = await A.client.from(TABLE).select("client_uuid").eq("client_uuid", stolen.id);
  check("and nothing landed in A's org from B", (data?.length ?? 0) === 0, `rows=${data?.length}`);
}

// B uploading into A's storage folder.
try {
  await transportB.uploadBytes({ ...stolen, payload: { org_id: env.ORG_A } }, bytes);
  check("B cannot upload into A's storage folder", false, "upload SUCCEEDED, that is a hole");
} catch (e) {
  check("B cannot upload into A's storage folder", true, e.message.slice(0, 50));
}

// ---------------------------------------------------------------- positive control
// If B were simply broken, every negative above would pass for the wrong reason.
console.log("\n=== positive control: B still works inside its OWN org ===");
const own = { ...item, id: uuidv7(), payload: { ...item.payload, org_id: env.ORG_B } };
try {
  const ack = await transportB.upsertRow(own);
  check("B CAN write to its own org (so the refusals were real)", ack.rowsWritten === 1,
    JSON.stringify(ack));
} catch (e) {
  check("B CAN write to its own org", false, e.message);
}

console.log(`\n${fails.length === 0 ? "HOSTED SUPABASE PATH PROVEN END TO END" : `${fails.length} FAILED`}`);
process.exit(fails.length ? 1 : 0);
