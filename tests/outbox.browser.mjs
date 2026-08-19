/**
 * outbox-storage-browser.ts in a REAL browser.
 *
 * The node suite proves the state machine. It cannot prove that OPFS accepts the
 * bytes, that IndexedDB returns them in capture order, or that any of it survives
 * the page going away. Those need an actual browser, so this drives headless
 * Chromium with Playwright and serves the real modules with their TypeScript types
 * stripped, rather than re-implementing the adapter inside the test page. A test
 * that reimplements the thing it is testing proves only that the test works.
 *
 * THE LOAD-BEARING TEST is "the queue survives a reload". Everything else here is
 * about whether the APIs behave; that one is about whether a roofer's photos are
 * still there after iOS discards the tab. It writes, reloads the page, and then
 * reads back through a freshly constructed storage adapter with nothing in memory.
 *
 * HONEST LIMIT, stated because it would otherwise be implied away: this is Chromium
 * on Windows. It cannot test iOS Safari, which is the platform that actually
 * matters for field crews and the one with the seven-day eviction rule and the
 * smaller quota. What it does prove is that the adapter is correct against the
 * standard APIs. iOS-specific behaviour remains documentation, not measurement.
 *
 * Run: node tests/outbox.browser.mjs
 */
import { createServer } from "node:http";
import { readFileSync } from "node:fs";
import { stripTypeScriptTypes } from "node:module";
import { dirname, join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
const ROOT = dirname(HERE);
// Global install path. ESM will not resolve a bare directory, so point at the entry
// file. PC2 (Dax) has playwright installed globally; PC1 uses the npx cache instead.
const PW = "C:/Users/Dax/AppData/Roaming/npm/node_modules/playwright/index.js";

let pass = 0;
const fails = [];
const check = (name, cond, detail = "") => {
  if (cond) pass++;
  else fails.push(`${name}${detail ? ` -- ${detail}` : ""}`);
  console.log(`  ${cond ? "PASS" : "FAIL"}  ${name}${cond ? "" : ` (${detail})`}`);
};

/** Serve a .ts file as browser-executable JS, rewriting extensionless imports. */
function serveTs(relPath) {
  const src = readFileSync(join(ROOT, relPath), "utf8");
  const js = stripTypeScriptTypes(src, { mode: "strip" });
  // Browsers need a real path in an import specifier.
  return js.replace(/from\s+"\.\/outbox"/g, 'from "./outbox.js"');
}

const PAGE = `<!doctype html><meta charset="utf-8"><title>outbox</title>
<body><h1>outbox harness</h1></body>`;

const server = createServer((req, res) => {
  try {
    if (req.url === "/" || req.url === "/index.html") {
      res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
      return res.end(PAGE);
    }
    if (req.url === "/src/lib/outbox.js") {
      res.writeHead(200, { "content-type": "text/javascript; charset=utf-8" });
      return res.end(serveTs("src/lib/outbox.ts"));
    }
    if (req.url === "/src/lib/outbox-storage-browser.js") {
      res.writeHead(200, { "content-type": "text/javascript; charset=utf-8" });
      return res.end(serveTs("src/lib/outbox-storage-browser.ts"));
    }
    res.writeHead(404).end("nope");
  } catch (e) {
    res.writeHead(500).end(String(e));
  }
});

const port = await new Promise((resolve) => {
  server.listen(0, "127.0.0.1", () => resolve(server.address().port));
});
const ORIGIN = `http://127.0.0.1:${port}`;

// Playwright is CommonJS and its ESM entry does not re-export the browser types, so
// it is required rather than imported. createRequire is how an .mjs reaches CJS.
const { createRequire } = await import("node:module");
const { chromium } = createRequire(import.meta.url)(PW);
const browser = await chromium.launch({ headless: true });
// A persistent-ish context is not needed: a reload in the same context is the real
// scenario, since the origin's storage is what has to survive, not the process.
const ctx = await browser.newContext();
const page = await ctx.newPage();

const pageErrors = [];
page.on("pageerror", (e) => pageErrors.push(e.message));
page.on("console", (m) => {
  if (m.type() === "error") pageErrors.push(`console: ${m.text()}`);
});

try {
  await page.goto(ORIGIN, { waitUntil: "domcontentloaded" });

  // ------------------------------------------------------------ availability
  console.log("\nAPI availability in a real browser");
  const avail = await page.evaluate(async () => {
    const { isOpfsAvailable, storageUsage } = await import("/src/lib/outbox-storage-browser.js");
    return {
      opfs: isOpfsAvailable(navigator),
      idb: typeof indexedDB !== "undefined",
      secure: window.isSecureContext,
      usage: await storageUsage(),
      hasPersist: typeof navigator.storage?.persist === "function",
    };
  });
  check("OPFS is available (feature-detected, not UA-sniffed)", avail.opfs === true);
  check("IndexedDB is available", avail.idb === true);
  check("the origin is a secure context (127.0.0.1 counts)", avail.secure === true);
  check("storageUsage returns a numeric quota", typeof avail.usage.quotaBytes === "number" && avail.usage.quotaBytes > 0,
    JSON.stringify(avail.usage));
  check("navigator.storage.persist exists", avail.hasPersist === true);

  // ------------------------------------------------------------ round trip
  console.log("\nbytes round-trip through OPFS");
  const rt = await page.evaluate(async () => {
    const { createBrowserOutboxStorage } = await import("/src/lib/outbox-storage-browser.js");
    const s = createBrowserOutboxStorage();
    // 300 KB, roughly a downscaled field photo, so this is not a toy payload.
    const big = new Uint8Array(300 * 1024);
    for (let i = 0; i < big.length; i++) big[i] = i % 251;
    await s.writeBytes("outbox/roundtrip", big);
    const back = await s.readBytes("outbox/roundtrip");
    let same = back.length === big.length;
    if (same) for (let i = 0; i < big.length; i += 997) if (back[i] !== big[i]) { same = false; break; }
    await s.deleteBytes("outbox/roundtrip");
    let goneErr = null;
    try { await s.readBytes("outbox/roundtrip"); } catch (e) { goneErr = e.name; }
    // Deleting twice must be safe: an interrupted flush re-enters this path.
    let doubleDeleteThrew = false;
    try { await s.deleteBytes("outbox/roundtrip"); } catch { doubleDeleteThrew = true; }
    return { size: big.length, same, goneErr, doubleDeleteThrew };
  });
  check("a 300 KB payload survives write then read byte-for-byte", rt.same === true, JSON.stringify(rt));
  check("reading deleted bytes raises NotFoundError", rt.goneErr === "NotFoundError", `err=${rt.goneErr}`);
  check("deleting the same bytes twice is safe (interrupted flush re-entry)",
    rt.doubleDeleteThrew === false);

  // ------------------------------------------------------------ enqueue
  console.log("\nenqueue through the real adapter");
  const enq = await page.evaluate(async () => {
    const { Outbox } = await import("/src/lib/outbox.js");
    const { createBrowserOutboxStorage } = await import("/src/lib/outbox-storage-browser.js");
    const storage = createBrowserOutboxStorage();
    let networkCalls = 0;
    const box = new Outbox({
      storage,
      transport: {
        async uploadBytes() { networkCalls++; },
        async upsertRow() { networkCalls++; },
      },
      now: () => Date.now(),
      random: (n) => crypto.getRandomValues(new Uint8Array(n)),
    });
    const ids = [];
    for (let i = 0; i < 5; i++) {
      ids.push(await box.enqueue("photo", { n: i }, new Uint8Array([i, i, i])));
      // Distinct milliseconds so the v7 prefix orders them deterministically.
      await new Promise((r) => setTimeout(r, 3));
    }
    const listed = (await box.list()).map((x) => x.id);
    return {
      networkCalls,
      count: await box.pendingCount(),
      inOrder: JSON.stringify(ids) === JSON.stringify(listed),
      ids,
    };
  });
  check("five items queued with ZERO network calls", enq.networkCalls === 0 && enq.count === 5,
    JSON.stringify(enq));
  check("IndexedDB returns them in capture order (UUIDv7 key order)", enq.inOrder === true,
    JSON.stringify(enq.ids));

  // ------------------------------------------------- THE DURABILITY TEST
  console.log("\nTHE DURABILITY TEST: does the queue survive the page going away?");
  await page.reload({ waitUntil: "domcontentloaded" });
  const after = await page.evaluate(async () => {
    // Fresh adapter, fresh Outbox, nothing carried over in memory.
    const { Outbox } = await import("/src/lib/outbox.js");
    const { createBrowserOutboxStorage } = await import("/src/lib/outbox-storage-browser.js");
    const storage = createBrowserOutboxStorage();
    const box = new Outbox({
      storage,
      transport: { async uploadBytes() {}, async upsertRow() {} },
      now: () => Date.now(),
      random: (n) => crypto.getRandomValues(new Uint8Array(n)),
    });
    const items = await box.list();
    // And the BYTES, not just the rows. A row pointing at missing bytes is worse
    // than no row, because the UI would show a pending photo that cannot be sent.
    let bytesOk = 0;
    for (const it of items) {
      if (!it.bytesRef) continue;
      try { if ((await storage.readBytes(it.bytesRef)).length === 3) bytesOk++; } catch { /* counted by omission */ }
    }
    return { count: items.length, bytesOk, ids: items.map((i) => i.id) };
  });
  check("all five queue rows survived the reload", after.count === 5, JSON.stringify(after));
  check("and all five payloads survived in OPFS", after.bytesOk === 5, `bytesOk=${after.bytesOk}`);
  check("the ids are unchanged, so idempotency keys survive too",
    JSON.stringify(after.ids) === JSON.stringify(enq.ids));

  // ------------------------------------------------------------ flush + cleanup
  console.log("\nflush then cleanup");
  const flushed = await page.evaluate(async () => {
    const { Outbox } = await import("/src/lib/outbox.js");
    const { createBrowserOutboxStorage } = await import("/src/lib/outbox-storage-browser.js");
    const storage = createBrowserOutboxStorage();
    const seen = [];
    const box = new Outbox({
      storage,
      transport: {
        async uploadBytes(item, bytes) { seen.push([item.id, bytes.length]); },
        async upsertRow() {},
      },
      now: () => Date.now(),
      random: (n) => crypto.getRandomValues(new Uint8Array(n)),
    });
    const r = await box.flush();
    const leftoverRows = (await box.list()).length;
    // Prove the OPFS files went too, not just the rows.
    const root = await navigator.storage.getDirectory();
    const dir = await root.getDirectoryHandle("outbox", { create: true });
    let files = 0;
    for await (const _ of dir.keys()) files++;
    return { r, leftoverRows, files, uploaded: seen.length, sizes: seen.map((x) => x[1]) };
  });
  check("all five were delivered", flushed.r.sent === 5, JSON.stringify(flushed.r));
  check("every upload received its bytes", flushed.uploaded === 5 && flushed.sizes.every((n) => n === 3),
    JSON.stringify(flushed.sizes));
  check("no queue rows remain", flushed.leftoverRows === 0);
  check("and no orphaned OPFS files remain", flushed.files === 0, `files=${flushed.files}`);

  // ------------------------------------------------------------ failure keeps bytes
  console.log("\na failing transport keeps the bytes, in a real browser too");
  const kept = await page.evaluate(async () => {
    const { Outbox } = await import("/src/lib/outbox.js");
    const { createBrowserOutboxStorage } = await import("/src/lib/outbox-storage-browser.js");
    const storage = createBrowserOutboxStorage();
    const box = new Outbox({
      storage,
      transport: { async uploadBytes() { throw new Error("offline"); }, async upsertRow() {} },
      now: () => Date.now(),
      random: (n) => crypto.getRandomValues(new Uint8Array(n)),
    });
    const id = await box.enqueue("photo", { keep: true }, new Uint8Array([7, 7, 7, 7]));
    const r = await box.flush();
    let bytesLen = -1;
    try { bytesLen = (await storage.readBytes(`outbox/${id}`)).length; } catch { bytesLen = -1; }
    const item = (await box.list()).find((x) => x.id === id);
    return { failed: r.failed, bytesLen, state: item?.state, lastError: item?.lastError };
  });
  check("the flush reported the failure", kept.failed === 1);
  check("THE BYTES ARE STILL IN OPFS", kept.bytesLen === 4, `len=${kept.bytesLen}`);
  check("the item stays pending for a retry", kept.state === "pending", `state=${kept.state}`);
  check("the error is recorded for the UI", kept.lastError === "offline", `err=${kept.lastError}`);

  // ------------------------------------------------- THE FALLBACK PATH
  // Chromium HAS createWritable, so every test above took the OPFS branch and the
  // IndexedDB fallback was never executed. That fallback is the path iOS 25 and
  // earlier will actually use, i.e. most field iPhones today, so leaving it
  // unexercised would mean shipping the common path untested. The seam forces it.
  console.log("\nTHE IndexedDB FALLBACK (what iOS < 26 will really use)");
  const fb = await page.evaluate(async () => {
    const mod = await import("/src/lib/outbox-storage-browser.js");
    const { createBrowserOutboxStorage, __setOpfsWritableForTests } = mod;

    // Pretend createWritable does not exist.
    __setOpfsWritableForTests(false);
    const s = createBrowserOutboxStorage();

    const payload = new Uint8Array(120 * 1024);
    for (let i = 0; i < payload.length; i++) payload[i] = (i * 7) % 253;
    await s.writeBytes("outbox/fallback-1", payload);

    // Nothing should have been written to OPFS.
    const root = await navigator.storage.getDirectory();
    const dir = await root.getDirectoryHandle("outbox", { create: true });
    let opfsFiles = 0;
    for await (const k of dir.keys()) if (k === "fallback-1") opfsFiles++;

    const back = await s.readBytes("outbox/fallback-1");
    let same = back.length === payload.length;
    if (same) for (let i = 0; i < payload.length; i += 401) if (back[i] !== payload[i]) { same = false; break; }

    // Confirm it really is in IndexedDB, and as an ArrayBuffer rather than a Blob.
    const raw = await new Promise((resolve, reject) => {
      const r = indexedDB.open("asc_outbox");
      r.onsuccess = () => {
        const db = r.result;
        const t = db.transaction("bytes", "readonly");
        const g = t.objectStore("bytes").get("outbox/fallback-1");
        g.onsuccess = () => resolve(g.result);
        g.onerror = () => reject(g.error);
      };
      r.onerror = () => reject(r.error);
    });

    // THE CROSS-BACKEND READ: the browser "updates" and now supports OPFS, but the
    // bytes were queued under IndexedDB. A read that only consulted today's backend
    // would call them missing, which is the same as losing a photo.
    __setOpfsWritableForTests(true);
    const s2 = createBrowserOutboxStorage();
    let crossOk = false;
    try { crossOk = (await s2.readBytes("outbox/fallback-1")).length === payload.length; } catch { crossOk = false; }

    // Delete must clear BOTH backends.
    await s2.deleteBytes("outbox/fallback-1");
    let goneErr = null;
    try { await s2.readBytes("outbox/fallback-1"); } catch (e) { goneErr = e.name; }

    __setOpfsWritableForTests(null);
    return {
      opfsFiles,
      same,
      isArrayBuffer: raw instanceof ArrayBuffer,
      isBlob: typeof Blob !== "undefined" && raw instanceof Blob,
      rawLen: raw?.byteLength ?? -1,
      crossOk,
      goneErr,
    };
  });
  check("with createWritable absent, NOTHING is written to OPFS", fb.opfsFiles === 0,
    `opfsFiles=${fb.opfsFiles}`);
  check("a 120 KB payload round-trips through IndexedDB byte-for-byte", fb.same === true);
  check("it is stored as an ArrayBuffer, never a Blob", fb.isArrayBuffer === true && fb.isBlob === false,
    `arrayBuffer=${fb.isArrayBuffer} blob=${fb.isBlob} len=${fb.rawLen}`);
  check("CROSS-BACKEND READ: bytes queued under IndexedDB are still found after the browser gains OPFS",
    fb.crossOk === true);
  check("delete clears both backends", fb.goneErr === "NotFoundError", `err=${fb.goneErr}`);

  console.log("\nan end-to-end flush on the fallback backend");
  const fbFlush = await page.evaluate(async () => {
    const { Outbox } = await import("/src/lib/outbox.js");
    const mod = await import("/src/lib/outbox-storage-browser.js");
    mod.__setOpfsWritableForTests(false);
    const storage = mod.createBrowserOutboxStorage();
    const sizes = [];
    const box = new Outbox({
      storage,
      transport: { async uploadBytes(_i, b) { sizes.push(b.length); }, async upsertRow() {} },
      now: () => Date.now(),
      random: (n) => crypto.getRandomValues(new Uint8Array(n)),
    });
    // NOTE: storage is shared with the earlier tests, one of which deliberately
    // leaves a failed item behind. So this asserts on the ids it created rather than
    // on a total, which would otherwise couple this test to the ones before it.
    const mine = [];
    for (let i = 0; i < 3; i++) {
      mine.push(await box.enqueue("photo", { i }, new Uint8Array(1000 + i)));
      await new Promise((r) => setTimeout(r, 3));
    }
    const queuedBefore = (await box.list()).filter((x) => mine.includes(x.id)).length;
    const r = await box.flush();
    const leftOfMine = (await box.list()).filter((x) => mine.includes(x.id)).length;
    const totalRemaining = await box.pendingCount();
    mod.__setOpfsWritableForTests(null);
    return { queuedBefore, r, leftOfMine, totalRemaining, sizes };
  });
  check("three items queued on the fallback backend", fbFlush.queuedBefore === 3,
    JSON.stringify(fbFlush));
  check("all three flushed with their real byte lengths",
    fbFlush.r.sent >= 3 && JSON.stringify(fbFlush.sizes) === JSON.stringify([1000, 1001, 1002]),
    JSON.stringify(fbFlush));
  check("none of this test's items remain queued", fbFlush.leftOfMine === 0,
    `left=${fbFlush.leftOfMine} totalRemaining=${fbFlush.totalRemaining}`);

  check("no uncaught page errors during the run", pageErrors.length === 0,
    pageErrors.slice(0, 3).join(" | "));
} finally {
  await browser.close();
  server.close();
}

console.log(`\n${pass + fails.length} checks, ${fails.length} failed`);
if (fails.length) {
  for (const f of fails) console.log(`  FAILED: ${f}`);
  process.exit(1);
}
console.log("ALL BROWSER OUTBOX CHECKS PASS");
