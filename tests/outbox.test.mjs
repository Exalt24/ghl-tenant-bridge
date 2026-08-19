/**
 * outbox.ts state-machine tests. No browser, no network, fully deterministic.
 *
 * Storage, transport, clock and randomness are all injected, which is the reason
 * these can assert things a browser test cannot reach: that bytes SURVIVE a failed
 * upload, that a retry reuses the original idempotency key, that two overlapping
 * flushes do not both run, and that backoff actually defers an item.
 *
 * The most important assertions here are the negative ones. "It uploads" is easy and
 * proves little. What matters is what happens when the upload throws halfway, because
 * that is the case that loses a roofer's photos, and it is the case a happy-path test
 * never visits.
 *
 * Run: node tests/outbox.test.mjs
 */
import { Outbox, requestPersistentStorage, uuidv7 } from "../src/lib/outbox.ts";

let pass = 0;
const fails = [];
const check = (name, cond, detail = "") => {
  if (cond) pass++;
  else fails.push(`${name}${detail ? ` -- ${detail}` : ""}`);
  console.log(`  ${cond ? "PASS" : "FAIL"}  ${name}${cond ? "" : ` (${detail})`}`);
};

// ---------------------------------------------------------------- fakes
function memStorage() {
  const items = new Map();
  const bytes = new Map();
  return {
    items,
    bytes,
    async put(i) { items.set(i.id, { ...i }); },
    async list() { return [...items.values()].sort((a, b) => (a.id < b.id ? -1 : 1)); },
    async delete(id) { items.delete(id); },
    async readBytes(ref) {
      if (!bytes.has(ref)) throw new Error(`no bytes at ${ref}`);
      return bytes.get(ref);
    },
    async writeBytes(ref, b) { bytes.set(ref, b); },
    async deleteBytes(ref) { bytes.delete(ref); },
  };
}

function fixedRandom(seed = 7) {
  let n = seed;
  return (count) => {
    const out = new Uint8Array(count);
    for (let i = 0; i < count; i++) { n = (n * 1103515245 + 12345) & 0x7fffffff; out[i] = n & 0xff; }
    return out;
  };
}

const clock = (start = 1_700_000_000_000) => {
  let t = start;
  return { now: () => t, advance: (ms) => (t += ms) };
};

// ---------------------------------------------------------------- uuidv7
console.log("\nuuidv7");
{
  const t = 1_700_000_000_000;
  const id = uuidv7(t, new Uint8Array([1, 2, 3, 4, 5, 6, 7, 8, 9, 10]));
  check("is a well-formed uuid", /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(id), id);
  check("version nibble is 7", id[14] === "7", `char14=${id[14]}`);
  check("variant bits are RFC 4122", "89ab".includes(id[19]), `char19=${id[19]}`);

  // The first 48 bits must BE the timestamp, which is what makes ids sort by time.
  const hexTs = id.replace(/-/g, "").slice(0, 12);
  check("first 48 bits decode back to the timestamp", parseInt(hexTs, 16) === t,
    `decoded ${parseInt(hexTs, 16)} vs ${t}`);

  const later = uuidv7(t + 1000, new Uint8Array(10));
  check("a later timestamp sorts after an earlier one", later > id, `${id} vs ${later}`);

  // Same millisecond, different randomness: still unique.
  const a = uuidv7(t, new Uint8Array([1, 1, 1, 1, 1, 1, 1, 1, 1, 1]));
  const b = uuidv7(t, new Uint8Array([2, 2, 2, 2, 2, 2, 2, 2, 2, 2]));
  check("two ids in the same millisecond differ", a !== b);

  let threw = false;
  try { uuidv7(t, new Uint8Array(4)); } catch { threw = true; }
  check("refuses insufficient randomness", threw);
}

