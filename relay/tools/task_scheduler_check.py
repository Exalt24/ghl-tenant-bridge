# LIFESPAN: permanent (the by hand check that Task Scheduler really restarts a killed relay worker; not part of the CI pytest run)
"""Registers the relay worker as a per-user Windows scheduled task, kills the worker in the middle of a delivery and watches
Task Scheduler start a new one.

    python tools/task_scheduler_check.py              run the exercise, print the timeline and PASS or FAIL
    python tools/task_scheduler_check.py --keep-logs  leave the temporary folder (the SQLite file) in place
    python tools/task_scheduler_check.py --probe-restart-on-failure
                                                      only show which failures "restart on failure" retries (about 3 minutes)

Why the task repeats its trigger every minute: on 2026-10-24 this check showed that Task Scheduler's "restart on failure" setting did
not restart a worker that had started and then been killed (LastTaskResult 4294967295, the task stayed Ready for 4 minutes), so the task
definition also repeats its trigger every minute and runs one instance at a time. `--probe-restart-on-failure` repeats that observation
with a plain task that exits 1 and one that cannot be launched.

It needs no elevation: the task is `deploy/windows/install-service.ps1 -RunAs CurrentUser` (logon type Interactive, run level Limited), so
it exercises the same task definition as the SYSTEM one except for the account, the trigger and the run level. The SYSTEM account
path needs an elevated shell and is not run here.

What it does, in order: a temporary SQLite file and a fake Slack sink on 127.0.0.1 (this process, one thread), the worker's settings
as USER level environment variables (a task that runs as the current user sees them), the task `RelayWorkerExercise`, one control
delivery that must arrive, one delivery the sink holds open, a hard kill (Stop-Process -Force) of the worker's PID found under
the Task Scheduler service, then a wait for Task Scheduler's own restart and for the held delivery to reach the sink exactly once
after its lease. It always removes the task, the variables, the sink and any worker it started, by PID.

Skips with a message (exit 0) on a system that is not Windows or where Task Scheduler cannot be queried. Exit 1 is a FAIL.
"""

from __future__ import annotations

import argparse
import datetime
import http.server
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

RELAY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RELAY))

TASK = "RelayWorkerExercise"
HOOK_PATH = "/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX"
SINK_TIMEOUT_S = 30  # the worker waits this long for the sink; the lease is three of these (90 s), longer than the 60 s restart interval
POLL_S = 3.0
START_WAIT_S = 60
RESTART_WAIT_S = 240
ENV_NAMES = [
    "RELAY_ENV", "RELAY_INGEST_TOKEN", "RELAY_ADMIN_TOKEN", "ACME_WORKFLOW_SECRET", "ACME_SLACK_URL", "RELAY_DB_PATH",
    "RELAY_CONFIG", "RELAY_WORKER_INTERVAL_S", "RELAY_LEASE_MIN_S",
]
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # a console child of a console-less caller must not open a window

timeline: list[str] = []


def note(msg: str) -> None:
    line = f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]}  {msg}"
    timeline.append(line)
    print(line, flush=True)


def ps(script: str, extra_env: dict[str, str] | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    env = {**os.environ, **(extra_env or {})}
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, env=env, creationflags=NO_WINDOW,
    )


def ps_file(script: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(RELAY / "deploy" / "windows" / script), *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120, creationflags=NO_WINDOW,
    )


# ---- the fake sink ---------------------------------------------------------------------------------------------------------------


