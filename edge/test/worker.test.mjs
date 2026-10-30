// Offline tests for the edge receiver: real Ed25519, real SQLite behind the D1 API, both directions per rule
// (the request that must pass AND the one that must fail), then a seeded property test over random delivery worlds.
// Run: node --test test/worker.test.mjs    (Node 22.18+ runs the .ts source directly)
import test from "node:test";
import assert from "node:assert/strict";
import { createWorker, MAX_ATTEMPTS, MAX_AGE_MS, MAX_BODY_BYTES, CLAIM_MS, DRAIN_BATCH, RETENTION_MS } from "../src/core.ts";
import { openDb, newKeyPair, sign, event, signedRequest, envFor, stubSink, count } from "./helpers.mjs";

const NOW = Date.parse("2026-10-22T08:00:00Z");
const world = async (clockStart = NOW) => {
  const keys = await newKeyPair();
  const db = openDb();
  const t = { now: clockStart };
  const worker = createWorker(() => t.now);
  return { keys, db, t, worker, env: envFor(db, keys.publicB64), post: async (body, headers) => worker.fetch(await signedRequest(keys.privateKey, body, headers), envFor(db, keys.publicB64)) };
};

test("a signed, recent, new delivery is stored and answered 200", async () => {
  const w = await world();
  const res = await w.post(event("w1", NOW));
  assert.equal(res.status, 200);
  assert.deepEqual(await res.json(), { ok: true, duplicate: false });
  assert.equal(count(w.db), 1);
});

test("the same webhookId again is a duplicate: 200, still one row", async () => {
  const w = await world();
  await w.post(event("w1", NOW));
  const again = await w.post(event("w1", NOW));
  assert.equal(again.status, 200);
  assert.equal((await again.json()).duplicate, true);
  assert.equal(count(w.db), 1);
});

test("a tampered body, a missing header, N/A and the legacy header alone are all 401 and store nothing", async () => {
  const w = await world();
  const good = event("w2", NOW);
  const sig = await sign(w.keys.privateKey, good);
  const cases = {
    tampered: new Request("https://edge.test/webhooks/ghl", { method: "POST", headers: { "x-ghl-signature": sig }, body: good.replace("loc_1", "loc_2") }),
    missing: new Request("https://edge.test/webhooks/ghl", { method: "POST", body: good }),
    na: new Request("https://edge.test/webhooks/ghl", { method: "POST", headers: { "x-ghl-signature": "N/A" }, body: good }),
    legacyOnly: new Request("https://edge.test/webhooks/ghl", { method: "POST", headers: { "x-wh-signature": sig }, body: good }),
    garbage: new Request("https://edge.test/webhooks/ghl", { method: "POST", headers: { "x-ghl-signature": "!!!not base64" }, body: good }),
  };
  for (const [name, req] of Object.entries(cases)) {
    assert.equal((await w.worker.fetch(req, w.env)).status, 401, name);
  }
  assert.equal(count(w.db), 0);
});

test("a signature from a different key is refused (positive control: the same body with the right key passes)", async () => {
  const w = await world();
  const other = await newKeyPair();
  const body = event("w3", NOW);
  assert.equal((await w.worker.fetch(await signedRequest(other.privateKey, body), w.env)).status, 401);
  assert.equal((await w.post(body)).status, 200);
});

test("the key override is ignored unless ENVIRONMENT is exactly test, so config cannot weaken verification", async () => {
  const w = await world();
  const body = event("w4", NOW);
  const req = await signedRequest(w.keys.privateKey, body);
  const prodLike = envFor(w.db, w.keys.publicB64, { ENVIRONMENT: "production" });
  assert.equal((await w.worker.fetch(req, prodLike)).status, 401);
});

