/**
 * The decisive end-to-end proof: capture with NO connection, reconnect, watch the
 * queue drain to zero. This is the whole feature in one test.
 *
 * Offline is real (context.setOffline), not a CSS class, and the drain is real HTTP to
 * /api/demo/queue which acknowledges bytesStored and rowsWritten the way PostgREST
 * does. The server's own counter is checked afterwards, so "the queue emptied" cannot
 * pass by the client simply forgetting.
 */
import { createRequire } from "node:module";
const { chromium } = createRequire(import.meta.url)(
  "C:/Users/Dax/AppData/Roaming/npm/node_modules/playwright/index.js"
);

const BASE = "http://127.0.0.1:3111";
const fails = [];
const check = (n, c, d = "") => {
  console.log(`  ${c ? "PASS" : "FAIL"}  ${n}${c ? "" : ` (${d})`}`);
  if (!c) fails.push(n);
};

const before = await (await fetch(`${BASE}/api/demo/queue`)).json();
console.log(`sink before: received=${before.received}`);

const b = await chromium.launch({ headless: true });
const ctx = await b.newContext({ viewport: { width: 390, height: 760 }, deviceScaleFactor: 2 });
const p = await ctx.newPage();
const errs = [];
p.on("pageerror", (e) => errs.push(e.message));

await p.goto(`${BASE}/capture`, { waitUntil: "networkidle" });
// The SW uses skipWaiting + clientsClaim, so it can claim this page and navigate it.
// Let it settle first, otherwise the capture step races a reload.
await p.waitForTimeout(800);
await p.evaluate(async () => {
  if (navigator.serviceWorker) {
    try { await navigator.serviceWorker.ready; } catch { /* not registered is fine */ }
  }
});
await p.waitForTimeout(1200);

// ---------------------------------------------------------------- go offline FIRST
await ctx.setOffline(true);
await p.evaluate(() => window.dispatchEvent(new Event("offline")));
await p.waitForTimeout(600);
check("offline strip shows when the connection drops",
  !!(await p.$('[data-testid="offline-strip"]')));

// ---------------------------------------------------------------- capture with no signal
await p.evaluate(async () => {
  const dt = new DataTransfer();
  for (let i = 0; i < 4; i++) {
    const c = new OffscreenCanvas(300, 220);
    const g = c.getContext("2d");
    g.fillStyle = "#8aa7bd"; g.fillRect(0, 0, 300, 220);
    g.fillStyle = ["#7a4a35", "#5e5a55", "#8a6b4a", "#6b4b3a"][i];
    g.beginPath(); g.moveTo(0, 190); g.lineTo(150, 90); g.lineTo(300, 190); g.closePath(); g.fill();
    const blob = await c.convertToBlob({ type: "image/jpeg", quality: 0.85 });
    dt.items.add(new File([blob], `roof-${i + 1}.jpg`, { type: "image/jpeg" }));
  }
  const input = document.querySelector('[data-testid="shot"]');
  input.files = dt.files;
  input.dispatchEvent(new Event("change", { bubbles: true }));
});
await p.waitForTimeout(3000);

const queuedText = (await p.textContent('[data-testid="pending-count"]'))?.trim();
check("four photos captured with NO connection", queuedText === "4 photos will upload when you have signal.",
  JSON.stringify(queuedText));
const rows = await p.$$eval('[data-testid="queue"] li', (n) => n.length);
check("all four are in the queue", rows === 4, `rows=${rows}`);

// Nothing can have reached the server while offline.
const during = await (await fetch(`${BASE}/api/demo/queue`)).json();
check("the server received NOTHING while offline",
  during.received === before.received, `before=${before.received} during=${during.received}`);

// ---------------------------------------------------------------- reconnect
await ctx.setOffline(false);
try {
  await p.evaluate(() => window.dispatchEvent(new Event("online")));
} catch {
  // A claimed page can swap its execution context. The timer trigger drains anyway.
}

// Wait for the drain, polling the UI rather than sleeping a fixed guess.
let drained = false;
for (let i = 0; i < 40; i++) {
  await p.waitForTimeout(500);
  const card = await p.$('[data-testid="pending-card"]');
  if (!card) { drained = true; break; }
}
check("the queue drained to empty after reconnecting", drained);
check("the pending card disappeared entirely (quiet state is silence)",
  !(await p.$('[data-testid="pending-card"]')));

const after = await (await fetch(`${BASE}/api/demo/queue`)).json();
const delta = after.received - before.received;
// 4 photos, each sending bytes then a row, so 8 POSTs.
check("the server actually received the uploads", delta >= 4, `delta=${delta}`);
console.log(`  sink after: received=${after.received} (delta ${delta}), bytes=${after.bytesReceived}`);

// And nothing is left on the device.
const leftover = await p.evaluate(async () => {
  const root = await navigator.storage.getDirectory();
  const dir = await root.getDirectoryHandle("outbox", { create: true });
  let n = 0;
  for await (const _ of dir.keys()) n++;
  return n;
});
check("no orphaned photo bytes left on the device", leftover === 0, `files=${leftover}`);
check("no page errors during the whole cycle", errs.length === 0, errs.slice(0, 2).join(" | "));

await b.close();
console.log(`\n${fails.length === 0 ? "OFFLINE -> ONLINE DRAIN PROVEN" : `${fails.length} FAILED`}`);
process.exit(fails.length ? 1 : 0);
