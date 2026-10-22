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

An amount must be a plain decimal string with no more decimals than its currency has. A row the loader refuses is named by its line and column, never by its cell.
