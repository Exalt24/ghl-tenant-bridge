# Windsor.ai connectors API: where each fixture comes from

Retrieved 2026-10-22 from pages saved in the research run's evidence folder (windsor.ai/api-documentation with its changelog entry of 2026-09-11, and connectors.windsor.ai/facebook/fields). Nothing here was captured from a real Windsor account, so no fixture is `observed`.

Provenance tags: `doc` means copied or derived from a vendor page. `ASSUMED` means the evidence does not state it, and a test says what would settle it.

| Fixture | Provenance | Source URL | What it holds |
|---|---|---|---|
| `auth.doc.json` | doc | https://windsor.ai/api-documentation/ | The `api_key` query parameter is the only authentication on the connectors host. Header authentication (`X-Api-Key`, `Authorization: Bearer`) is not supported and returns `{"error":"Not authorized"}`. |
| `query.doc.json` | doc | https://windsor.ai/api-documentation/ | The URL shape `https://connectors.windsor.ai/{connector}?fields=..&date_from=..&date_to=..&api_key=..`, the `date_from` and `date_to` format, and that `date_to` defaults to today. |
| `response.doc.json` | doc | https://windsor.ai/api-documentation/ | The documented response example: `{"data": [...]}` with `spend` as a JSON number, plus the page's own example of an error body. |
| `errors.doc.json` | doc | https://windsor.ai/api-documentation/ | The status and error code table (400, 401, 403, 404, 429, 500) and the two limits, 600 requests a minute and 10,000 a day. |
| `fields.doc.json` | doc | https://connectors.windsor.ai/facebook/fields | The seven Facebook connector fields the pull asks for, with the type and description the fields list gives each. The list holds 858 fields. |

## What the relay does that Windsor does not document

- `Retry-After` on a 429: the page says the response carries "headers indicating your remaining quota and when it will reset" and names no header. The pull honours `Retry-After` when it is present and backs off exponentially when it is not. ASSUMED that the header may appear; one real 429 would settle it.
- `spend` is read as being in `account_currency`. The fields list says `account_currency` is the "Currency that is used by your ad account" and says nothing about the currency of `spend`. ASSUMED.
- One account per label: the pull stores every row under the label the config gives. Two rows for one campaign and one day are refused (a second account, or a breakdown the pull did not ask for) rather than added together.
- 401 and 403 are both read as a refused key. The table lists them separately for an invalid key and for a key without permission.

## Not covered

Any answer from a real Windsor account: the facebook connector's behaviour for accounts with more than one currency, how a restated day arrives, and what the 429 body looks like on the wire.