// ---------------------------------------------------------------- capture
console.log("\ncapture writes locally and never waits on the network");
{
  const s = memStorage();
  let called = 0;
  const box = new Outbox({
    storage: s,
    transport: {
      async uploadBytes() { called++; },
      async upsertRow() { called++; },
    },
    now: clock().now,
    random: fixedRandom(),
  });

  const id = await box.enqueue("photo", { address: "1 Test St" }, new Uint8Array([9, 9, 9]));
  check("enqueue made NO network call", called === 0, `called=${called}`);
  check("the item is in storage", s.items.size === 1);
  check("the bytes are in storage", s.bytes.size === 1);
  check("pendingCount reflects it", (await box.pendingCount()) === 1);
  check("the returned id is the item id", (await box.list())[0].id === id);
  check("bytesRef is derived from the id", (await box.list())[0].bytesRef === `outbox/${id}`);

  // Ordering is capture order with no separate sequence column, because ids are v7.
  const c = clock();
  const box2 = new Outbox({ storage: memStorage(), transport: { async uploadBytes() {}, async upsertRow() {} },
    now: c.now, random: fixedRandom(3) });
  const ids = [];
  for (let i = 0; i < 5; i++) { c.advance(5); ids.push(await box2.enqueue("form", { i })); }
  const listed = (await box2.list()).map((x) => x.id);
  check("the queue drains in capture order without a sequence column",
    JSON.stringify(listed) === JSON.stringify(ids), `${listed} vs ${ids}`);
}

// ------------------------------------------------- THE ONE THAT MATTERS
console.log("\nA FAILED UPLOAD MUST NOT LOSE THE BYTES");
{
  const s = memStorage();
  const box = new Outbox({
    storage: s,
    transport: {
      async uploadBytes() { throw new Error("network down"); },
      async upsertRow() { throw new Error("should never be reached"); },
    },
    now: clock().now,
    random: fixedRandom(),
    maxAttempts: 3,
  });

  await box.enqueue("photo", { a: 1 }, new Uint8Array([1, 2, 3]));
  const r = await box.flush();
  check("the flush reports the failure", r.failed === 1 && r.sent === 0, JSON.stringify(r));
  check("THE BYTES ARE STILL THERE", s.bytes.size === 1);
  check("the queue row is still there", s.items.size === 1);
  const item = (await box.list())[0];
  check("attempts was incremented", item.attempts === 1, `attempts=${item.attempts}`);
  check("the error was recorded for the user", item.lastError === "network down", item.lastError);
  check("it is still pending, not failed", item.state === "pending", item.state);
}

console.log("\na row upsert that fails AFTER the bytes upload also keeps everything");
{
  // This is the dangerous interleaving: bytes made it, the row did not. Deleting
  // locally here would lose the association even though the file exists remotely.
  const s = memStorage();
  let uploads = 0;
  const box = new Outbox({
    storage: s,
    transport: {
      async uploadBytes() { uploads++; },
      async upsertRow() { throw new Error("row rejected"); },
    },
    now: clock().now, random: fixedRandom(),
  });
  const id = await box.enqueue("photo", { a: 1 }, new Uint8Array([4, 5]));
  await box.flush();
  check("bytes were uploaded", uploads === 1);
  check("local bytes retained anyway", s.bytes.has(`outbox/${id}`));
  check("the item is retained for retry", s.items.has(id));
}

// ---------------------------------------------------------------- idempotency
console.log("\nretry reuses the ORIGINAL idempotency key");
{
  const s = memStorage();
  const seen = [];
  let failNext = true;
  const c = clock();
  const box = new Outbox({
    storage: s,
    transport: {
      async uploadBytes() {},
      async upsertRow(item) {
        seen.push(item.id);
        if (failNext) { failNext = false; throw new Error("transient"); }
      },
    },
    now: c.now, random: fixedRandom(), backoffBaseMs: 100,
  });

  const id = await box.enqueue("form", { x: 1 });
  await box.flush();                 // fails
  c.advance(60_000);                 // past any backoff
  const r2 = await box.flush();      // succeeds

  check("the server saw the same key twice", seen.length === 2 && seen[0] === seen[1], JSON.stringify(seen));
  check("that key is the id minted at capture", seen[0] === id);
  check("the item is gone after success", s.items.size === 0);
  check("the second flush reports it sent", r2.sent === 1, JSON.stringify(r2));
}

// ---------------------------------------------------------------- single flight
console.log("\nsingle flight");
{
  const s = memStorage();
  let concurrent = 0;
  let maxConcurrent = 0;
  const box = new Outbox({
    storage: s,
    transport: {
      async uploadBytes() {},
      async upsertRow() {
        concurrent++;
        maxConcurrent = Math.max(maxConcurrent, concurrent);
        await new Promise((r) => setTimeout(r, 20));
        concurrent--;
      },
    },
    now: clock().now, random: fixedRandom(),
  });
  for (let i = 0; i < 3; i++) await box.enqueue("form", { i });

  const [a, b] = await Promise.all([box.flush(), box.flush()]);
  const ran = [a, b].filter((r) => !r.alreadyRunning);
  const blocked = [a, b].filter((r) => r.alreadyRunning);
  check("exactly one flush ran", ran.length === 1, JSON.stringify([a, b]));
  check("the other reported alreadyRunning", blocked.length === 1);
  check("no two items were ever in flight together", maxConcurrent === 1, `max=${maxConcurrent}`);
  check("all three were still delivered once", ran[0].sent === 3, JSON.stringify(ran[0]));
}

