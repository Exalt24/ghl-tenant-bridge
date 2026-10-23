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

## Importing history and a suppression list

`python -m relay.import_history <file.csv>` loads prior outbound contacts and do-not-contact lists, with a dry run by default. The file format, the report, the exit codes and what the worker does with a listed contact are in [history-import.md](history-import.md).

## Run the worker as a Windows scheduled task

For a Windows machine that has to keep the worker running with no one logged on, `deploy/windows/install-service.ps1` registers one Task Scheduler task. No third party binary is involved.

1. Set the worker's settings as MACHINE environment variables from an elevated PowerShell, because a task that runs as SYSTEM does not see a user's variables: `[Environment]::SetEnvironmentVariable('DATABASE_URL', '<value>', 'Machine')`, and the same for `RELAY_CONFIG` and every variable the config names (sink URLs, tokens). With SQLite in prod the worker needs `RELAY_DB_PATH` (an absolute path) and `RELAY_INLINE_WORKER=1`.
2. Look at what would be registered: `powershell -ExecutionPolicy Bypass -File deploy/windows/install-service.ps1 -WhatIf`. It prints the task definition from real task objects and registers nothing. It also says, by name only, which of `DATABASE_URL`, `RELAY_DB_PATH` and `RELAY_CONFIG` are not set at machine level. It never prints a value.
3. Register it from an elevated PowerShell with the same command without `-WhatIf`. Options: `-TaskName` (default `RelayWorker`), `-PythonExe` (default `.venv/Scripts/python.exe` in the relay folder, else `python.exe` from PATH), `-RelayDir`, `-RunAs` (`SYSTEM`, `LOCAL SERVICE` or `NETWORK SERVICE`) and `-NoStart`. The Python and the relay folder must be readable by that account.
4. Remove it with `deploy/windows/uninstall-service.ps1` (also has `-WhatIf`).

The task: trigger at startup, run as SYSTEM as a service account (so whether or not a user is logged on) at the highest run level, action `<python> -m relay.worker` in the relay folder, restart on failure every 1 minute up to 999 times, no execution time limit (the Task Scheduler default of 3 days would stop it), one instance at a time (`IgnoreNew`).

What a crash does, and what was tested: a worker that dies holds its claim until the lease runs out (three sink timeouts, at least 300 seconds), so nothing else sends that row in the meantime, and the restarted worker then completes it. `tests/test_windows_recovery.py` runs `python -m relay.worker` as a child process on a temporary SQLite file, kills it hard (TerminateProcess) while it holds a claimed delivery that a fake Slack is holding open, starts a second worker, and checks that the row stays unsent inside the lease and is delivered exactly once after it (the test lowers the lease to six seconds with `RELAY_LEASE_MIN_S`). The test starts the second worker itself: Task Scheduler's own restart on failure, and `Register-ScheduledTask`, were not exercised, because the tests register no task. A worker that exits with a configuration error exits non-zero, so the task will keep restarting it once a minute until the settings are fixed.

## Alert when jobs stop

The worker writes one row with the time (`heartbeat`, name `worker`) on every cycle, from the database clock. `python -m relay.health_check` reads it and exits 1 with one line per finding:

- `heartbeat`: no heartbeat at all, one older than `--max-heartbeat-age-s` (default 120), or one that cannot be read. The worker is not running.
- `backlog`: the oldest delivery still pending was created more than `--max-oldest-pending-s` ago (default 3600), or that cannot be read. The worker runs but work is stuck (a paused sink, a vendor that keeps failing).

A read that fails is a finding, never a pass. Exit 0 means healthy, 2 means the setup was refused (a limit that is not a positive number, for example).

Schedule it every minute or two from the same machine (Task Scheduler runs `python -m relay.health_check --alert`) with the same environment as the worker. With `--alert` it also posts ONE message through the Slack sink to `ALERT_WEBHOOK`, then stays quiet for `--cooldown-s` (default 3600) so one outage does not alert on every run; a long outage reminds once per cooldown, and a post that fails is tried again after one minute. The cooldown is kept in the database (`alert_state`, key `health`). Without `ALERT_WEBHOOK` the alert goes to the log as a `RELAY_ALERT` line.

Limit: when the database itself cannot be opened there is nowhere to keep the cooldown, so no Slack alert is posted. The printed `FINDING database` line and the exit code 1 are the signal, so have the scheduler that runs the check raise its own alarm on a non-zero exit.

## Deploy on Railway (notes, not done)

- Builder: Dockerfile. No `railway.toml`, no Procfile: Railway's config as code is deprecated and its pages give a 2026-12-01 cutoff, so the Dockerfile plus these service settings are the whole description.
- The Dockerfile command runs through `/bin/sh -c` because the exec form does not expand `${PORT}`, a generic Docker behaviour.
- Healthcheck path `/health` on Railway. On AWS (see `../../infra/`) the load balancer and the container check use `/livez`, which does not depend on the database.
- `DATABASE_URL`: a variable reference to a Railway Postgres service, or a Supabase session mode pooler string. Supabase direct connections are IPv6 unless the project has the IPv4 add-on, so use the pooler if the Railway service has no IPv6 route (not established).
- The worker is a second service from the same image, started with `python -m relay.worker`. A cron style service is a separate one shot service (`python -m relay.worker --once`, `python -m relay.pulls ...`) and needs Postgres, because it cannot share a SQLite file with the web service.
