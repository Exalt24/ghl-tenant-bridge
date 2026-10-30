// Mutation check: each mutant deletes or weakens ONE guard in a copy of core.ts, and the offline suite must go red.
// A guard whose removal leaves the suite green is a guard nobody tests. Run: node test/mutants.mjs
import { cpSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

const here = new URL("../", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const SRC = readFileSync(join(here, "src", "core.ts"), "utf8");

const MUTANTS = [
  ["signature check removed", `if (!(await verifySignature(env, raw, req.headers.get("x-ghl-signature")))) {`, `if (false) {`],
  ["legacy header accepted", `req.headers.get("x-ghl-signature")))) {`, `req.headers.get("x-ghl-signature") ?? req.headers.get("x-wh-signature")))) {`],
  ["test key honoured outside test", `env.ENVIRONMENT === "test" && env.TEST_PUBLIC_KEY_B64`, `env.TEST_PUBLIC_KEY_B64`],
  ["duplicate insert not ignored", ` ON CONFLICT(webhook_id) DO NOTHING`, ``],
  ["duplicate reported as new", `duplicate: res.meta.changes === 0`, `duplicate: false`],
  ["age window removed", `if (now - sent > MAX_AGE_MS || sent - now > MAX_FUTURE_MS) return json(400, { error: "stale-or-future" });`, ``],
  ["age window wider than retention", `export const MAX_AGE_MS = RETENTION_MS;`, `export const MAX_AGE_MS = RETENTION_MS * 2;`],
  ["body cap removed", `if (total > max) {`, `if (false) {`],
  ["content-length cap removed", `if (declared > max) return null;`, ``],
  ["claim ignores expired claims", ` OR (state = 'claimed' AND claim_until < ?)`, ``],
  ["claim ignores the batch limit", `ORDER BY next_at LIMIT ?)`, `ORDER BY next_at LIMIT 1000)`],
  ["claim does not mark the row claimed", `SET state = 'claimed', claim_until = ?, attempts = attempts + 1`, `SET claim_until = ?, attempts = attempts + 1`],
  ["outcome not batched", `await env.DB.batch([`, `for (const s of [`],
  ["idempotency key dropped", `"idempotency-key": row.webhook_id,`, ``],
  ["permanent 4xx retried", `const permanent = status !== null && status >= 400 && status < 500 && status !== 408 && status !== 429;`, `const permanent = false;`],
  ["attempt cap removed", `(permanent || row.attempts >= MAX_ATTEMPTS)`, `permanent`],
  ["backoff removed", `const backoffMs = (attempts: number): number => Math.min(60_000 * 2 ** attempts, 60 * 60_000);`, `const backoffMs = (_attempts: number): number => 0;`],
  ["retention prune removed", `await env.DB.prepare(\`DELETE FROM inbox WHERE state = 'done' AND received_at < ?\`).bind(now - RETENTION_MS).run();`, ``],
  // The queue: each guard below is one way at-least-once delivery turns into a double forward, a lost row or a poisoned batch.
  ["duplicate re-enqueued", `if (fresh && env.INBOX_QUEUE) {`, `if (env.INBOX_QUEUE) {`],
  ["a down queue fails the webhook", `} catch { /* the row is durable; the cron drain sweeps it */ }`, `} catch (e) { throw e; }`],
  ["body with no id retried", `if (typeof id !== "string" || id.length === 0) { msg.ack(); continue; }`, `if (false) { msg.ack(); continue; }`],
  ["consumer claims any due row", `(? IS NULL OR webhook_id = ?)`, `(? IS NULL OR webhook_id = webhook_id OR ? IS NULL)`],
  ["good forward not acked", `if (r.ok || r.dead) msg.ack();`, `if (r.dead) msg.ack();`],
  ["retry without backoff", `msg.retry({ delaySeconds: Math.ceil(backoffMs(row.attempts) / 1000) });`, `msg.retry();`],
  ["finished row not acked on redelivery", `if (!cur || cur.state === "done" || cur.state === "dead") msg.ack();`, `if (!cur) msg.ack();`],
  ["not-due row retried with no wait", `msg.retry({ delaySeconds: Math.min(3600, Math.max(30, Math.ceil((cur.next_at - now) / 1000))) });`, `msg.retry({ delaySeconds: 1 });`],
  ["one failing message throws the batch", `    } catch {\n      msg.retry({ delaySeconds: 60 });`, `    } catch (e) {\n      throw e;`],
];

let failed = 0;
for (const [name, from, to] of MUTANTS) {
  const count = SRC.split(from).length - 1;
  if (count !== 1) { console.log(`BROKEN MUTANT  ${name}: pattern found ${count} times`); failed++; continue; }
  let mutated = SRC.replace(from, () => to);
  // The two structural mutants need a syntactically complete replacement.
  if (name === "outcome not batched") {
    mutated = mutated.replace(`    ),\n    env.DB.prepare(\`INSERT INTO deliveries (webhook_id, attempt, at, status, ok) VALUES (?, ?, ?, ?, ?)\`).bind(\n      row.webhook_id, row.attempts, now, status, ok ? 1 : 0\n    ),\n  ]);`, `    ),\n    env.DB.prepare(\`INSERT INTO deliveries (webhook_id, attempt, at, status, ok) VALUES (?, ?, ?, ?, ?)\`).bind(\n      row.webhook_id, row.attempts, now, status, ok ? 1 : 0\n    ),\n  ]) await s.run();`);
  }
  const dir = mkdtempSync(join(tmpdir(), "edge-mutant-"));
  try {
    cpSync(join(here, "src"), join(dir, "src"), { recursive: true });
    cpSync(join(here, "test"), join(dir, "test"), { recursive: true });
    cpSync(join(here, "schema.sql"), join(dir, "schema.sql"));
    cpSync(join(here, "wrangler.toml"), join(dir, "wrangler.toml"));
    writeFileSync(join(dir, "src", "core.ts"), mutated);
    const r = spawnSync(process.execPath, ["--test", "test/worker.test.mjs"], { cwd: dir, encoding: "utf8", timeout: 120000 });
    const caught = r.status !== 0;
    console.log(`${caught ? "caught " : "SURVIVED"}  ${name}`);
    if (!caught) failed++;
  } finally {
    rmSync(dir, { recursive: true, force: true });
  }
}
console.log(failed === 0 ? `\nALL ${MUTANTS.length} MUTANTS CAUGHT` : `\n${failed} PROBLEM(S)`);
process.exitCode = failed === 0 ? 0 : 1;
