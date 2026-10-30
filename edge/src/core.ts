/**
 * Edge receiver for GoHighLevel webhooks: verify, de-duplicate, store, forward. (The Worker entry is worker.ts: a Workers
 * module may export only handlers, so the constants and helpers the tests import live here.)
 *
 *   POST /webhooks/ghl  verify X-GHL-Signature (Ed25519) over the raw bytes, check the delivery is recent,
 *                       insert it into D1 keyed by GHL's webhookId, answer 200 (also for a duplicate, so GHL stops retrying)
 *   queue()             the same forward, one message at a time, for rows ingest just stored (Cloudflare Queues, at least once)
 *   scheduled()         claim due rows, POST each to SINK_URL with an Idempotency-Key, record the attempt, back off on failure.
 *                       Still runs every 5 minutes as the sweeper: a row whose queue message was lost or ran out of retries is
 *                       found here, because D1 and not the queue holds the truth about a delivery.
 *   GET  /health        row counts by state, no payloads
 *
 * Two choices worth defending:
 *  - De-duplication lives in D1 (a PRIMARY KEY), not in KV. KV is eventually consistent, so two concurrent
 *    deliveries can both read "not seen" and both write; an INSERT that conflicts cannot.
 *  - The legacy X-WH-Signature header is not accepted at all. GHL stopped sending it on 2026-09-01, so a request
 *    that carries only that header is a spoof of a retired scheme.
 */

export interface Env {
  DB: D1Database;
  /** Optional on purpose: without the binding every row is delivered by the cron drain alone. */
  INBOX_QUEUE?: Queue<QueueBody>;
  /** Where verified events are forwarded. */
  SINK_URL: string;
  /** Bearer token for the sink. Set with `wrangler secret put`, never in a file. */
  SINK_TOKEN?: string;
  /** Tests only. The key override below is honoured ONLY when this is exactly "test"; wrangler.toml never sets it. */
  ENVIRONMENT?: string;
  TEST_PUBLIC_KEY_B64?: string;
}

/** Published by HighLevel for X-GHL-Signature (Webhook Integration Guide). Base64 of the SPKI DER. */
export const GHL_ED25519_SPKI_B64 = "MCowBQYDK2VwAyEAi2HR1srL4o18O8BRa7gVJY7G7bupbN3H9AwJrHCDiOg=";

export const MAX_BODY_BYTES = 256 * 1024;
export const RETENTION_MS = 30 * 24 * 60 * 60 * 1000;
// HighLevel retries a failed delivery up to 12 times with backoff and publishes neither the span of those retries nor a
// replay window. So the age limit equals the retention of the dedupe rows: anything younger is still recognised by its
// webhookId, anything older is refused, and no replay can slip between the two.
export const MAX_AGE_MS = RETENTION_MS;
export const MAX_FUTURE_MS = 5 * 60 * 1000;
export const CLAIM_MS = 2 * 60 * 1000;
export const MAX_ATTEMPTS = 6;
export const DRAIN_BATCH = 10;

/** What a queue message carries: only the id. The payload stays in D1, so a redelivered message cannot forward a stale copy. */
export interface QueueBody {
  webhookId: string;
}

const json = (status: number, body: unknown): Response =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

const b64 = (s: string): Uint8Array => Uint8Array.from(atob(s), (c) => c.charCodeAt(0));

const keyCache = new Map<string, Promise<CryptoKey>>();
function importKey(spkiB64: string): Promise<CryptoKey> {
  let k = keyCache.get(spkiB64);
  if (!k) {
    k = crypto.subtle.importKey("spki", b64(spkiB64), { name: "Ed25519" }, false, ["verify"]);
    keyCache.set(spkiB64, k);
  }
  return k;
}

function publicKeyFor(env: Env): string {
  return env.ENVIRONMENT === "test" && env.TEST_PUBLIC_KEY_B64 ? env.TEST_PUBLIC_KEY_B64 : GHL_ED25519_SPKI_B64;
}

/** Reads at most `max` bytes. Returns null when the body is larger, without buffering the rest. */
async function readCapped(req: Request, max: number): Promise<Uint8Array | null> {
  const declared = Number(req.headers.get("content-length") ?? "0");
  if (declared > max) return null;
  if (!req.body) return new Uint8Array(0);
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > max) {
      await reader.cancel();
      return null;
    }
    chunks.push(value);
  }
  const out = new Uint8Array(total);
  let at = 0;
  for (const c of chunks) {
    out.set(c, at);
    at += c.byteLength;
  }
  return out;
}

