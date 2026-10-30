# Edge receiver

A Cloudflare Worker in front of the bridge. It takes GoHighLevel webhooks, checks the Ed25519 signature, stores each delivery once in D1 and forwards it to a sink on a schedule, so a slow or down backend never costs a delivery.

```
GHL --POST /webhooks/ghl--> verify X-GHL-Signature --> D1 inbox (primary key = webhookId) --> 200
                                                          |
                                   |-- send {webhookId} --> Queue --> queue(): claim that row --> POST SINK_URL --> ack | retry | dead letter
                              cron every 5 min (sweeper): claim due rows --> POST SINK_URL (Idempotency-Key) --> done | retry with backoff | dead
```

## Why it is built this way

- Duplicates are stopped by the database. The inbox primary key is HighLevel's own `webhookId`, so two deliveries that arrive at the same moment cannot both insert. KV was not used for this on purpose: it is eventually consistent, and Cloudflare's docs say a change can take 60 seconds or more to show up in other locations.
- A drain claims rows in one `UPDATE ... RETURNING`, so two overlapping cron runs cannot hold the same row, and a claim that expires after two minutes is picked up again. Cloudflare's cron pages do not say what happens when a run overlaps the previous one, so the handler is safe either way.
- The state change and the attempt log are one `batch`, which D1 runs as a single transaction.
- HighLevel retries a failed delivery up to 12 times with exponential backoff. It publishes neither how long those retries last nor a replay window, so a delivery is accepted when its timestamp is younger than the 30 days the dedupe rows are kept, and refused when older.

## Run it

```
cd edge
npm install
npm run test:all
```

`test:all` runs the typecheck, 43 offline tests (a seeded property test among them), 28 mutants that must all be caught and 14 end-to-end checks in real workerd through `wrangler dev --local`. Last full run 2026-10-30: 43 of 43, 28 of 28, 14 of 14.

To deploy: `wrangler d1 create ghl-edge`, paste the id into `wrangler.toml`, `wrangler d1 execute ghl-edge --remote --file schema.sql`, `wrangler secret put SINK_TOKEN`, set `SINK_URL`, `wrangler deploy`.

## Limits

- The queue ran only in `wrangler dev --local` (Miniflare). No Cloudflare queue exists, `ghl-forward` and `ghl-forward-dlq` are not created, nothing is subscribed to the dead letter queue, and Cloudflare documents consumer concurrency as unsupported locally, so retry timing and concurrency are unproven on the real service. Queue delivery is at least once: a message can arrive twice, and the consumer acks a row that is already done instead of forwarding it again. If a send to the queue fails, the webhook is still answered 200 and the cron drain delivers the row.
- Not deployed. The free plan allows 5 cron triggers per account and the account this was built on uses all 5, so the cron is proven in workerd with `--test-scheduled`, not on the edge network.
- The signature check was proven against signatures made with a test key. The published HighLevel key is what production uses, and no real HighLevel delivery has been sent to this Worker.
- A sink answer of 400, 410, 413 or 422 marks the row `dead` at once and nothing requeues it; 401, 403, 404, 408 and 429 are retried up to six attempts with backoff, so a rotated token or a moved route that is fixed within about an hour (six attempts over 62 minutes) loses nothing, and a longer outage ends in `dead` rows that `/health` counts. To resend them: `UPDATE inbox SET state='pending', next_at=0 WHERE state='dead'` (leave `attempts` alone: the delivery log is keyed on webhook id plus attempt, so resetting it to 0 makes the next forward collide and the drain throw; each requeued row gets one more try and goes dead again if that fails).
- One sink, one tenant. The forward is at-least-once: if the sink handles a request and then times out, it sees the same `Idempotency-Key` again and has to ignore the repeat.
- Free-plan CPU time is 10 ms per request. The signature check and one insert fit, and no load test was run.
