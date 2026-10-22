# Meta Conversions API: where each fixture comes from

Retrieved 2026-10-22 from pages saved in the research run's evidence folder (`evidence/A-capi`). Nothing here was captured from a real Meta account, a real Pixel or a real token, so no fixture is `observed`.

Provenance tags: `doc` means copied or derived from a vendor page. `ASSUMED` means the evidence does not state it, and a test or a comment says what would settle it.

| Fixture | Provenance | Source URL | What it holds |
|---|---|---|---|
| `hashing.doc.json` | doc | https://developers.facebook.com/docs/marketing-api/conversions-api/parameters/customer-information-parameters (updated Jan 9, 2026) | Input, normalised form and SHA-256 for email, phone, first name (three spellings) and country, exactly as printed on the page. City, state and zip carry the page's normalised examples with no digest, so the test computes the hash. The four never-hash fields. |
| `fbc.doc.json` | doc | https://developers.facebook.com/docs/marketing-api/conversions-api/parameters/fbp-and-fbc (updated Jan 9, 2026) | The fbc format, creation time in milliseconds, subdomain index 1 on a server, the case sensitive fbclid, the page's example fbc and fbp. |
| `server_event.doc.json` | doc | https://developers.facebook.com/docs/marketing-api/conversions-api/using-the-api, `.../parameters/server-event-parameters` (updated Oct 15, 2026) and `.../deduplicate-pixel-and-server-events` | Endpoint shape, required fields, website event requirements, the 7 day event_time rule, action_source values, the `test_event_code` position and what it does, the 48 hour Pixel and server window. |
| `errors.doc.json` | doc for the error object, the code names and the usage header, ASSUMED for which HTTP status each code arrives with | https://developers.facebook.com/docs/graph-api/guides/error-handling and https://developers.facebook.com/documentation/ads-commerce/marketing-api/overview/rate-limiting | The Graph error object, codes 1, 2, 4, 17, 102, 190, 341 from the generic table, 613 and the 80000 series from the rate limit page, and `estimated_time_to_regain_access` in minutes. |
| `response.assumed.json` | ASSUMED | none | A plausible success body. No saved page shows one. A 2xx is the only documented success signal. |

## What the relay does that Meta does not document

- UTM values go in `custom_data` as `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term`. Meta's Conversions API docs define no UTM fields for `custom_data`; Meta's URL builder page (https://www.facebook.com/business/google_analytics/build_your_url) defines those five names for ad URLs; fbc and fbp improve event matching.
- The error table (which code is a retry, which is a pause, which is dead) is ASSUMED: the Conversions API handling-errors page was a 404 in the evidence. Retry for 1, 2, 4, 17, 341, 613 and the 80000 series, pause for 190 and 102, any other 4xx is dead with the code in the reason.
- A bare phone number with no `+` and no leading `00` is accepted only at 11 to 15 digits (ASSUMED, the page gives no rule). A state outside the US is lowercased with spaces and punctuation removed, the page's own sentence.
- Server-only duplicates: the page says the first copy is kept for the same `event_id` and `event_name`, with no window. The 48 hour window it states is for the Pixel and server pairing. The relay's retries rely on the first sentence.
- `test_event_code` is not a sandbox. Meta says events sent with it flow into Events Manager and are used for targeting and measurement. What keeps this repo off a real Pixel is that a non-local base URL is refused unless `RELAY_LIVE=1`.

## Not covered

Any answer from a real Pixel. The page-side half of deduplication (a browser Pixel's `eventID` matching the server `event_id`) is not built; the relay only reuses a page `eventID` when the lead carries one.
