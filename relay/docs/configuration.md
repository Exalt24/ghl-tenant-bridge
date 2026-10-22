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
