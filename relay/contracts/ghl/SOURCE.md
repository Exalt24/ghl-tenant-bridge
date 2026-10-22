# HighLevel webhooks and attribution: where each fixture comes from

Retrieved 2026-10-22 from pages saved in the research run's evidence folder (`evidence/D-ghl-railway`). Nothing here is a real HighLevel delivery, so no fixture is `observed`, and no real HighLevel delivery has reached the relay.

Provenance tags: `doc` means copied or derived from a vendor page. `ASSUMED` means the evidence does not state it, and a test or a comment says what would settle it.

| Fixture | Provenance | Source URL | What it holds |
|---|---|---|---|
| `contact_create.doc.json` | doc | https://marketplace.gohighlevel.com/docs/webhook/ContactCreate | The page's example, as printed (the page's email has a typo, `gmail.comm`, kept as is). A flat object. |
| `appointment_create.doc.json` | doc | https://marketplace.gohighlevel.com/docs/webhook/AppointmentCreate | The page's example. `id`, `dateAdded` and `dateUpdated` are nested under `appointment`, which is why the relay reads them per event type. |
| `opportunity_create.doc.json` | doc | https://marketplace.gohighlevel.com/docs/webhook/OpportunityCreate | The page's example. `status` is a plain string (`open`) and there is no currency. "Won" is not shown on a saved page: the relay maps `status == "won"` of `OpportunityStatusUpdate` to a Purchase and marks that mapping ASSUMED. |
| `webhook_signature.doc.json` | doc for the headers, the public key, the retry rule and the envelope; ASSUMED for which bytes are signed | https://marketplace.gohighlevel.com/docs/webhook/WebhookIntegrationGuide | `X-GHL-Signature` (Ed25519) and the legacy `X-WH-Signature`, the published public key, retries up to 12 times, the envelope example with `data`. The vendor's sample verifies `JSON.stringify(req.body)`, so the relay accepts the raw bytes or the compact re-serialisation. |
| `attribution.doc.json` | doc | https://marketplace.gohighlevel.com/docs/ghl/contacts/get-contact, https://help.gohighlevel.com/support/solutions/articles/155000003305 and .../48001238167 | The Get Contact attribution keys, the flat workflow keys the relay also reads, and what the saved help pages say about the Custom Webhook action (no signature documented, author defined headers and body). |

## What the relay does that HighLevel does not document

- The per event pages show a flat object with no `webhookId`; the integration guide's envelope has `type`, `timestamp`, `webhookId` and `data`. The two disagree. The relay reads either shape, dedupes on a native `webhookId` when there is one, and otherwise builds a key from the type, the entity id, the stamp and a hash of the raw body (ASSUMED, confirm on a live delivery).
- No timestamp window by default: HighLevel retries up to 12 times with jitter and what `timestamp` means on a retry is not documented. A tenant can enable one (not older than 30 days, not more than 5 minutes ahead, ASSUMED, copied from the Worker in `edge/`).
- A request with only `X-WH-Signature` is refused. The page announces its deprecation for 2026-09-01; whether it still arrives was not observed.

## Not covered

A signature from the real HighLevel key (its private half is not ours), and any real delivery.