// ---------------------------------------------------------------- backoff
console.log("\nbackoff defers, and exhaustion parks the item");
{
  const s = memStorage();
  const c = clock();
  const box = new Outbox({
    storage: s,
    transport: { async uploadBytes() {}, async upsertRow() { throw new Error("nope"); } },
    now: c.now, random: fixedRandom(), maxAttempts: 3, backoffBaseMs: 1000,
  });
  await box.enqueue("form", { a: 1 });

  await box.flush();
  const after1 = (await box.list())[0];
  check("nextAttemptAt was set into the future", after1.nextAttemptAt > c.now(),
    `next=${after1.nextAttemptAt} now=${c.now()}`);

  const immediate = await box.flush();
  check("an immediate re-flush SKIPS the item rather than hammering",
    immediate.skipped === 1 && immediate.failed === 0, JSON.stringify(immediate));

  c.advance(10_000); await box.flush();
  c.advance(10_000); await box.flush();
  const parked = (await box.list())[0];
  check("after maxAttempts the item is parked as failed", parked.state === "failed",
    `state=${parked.state} attempts=${parked.attempts}`);
  check("failedItems surfaces it for a human", (await box.failedItems()).length === 1);

  const skip = await box.flush();
  check("a failed item is skipped, not retried forever", skip.skipped === 1);

  const n = await box.retryFailed();
  check("retryFailed resets it", n === 1 && (await box.list())[0].state === "pending");
  check("and clears the attempt count", (await box.list())[0].attempts === 0);
}

// ---------------------------------------------------------------- isolation
console.log("\none bad item does not block the queue");
{
  const s = memStorage();
  const c = clock();
  const box = new Outbox({
    storage: s,
    transport: {
      async uploadBytes() {},
      async upsertRow(item) { if (item.payload.bad) throw new Error("bad row"); },
    },
    now: c.now, random: fixedRandom(),
  });
  c.advance(1); await box.enqueue("form", { bad: true });
  c.advance(1); await box.enqueue("form", { bad: false, n: 2 });
  c.advance(1); await box.enqueue("form", { bad: false, n: 3 });

  const r = await box.flush();
  check("the two good items still went", r.sent === 2, JSON.stringify(r));
  check("the bad one was kept", r.failed === 1 && s.items.size === 1);
  check("and the survivor is the bad one", (await box.list())[0].payload.bad === true);
}

// ---------------------------------------------------------------- session refresh
console.log("\nsession refresh happens once per flush, not per item");
{
  const s = memStorage();
  let refreshes = 0;
  const c = clock();
  const box = new Outbox({
    storage: s,
    transport: { async uploadBytes() {}, async upsertRow() {} },
    now: c.now, random: fixedRandom(),
    beforeFlush: async () => { refreshes++; },
  });
  for (let i = 0; i < 4; i++) { c.advance(1); await box.enqueue("form", { i }); }
  await box.flush();
  check("four items, ONE refresh (concurrent refreshes revoke a Supabase session)",
    refreshes === 1, `refreshes=${refreshes}`);
  check("all four delivered", s.items.size === 0);
}

// ---------------------------------------------------------------- persistence
console.log("\nrequestPersistentStorage reports honestly");
{
  let r = await requestPersistentStorage({});
  check("unsupported is reported, not assumed granted", r.supported === false && r.persisted === false);

  r = await requestPersistentStorage({ storage: { persist: async () => false } });
  check("a REFUSED grant returns false rather than being swallowed",
    r.supported === true && r.persisted === false);

  r = await requestPersistentStorage({ storage: { persist: async () => true } });
  check("a granted request returns true", r.persisted === true);

  let persistCalls = 0;
  r = await requestPersistentStorage({
    storage: { persisted: async () => true, persist: async () => { persistCalls++; return true; } },
  });
  check("an already-persisted origin is not re-requested",
    r.persisted === true && persistCalls === 0, `persistCalls=${persistCalls}`);
}

console.log(`\n${pass + fails.length} checks, ${fails.length} failed`);
if (fails.length) {
  for (const f of fails) console.log(`  FAILED: ${f}`);
  process.exit(1);
}
console.log("ALL OUTBOX CHECKS PASS");