test("an old delivery, a future one, a missing timestamp, bad JSON and a missing id are 400", async () => {
  const w = await world();
  assert.equal((await w.post(event("a", NOW - MAX_AGE_MS - 1000))).status, 400);
  assert.equal((await w.post(event("b", NOW + 10 * 60 * 1000))).status, 400);
  assert.equal((await w.post(event("c", NOW, { timestamp: undefined }))).status, 400);
  assert.equal((await w.post("{not json")).status, 400);
  assert.equal((await w.post(event("", NOW))).status, 400);
  assert.equal((await w.post(JSON.stringify({ type: "X", timestamp: new Date(NOW).toISOString() }))).status, 400);
  assert.equal(count(w.db), 0);
  assert.equal((await w.post(event("d", NOW - MAX_AGE_MS + 1000))).status, 200, "just inside the window still passes");
});

test("the age limit never exceeds the retention of the dedupe rows, so no replay falls between the two", () => {
  assert.ok(MAX_AGE_MS <= RETENTION_MS);
});

test("a body over the cap is 413 whether or not content-length admits it", async () => {
  const w = await world();
  const big = "x".repeat(MAX_BODY_BYTES + 1);
  assert.equal((await w.post(big)).status, 413);
  const lying = new Request("https://edge.test/webhooks/ghl", { method: "POST", headers: { "x-ghl-signature": "AA==" }, body: big });
  assert.equal((await w.worker.fetch(lying, w.env)).status, 413);
  // A declared length over the cap is refused before a single byte is read, even when the body itself is small.
  const declared = new Request("https://edge.test/webhooks/ghl", { method: "POST", headers: { "x-ghl-signature": "AA==", "content-length": String(MAX_BODY_BYTES + 1) }, body: "{}" });
  assert.equal((await w.worker.fetch(declared, w.env)).status, 413);
});

test("routing: GET on the webhook path is 405, unknown paths 404, /health shows counts and no payload", async () => {
  const w = await world();
  assert.equal((await w.worker.fetch(new Request("https://edge.test/webhooks/ghl"), w.env)).status, 405);
  assert.equal((await w.worker.fetch(new Request("https://edge.test/nope"), w.env)).status, 404);
  await w.post(event("h1", NOW));
  const res = await w.worker.fetch(new Request("https://edge.test/health"), w.env);
  const text = await res.text();
  assert.deepEqual(JSON.parse(text), { ok: true, inbox: { pending: 1 } });
  assert.ok(!text.includes("loc_1"));
});

test("drain forwards once with the Idempotency-Key and the bearer token, then marks done and logs the attempt", async () => {
  const w = await world();
  await w.post(event("f1", NOW));
  const sink = stubSink(() => new Response("ok", { status: 200 }));
  try {
    await w.worker.scheduled({}, { ...w.env, SINK_TOKEN: "s3cret" });
    assert.equal(sink.calls.length, 1);
    assert.equal(sink.calls[0].key, "f1");
    assert.equal(sink.calls[0].auth, "Bearer s3cret");
    await w.worker.scheduled({}, w.env);
    assert.equal(sink.calls.length, 1, "a done row is never forwarded again");
  } finally { sink.restore(); }
  assert.equal(count(w.db, "state = 'done'"), 1);
  assert.deepEqual(w.db.raw.prepare("SELECT attempt, ok, status FROM deliveries").all().map((r) => ({ ...r })), [{ attempt: 1, ok: 1, status: 200 }]);
});

test("a failing sink backs off, retries later, and a row goes dead after MAX_ATTEMPTS", async () => {
  const w = await world();
  await w.post(event("r1", NOW));
  const sink = stubSink(() => new Response("down", { status: 503 }));
  try {
    await w.worker.scheduled({}, w.env);
    assert.equal(sink.calls.length, 1);
    await w.worker.scheduled({}, w.env);
    assert.equal(sink.calls.length, 1, "not due yet: backoff holds it");
    for (let i = 0; i < MAX_ATTEMPTS + 2; i++) { w.t.now += 2 * 60 * 60 * 1000; await w.worker.scheduled({}, w.env); }
  } finally { sink.restore(); }
  assert.equal(sink.calls.length, MAX_ATTEMPTS);
  assert.equal(count(w.db, "state = 'dead'"), 1);
  assert.equal(w.db.raw.prepare("SELECT MAX(attempt) AS m FROM deliveries").get().m, MAX_ATTEMPTS);
});