class Sink:
    """A Slack style incoming webhook: answers 200 `ok`, and can hold one request open so a worker is inside the send."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.hold_next = False
        self.holding = False
        self.release = threading.Event()
        self.lock = threading.Lock()
        sink = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a) -> None:  # noqa: D401 - silence the default stderr log
                pass

            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                with sink.lock:
                    held = sink.hold_next
                    sink.hold_next = False
                    rec = {"n": len(sink.requests) + 1, "at": time.time(), "held": held, "accepted": False, "bytes": len(body)}
                    sink.requests.append(rec)
                if held:
                    sink.holding = True
                    sink.release.wait(timeout=150)  # the worker is killed meanwhile; this only ends the handler
                    sink.holding = False
                    return
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/plain")
                    self.send_header("Content-Length", "2")
                    self.end_headers()
                    self.wfile.write(b"ok")
                    rec["accepted"] = True
                except OSError:
                    pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}{HOOK_PATH}"

    def accepted(self) -> int:
        with self.lock:
            return sum(1 for r in self.requests if r["accepted"])

    def stop(self) -> None:
        self.release.set()
        self.server.shutdown()
        self.server.server_close()


# ---- Task Scheduler and process queries ------------------------------------------------------------------------------------------


def task_state() -> dict:
    res = ps(
        f"$t = Get-ScheduledTask -TaskName '{TASK}' -ErrorAction SilentlyContinue; "
        f"if ($t) {{ $i = Get-ScheduledTaskInfo -TaskName '{TASK}'; [pscustomobject]@{{exists=$true; state=[string]$t.State; "
        f"last_run=$i.LastRunTime.ToString('s'); last_result=[int64]$i.LastTaskResult; missed=$i.NumberOfMissedRuns; "
        f"logon=[string]$t.Principal.LogonType; run_level=[string]$t.Principal.RunLevel; restart_count=$t.Settings.RestartCount; "
        f"restart_interval=[string]$t.Settings.RestartInterval}} | ConvertTo-Json -Compress }} else {{ '{{\"exists\":false}}' }}"
    )
    try:
        return json.loads(res.stdout.strip())
    except ValueError:
        return {"error": (res.stdout + res.stderr).strip()[:300]}


def task_exists() -> bool | None:
    state = task_state()
    return state.get("exists") if "error" not in state else None


def worker_processes() -> list[dict]:
    """Every python process whose command line holds `relay.worker` and whose ancestry reaches the Task Scheduler service host
    (the `Schedule` service). A worker started by anything else, a pytest child for instance, is never listed, so it is never killed."""
    res = ps(
        "$s = (Get-CimInstance Win32_Service -Filter \"Name='Schedule'\").ProcessId; "
        "$all = @(Get-CimInstance Win32_Process | Where-Object { $_.Name -in 'python.exe','pythonw.exe' } | "
        "ForEach-Object { [pscustomobject]@{pid=[int]$_.ProcessId; ppid=[int]$_.ParentProcessId; name=$_.Name; cmd=[string]$_.CommandLine; "
        "created=$_.CreationDate.ToString('HH:mm:ss.fff')} }); "
        "[pscustomobject]@{schedule=[int]$s; procs=$all} | ConvertTo-Json -Compress -Depth 4"
    )
    try:
        data = json.loads(res.stdout.strip())
    except ValueError:
        return []
    procs = data["procs"] if isinstance(data["procs"], list) else [data["procs"]]
    by_pid = {p["pid"]: p for p in procs}
    out = []
    for p in procs:
        if "relay.worker" not in p["cmd"]:
            continue
        cur, seen = p, 0
        while cur is not None and seen < 5:
            if cur["ppid"] == data["schedule"]:
                out.append(p)
                break
            cur = by_pid.get(cur["ppid"])
            seen += 1
    return out


def leaf_workers(procs: list[dict]) -> list[dict]:
    """The process that runs the worker: the one no other listed process has as its parent (a venv python.exe is a launcher that
    starts the real interpreter as its child)."""
    parents = {p["ppid"] for p in procs}
    return [p for p in procs if p["pid"] not in parents]


def pid_alive(pid: int) -> bool:
    res = ps(f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ 'yes' }} else {{ 'no' }}")
    return res.stdout.strip() == "yes"


def kill_pid(pid: int) -> None:
    ps(f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue")


def set_user_env(values: dict[str, str]) -> None:
    for name, value in values.items():
        ps(f"[Environment]::SetEnvironmentVariable('{name}', $env:RELAY_TC_VALUE, 'User')", {"RELAY_TC_VALUE": value})


def clear_user_env() -> None:
    for name in ENV_NAMES:
        ps(f"[Environment]::SetEnvironmentVariable('{name}', $null, 'User')")


def user_env_set() -> list[str]:
    res = ps("; ".join(f"if ([Environment]::GetEnvironmentVariable('{n}', 'User')) {{ '{n}' }}" for n in ENV_NAMES))
    return [x for x in res.stdout.split() if x]


PROBES = {
    "RelayWorkerExerciseProbeExit": ("cmd.exe", "/c exit 1", "starts and exits 1"),
    "RelayWorkerExerciseProbeLaunch": ("C:/no/such/program.exe", "x", "cannot be launched"),
}


def probe_restart_on_failure() -> bool:
    """Two throwaway tasks with RestartCount 3 and RestartInterval 1 minute: one whose program starts and exits 1, one whose program
    does not exist. Reports how many times each ran in 150 seconds. Informational: it returns False only when it could not run."""
    try:
        for name, (exe, args, _what) in PROBES.items():
            script = (
                "$u = $env:USERDOMAIN + [char]92 + $env:USERNAME; "
                f"$a = New-ScheduledTaskAction -Execute '{exe}' -Argument '{args}'; "
                "$t = New-ScheduledTaskTrigger -AtLogOn -User $u; "
                "$p = New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Limited; "
                "$s = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew; "
                f"Register-ScheduledTask -TaskName {name} -InputObject (New-ScheduledTask -Action $a -Trigger $t -Principal $p -Settings $s) -ErrorAction Stop | Out-Null; "
                f"Start-ScheduledTask -TaskName {name} -ErrorAction SilentlyContinue"  # a missing program makes the start itself report an error
            )
            res = ps(script)
            if res.returncode != 0 and "Register" in (res.stderr + res.stdout):
                note(f"could not register {name}: {(res.stderr or res.stdout).strip()[:200]}")
                return False
        runs: dict[str, set[str]] = {n: set() for n in PROBES}
        results: dict[str, str] = {}
        end = time.time() + 150
        while time.time() < end:
            time.sleep(POLL_S)  # WAIT-OK: polls the real task state every pass and prints the result at the end
            for name in PROBES:
                r = ps(f"$i = Get-ScheduledTaskInfo -TaskName {name}; $i.LastRunTime.ToString('HH:mm:ss') + '|' + $i.LastTaskResult")
                if "|" in r.stdout:
                    when, code = r.stdout.strip().split("|")
                    if when != "00:00:00":
                        runs[name].add(when)
                        results[name] = code
        for name, (_exe, _args, what) in PROBES.items():
            note(f"probe task whose program {what}: ran {len(runs[name])} time(s) in 150 s, last result {results.get(name)}")
        return True
    finally:
        for name in PROBES:
            ps(f"Stop-ScheduledTask -TaskName {name} -ErrorAction SilentlyContinue; Unregister-ScheduledTask -TaskName {name} -Confirm:$false -ErrorAction SilentlyContinue")
        left = ps("(Get-ScheduledTask -TaskName 'RelayWorkerExerciseProbe*' -ErrorAction SilentlyContinue | Measure-Object).Count").stdout.strip()
        note(f"probe tasks remaining: {left}")


# ---- the exercise ----------------------------------------------------------------------------------------------------------------


def wait_until(cond, timeout: float, what: str, step: float = POLL_S):
    """Polls in 3 second steps; returns the truthy value of cond, or None at the deadline."""
    end = time.time() + timeout
    while True:
        value = cond()
        if value:
            return value
        if time.time() >= end:
            note(f"gave up waiting for {what} after {int(timeout)} s")
            return None
        time.sleep(step)


def exercise(workdir: Path, tracked: set[int]) -> bool:
    from relay import ghl
    from relay.config import load_config
    from relay.fanout import ingest
    from relay.store.sqlite import SqliteStore

    sink = Sink()
    store = None
    ok = False
    try:
        db = str(workdir / "relay.db")
        config = json.dumps({
            "locations": {"LOC1": "acme"},
            "tenants": {"acme": {"workflow_secret_env": "ACME_WORKFLOW_SECRET", "sinks": {"slack": {"url_env": "ACME_SLACK_URL", "timeout_s": SINK_TIMEOUT_S}}}},
        })
        values = {
            "RELAY_ENV": "test", "RELAY_INGEST_TOKEN": secrets.token_hex(16), "RELAY_ADMIN_TOKEN": secrets.token_hex(16),
            "ACME_WORKFLOW_SECRET": secrets.token_hex(16), "ACME_SLACK_URL": sink.url, "RELAY_DB_PATH": db, "RELAY_CONFIG": config,
            "RELAY_WORKER_INTERVAL_S": "0.5", "RELAY_LEASE_MIN_S": "1",
        }
        assert set(values) == set(ENV_NAMES)
        cfg = load_config(values, role="web")
        store = SqliteStore(db)
        store.init_schema()
        note(f"temporary folder {workdir}; fake sink on 127.0.0.1:{sink.server.server_port}; sink timeout {SINK_TIMEOUT_S} s, so a lease of {3 * SINK_TIMEOUT_S} s")

        taken = user_env_set()
        if taken:
            note(f"FAIL: these user environment variables are already set and would be overwritten: {', '.join(taken)}")
            return False
        if task_exists():
            note(f"FAIL: a task named {TASK} already exists; remove it with deploy/windows/uninstall-service.ps1 -TaskName {TASK}")
            return False
        set_user_env(values)
        note(f"set {len(values)} USER level environment variables (names only: {', '.join(ENV_NAMES)})")

        windowless = Path(sys.executable).with_name("pythonw.exe")  # an Interactive task would open a console window for python.exe
        python_for_task = str(windowless) if windowless.exists() else sys.executable
        res = ps_file("install-service.ps1", "-RunAs", "CurrentUser", "-TaskName", TASK, "-PythonExe", python_for_task)
        if res.returncode != 0:
            note("FAIL: install-service.ps1 -RunAs CurrentUser failed: " + (res.stderr or res.stdout).strip()[:400])
            return False
        note(f"install-service.ps1 -RunAs CurrentUser -PythonExe {Path(python_for_task).name}: " + " | ".join(l for l in res.stdout.splitlines() if l.startswith(("Registered", "Started"))))
        st = task_state()
        note(f"registered definition: logon={st.get('logon')} run_level={st.get('run_level')} restart_count={st.get('restart_count')} restart_interval={st.get('restart_interval')} state={st.get('state')}")

        def first_worker():
            leaves = leaf_workers(worker_processes())
            return leaves[0] if leaves else None

        w1 = wait_until(first_worker, START_WAIT_S, "Task Scheduler to start the worker")
        if not w1:
            note(f"FAIL: no worker process under Task Scheduler; task state {task_state()}")
            return False
        tracked.add(w1["pid"])
        for p in worker_processes():
            tracked.add(p["pid"])
        note(f"worker 1 is PID {w1['pid']} (parent PID {w1['ppid']}), started {w1['created']}; processes under the Schedule service: {[p['pid'] for p in worker_processes()]}")

        if not wait_until(lambda: store.heartbeat_age_s("worker") is not None, 30, "the worker's first heartbeat"):
            note("FAIL: the task started a process but the worker never wrote a heartbeat (did it see the USER variables?)")
            return False
        note("worker 1 wrote a heartbeat to the SQLite file, so it read its settings from the user environment")

        def lead(i: int):
            body = {"type": "ContactCreate", "locationId": "LOC1", "id": f"tscheck{i:04d}", "firstName": "Pat", "lastName": f"Lead{i}",
                    "name": f"Pat Lead{i}", "email": f"pat{i}@example.com", "phone": "+14155550100", "city": "Austin", "state": "TX",
                    "source": "roof form", "dateAdded": "2026-10-24T12:41:02.193Z", "dateUpdated": "2026-10-24T12:41:03.193Z", "tags": ["hail"]}
            return ingest(store, cfg, tenant="acme", key=f"tscheck-{secrets.token_hex(8)}", basis=ghl.BASIS_NATIVE, source="ingest", payload_json=json.dumps(body))

        control = lead(1)
        if not wait_until(lambda: store.delivery(control.event_id, "slack")["state"] == "done", 30, "the control delivery"):
            note("FAIL: the control delivery never completed, so a later result would prove nothing")
            return False
        note(f"control delivery done under worker 1 (sink accepted {sink.accepted()})")

        sink.hold_next = True
        victim = lead(2)
        if not wait_until(lambda: sink.holding, 30, "the sink to hold the victim request"):
            note("FAIL: the worker never sent the victim delivery to the sink")
            return False
        claimed = store.delivery(victim.event_id, "slack")
        note(f"sink is holding the victim request; row state={claimed['state']} claims={claimed['claims']} claim_until={claimed['claim_until']}")

        # the PID is looked up again just before the kill: it must still be the one captured from the task
        current = [p for p in leaf_workers(worker_processes()) if p["pid"] == w1["pid"]]
        if not current:
            note("FAIL: worker 1 is no longer under the Task Scheduler service")
            return False
        killed_at = time.time()
        kill_pid(w1["pid"])
        note(f"Stop-Process -Force on PID {w1['pid']} (state before: task {task_state().get('state')})")
        sink.release.set()
        wait_until(lambda: not pid_alive(w1["pid"]), 15, "PID of worker 1 to be gone", step=1.0)
        note(f"PID {w1['pid']} alive: {pid_alive(w1['pid'])}")
        row = store.delivery(victim.event_id, "slack")
        note(f"victim row after the kill: state={row['state']} (the dead worker's claim is still on it)")

        seen_states: list[str] = []

        def restarted():
            st = task_state()
            tag = f"task state={st.get('state')} last_result={st.get('last_result')} last_run={st.get('last_run')}"
            if not seen_states or seen_states[-1] != tag:
                seen_states.append(tag)
                note(f"+{int(time.time() - killed_at)} s after kill: {tag}; worker pids {[p['pid'] for p in worker_processes()]}")
            for p in leaf_workers(worker_processes()):
                if p["pid"] != w1["pid"]:
                    return p
            return None

        w2 = wait_until(restarted, RESTART_WAIT_S, "Task Scheduler to restart the worker")
        if not w2:
            st = task_state()
            note(f"FAIL: Task Scheduler did NOT start a new worker within {RESTART_WAIT_S} s of the kill. Final task state: {st}")
            return False
        tracked.update(p["pid"] for p in worker_processes())
        restart_after = time.time() - killed_at
        note(f"Task Scheduler started worker 2: PID {w2['pid']} (parent PID {w2['ppid']}), started {w2['created']}, {restart_after:.0f} s after the kill")

        def done():
            return store.delivery(victim.event_id, "slack")["state"] == "done"

        if not wait_until(done, 180, "the victim delivery to complete under worker 2"):
            note(f"FAIL: the victim delivery did not complete; row {dict(store.delivery(victim.event_id, 'slack'))['state']}")
            return False
        final = store.delivery(victim.event_id, "slack")
        completed_after = time.time() - killed_at
        with sink.lock:
            reqs = list(sink.requests)
        accepted = [r for r in reqs if r["accepted"]]
        note(f"victim delivery done {completed_after:.0f} s after the kill; attempts={final['attempts']} claims={final['claims']} finished_at={final['finished_at']}")
        note("sink saw " + ", ".join(f"#{r['n']} at {r['at'] - killed_at:+.0f} s" + (" held" if r["held"] else " accepted" if r["accepted"] else "") for r in reqs) + " (times relative to the kill; the held one started before it)")
        problems = []
        if len(accepted) != 2:
            problems.append(f"the sink accepted {len(accepted)} requests, expected 2 (the control and the victim once)")
        if final["attempts"] != 1:
            problems.append(f"the victim has {final['attempts']} recorded attempts, expected 1")
        if str(final["finished_at"]) < str(claimed["claim_until"]):
            problems.append("the delivery finished before the dead worker's lease had run out")
        if store.delivery(control.event_id, "slack")["attempts"] != 1:
            problems.append("the control lead was sent again")
        if problems:
            for p in problems:
                note("FAIL: " + p)
            return False
        ok = True
        return True
    finally:
        sink.stop()
        if store is not None:
            store.close()
        note(f"sink stopped; PASS so far: {ok}")


def cleanup(tracked: set[int]) -> bool:
    clean = True
    res = ps_file("uninstall-service.ps1", "-TaskName", TASK)
    note("uninstall-service.ps1: " + " | ".join(l for l in res.stdout.splitlines() if l.startswith(("Exists", "Removed", "Nothing"))) + (f" (exit {res.returncode}: {res.stderr.strip()[:200]})" if res.returncode else ""))
    time.sleep(2)
    for pid in sorted(tracked):
        if pid_alive(pid):
            kill_pid(pid)
            note(f"worker PID {pid} was still running after the task was removed; stopped it by PID")
    leftovers = [p["pid"] for p in worker_processes()]
    if leftovers:
        note(f"worker processes still under the Task Scheduler service: {leftovers}")
        for pid in leftovers:
            kill_pid(pid)
        clean = False
    clear_user_env()
    left_env = user_env_set()
    note("user environment variables removed" if not left_env else f"STILL SET: {left_env}")
    clean = clean and not left_env
    exists = task_exists()
    note(f"Get-ScheduledTask {TASK}: {'still registered' if exists else 'nothing remains' if exists is False else 'could not be queried'}")
    return clean and exists is False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--keep-logs", action="store_true", help="leave the temporary folder in place")
    ap.add_argument("--probe-restart-on-failure", action="store_true", help="only show which failures Task Scheduler's restart on failure retries")
    args = ap.parse_args()

    if sys.platform != "win32":
        print("SKIP: this check needs Windows (Task Scheduler and PowerShell); nothing was run.")
        return 0
    if not POWERSHELL:
        print("SKIP: powershell.exe is not on PATH, so Task Scheduler cannot be used; nothing was run.")
        return 0
    probe = ps("Get-ScheduledTask -TaskName 'RelayWorkerExerciseProbe' -ErrorAction SilentlyContinue | Out-Null; 'reachable'")
    if probe.returncode != 0 or "reachable" not in probe.stdout:
        print("SKIP: Task Scheduler cannot be queried here (" + (probe.stderr or probe.stdout).strip()[:200] + "); nothing was run.")
        return 0

    if args.probe_restart_on_failure:
        note("probe: restart on failure (RestartCount 3, every 1 minute) for a program that starts and exits 1, and one that cannot be launched")
        return 0 if probe_restart_on_failure() else 1

    workdir = Path(tempfile.mkdtemp(prefix="relay-task-check-"))
    tracked: set[int] = set()
    note(f"task_scheduler_check on {os.environ.get('COMPUTERNAME', '?')} as {os.environ.get('USERDOMAIN', '?')}\\{os.environ.get('USERNAME', '?')}, no elevation used")
    passed = False
    try:
        passed = exercise(workdir, tracked)
    except Exception as exc:  # the cleanup below must run whatever went wrong
        note(f"FAIL: unexpected {type(exc).__name__}: {str(exc)[:300]}")
    finally:
        cleaned = cleanup(tracked)
        if not args.keep_logs:
            shutil.rmtree(workdir, ignore_errors=True)
    result = "PASS" if passed and cleaned else "FAIL"
    print(f"\n{result}: Task Scheduler {'restarted the killed worker and the delivery reached the sink exactly once' if passed else 'check did not complete as expected'}"
          f"; cleanup {'complete' if cleaned else 'INCOMPLETE'}.")
    return 0 if result == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
