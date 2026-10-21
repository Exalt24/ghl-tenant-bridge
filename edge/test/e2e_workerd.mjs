// End-to-end in the REAL Workers runtime (workerd, through `wrangler dev --local`) with a real local D1 database:
// signed deliveries go over HTTP, a duplicate and a forgery are refused, the cron handler is triggered, and a local
// sink proves each verified event is forwarded once with its Idempotency-Key. Run: node test/e2e_workerd.mjs
import { createServer } from "node:http";
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { setTimeout as sleep } from "node:timers/promises";
import { newKeyPair, sign, event } from "./helpers.mjs";

const edgeDir = new URL("../", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const PORT = 8787 + Math.floor(Math.random() * 200);
const results = [];
const check = (name, ok, detail = "") => { results.push(ok); console.log(`${ok ? "PASS" : "FAIL"}  ${name}${ok || !detail ? "" : `  (${detail})`}`); };

const received = [];
const sink = createServer((req, res) => {
  let body = "";
  req.on("data", (c) => (body += c));
  req.on("end", () => { received.push({ key: req.headers["idempotency-key"], auth: req.headers.authorization, body }); res.writeHead(200).end("ok"); });
});
await new Promise((r) => sink.listen(0, "127.0.0.1", r));
const sinkUrl = `http://127.0.0.1:${sink.address().port}/in`;

const keys = await newKeyPair();
const state = mkdtempSync(join(tmpdir(), "edge-e2e-"));
const wr = spawn(process.execPath, [join(edgeDir, "node_modules", "wrangler", "bin", "wrangler.js"), "dev", "--local", "--port", String(PORT), "--persist-to", state, "--test-scheduled",
  "--var", "ENVIRONMENT:test", "--var", `TEST_PUBLIC_KEY_B64:${keys.publicB64}`, "--var", `SINK_URL:${sinkUrl}`, "--var", "SINK_TOKEN:e2e-token"], { cwd: edgeDir, stdio: ["ignore", "pipe", "pipe"] });
let log = "";
wr.stdout.on("data", (d) => (log += d));
wr.stderr.on("data", (d) => (log += d));
const base = `http://127.0.0.1:${PORT}`;

async function up() {
  for (let i = 0; i < 150; i++) {
    // Any HTTP answer means workerd is listening; /health is 500 until the schema is applied, which happens next.
    try { await fetch(`${base}/health`); return true; } catch { /* not listening yet */ }
    await sleep(300);
  }
  return false;
}

async function post(body, headers = {}) {
  return fetch(`${base}/webhooks/ghl`, { method: "POST", headers: { "x-ghl-signature": await sign(keys.privateKey, body), ...headers }, body });
}

try {
  if (!(await up())) { console.log("FAIL  workerd never answered /health\n" + log.slice(-1500)); process.exit(1); }
  // The schema is applied the way a deploy would apply it.
  const exec = spawn(process.execPath, [join(edgeDir, "node_modules", "wrangler", "bin", "wrangler.js"), "d1", "execute", "ghl-edge", "--local", "--persist-to", state, "--file", join(edgeDir, "schema.sql")], { cwd: edgeDir, stdio: "ignore" });
  await new Promise((r) => exec.on("exit", r));

  const now = Date.now();
  const first = await post(event("e2e-1", now));
  check("signed delivery accepted in workerd", first.status === 200 && (await first.json()).duplicate === false, String(first.status));
  const dup = await post(event("e2e-1", now));
  check("the same webhookId again is a duplicate", dup.status === 200 && (await dup.json()).duplicate === true);
  const forged = await fetch(`${base}/webhooks/ghl`, { method: "POST", headers: { "x-ghl-signature": "AAAA" }, body: event("e2e-2", now) });
  check("a forged signature is 401", forged.status === 401);
  const second = await post(event("e2e-3", now));
  check("a second distinct delivery accepted", second.status === 200);

  const health = await (await fetch(`${base}/health`)).json();
  check("health shows two pending rows and no payload", health.inbox?.pending === 2 && !JSON.stringify(health).includes("loc_1"), JSON.stringify(health));

  // wrangler's --test-scheduled exposes the cron handler on /cdn-cgi/handler/scheduled.
  const tick = await fetch(`${base}/cdn-cgi/handler/scheduled`);
  check("the scheduled handler ran", tick.status === 200, String(tick.status));
  await sleep(500);
  check("each verified event reached the sink exactly once", received.length === 2 && new Set(received.map((r) => r.key)).size === 2, JSON.stringify(received.map((r) => r.key)));
  check("the sink got the bearer token and the original body", received.every((r) => r.auth === "Bearer e2e-token") && received[0].body.includes("e2e-1"));

  await fetch(`${base}/cdn-cgi/handler/scheduled`);
  await sleep(300);
  check("a second tick forwards nothing more", received.length === 2);
  const after = await (await fetch(`${base}/health`)).json();
  check("both rows are done", after.inbox?.done === 2, JSON.stringify(after));
} finally {
  wr.kill();
  sink.close();
  await sleep(500);
  try { rmSync(state, { recursive: true, force: true }); } catch { /* workerd may still hold a file; the OS temp cleaner takes it */ }
}
const failed = results.filter((r) => !r).length;
console.log(failed === 0 ? `\nALL ${results.length} WORKERD CHECKS PASSED` : `\n${failed} FAILED`);
process.exit(failed === 0 ? 0 : 1);
