# Configuration

Back to the [relay README](../README.md).

`RELAY_CONFIG` is a JSON file path or an inline JSON string, holding names only (see `config.example.json`). A literal secret in it is refused by name. Every `*_env` value is the name of an environment variable.

| variable | meaning |
|---|---|
| `RELAY_ENV` | `dev`, `test` or `prod`. Unset means prod. |
| `RELAY_LIVE` | Only the exact value `1` allows a non local https sink or pull URL. Without it a real vendor URL is refused when the config loads. |
| `DATABASE_URL` | Postgres. Without it SQLite is used at `RELAY_DB_PATH`. |
| `RELAY_DB_PATH`, `RELAY_INLINE_WORKER` | SQLite in prod is refused unless both are set (an absolute path on a mounted volume, and the worker running as a thread in the same container). A separate `worker --once` service or a pull service needs Postgres. |
| `RELAY_INGEST_TOKEN`, `RELAY_INGEST_TOKEN_PREVIOUS`, `RELAY_ADMIN_TOKEN` | At least 16 characters. The admin token must differ from the ingest tokens. |
| `ALERT_WEBHOOK` | A Slack webhook for relay alerts, one digest per sink per 15 minutes. Without it the digest goes to the log as `RELAY_ALERT`. |
| `RELAY_WORKER_INTERVAL_S` | Idle wait between passes, default 5. |
| `RELAY_TEST_GHL_PUBLIC_KEY` | Test key for signature tests, honoured only when `RELAY_ENV` is `test`. A test asserts nothing shipped sets it. |

## Sinks

Each tenant lists its sinks under `sinks`. Every sink takes `url_env` (the name of the variable holding its URL) and the optional `enabled`, `timeout_s`, `min_interval_s`, `pause_s` and `max_age_s`. An identifier or key material (a pixel id, a spreadsheet id, a service account) is never written in `options`: it goes in the sink's `env` map as the name of an environment variable, and the config is refused if a literal one is found.

| sink | `url_env` points at | `apikey_env` | `env` (names of variables) | `options` |
|---|---|---|---|---|
| `slack` | the incoming webhook URL | none | none | none |
| `make` | the custom webhook URL | optional, sent as `x-make-apikey` | none | none |
| `capi` | the Graph base URL with its version, `https://graph.facebook.com/v26.0` | required, the access token | `pixel_id` | `test_event_code`, `live`, `event_map`, `capi_require_click_id`, `click_id_sources`, `default_country_code`, `currency` |
| `sheets` | the Sheets base URL, `https://sheets.googleapis.com` | none | `spreadsheet_id`, `service_account` (the key file JSON) | `sheet`, `columns` |

```json
"sinks": {
  "capi": {
    "url_env": "ACME_META_URL", "apikey_env": "ACME_META_TOKEN", "env": {"pixel_id": "ACME_PIXEL_ID"},
    "options": {"test_event_code": "TEST12345", "currency": "USD", "capi_require_click_id": true}
  },
  "sheets": {
    "url_env": "ACME_SHEETS_URL", "env": {"spreadsheet_id": "ACME_SHEET_ID", "service_account": "ACME_SA_JSON"},
    "options": {"sheet": "Leads", "columns": ["event_id", "received_at", "name", "email", "phone", "utm_source"]}
  }
}
```

Meta Conversions API options:

- `test_event_code` is required unless `live` is true, and the sink sends it until then. It is not a sandbox: events sent with it still reach Events Manager. `live: true` is refused unless the process has `RELAY_LIVE=1`.
- `capi_require_click_id` (default `true`): a lead with none of `fbc`, `fbp`, `fbclid` or a `source` listed in `click_id_sources` is `skipped` with the reason `no click id`, so an import or a walk in is never reported to Meta as an ad conversion. Set it to `false` to send every lead that has an email, a phone, an `fbc` or an `fbp`.
- `click_id_sources`: lead `source` names that count as a click id (a list of strings, default empty).
- `event_map`: extra `type` to event name pairs on top of the defaults (`ContactCreate` and `Lead` to `Lead`, `AppointmentCreate` to `Schedule`, `OpportunityStatusUpdate` with status `won` to `Purchase`). The event `type` must be in the body: a delivery with no `type` is skipped and raises an alert (see the workflow route in [routes](routes.md)).
- `default_country_code`: 1 to 3 digits, no plus, applied to a phone that starts with a single `0`. `currency`: a three letter code, required for a `Purchase`.

Google Sheets options: `sheet` is a bare sheet name (letters, digits and underscore, no spaces or quotes) and `columns` is a list that starts with `event_id`, chosen from `event_id`, `received_at`, `event_type`, `name`, `email`, `phone`, `city`, `state`, `source`, `status`, `tags`, `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term`, `fbclid` and `gclid`. The sheet must be shared with the service account's email.

## Per tenant blocks added in tier 3

```json
"fathom": {"secret_env": "ACME_FATHOM_SECRET", "slack": true, "tolerance_s": 300, "max_body_bytes": 2097152},
"pulls": {
  "meta_insights": {"base_url_env": "...", "token_env": "...", "account_env": "...", "days": 28, "limit": 500},
  "windsor": {"base_url_env": "...", "api_key_env": "...", "label": "main", "days": 28},
  "fathom": {"base_url_env": "...", "api_key_env": "...", "days": 28, "include_summary": false}
}
```

- The Fathom secret must be `whsec_` followed by base64 of at least 16 bytes. `slack: true` needs an enabled Slack sink for the tenant, and only that sink ever gets a meeting.
- A pull base URL goes through the same `RELAY_LIVE` guard as a sink URL. The Meta base URL includes the Graph version (`https://graph.facebook.com/v26.0`).
- `days` is at most 365. A Windsor `label` is stored as the account of every row.
- A token, an API key and a secret are masked by value in every log line.
