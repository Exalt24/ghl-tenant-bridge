# Make custom webhook: where each fixture comes from

Retrieved 2026-10-22 from pages saved in the research run's evidence folder (help.make.com/webhooks updated 21 Oct 2026, apps.make.com/gateway updated 04 Aug 2026). Nothing here was captured from a real Make account, so no fixture is `observed`.

Provenance tags: `doc` means copied or derived from a vendor page. `ASSUMED` means the evidence does not state it, a test says what would settle it.

| Fixture | Provenance | Source URL | What it holds |
|---|---|---|---|
| `responses.doc.json` | doc | https://help.make.com/webhooks | The default answers: 200 `Accepted` (a queue receipt only), 400 `Queue is full`, 429 `Too many requests`. The page says a Webhook response module can replace the default answer, so the relay treats any 2xx as accepted. |
| `limits.doc.json` | doc | https://help.make.com/webhooks and https://apps.make.com/gateway | 300 requests per 10 seconds, 410 Gone for a hook inactive for 5 days, the 5 MB payload ceiling (5,242,880 bytes, stated on the gateway page only because the separate technical limits page was a 404 in the evidence). |
| `apikey.doc.json` | doc for the header name, ASSUMED for the failure status | https://apps.make.com/gateway | The optional `x-make-apikey` header. The evidence does not say what Make answers to a wrong or missing key. The fake answers 401, which is ASSUMED. One real call settles it. |

## What the relay adds that Make does not document

`Idempotency-Key: <event id>` is the relay's own header. Make documents no idempotency header, and whether a scenario can use it was not tested, so the README makes no claim about it. Delivery to Make is at least once: a 5xx or a timeout is ambiguous and is retried.

## Not covered

Whether the scenario behind the hook actually ran. A 200 means the call was queued, nothing more.
