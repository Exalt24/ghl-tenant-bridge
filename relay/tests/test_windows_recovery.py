"""The worker as a Windows scheduled task: what the install script would register (`-WhatIf`, nothing is registered), and the
recovery that Task Scheduler's restart on failure relies on, run for real against a child process: the worker is killed hard in the
middle of a claimed delivery, started again, and the delivery is completed once after its lease has run out (the fake Slack never accepted it before the kill, so this covers an unsent claim only).

Windows only: the install script and TerminateProcess are Windows things. On any other system the whole file is skipped with that
reason, and it runs on the Windows machine this relay is deployed to.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from relay.store.sqlite import SqliteStore

from .helpers import config_json, env_for, ingest_one, make_cfg, payload, wait_for

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows only: the scheduled task scripts and the hard kill need Windows (Task Scheduler, PowerShell, TerminateProcess)"
)

RELAY = Path(__file__).resolve().parent.parent
DEPLOY = RELAY / "deploy" / "windows"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")


def powershell(script: str, *args: str) -> subprocess.CompletedProcess:
    assert POWERSHELL, "Windows PowerShell is not on PATH"
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(DEPLOY / script), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )


def definition(out: str) -> dict[str, str]:
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"^([A-Za-z ]+): (.*)$", out, re.MULTILINE)}


def task_exists(name: str) -> bool:
    res = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", f"(Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue | Measure-Object).Count"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    return res.stdout.strip() not in ("", "0")


# ---- the install and uninstall scripts, as a dry run -----------------------------------------------------------------------------


def test_the_install_script_prints_the_exact_task_definition_and_registers_nothing():
    name = "RelayWorkerTest-" + uuid.uuid4().hex[:8]
    res = powershell("install-service.ps1", "-WhatIf", "-TaskName", name, "-PythonExe", sys.executable)
    assert res.returncode == 0, res.stderr
    d = definition(res.stdout)
    assert d["TaskName"] == name
    assert d["Trigger"].startswith("at startup") and "BootTrigger" in d["Trigger"]  # at startup, not at logon
    assert d["RunAs"] == "SYSTEM" and d["LogonType"] == "ServiceAccount"  # whether or not a user is logged on
    assert d["RunLevel"] == "Highest"
    assert d["Execute"].lower() == sys.executable.lower()
    assert d["Arguments"] == "-m relay.worker"
    assert d["WorkingDirectory"] and Path(d["WorkingDirectory"]).resolve() == RELAY.resolve()  # worked out from the script's own place (an empty one would resolve to the test's own folder, which is this one)
    assert d["RestartCount"] == "999" and d["RestartInterval"] == "PT1M"  # every minute, up to 999 times
    assert d["ExecutionTimeLimit"].startswith("unlimited")  # the 3 day default would stop a worker that is meant to run for ever
    assert d["MultipleInstances"] == "IgnoreNew" and d["StartWhenAvailable"] == "True"
    assert "What if: Performing the operation" in res.stdout and name in res.stdout
    assert "Machine environment RELAY_CONFIG: " in res.stdout  # a SYSTEM task reads machine variables
    assert not task_exists(name)  # nothing was registered on this machine


def test_the_install_script_says_which_settings_are_missing_without_printing_a_value(monkeypatch):
    monkeypatch.setenv("RELAY_CONFIG", '{"secret": "do-not-print-me"}')  # a process variable: the script reads the MACHINE ones only
    res = powershell("install-service.ps1", "-WhatIf", "-PythonExe", sys.executable)
    assert res.returncode == 0 and "Machine environment RELAY_CONFIG: " in res.stdout
    assert "do-not-print-me" not in res.stdout + res.stderr


@pytest.mark.parametrize("args,message", [
    (["-PythonExe", str(RELAY / "no-such-python.exe")], "does not exist"),
    (["-PythonExe", sys.executable, "-RelayDir", str(RELAY / "tests")], "does not hold relay"),
    (["-PythonExe", sys.executable, "-RunAs", "Administrator"], "RunAs"),
])
def test_the_install_script_refuses_a_wrong_python_folder_or_account_before_it_registers_anything(args, message):
    res = powershell("install-service.ps1", "-WhatIf", *args)
    assert res.returncode != 0 and message in (res.stdout + res.stderr)
    assert "TASK DEFINITION" not in res.stdout  # the definition is only printed for a setup that is sound


def is_elevated() -> bool:
    res = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
         "([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60,
    )
    return res.stdout.strip() == "True"


def registered(name: str) -> dict:
    """The task as Task Scheduler stored it, read back with Get-ScheduledTask."""
    script = (
        f"$t = Get-ScheduledTask -TaskName '{name}'; [pscustomobject]@{{user=$t.Principal.UserId; logon=[string]$t.Principal.LogonType; "
        "run=[string]$t.Principal.RunLevel; rc=$t.Settings.RestartCount; ri=[string]$t.Settings.RestartInterval; mi=[string]$t.Settings.MultipleInstances; "
        "exe=$t.Actions[0].Execute; n=@($t.Triggers).Count; first=$t.Triggers[0].CimClass.CimClassName; rep=[string]$t.Triggers[1].Repetition.Interval; "
        "start=[string]$t.Triggers[1].StartBoundary; state=[string]$t.State } | ConvertTo-Json -Compress"
    )
    res = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout)


def test_the_per_user_dry_run_prints_a_limited_interactive_task_that_repeats_every_minute_and_registers_nothing():
    name = "RelayWorkerTest-" + uuid.uuid4().hex[:8]
    res = powershell("install-service.ps1", "-WhatIf", "-RunAs", "CurrentUser", "-TaskName", name, "-PythonExe", sys.executable)
    assert res.returncode == 0, res.stderr
    d = definition(res.stdout)
    user = os.environ["USERNAME"].lower()
    assert d["TaskName"] == name
    assert d["Trigger"].startswith("at logon of") and "LogonTrigger" in d["Trigger"]  # at logon of this user, not at startup
    assert d["RunAs"].lower().endswith("\\" + user) and d["RunAs"].lower() != "system"  # the current user
    assert d["LogonType"] == "Interactive"  # S4U was refused by Windows (Access is denied) from a non-elevated shell
    assert d["RunLevel"] == "Limited"  # so registering needs no elevation
    assert d["Execute"].lower() == sys.executable.lower() and d["Arguments"] == "-m relay.worker"
    assert d["WorkingDirectory"] and Path(d["WorkingDirectory"]).resolve() == RELAY.resolve()
    assert d["RestartCount"] == "999" and d["RestartInterval"] == "PT1M"
    assert d["ExecutionTimeLimit"].startswith("unlimited")
    assert d["MultipleInstances"] == "IgnoreNew"
    # the trigger that really restarts a dead worker: restart on failure only retries a launch failure (observed 2026-10-24)
    assert d["RepeatTrigger"].endswith("TimeTrigger") and d["RepeatInterval"] == "PT1M" and d["RepeatDuration"] == "indefinitely"
    assert abs((datetime.datetime.fromisoformat(d["RepeatStart"]) - datetime.datetime.now()).total_seconds()) < 180  # counts from now
    assert "User environment RELAY_CONFIG: " in res.stdout and "Machine environment" not in res.stdout  # a user task reads user variables
    assert "What if: Performing the operation" in res.stdout and name in res.stdout
    assert not task_exists(name)  # nothing was registered


@pytest.mark.parametrize("run_as", ["CurrentUser", "SYSTEM"])
def test_with_nostart_the_repeat_trigger_begins_later_so_registering_does_not_start_the_worker(run_as):
    res = powershell("install-service.ps1", "-WhatIf", "-NoStart", "-RunAs", run_as, "-PythonExe", sys.executable)
    assert res.returncode == 0, res.stderr
    start = datetime.datetime.fromisoformat(definition(res.stdout)["RepeatStart"])
    assert 300 < (start - datetime.datetime.now()).total_seconds() < 900


def test_the_system_dry_run_also_repeats_its_trigger_every_minute_beside_the_boot_trigger():
    res = powershell("install-service.ps1", "-WhatIf", "-PythonExe", sys.executable)
    d = definition(res.stdout)
    assert "BootTrigger" in d["Trigger"] and d["RepeatTrigger"].endswith("TimeTrigger")
    assert d["RepeatInterval"] == "PT1M" and d["RepeatDuration"] == "indefinitely"


def test_the_per_user_default_python_is_the_windowless_twin_and_the_system_default_is_not():
    user = definition(powershell("install-service.ps1", "-WhatIf", "-RunAs", "CurrentUser").stdout)["Execute"]
    system = definition(powershell("install-service.ps1", "-WhatIf").stdout)["Execute"]
    twin = Path(system).with_name("pythonw.exe")
    if not twin.exists():
        pytest.skip("no pythonw.exe next to the default python.exe on this machine")
    assert user.lower() == str(twin).lower()  # an Interactive task would open a console window for python.exe
    assert system.lower().endswith("python.exe")  # a service account has no desktop, so the SYSTEM default is unchanged


def test_a_system_task_is_still_refused_without_elevation_and_registers_nothing():
    if is_elevated():
        pytest.skip("this shell is elevated: the refusal cannot be shown, and a real SYSTEM task must not be registered by a test")
    name = "RelayWorkerTest-" + uuid.uuid4().hex[:8]
    try:
        res = powershell("install-service.ps1", "-NoStart", "-TaskName", name, "-PythonExe", sys.executable)
        assert res.returncode != 0 and "needs an elevated PowerShell" in (res.stdout + res.stderr)
    finally:
        if task_exists(name):
            powershell("uninstall-service.ps1", "-TaskName", name)
    assert not task_exists(name)


def test_the_per_user_task_registers_without_elevation_exactly_as_printed_and_the_uninstall_script_removes_it():
    name = "RelayWorkerTest-" + uuid.uuid4().hex[:8]
    try:
        res = powershell("install-service.ps1", "-RunAs", "CurrentUser", "-NoStart", "-TaskName", name, "-PythonExe", sys.executable)
        assert res.returncode == 0, res.stderr  # no elevation was needed (this is the guard: S4U and a Highest run level both fail here)
        assert f"Registered task {name}." in res.stdout and "Started task" not in res.stdout
        t = registered(name)
        assert t["user"].lower().endswith("\\" + os.environ["USERNAME"].lower()) or t["user"].lower() == os.environ["USERNAME"].lower()
        assert t["logon"] == "Interactive" and t["run"] == "Limited"
        assert t["rc"] == 999 and t["ri"] == "PT1M" and t["mi"] == "IgnoreNew"
        assert t["exe"].lower() == sys.executable.lower()
        assert t["n"] == 2 and "LogonTrigger" in t["first"] and t["rep"] == "PT1M"
        assert t["state"] != "Running"  # -NoStart: nothing started, and the repeat trigger is not due for ten minutes
    finally:
        powershell("uninstall-service.ps1", "-TaskName", name)
    assert not task_exists(name)


def test_the_uninstall_script_dry_run_changes_nothing_and_a_missing_task_is_not_an_error():
    name = "RelayWorkerTest-" + uuid.uuid4().hex[:8]
    res = powershell("uninstall-service.ps1", "-WhatIf", "-TaskName", name)
    assert res.returncode == 0, res.stderr
    d = definition(res.stdout)
    assert d["TaskName"] == name and d["Exists"] == "no" and "Nothing to remove." in res.stdout


# ---- kill the worker in the middle of a claimed delivery -------------------------------------------------------------------------


LEASE_S = 6.0
SINK_TIMEOUT_S = 2.0  # the lease is three timeouts: 6 s, so the test waits for a real expiry, not a faked one


def child_env(db_path: str, slack_url: str) -> dict[str, str]:
    base = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "DATABASE_URL_TEST", "RELAY_CONFIG", "ALERT_WEBHOOK")}
    base.update(env_for(slack_url, None, RELAY_DB_PATH=db_path, RELAY_WORKER_INTERVAL_S="0.2", RELAY_LEASE_MIN_S="1", PYTHONDONTWRITEBYTECODE="1"))
    base["RELAY_CONFIG"] = config_json(slack=True, make=False, slack_timeout=SINK_TIMEOUT_S)
    return base


class Child:
    """The worker as its own process, the way Task Scheduler runs it: `python -m relay.worker`."""

    def __init__(self, env: dict[str, str], log: Path) -> None:
        self._log = open(log, "wb")
        self.proc = subprocess.Popen([sys.executable, "-m", "relay.worker"], cwd=str(RELAY), env=env, stdout=self._log, stderr=subprocess.STDOUT)

    def kill(self) -> int:
        self.proc.kill()  # TerminateProcess on Windows: no cleanup, no final write, nothing is flushed
        return self.proc.wait(timeout=30)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=30)
        self._log.close()


def test_a_worker_killed_mid_delivery_is_restarted_and_completes_the_delivery_exactly_once_after_its_lease(slack, tmp_path):
    db = str(tmp_path / "relay.db")
    cfg = make_cfg(slack.url, None, slack_timeout=SINK_TIMEOUT_S)
    store = SqliteStore(db)
    store.init_schema()
    env = child_env(db, slack.url)
    first = Child(env, tmp_path / "worker1.log")
    second = None
    try:
        # POSITIVE CONTROL: the child, unharmed, delivers a lead to the fake. A recovery below cannot be a worker that sends nothing.
        control = ingest_one(store, cfg, payload(1))
        wait_for(lambda: store.delivery(control.event_id, "slack")["state"] == "done", timeout=30, what="the control lead")
        assert slack.effects == 1 and store.heartbeat_age_s("worker") is not None  # and it beat its heartbeat while it ran
        slack.reset()  # effects back to zero for the case under test

        slack.queue("hang")  # the next request is held by the fake: the worker is inside the send, holding the claim
        victim = ingest_one(store, cfg, payload(2))
        wait_for(lambda: len(slack.requests) == 1, timeout=30, what="the worker to be inside the send")
        claimed = store.delivery(victim.event_id, "slack")
        assert claimed["state"] == "pending" and claimed["claim_token"] is not None and claimed["claims"] == 1  # claimed, not finished
        assert first.kill() != 0  # a hard kill is a failure exit, which is what Task Scheduler restarts on
        slack.release.set()  # let the fake's held request end (nobody is listening) and make sure it never counts as an effect
        assert slack.effects == 0

        held = store.delivery(victim.event_id, "slack")
        assert held["state"] == "pending" and held["claim_token"] == claimed["claim_token"]  # the dead worker's claim is still on the row

        second = Child(env, tmp_path / "worker2.log")  # Task Scheduler's restart
        wait_for(lambda: store.heartbeat_age_s("worker") is not None and store.heartbeat_age_s("worker") < 2, timeout=30, what="the restarted worker")
        still = store.delivery(victim.event_id, "slack")
        assert still["state"] == "pending" and slack.effects == 0  # inside the lease nobody may take the row: no duplicate send

        wait_for(lambda: store.delivery(victim.event_id, "slack")["state"] == "done", timeout=60, what="the lease to expire and the delivery to complete")
        done = store.delivery(victim.event_id, "slack")
        assert slack.effects == 1  # delivered exactly once
        assert len([r for r in slack.requests if r["accepted"]]) == 1
        assert done["attempts"] == 1 and done["claims"] == done["attempts"] + done["retries"]
        assert str(done["finished_at"]) >= str(claimed["claim_until"])  # it completed only after the dead worker's lease had expired
        (attempt,) = store.attempts_for(victim.event_id, "slack")
        assert attempt["ok"] == 1
        assert store.delivery(control.event_id, "slack")["state"] == "done"  # and the earlier lead was not sent again
        assert second.proc.poll() is None  # the restarted worker is still running
    finally:
        for c in (first, second):
            if c is not None:
                c.close()
        store.close()