test("a permanent 4xx from the sink goes straight to dead, a 429 is retried", async () => {
  const w = await world();
  await w.post(event("p1", NOW));
  await w.post(event("p2", NOW));
  const sink = stubSink((c) => new Response("", { status: c.key === "p1" ? 422 : 429 }));
  try { await w.worker.scheduled({}, w.env); } finally { sink.restore(); }
  assert.equal(w.db.raw.prepare("SELECT state FROM inbox WHERE webhook_id='p1'").get().state, "dead");
  assert.equal(w.db.raw.prepare("SELECT state FROM inbox WHERE webhook_id='p2'").get().state, "pending");
});

test("two overlapping drains never forward the same row twice", async () => {
  const w = await world();
  for (let i = 0; i < 6; i++) await w.post(event(`o${i}`, NOW));
  const sink = stubSink(async () => { await new Promise((r) => setTimeout(r, 5)); return new Response("ok"); });
  try { await Promise.all([w.worker.scheduled({}, w.env), w.worker.scheduled({}, w.env), w.worker.scheduled({}, w.env)]); } finally { sink.restore(); }
  assert.equal(sink.calls.length, 6);
  assert.equal(new Set(sink.calls.map((c) => c.key)).size, 6);
});

test("a drain that died after claiming leaves a row that the next drain picks up once the claim expires, not before", async () => {
  const w = await world();
  await w.post(event("k1", NOW));
  w.db.raw.prepare("UPDATE inbox SET state='claimed', claim_until=?, attempts=1 WHERE webhook_id='k1'").run(NOW + CLAIM_MS);
  const sink = stubSink(() => new Response("ok"));
  try {
    await w.worker.scheduled({}, w.env);
    assert.equal(sink.calls.length, 0, "claim still live");
    w.t.now += CLAIM_MS + 1;
    await w.worker.scheduled({}, w.env);
    assert.equal(sink.calls.length, 1);
  } finally { sink.restore(); }
});

test("drain takes at most DRAIN_BATCH rows per tick, oldest due first", async () => {
  const w = await world();
  for (let i = 0; i < DRAIN_BATCH + 3; i++) { w.t.now = NOW + i * 1000; await w.post(event(`b${i}`, w.t.now)); }
  w.t.now = NOW + 60_000;
  const sink = stubSink(() => new Response("ok"));
  try { await w.worker.scheduled({}, w.env); } finally { sink.restore(); }
  assert.equal(sink.calls.length, DRAIN_BATCH);
  assert.equal(sink.calls[0].key, "b0");
});

test("a failure while recording the outcome leaves the row claimed, never half done (batch is all or nothing)", async () => {
  const w = await world();
  await w.post(event("t1", NOW));
  w.db.raw.exec("DROP TABLE deliveries");
  const sink = stubSink(() => new Response("ok"));
  try { await assert.rejects(w.worker.scheduled({}, w.env)); } finally { sink.restore(); }
  assert.equal(w.db.raw.prepare("SELECT state FROM inbox WHERE webhook_id='t1'").get().state, "claimed");
});

test("done rows older than the retention window are pruned, recent and unfinished ones are kept", async () => {
  const w = await world();
  const ins = w.db.raw.prepare("INSERT INTO inbox (webhook_id, type, body, received_at, state, next_at) VALUES (?, 'X', '{}', ?, ?, 0)");
  ins.run("old-done", NOW - 31 * 24 * 3600 * 1000, "done");
  ins.run("new-done", NOW - 1000, "done");
  ins.run("old-dead", NOW - 31 * 24 * 3600 * 1000, "dead");
  await w.worker.scheduled({}, w.env);
  const ids = w.db.raw.prepare("SELECT webhook_id FROM inbox ORDER BY webhook_id").all().map((r) => r.webhook_id);
  assert.deepEqual(ids, ["new-done", "old-dead"]);
});

