# Fathom: where each fixture comes from

Retrieved 2026-10-22 from pages saved in the research run's evidence folder (developers.fathom.ai: webhooks, faq, api-overview, and the OpenAPI file with its list-meetings and new-meeting-content-ready pages). Nothing here was captured from a real Fathom account, so no fixture is `observed`.

Provenance tags: `doc` means copied or derived from a vendor page. `ASSUMED` means the evidence does not state it, and a test or a comment says what would settle it.

| Fixture | Provenance | Source URL | What it holds |
|---|---|---|---|
| `webhook_payload.doc.json` | doc | https://developers.fathom.ai/api-reference/openapi.yaml (the `newMeeting` webhook example) | The documented webhook body: a meeting with `recording_id`, `title`, `default_summary.markdown_formatted`, `action_items`, a transcript and invitees. Made-up names and addresses from the page. |
| `webhook_headers.doc.json` | doc | https://developers.fathom.ai/webhooks and https://developers.fathom.ai/api-reference/openapi.yaml | The three headers, the page's example id, timestamp and signature value, the `v1,<b64> v2,<b64>` multi-signature example, the page's example secret, and the seven verification steps. |
| `list_meetings.doc.json` | doc | https://developers.fathom.ai/api-reference/openapi.yaml (`GET /meetings`) | The query parameters the puller uses, the reply shape `{limit, next_cursor, items}` with `next_cursor` null on the last page, and the 10 per page the FAQ gives. |
| `rate_limits.doc.json` | doc | https://developers.fathom.ai/api-overview | 60 calls per 60 seconds, 30 per 60 seconds for heavy calls (down to 5 under load), the `RateLimit-*` headers and `Retry-After` on a 429 only. |
| `delivery.doc.json` | doc | https://developers.fathom.ai/faq | What the FAQ says about delivery: a webhook is tied to the summary email, so no summary means no webhook; non-2xx responses or timeouts may trigger retries on an unpublished schedule; the `webhook-id` stays the same on a retry; no re-fire when content changes; the payload does not say which user's key it belongs to, so each user gets their own destination URL. |

## What the relay does that Fathom does not document

- The 5 minute timestamp tolerance is the page's "typically 5 minutes", a recommendation and not a vendor limit. It is a default and can be set per tenant.
- 60 and 30 calls per 60 seconds are Fathom's current defaults, which it may lower under load. The puller keeps inside them with its own limiter and also honours `Retry-After`.
- The retry schedule and the number of attempts are unpublished, so nothing about retry timing is assumed beyond "the same `webhook-id` comes again".
- A webhook body can carry a transcript, invitee emails and CRM matches. The relay stores a title, the summary and the action items, and nothing else of the body (the stored copy is a reduced record, not the delivery).
- The destination URL carries the tenant (`/webhooks/fathom/<tenant>`), which is how the FAQ says to tell users apart. Every tenant has its own secret.
- Whether Fathom accepts a 413 as final or retries it is not stated. The route's default cap of 2 MB is the relay's own choice, so that a transcript-sized body is not turned away.

## Not covered

A real signature from a real Fathom webhook. The page's example secret and signature value cannot be checked here because the page does not print the body they were made from, so verification is proven with signatures built from the documented recipe by code that does not import the relay (`tests/fakes/fathom.py`).