async function verifySignature(env: Env, raw: Uint8Array, header: string | null): Promise<boolean> {
  if (!header) return false; // GHL sometimes sends the literal "N/A"; it decodes to garbage and fails verification below.
  try {
    return await crypto.subtle.verify("Ed25519", await importKey(publicKeyFor(env)), b64(header), raw);
  } catch {
    return false;
  }
}

export async function ingest(req: Request, env: Env, now: number): Promise<Response> {
  const raw = await readCapped(req, MAX_BODY_BYTES);
  if (raw === null) return json(413, { error: "body-too-large" });

  // Signature first, over the exact bytes received. Nothing below runs on an unverified body.
  if (!(await verifySignature(env, raw, req.headers.get("x-ghl-signature")))) {
    return json(401, { error: "bad-signature" });
  }

  let event: { webhookId?: unknown; type?: unknown; timestamp?: unknown; locationId?: unknown };
  try {
    event = JSON.parse(new TextDecoder().decode(raw));
  } catch {
    return json(400, { error: "not-json" });
  }
  const id = event.webhookId;
  if (typeof id !== "string" || id.length === 0 || id.length > 128) return json(400, { error: "bad-webhook-id" });
  if (typeof event.type !== "string") return json(400, { error: "bad-type" });
  const sent = typeof event.timestamp === "string" ? Date.parse(event.timestamp) : NaN;
  if (Number.isNaN(sent)) return json(400, { error: "bad-timestamp" });
  if (now - sent > MAX_AGE_MS || sent - now > MAX_FUTURE_MS) return json(400, { error: "stale-or-future" });

  const res = await env.DB.prepare(
    `INSERT INTO inbox (webhook_id, type, location_id, body, received_at, next_at)
     VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(webhook_id) DO NOTHING`
  )
    .bind(id, event.type, typeof event.locationId === "string" ? event.locationId : null, new TextDecoder().decode(raw), now, now)
    .run();
  const fresh = res.meta.changes === 1;
  // Only a row that was just inserted is enqueued, so a duplicate delivery adds no message.
  if (fresh && env.INBOX_QUEUE) {
    try {
      await env.INBOX_QUEUE.send({ webhookId: id });
    } catch { /* the row is durable; the cron drain sweeps it */ }
  }
  return json(200, { ok: true, duplicate: res.meta.changes === 0 });
}

interface Claimed {
  webhook_id: string;
  body: string;
  attempts: number;
}

/**
 * Claims due rows in ONE statement, so two overlapping drains (or a drain and a queue consumer) can never hold the same
 * row. `onlyId` narrows the claim to one webhook for the queue consumer; the drain passes null.
 */
async function claim(env: Env, now: number, limit: number, onlyId: string | null = null): Promise<Claimed[]> {
  const { results } = await env.DB.prepare(
    `UPDATE inbox SET state = 'claimed', claim_until = ?, attempts = attempts + 1
     WHERE webhook_id IN (
       SELECT webhook_id FROM inbox
       WHERE ((state = 'pending' AND next_at <= ?) OR (state = 'claimed' AND claim_until < ?)) AND (? IS NULL OR webhook_id = ?)
       ORDER BY next_at LIMIT ?)
     RETURNING webhook_id, body, attempts`
  )
    .bind(now + CLAIM_MS, now, now, onlyId, onlyId, limit)
    .all<Claimed>();
  return results;
}

const backoffMs = (attempts: number): number => Math.min(60_000 * 2 ** attempts, 60 * 60_000);

