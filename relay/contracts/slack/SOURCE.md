# Slack incoming webhook: where each fixture comes from

Retrieved 2026-10-22 from pages saved in the research run's evidence folder (docs.slack.dev, no version date on the pages). Nothing here was captured from a real Slack workspace, so no fixture is `observed`.

Provenance tags: `doc` means copied or derived from a vendor page. `ASSUMED` means the evidence does not state it, a test says what would settle it.

| Fixture | Provenance | Source URL | What it holds |
|---|---|---|---|
| `hello.doc.json` | doc | https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks | The minimal request body, `{"text": "Hello, world."}`, and the plain `ok` answer. |
| `review_blocks.doc.json` | doc | https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks | The page's advanced Block Kit example, byte for byte in structure. It uses link syntax on purpose, so it is run against a fake with the relay's own strict rules switched off. |
| `errors.doc.json` | doc for the error names, ASSUMED for the status each one arrives with | https://docs.slack.dev/messaging/sending-messages-using-incoming-webhooks | The error names the page lists. The page says incoming webhooks return "more relevant HTTP status codes (like 400, 403, 404)" and points to a changelog that was not retrieved, so which name comes with which status is ASSUMED. Settled by one real delivery. |
| `rate_limit.doc.json` | doc | https://docs.slack.dev/apis/web-api/rate-limits | `429` with `Retry-After` in seconds, and about one message per second per channel. |
| `limits.doc.json` | doc | https://docs.slack.dev/reference/block-kit/blocks and the section, header and context block pages | 50 blocks per message, section text up to 3000 characters, header text up to 150, the 4000 character recommendation for `text` and the 40000 character truncation point. |

## What the fake adds that Slack does not say

`tests/fakes/slack.py` also refuses a raw `<`, `>` or bare `&` in mrkdwn text and an mrkdwn object without `verbatim: true`. Slack would accept both and silently reinterpret the text. The fake refuses them so a test goes red if untrusted text ever reaches Slack unescaped. These two rules are the relay's own, not Slack's.

## Not covered

No idempotency key is documented for incoming webhooks, so a repeat send can post twice. No retry policy is documented for a 5xx. The relay treats a 5xx or a timeout as ambiguous and retries, and says so in the README.