test("no secret or account id is committed in the edge folder", async () => {
  const { readdirSync, readFileSync, statSync } = await import("node:fs");
  const root = new URL("../", import.meta.url);
  const walk = (dir) => readdirSync(dir).flatMap((n) => (n === "node_modules" || n === ".wrangler" ? [] : statSync(new URL(n, dir)).isDirectory() ? walk(new URL(n + "/", dir)) : [new URL(n, dir)]));
  for (const f of walk(root)) {
    if (!/\.(ts|mjs|toml|json|sql|md)$/.test(f.pathname) || f.pathname.endsWith("package-lock.json") || f.pathname.endsWith("worker.test.mjs")) continue;
    const text = readFileSync(f, "utf8");
    assert.ok(!/account_id\s*=\s*"[0-9a-f]{32}"/.test(text), `account id in ${f.pathname}`);
    assert.ok(!/(?:Bearer\s+[A-Za-z0-9_-]{20,}|sk_live_|-----BEGIN (?:RSA |EC )?PRIVATE KEY)/.test(text), `secret-looking text in ${f.pathname}`);
  }
  assert.ok(!/ENVIRONMENT\s*=/.test(readFileSync(new URL("../wrangler.toml", import.meta.url), "utf8").replace(/^\s*#.*$/gm, "")), "wrangler.toml must not set ENVIRONMENT");
});

// ---- Property test: random delivery worlds. The rules, not examples. ----
// Dimensions varied together: duplicate and reordered deliveries, forged ones mixed in, a sink that fails at random
// (including answering 5xx after it already processed the row), overlapping drains, and the clock moving between ticks.
function rng(seed) { let s = seed >>> 0; return () => ((s = (Math.imul(s, 1664525) + 1013904223) >>> 0) / 2 ** 32); }

for (const seed of [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]) {
  test(`property: seed ${seed} keeps the delivery rules`, async () => {
    const rand = rng(seed);
    const w = await world();
    const other = await newKeyPair();
    const legit = new Set();
    const processed = new Map(); // id -> how many times the sink saw it
    const sink = stubSink((call) => {
      processed.set(call.key, (processed.get(call.key) ?? 0) + 1);
      const roll = rand();
      return new Response("", { status: roll < 0.3 ? 503 : roll < 0.35 ? 422 : 200 });
    });
    try {
      for (let step = 0; step < 60; step++) {
        const action = rand();
        if (action < 0.45) {
          const id = `e${Math.floor(rand() * 25)}`; // few ids, so duplicates are common
          const forged = rand() < 0.2;
          const body = event(id, w.t.now);
          const res = forged ? await w.worker.fetch(await signedRequest(other.privateKey, body), w.env) : await w.post(body);
          if (forged) assert.equal(res.status, 401);
          else { assert.equal(res.status, 200); legit.add(id); }
        } else if (action < 0.85) {
          const n = 1 + Math.floor(rand() * 3);
          await Promise.all(Array.from({ length: n }, () => w.worker.scheduled({}, w.env)));
        } else {
          w.t.now += Math.floor(rand() * 3 * 60 * 60 * 1000);
        }
        // Invariants after EVERY step.
        assert.ok(count(w.db) <= legit.size, "a forged id never reaches the inbox");
        assert.equal(w.db.raw.prepare("SELECT COUNT(*) AS n FROM (SELECT webhook_id FROM inbox GROUP BY webhook_id HAVING COUNT(*) > 1)").get().n, 0);
        assert.equal(w.db.raw.prepare("SELECT COUNT(*) AS n FROM inbox WHERE attempts > ?").get(MAX_ATTEMPTS).n, 0);
        assert.equal(w.db.raw.prepare("SELECT COUNT(*) AS n FROM inbox WHERE state = 'claimed' AND claim_until IS NULL").get().n, 0);
        for (const id of processed.keys()) assert.ok(legit.has(id), `sink saw an id that was never legitimately received: ${id}`);
      }
      // Drain to quiescence: every legitimate id must end done or dead, never stuck or lost.
      for (let i = 0; i < MAX_ATTEMPTS + 3; i++) { w.t.now += 3 * 60 * 60 * 1000; await w.worker.scheduled({}, w.env); }
    } finally { sink.restore(); }
    for (const id of legit) {
      const row = w.db.raw.prepare("SELECT state FROM inbox WHERE webhook_id = ?").get(id);
      assert.ok(row && (row.state === "done" || row.state === "dead"), `${id} ended ${row?.state ?? "missing"}`);
    }
    // A row that finished OK was forwarded successfully exactly once in the delivery log.
    assert.equal(w.db.raw.prepare("SELECT COUNT(*) AS n FROM (SELECT webhook_id FROM deliveries WHERE ok = 1 GROUP BY webhook_id HAVING COUNT(*) > 1)").get().n, 0);
  });
}

// ---- the queue: a producer in ingest and a consumer for the same forward -------------------------------------------

import { fakeQueue, makeBatch } from "./helpers.mjs";

const qworld = async (queue = fakeQueue()) => {
  const w = await world();
  w.queue = queue;
  w.qenv = { ...w.env, INBOX_QUEUE: queue };
  w.postq = async (body) => w.worker.fetch(await signedRequest(w.keys.privateKey, body), w.qenv);
  return w;
};
const outcome = (m) => (m.acked ? "ack" : m.retried ? `retry:${m.retried.delaySeconds}` : "none");

test("a new delivery is enqueued once with only its id, and a duplicate adds no message", async () => {
  const w = await qworld();
  await w.postq(event("q1", NOW));
  await w.postq(event("q1", NOW));
  assert.deepEqual(w.queue.sent, [{ webhookId: "q1" }]);
  assert.equal(count(w.db), 1);
});

test("a queue that is down does not fail the webhook: 200, the row is stored, and the cron drain still delivers it", async () => {
  const w = await qworld(fakeQueue(new Error("queue down")));
  const res = await w.postq(event("q2", NOW));
  assert.equal(res.status, 200);
  assert.equal(count(w.db, "state = 'pending'"), 1);
  const sink = stubSink(() => new Response("ok", { status: 200 }));
  try { await w.worker.scheduled({}, w.qenv); } finally { sink.restore(); }
  assert.equal(count(w.db, "state = 'done'"), 1);
});

test("without the queue binding ingest still works (cron only)", async () => {
  const w = await world();
  assert.equal((await w.post(event("q3", NOW))).status, 200);
});

test("consumer: a good forward is sent once with the Idempotency-Key, acked, and the row is done", async () => {
  const w = await qworld();
  await w.postq(event("c1", NOW));
  const batch = makeBatch([{ webhookId: "c1" }]);
  const sink = stubSink(() => new Response("ok", { status: 200 }));
  try { await w.worker.queue(batch, w.qenv); } finally { sink.restore(); }
  assert.equal(sink.calls.length, 1);
  assert.equal(sink.calls[0].key, "c1");
  assert.deepEqual(batch.messages.map(outcome), ["ack"]);
  assert.equal(count(w.db, "state = 'done'"), 1);
});

test("consumer: a redelivered message for a finished row is acked and the sink is not called again", async () => {
  const w = await qworld();
  await w.postq(event("c2", NOW));
  const sink = stubSink(() => new Response("ok", { status: 200 }));
  try {
    await w.worker.queue(makeBatch([{ webhookId: "c2" }]), w.qenv);
    const again = makeBatch([{ webhookId: "c2" }, { webhookId: "c2" }]);
    await w.worker.queue(again, w.qenv);
    assert.deepEqual(again.messages.map(outcome), ["ack", "ack"]);
  } finally { sink.restore(); }
  assert.equal(sink.calls.length, 1, "at-least-once delivery must not become two forwards");
});

test("consumer: a 503 retries the message with a backoff delay and leaves the row pending for the sweeper", async () => {
  const w = await qworld();
  await w.postq(event("c3", NOW));
  const batch = makeBatch([{ webhookId: "c3" }]);
  const sink = stubSink(() => new Response("down", { status: 503 }));
  try { await w.worker.queue(batch, w.qenv); } finally { sink.restore(); }
  assert.deepEqual(batch.messages.map(outcome), ["retry:120"]);
  assert.equal(count(w.db, "state = 'pending'"), 1);
});

test("consumer: a permanent 4xx marks the row dead and acks, a 429 is retried", async () => {
  const w = await qworld();
  await w.postq(event("c4", NOW));
  await w.postq(event("c5", NOW));
  const bad = makeBatch([{ webhookId: "c4" }]);
  const slow = makeBatch([{ webhookId: "c5" }]);
  let sink = stubSink(() => new Response("no", { status: 400 }));
  try { await w.worker.queue(bad, w.qenv); } finally { sink.restore(); }
  sink = stubSink(() => new Response("slow down", { status: 429 }));
  try { await w.worker.queue(slow, w.qenv); } finally { sink.restore(); }
  assert.deepEqual([bad, slow].map((b) => outcome(b.messages[0])), ["ack", "retry:120"]);
  assert.equal(count(w.db, "webhook_id = 'c4' AND state = 'dead'"), 1);
  assert.equal(count(w.db, "webhook_id = 'c5' AND state = 'pending'"), 1);
});

test("consumer: the attempt cap marks the row dead and acks instead of retrying forever", async () => {
  const w = await qworld();
  await w.postq(event("c6", NOW));
  const sink = stubSink(() => new Response("down", { status: 503 }));
  let last;
  try {
    for (let i = 0; i < MAX_ATTEMPTS; i++) {
      w.t.now += 2 * 60 * 60 * 1000;
      last = makeBatch([{ webhookId: "c6" }]);
      await w.worker.queue(last, w.qenv);
    }
  } finally { sink.restore(); }
  assert.equal(sink.calls.length, MAX_ATTEMPTS);
  assert.deepEqual(last.messages.map(outcome), ["ack"]);
  assert.equal(count(w.db, "state = 'dead'"), 1);
});

test("consumer: an unknown id and a body with no id are acked, a row that is not due yet is retried for its remaining wait", async () => {
  const w = await qworld();
  await w.postq(event("c7", NOW));
  let sink = stubSink(() => new Response("down", { status: 503 }));
  try { await w.worker.queue(makeBatch([{ webhookId: "c7" }]), w.qenv); } finally { sink.restore(); }
  const batch = makeBatch([{ webhookId: "nope" }, {}, null, { webhookId: "c7" }]);
  sink = stubSink(() => new Response("ok", { status: 200 }));
  try { await w.worker.queue(batch, w.qenv); } finally { sink.restore(); }
  assert.deepEqual(batch.messages.map(outcome), ["ack", "ack", "ack", "retry:120"]);
  assert.equal(sink.calls.length, 0, "a row inside its backoff is not forwarded early");
});

test("consumer: each message claims only its own row", async () => {
  const w = await qworld();
  await w.postq(event("d1", NOW));
  await w.postq(event("d2", NOW));
  const sink = stubSink(() => new Response("ok", { status: 200 }));
  try { await w.worker.queue(makeBatch([{ webhookId: "d2" }]), w.qenv); } finally { sink.restore(); }
  assert.deepEqual(sink.calls.map((c) => c.key), ["d2"]);
  assert.equal(count(w.db, "webhook_id = 'd1' AND state = 'pending'"), 1);
});

test("consumer: a database error on one message retries that message and does not touch the others in the batch", async () => {
  const w = await qworld();
  await w.postq(event("e1", NOW));
  await w.postq(event("e2", NOW));
  const realPrepare = w.db.prepare;
  const flaky = {
    ...w.db,
    prepare: (text) => {
      const s = realPrepare(text);
      if (!text.startsWith("UPDATE inbox SET state = 'claimed'")) return s;
      return { ...s, bind: (...a) => (a.includes("e1") ? { all: async () => { throw new Error("D1 unavailable"); } } : s.bind(...a)) };
    },
  };
  const batch = makeBatch([{ webhookId: "e1" }, { webhookId: "e2" }]);
  const sink = stubSink(() => new Response("ok", { status: 200 }));
  try { await w.worker.queue(batch, { ...w.qenv, DB: flaky }); } finally { sink.restore(); }
  assert.deepEqual(batch.messages.map(outcome), ["retry:60", "ack"]);
  assert.deepEqual(sink.calls.map((c) => c.key), ["e2"]);
});
