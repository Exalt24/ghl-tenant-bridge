// Shared test helpers: a real Ed25519 key pair, signed requests, and the D1 binding API on a REAL SQLite
// database (node:sqlite), so the SQL in worker.ts runs for real instead of against a mock.
import { DatabaseSync } from "node:sqlite";
import { readFileSync } from "node:fs";

const SCHEMA = readFileSync(new URL("../schema.sql", import.meta.url), "utf8");

export function openDb() {
  const sql = new DatabaseSync(":memory:");
  sql.exec(SCHEMA);
  const stmt = (text, args = []) => ({
    bind: (...a) => stmt(text, a),
    all: async () => ({ results: sql.prepare(text).all(...args) }),
    first: async () => sql.prepare(text).get(...args) ?? null,
    run: async () => ({ meta: { changes: Number(sql.prepare(text).run(...args).changes) } }),
    _run: () => sql.prepare(text).run(...args),
  });
  return {
    raw: sql,
    prepare: (text) => stmt(text),
    // D1's batch is one transaction: all statements apply or none do.
    batch: async (list) => {
      sql.exec("BEGIN");
      try {
        for (const s of list) s._run();
        sql.exec("COMMIT");
      } catch (e) {
        sql.exec("ROLLBACK");
        throw e;
      }
      return list.map(() => ({ success: true }));
    },
  };
}

export async function newKeyPair() {
  const pair = await crypto.subtle.generateKey("Ed25519", true, ["sign", "verify"]);
  const spki = new Uint8Array(await crypto.subtle.exportKey("spki", pair.publicKey));
  return { privateKey: pair.privateKey, publicB64: Buffer.from(spki).toString("base64") };
}

export async function sign(privateKey, bodyString) {
  const sig = await crypto.subtle.sign("Ed25519", privateKey, new TextEncoder().encode(bodyString));
  return Buffer.from(new Uint8Array(sig)).toString("base64");
}

export function event(id, nowMs, extra = {}) {
  return JSON.stringify({ type: "ContactCreate", webhookId: id, locationId: "loc_1", timestamp: new Date(nowMs).toISOString(), ...extra });
}

export async function signedRequest(privateKey, body, headers = {}) {
  return new Request("https://edge.test/webhooks/ghl", {
    method: "POST",
    headers: { "x-ghl-signature": await sign(privateKey, body), "content-type": "application/json", ...headers },
    body,
  });
}

export function envFor(db, publicB64, extra = {}) {
  return { DB: db, SINK_URL: "https://sink.test/in", ENVIRONMENT: "test", TEST_PUBLIC_KEY_B64: publicB64, ...extra };
}

/** Replaces global fetch with a programmable sink. Returns { calls, restore }. */
export function stubSink(handler) {
  const real = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (url, init) => {
    const call = { url: String(url), key: init.headers["idempotency-key"], auth: init.headers.authorization, body: init.body };
    calls.push(call);
    return handler(call, calls.length);
  };
  return { calls, restore: () => (globalThis.fetch = real) };
}

export const count = (db, where = "1=1") => db.raw.prepare(`SELECT COUNT(*) AS n FROM inbox WHERE ${where}`).get().n;

/** A producer binding that records what was sent. `failWith` makes send() throw, like a queue that is down. */
export function fakeQueue(failWith = null) {
  const sent = [];
  return { sent, send: async (body) => { if (failWith) throw failWith; sent.push(body); }, sendBatch: async (list) => { for (const m of list) sent.push(m.body); } };
}

/** A MessageBatch shaped like Cloudflare's: each message records its own ack() and retry({delaySeconds}). */
export function makeBatch(bodies, queue = "ghl-forward") {
  const messages = bodies.map((body, i) => {
    const m = { id: `m${i}`, timestamp: new Date(0), body, attempts: 1, acked: false, retried: null };
    m.ack = () => { m.acked = true; };
    m.retry = (opts) => { m.retried = opts ?? {}; };
    return m;
  });
  return { queue, messages, ackAll: () => messages.forEach((m) => m.ack()), retryAll: () => messages.forEach((m) => m.retry()) };
}
