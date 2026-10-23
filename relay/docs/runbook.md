# Runbook

Back to the [relay README](../README.md).

## Re-queue dead rows in the Worker's D1 after a relay outage

The Worker retries a failed forward with backoff and marks a row dead after 6 attempts, or at once on a 4xx other than 408 and 429. What Railway answers for a stopped deployment is ASSUMED, so a stopped relay may produce dead rows in the Worker. Column names below come from `edge/schema.sql`. The relay dedupes on the `webhookId` the Worker sends as `Idempotency-Key`, so re-queueing is safe.

```
wrangler d1 execute ghl-edge --remote --command "UPDATE inbox SET state = 'pending', attempts = 0, next_at = CAST(strftime('%s','now') AS INTEGER) * 1000, claim_until = NULL, last_error = NULL WHERE state = 'dead';"
```

## Retention

An event payload is nulled after 30 days by `python -m relay.worker --purge`, which keeps the event row so a late retry is still a duplicate. Nothing schedules it yet: run it from a cron style service that has Postgres.

## Scheduling the pulls

Run `python -m relay.pulls meta_insights --tenant <name>` (and `windsor`, `fathom`) from a one shot service on a schedule. A pull is safe to repeat because it upserts. Exit code 3 means the vendor asked for a longer wait than one run holds, and the next scheduled run tries again. Exit code 4 means a credential was refused: replace it, do not retry in a loop.

## Reconciling a payments export

```python
from relay.reconcile import load_csv, reconcile

payments = load_csv("payments.csv", {"id": "Reference", "amount": "Gross", "currency": "Cur", "date": "Paid on"})
ledger = load_csv("ledger.csv", {"id": "Invoice", "amount": "Total", "date": "Day", "bucket": "Category"}, currency="USD")
result = reconcile(payments, ledger, tolerance_minor=0, expected_bucket="sales")
print(result.counts())
```

A CSV may be UTF-8 (with or without the byte order mark Excel adds to "CSV UTF-8") or Windows-1252 (what Excel's plain "CSV (Comma delimited)" writes), and the loader reads either. A file that is neither, or UTF-16 (Excel's "Unicode Text"), is refused with an error that names the file and the offset of the first byte it could not read; re-save it as CSV UTF-8. An amount must be a plain decimal string with no more decimals than its currency has. A row the loader refuses is named by its line and column, never by its cell.

## Deploy on Railway (notes, not done)

- Builder: Dockerfile. No `railway.toml`, no Procfile: Railway's config as code is deprecated and its pages give a 2026-12-01 cutoff, so the Dockerfile plus these service settings are the whole description.
- The Dockerfile command runs through `/bin/sh -c` because the exec form does not expand `${PORT}`, a generic Docker behaviour.
- Healthcheck path `/health`.
- `DATABASE_URL`: a variable reference to a Railway Postgres service, or a Supabase session mode pooler string. Supabase direct connections are IPv6 unless the project has the IPv4 add-on, so use the pooler if the Railway service has no IPv6 route (not established).
- The worker is a second service from the same image, started with `python -m relay.worker`. A cron style service is a separate one shot service (`python -m relay.worker --once`, `python -m relay.pulls ...`) and needs Postgres, because it cannot share a SQLite file with the web service.
