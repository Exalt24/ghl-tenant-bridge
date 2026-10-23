# Google service account auth and Sheets append: where each fixture comes from

Retrieved 2026-10-22 from pages saved in the research run's evidence folder (`evidence/B-sinks`, plus `evidence/B-sinks/rfc7523` for the grant type). The `doc` fixtures below were not captured from a real Google account.

On 2026-10-23 the Sheets v4 REST calls the sink makes (`values.append`, `values.get`) were run against the real API and the answers saved as `observed` fixtures. Be exact about what that covers: the calls were made with a USER OAuth access token (an installed app consent flow for a personal Google account), on a scratch spreadsheet created for the check and trashed the same day. NOT observed: the service account JWT sign-in (`oauth2.googleapis.com/token` with a signed assertion), a service account's access to a private sheet shared with it, and the relay's own HTTP client talking to real Google. Those stay `doc` or ASSUMED.

Provenance tags: `doc` means copied or derived from a vendor page. `observed` means captured from the real API on the date and with the token kind given above. `ASSUMED` means the evidence does not state it, and a test or a comment says what would settle it.

| Fixture | Provenance | Source URL | What it holds |
|---|---|---|---|
| `jwt.doc.json` | doc, except the encoding tags below | https://developers.google.com/identity/protocols/oauth2/service-account and https://cloud.google.com/docs/authentication/token-types | The JWT header, the claim set (iss, scope, aud, iat, exp), valid up to one hour, the invalid_grant text that names a lifetime over 65 minutes, the "without newlines or padding equal signs" sentence. RS256 as RSASSA-PKCS1-v1_5 with SHA-256 and base64url without padding are tagged `doc: RFC 7515 and RFC 7518 (not retrieved)`. |
| `token_request.rfc7523.json` | doc: RFC 7523 | https://www.rfc-editor.org/rfc/rfc7523 | The grant type literal `urn:ietf:params:oauth:grant-type:jwt-bearer` and the form fields. Google's own page for this request (the HTTP tab) was not retrieved. |
| `append.doc.json` | doc | https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/append, `.../guides/values`, `.../limits` | The append request, `valueInputOption` as a required parameter, RAW keeping `=1+2` as a string, INSERT_ROWS, the `tableRange` field ("Empty if no table was found"), the scope, 60 writes per minute per user per project, a 2 MB payload recommendation. |
| `table_detection.doc.json` | doc | https://developers.google.com/workspace/sheets/api/guides/values | The page's worked example of how a table is found and where a row lands. The fake is tested against it. |
| `statuses.assumed.json` | ASSUMED, with the observed moves listed inside | none | What is still not known: that one refreshed token heals a 401, and what a PRIVATE sheet not shared with the account answers. It also lists which statuses moved to `observed` on 2026-10-23. |
| `append_layout.observed.json` | observed 2026-10-23, user OAuth token, scratch spreadsheet | https://sheets.googleapis.com/v4/spreadsheets/{id}/values/{range}:append | Appends of the relay's default 12 column row with `valueInputOption=RAW&insertDataOption=INSERT_ROWS`, range `Tab` versus `Tab!A:A`. The former F7 shape (utm_campaign blank in every row, between utm_source and fbclid) scattered rows with the bare name (`L2:W2`, `W3:AH3`) and kept them in column A with `Tab!A:A`. Real `tableRange` and `updatedRange` for each. |
| `values_raw_and_cell_limit.observed.json` | observed 2026-10-23, user OAuth token, scratch spreadsheet | same | RAW keeps `=1+2`, `'text`, `007`, `+5`, `@x` as text (USER_ENTERED does not). The cell limit is 50,000 UTF-16 code units: 49,000 emoji (98,000 units) were refused with a 400 and nothing of the append was written. |
| `values_get.observed.json` | observed 2026-10-23, user OAuth token, scratch spreadsheet | https://sheets.googleapis.com/v4/spreadsheets/{id}/values/{range} | Read-back of column A: an empty sheet has no `values` key, a blank cell is `[]`, trailing blank rows are dropped, the `range` is expanded (`Tab!A1:A1000`). |
| `rate_limit.observed.json` | observed 2026-10-23, user OAuth token, scratch spreadsheet | same | A tight loop of appends: 90 accepted in 33.6 s, then a 429 RESOURCE_EXHAUSTED with ErrorInfo details, no `Retry-After` header, and the first write that succeeded again 48.2 s later. |
| `errors.observed.json` | observed 2026-10-23, user OAuth token, scratch spreadsheet | same | A bad range, a missing tab, an unknown spreadsheet id (404), a wrong or absent bearer token (401, and 403 for an anonymous GET), bodies the API rejects (400), and the 403 for a write to a sheet the account may only view. |

## What the relay does that Google does not document

- The append names column A as its range (`Sheet1!A:A`), because the page says the range is searched for the table: column A is never empty, so a column that is empty in every row cannot split the table. Observed on 2026-10-23: that is true on the real API, and the bare tab name does split it. The sheet name is bare, and config refuses a title with a space or a quote because the quoting rule for such a title was not retrieved.
- Column A is always the event id. `values.append` writes at the table's first column, so the sink reads `tableRange` from the first answer and refuses a table that does not begin at column A.
- A cell is cut to 49,000 UTF-16 code units without splitting an emoji, because the real limit is counted in units (observed). A cut by Python characters let 98,000 units through and Sheets refused the whole row.
- A 400 whose message starts "Unable to parse range" (a renamed or deleted tab, observed) pauses the sink for the tenant to fix. Any other 400 is dead.
- One append in flight per spreadsheet across processes. Concurrent appends to one sheet: not found, the concurrent-edits page was not retrieved.
- A read-back of column A, to find a row an ambiguous send may have written, is the relay's own method; no saved page describes how long a slow append can take to land, so a read-back that finds nothing counts only after 60 seconds have passed since the last try that may have written.
- A 401 gets one refreshed token and one more try, then dead (ASSUMED).
- A 429 retries with min(2^n + jitter, 32 s). The observed wait was 48 seconds from the first 429 with no `Retry-After`, so a few retries pass before a write succeeds again. A 429 spends no attempt.

## Not covered

The service account JWT sign-in against the real token endpoint. A service account's access to a private sheet shared with it (and the status of one that is not). The relay's own HTTP client against real Google. How Sheets orders two appends that overlap.
