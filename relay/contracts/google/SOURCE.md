# Google service account auth and Sheets append: where each fixture comes from

Retrieved 2026-10-22 from pages saved in the research run's evidence folder (`evidence/B-sinks`, plus `evidence/B-sinks/rfc7523` for the grant type). Nothing here was captured from a real Google account, so no fixture is `observed`.

Provenance tags: `doc` means copied or derived from a vendor page. `ASSUMED` means the evidence does not state it, and a test or a comment says what would settle it.

| Fixture | Provenance | Source URL | What it holds |
|---|---|---|---|
| `jwt.doc.json` | doc, except the encoding tags below | https://developers.google.com/identity/protocols/oauth2/service-account and https://cloud.google.com/docs/authentication/token-types | The JWT header, the claim set (iss, scope, aud, iat, exp), valid up to one hour, the invalid_grant text that names a lifetime over 65 minutes, the "without newlines or padding equal signs" sentence. RS256 as RSASSA-PKCS1-v1_5 with SHA-256 and base64url without padding are tagged `doc: RFC 7515 and RFC 7518 (not retrieved)`. |
| `token_request.rfc7523.json` | doc: RFC 7523 | https://www.rfc-editor.org/rfc/rfc7523 | The grant type literal `urn:ietf:params:oauth:grant-type:jwt-bearer` and the form fields. Google's own page for this request (the HTTP tab) was not retrieved. |
| `append.doc.json` | doc | https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/append, `.../guides/values`, `.../limits` | The append request, `valueInputOption` as a required parameter, RAW keeping `=1+2` as a string, INSERT_ROWS, the `tableRange` field ("Empty if no table was found"), the scope, 60 writes per minute per user per project, a 2 MB payload recommendation. |
| `table_detection.doc.json` | doc | https://developers.google.com/workspace/sheets/api/guides/values | The page's worked example of how a table is found and where a row lands. The fake is tested against it. |
| `statuses.assumed.json` | ASSUMED | none | What 401, 403 and 404 mean. The troubleshooting page lists only 400, 500 and 503. |

## What the relay does that Google does not document

- A bare sheet name is the whole range (`Sheet1`), and config refuses a title with a space or a quote because the quoting rule for such a title was not retrieved.
- Column A is always the event id. `values.append` writes at the table's first column, so the sink reads `tableRange` from the first answer and refuses a table that does not begin at column A.
- One append in flight per spreadsheet across processes. Concurrent appends to one sheet: not found, the concurrent-edits page was not retrieved.
- A read-back of column A, to find a row an ambiguous send may have written, is the relay's own method; no saved page describes how long a slow append can take to land, so a read-back that finds nothing counts only after 60 seconds have passed since the last try that may have written.
- A 401 gets one refreshed token and one more try, then dead (ASSUMED).

## Not covered

Any answer from a real spreadsheet. How Sheets orders two appends that overlap.