/** One forward attempt for a claimed row and the state change that goes with it. The cron drain and the queue consumer both call it, so they cannot drift apart. */
async function deliver(env: Env, row: Claimed, now: number): Promise<{ ok: boolean; dead: boolean }> {
  let status: number | null = null;
  let ok = false;
  let error = "";
  try {
    const res = await fetch(env.SINK_URL, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "idempotency-key": row.webhook_id,
        ...(env.SINK_TOKEN ? { authorization: `Bearer ${env.SINK_TOKEN}` } : {}),
      },
      body: row.body,
      signal: AbortSignal.timeout(8000),
    });
    status = res.status;
    ok = res.ok;
    if (!ok) error = `sink answered ${status}`;
  } catch (e) {
    error = e instanceof Error ? e.message : "fetch failed";
  }
  // A 4xx other than 408 and 429 will not get better on retry, so it goes straight to dead.
  const permanent = status !== null && status >= 400 && status < 500 && status !== 408 && status !== 429;
  const dead = !ok && (permanent || row.attempts >= MAX_ATTEMPTS);
  const state = ok ? "done" : dead ? "dead" : "pending";
  // The state change and the attempt log are one batch: the log cannot say "delivered" while the row says "pending".
  await env.DB.batch([
    env.DB.prepare(
      `UPDATE inbox SET state = ?, next_at = ?, claim_until = NULL, last_error = ? WHERE webhook_id = ? AND state = 'claimed'`
    ).bind(state, now + backoffMs(row.attempts), ok ? null : error, row.webhook_id),
    env.DB.prepare(`INSERT INTO deliveries (webhook_id, attempt, at, status, ok) VALUES (?, ?, ?, ?, ?)`).bind(
      row.webhook_id, row.attempts, now, status, ok ? 1 : 0
    ),
  ]);
  return { ok, dead };
}

export async function drain(env: Env, now: number): Promise<{ claimed: number; done: number; failed: number; dead: number }> {
  const rows = await claim(env, now, DRAIN_BATCH);
  const out = { claimed: rows.length, done: 0, failed: 0, dead: 0 };
  for (const row of rows) {
    const r = await deliver(env, row, now);
    if (r.ok) out.done++;
    else if (r.dead) out.dead++;
    else out.failed++;
  }
  await env.DB.prepare(`DELETE FROM inbox WHERE state = 'done' AND received_at < ?`).bind(now - RETENTION_MS).run();
  return out;
}

/**
 * The queue consumer. Queues deliver at least once and retry a WHOLE batch when the handler throws, so this never throws:
 * every message ends in ack() or retry(), and one bad message cannot make the others be forwarded twice. A message past
 * the queue's max_retries goes to the dead letter queue; the row itself stays in D1 and the cron drain still delivers it.
 */
export async function consume(batch: MessageBatch<QueueBody>, env: Env, now: number): Promise<void> {
  for (const msg of batch.messages) {
    const id = msg.body?.webhookId;
    if (typeof id !== "string" || id.length === 0) { msg.ack(); continue; } // a body no retry can fix
    try {
      const [row] = await claim(env, now, 1, id);
      if (row) {
        const r = await deliver(env, row, now);
        if (r.ok || r.dead) msg.ack();
        else msg.retry({ delaySeconds: Math.ceil(backoffMs(row.attempts) / 1000) });
        continue;
      }
      // Nothing to claim: finished, gone, not due yet, or held by a live claim.
      const cur = await env.DB.prepare(`SELECT state, next_at FROM inbox WHERE webhook_id = ?`).bind(id).first<{ state: string; next_at: number }>();
      if (!cur || cur.state === "done" || cur.state === "dead") msg.ack();
      else msg.retry({ delaySeconds: Math.min(3600, Math.max(30, Math.ceil((cur.next_at - now) / 1000))) });
    } catch {
      msg.retry({ delaySeconds: 60 });
    }
  }
}

async function health(env: Env): Promise<Response> {
  const { results } = await env.DB.prepare(`SELECT state, COUNT(*) AS n FROM inbox GROUP BY state`).all<{ state: string; n: number }>();
  return json(200, { ok: true, inbox: Object.fromEntries(results.map((r) => [r.state, r.n])) });
}

export function createWorker(clock: () => number = Date.now) {
  return {
    async fetch(req: Request, env: Env): Promise<Response> {
      const url = new URL(req.url);
      if (url.pathname === "/webhooks/ghl") {
        return req.method === "POST" ? ingest(req, env, clock()) : json(405, { error: "method-not-allowed" });
      }
      if (url.pathname === "/health" && req.method === "GET") return health(env);
      return json(404, { error: "not-found" });
    },
    async scheduled(_controller: ScheduledController, env: Env): Promise<void> {
      await drain(env, clock());
    },
    async queue(batch: MessageBatch<QueueBody>, env: Env): Promise<void> {
      await consume(batch, env, clock());
    },
  };
}

