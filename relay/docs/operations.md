# Operations detail

*Text moved unchanged out of the [relay README](../README.md) so the README stays short. Each section names where it came from.*

## History import, Windows service, cutover demo and health check (from How it works)

- `relay/import_history.py`: `python -m relay.import_history <file.csv>` loads prior outbound contacts or a do-not-contact list (dry run unless `--apply`, all or nothing, idempotent); a suppressed contact is refused at send time and the reason is stored. [docs/history-import.md](docs/history-import.md).
- `deploy/windows/`: registers the worker as a Windows scheduled task that restarts itself, with `-WhatIf` to print the task and register nothing, and `-RunAs CurrentUser` for a per-user task that needs no elevation. On 2026-10-24, on one Windows 11 machine, that per-user task was registered and Task Scheduler restarted a killed worker (`python tools/task_scheduler_check.py`); the SYSTEM account was not run. [docs/runbook.md](docs/runbook.md).
- `tools/demo_cutover.py`: one script that runs the whole move from an old system (history import, replayed leads, a killed worker, a health alert) and asserts each step; [docs/demo.md](docs/demo.md) has the replay and the numbers.
- `relay/health_check.py`: `python -m relay.health_check` exits non-zero when the worker heartbeat is stale or the oldest unfinished delivery is too old, and `--alert` posts one Slack alert per cooldown. [docs/runbook.md](docs/runbook.md).

## The commands the worker's operators use (from Run it)

The three commands the worker's operators use besides `relay.worker` (the history import is a dry run until `--apply`):

```
python -m relay.import_history history.csv --kind ContactCreate [--apply]
python -m relay.import_history dnc.csv --list suppression [--apply]
python -m relay.health_check [--alert]
powershell -ExecutionPolicy Bypass -File deploy/windows/install-service.ps1 -WhatIf
```

## How the mutation harness counts (from Run it)

The mutation harness copies the package, breaks one guard per mutant, and counts it caught only when a named test fails for the mutated behaviour and that same test passes on the unmutated copy. A tree that no longer imports, a fixture error or a missing test file is reported as BROKEN, not caught. `--shard i/n` runs every n-th mutant, which is how CI splits the list.

## Versions checked at install time (from ASSUMED)

- Versions: Python 3.13, `python:3.13-slim`, `psycopg[binary]` and `cryptography`'s Ed25519 API were checked at install time on 2026-10-22: every pinned version was at least 14 days old on PyPI (Flask 3.1.3, gunicorn 26.2.0, httpx 0.28.1, cryptography 50.0.2, psycopg 3.3.6, pytest 9.1.1).

## A claim predicate with no mutant (from Known limits)

- The outer repeat of the claimable predicate in the Postgres claim is belt and braces: under READ COMMITTED with `SKIP LOCKED` removing it changes no behaviour, so it has no mutant.
