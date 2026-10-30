# Runbook

Back to the [relay README](../README.md).

## Re-queue dead rows in the Worker's D1 after a relay outage

The Worker retries a failed forward with backoff and marks a row dead after 6 attempts, or at once on a 400, 410, 413, 422 or any other 4xx except 401, 403, 404, 408 and 429, which are retried. What Railway answers for a stopped deployment is ASSUMED, so a stopped relay may produce dead rows in the Worker. Column names below come from `edge/schema.sql`. The relay dedupes on the `webhookId` the Worker sends as `Idempotency-Key`, so re-queueing is safe.

```
wrangler d1 execute ghl-edge --remote --command "UPDATE inbox SET state = 'pending', attempts = 0, next_at = CAST(strftime('%s','now') AS INTEGER) * 1000, claim_until = NULL, last_error = NULL WHERE state = 'dead';"
```

## A held send

A sink with `hold_unresolved: true` (Slack and Make; see `docs/configuration.md`) never sends a message a second time on its own. An ambiguous answer, a timeout or a 5xx after the vendor may have posted, is stored as a dead delivery whose error starts with `held:`, with an alert. So is a row that was claimed, may have posted, and then lost its worker before the final write.

A held row also appears for a worker that died BEFORE it sent, because nothing can tell the two apart afterwards. That is the price of the option: an operator takes the full event id from the `deliveries` or `alerts` table, looks in the destination channel or the scenario history for its first 8 characters (Slack's footer carries them), and then either leaves the row or replays it with `POST /admin/replay` and the admin token (`{"event_id": ..., "sink": "slack"}`). A replay is the operator's decision to send it again, resets the row, and sends it once more.

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
3. Register it from an elevated PowerShell with the same command without `-WhatIf`. Options: `-TaskName` (default `RelayWorker`), `-PythonExe` (default `.venv/Scripts/python.exe` in the relay folder, else `python.exe` from PATH), `-RelayDir`, `-RunAs` (`SYSTEM`, `LOCAL SERVICE`, `NETWORK SERVICE` or `CurrentUser`) and `-NoStart`. The Python and the relay folder must be readable by that account.
4. Remove it with `deploy/windows/uninstall-service.ps1` (also has `-WhatIf`).

The task: trigger at startup, run as SYSTEM as a service account (so whether or not a user is logged on) at the highest run level, action `<python> -m relay.worker` in the relay folder, restart on failure every 1 minute up to 999 times, no execution time limit (the Task Scheduler default of 3 days would stop it), one instance at a time (`IgnoreNew`). A second trigger, a time trigger that repeats every minute indefinitely, is what actually restarts a worker that has died (next section). With `-NoStart` that repeat begins ten minutes later, so registering does not start the worker.

`-RunAs CurrentUser` is the same task for a machine with no elevated shell: it runs as the current user, logon type Interactive, run level Limited, triggered at that user's logon (plus the repeat trigger), and reads USER level environment variables (`[Environment]::SetEnvironmentVariable('NAME', 'value', 'User')`), not machine level ones. It runs only while that user is logged on, so it is for a developer machine, not a server. Without `-PythonExe` it uses `pythonw.exe` from the folder of the default `python.exe`, because an Interactive task opens a console window for `python.exe`. Logon type S4U, which would run it whether or not the user is logged on, was tried and Windows refused to register it from a non-elevated shell (Access is denied).

### What was exercised on a real machine (2026-10-24)

`python tools/task_scheduler_check.py` runs this on a Windows machine without elevation and prints PASS or FAIL with its timeline (it skips with a message elsewhere; it is not part of the CI pytest run). On 2026-10-24, on one Windows 11 machine (user `Dax`, not elevated), it registered the per-user task `RelayWorkerExercise` with `install-service.ps1 -RunAs CurrentUser`, started it, and watched Task Scheduler restart a worker killed with `Stop-Process -Force`:

- 09:33:28 the task registered and started (`Register-ScheduledTask` and `Start-ScheduledTask` both worked); Task Scheduler launched the worker as PID 11004 (a launcher process 40100 above it) and it wrote its heartbeat to the SQLite file, so it had read its settings from the user environment. A control delivery reached a local fake Slack, then a second delivery was held open by the fake with the worker inside the send (the row claimed, 90 second lease).
- 09:33:37.969 `Stop-Process -Force` on PID 11004. The task went to Ready with LastTaskResult 4294967295 and nothing was running.
- 09:34:28, 54 to 56 seconds later, Task Scheduler started a new worker (PID 41320) from the repeat trigger. At 09:35:04, 87 seconds after the kill and just after the 90 second lease ran out, that worker completed the held delivery: the fake saw it exactly once (3 requests in all: the control, the held one that died with the worker, and the one that was accepted).
- Cleanup: the task was removed with `uninstall-service.ps1`, the nine user environment variables were deleted, and `Get-ScheduledTask` showed nothing left.

What this changed. Two earlier runs of the same exercise, one with the task as it was first written (restart on failure every 1 minute, up to 999 times) and one with the repetition set on the logon trigger itself, did NOT restart the killed worker: the task stayed Ready with LastTaskResult 4294967295 for 4 minutes each. Small probe tasks (`python tools/task_scheduler_check.py --probe-restart-on-failure`) showed why: Task Scheduler's restart on failure retried a task whose program could not be launched (3 runs in 150 seconds), and did not retry a program that started and exited with code 1 (1 run), nor one killed by `Stop-Process`. So a worker that dies is not restarted by that setting alone, and the task now also carries the repeating time trigger. A repetition set on the logon or boot trigger itself does not help, because it only counts from the next logon or boot, not from registration, which is why the repeat is a separate time trigger. The task with that second trigger restarted the worker as above.

Not exercised: the SYSTEM account path (`-RunAs SYSTEM` and the boot trigger) needs an elevated shell and was not run, so it has only been checked as a `-WhatIf` definition; the SYSTEM task carries the same repeat trigger, and that is the one change to its definition that no run has exercised. A restart after a reboot, a logoff or a sleep was not tried. Restart on failure was observed to retry only a launch failure on this machine and one Windows build; it was not tried under SYSTEM.

What a crash does, and what was tested: a worker that dies holds its claim until the lease runs out (three sink timeouts, at least 300 seconds), so nothing else sends that row in the meantime, and the restarted worker then completes it. `tests/test_windows_recovery.py` runs `python -m relay.worker` as a child process on a temporary SQLite file, kills it hard (TerminateProcess) while it holds a claimed delivery that a fake Slack is holding open, starts a second worker, and checks that the row stays unsent inside the lease and is delivered once after it (the test lowers the lease to six seconds with `RELAY_LEASE_MIN_S`). The fake Slack never accepts the held request before the kill, so this covers an unsent claim only: when the provider accepted a send and the acknowledgment was lost, Slack and Make are sent again after the lease, and `tests/test_crash_and_concurrency.py` asserts that second send. The test starts the second worker itself, so it does not exercise Task Scheduler; the section above says what did. A worker that exits with a configuration error exits non-zero, and the repeat trigger starts it again every minute until the settings are fixed.

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
