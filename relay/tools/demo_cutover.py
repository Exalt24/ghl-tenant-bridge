# LIFESPAN: permanent
"""The cutover story from the old system to this relay, run end to end against local fakes with made-up data.

    python tools/demo_cutover.py

What it does, in order, on one temporary SQLite database:
  1. writes the old system's messy export (a Windows-1252 CSV: mixed phone formats, upper case emails, repeats, rows that
     are not contacts) and a do-not-contact list;
  2. imports both with the real `relay.import_history` (a dry run, then --apply, then --apply again, which adds nothing);
  3. replays leads through the real ingest and worker: people already messaged by the old system, people on the
     do-not-contact list, brand new people, the same delivery posted twice, and one Sheets append that is written and then
     answered with a 500 (the relay reads the sheet back and does not append again);
  4. kills a worker hard while it holds a claimed delivery, starts a second one, and shows the delivery goes out once
     after the lease and never inside it;
  5. runs `relay.health_check` healthy, stops the worker, and shows it exit non-zero with one alert per cooldown.

Every expectation is asserted. The output ends in PASS or FAIL and the exit code says which. Every number printed is
counted from this run. The destinations are the suite's own fakes (Slack and Google Sheets), the data is invented, and
nothing here has touched a real account or a real person.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RELAY = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RELAY))

from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

from relay.sinks.sheets import SheetsSink  # noqa: E402
from relay.sinks.slack import SlackSink  # noqa: E402
from relay.store.sqlite import SqliteStore  # noqa: E402
from tests.fakes.sheets import SPREADSHEET_ID, FakeSheets, service_account_json  # noqa: E402
from tests.fakes.slack import FakeSlack  # noqa: E402
from tests.helpers import config_json, env_for, ingest_one, make_cfg, make_rig, payload, wait_for  # noqa: E402

SINK_TIMEOUT_S = 2.0  # the lease is three sink timeouts, so the restart step waits for a real expiry
FAILURES: list[str] = []
logging.getLogger("werkzeug").setLevel(logging.ERROR)  # the fakes log every request; the demo prints its own story


def say(text: str = "") -> None:
    print(text, flush=True)


def step(title: str) -> None:
    say()
    say(f"== {title}")


def expect(ok: bool, what: str) -> None:
    say(f"   [{'ok' if ok else 'FAIL'}] {what}")
    if not ok:
        FAILURES.append(what)


def phone_formats(n: int) -> str:
    """The same made-up number written four ways, the way an old export holds them."""
    d = f"415555{n:04d}"
    return [f"({d[:3]}) {d[3:6]}-{d[6:]}", f"+1 {d[:3]} {d[3:6]} {d[6:]}", f"{d[:3]}.{d[3:6]}.{d[6:]}", d][n % 4]


def e164(n: int) -> str:
    return f"+1415555{n:04d}"


def write_exports(folder: Path) -> tuple[Path, Path, dict[str, int]]:
    history = folder / "old_system_sent.csv"
    dnc = folder / "do_not_contact.csv"
    rows = ["Contact,Kind,Name"]
    for i in range(300):
        if i % 7 == 3:
            who = f"HIST{i}@Example.COM"  # an upper case email
        else:
            who = phone_formats(100 + i)
        rows.append(f"{who},ContactCreate,Sample Person {i}")
    for i in range(0, 24, 2):  # the same people again, written differently: twelve repeats
        rows.append(f"{phone_formats(100 + i)},ContactCreate,Sample Person {i} (again)" if i % 7 != 3 else f"hist{i}@example.com,ContactCreate,again")
    rows += ["n/a,ContactCreate,Not a contact", "12345,ContactCreate,Too short", ",ContactCreate,Empty"]  # three rows that are not contacts
    history.write_bytes(("\r\n".join(rows) + "\r\n").encode("cp1252"))  # what Excel's plain CSV writes
    drows = ["contact,reason"]
    reasons = ["asked to stop", "bounced twice", "legal hold"]
    for k in range(20):
        drows.append(f"{phone_formats(500 + k)},{reasons[k % 3]}")
    for i in range(5):  # five people who are also in the history file: suppression has to win
        drows.append(f"{phone_formats(100 + i) if i % 7 != 3 else f'hist{i}@example.com'},asked to stop")
    dnc.write_bytes(("\r\n".join(drows) + "\r\n").encode("cp1252"))
    return history, dnc, {"history_rows": 300 + 12 + 3, "dnc_rows": 25}


def run_cli(module: str, args: list[str], env: dict[str, str]) -> tuple[int, str]:
    res = subprocess.run(
        [sys.executable, "-m", module, *args], cwd=str(RELAY), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    return res.returncode, (res.stdout + res.stderr).strip()


def show(text: str) -> None:
    for line in text.splitlines():
        say("   | " + line)


def number(text: str, label: str) -> int:
    for line in text.splitlines():
        if line.startswith(label + ":"):
            return int(line.split(":", 1)[1].strip())
    raise AssertionError(f"no '{label}' line in the output")


class Worker:
    """`python -m relay.worker` as its own process, the way Task Scheduler runs it."""

    def __init__(self, env: dict[str, str], log: Path) -> None:
        self._log = open(log, "wb")
        self.proc = subprocess.Popen([sys.executable, "-m", "relay.worker"], cwd=str(RELAY), env=env, stdout=self._log, stderr=subprocess.STDOUT)

    def kill(self) -> None:
        self.proc.kill()  # TerminateProcess on Windows: no cleanup, nothing is flushed
        self.proc.wait(timeout=30)

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=30)
        self._log.close()


def drain(rig, store, limit_s: float = 150.0) -> None:
    """Run the worker in this process until nothing is due, on the real clock (a pass that finds work returns its count)."""
    deadline = time.monotonic() + limit_s
    while time.monotonic() < deadline:
        rig.worker.run_once(wait_horizon_s=0)
        if store.due_in_s() is None:
            return
        time.sleep(0.05)
    raise AssertionError(f"deliveries are still pending after {limit_s:.0f}s")


def main() -> int:
    started = time.monotonic()
    work = Path(tempfile.mkdtemp(prefix="relay_demo_"))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)  # a throwaway key for the Sheets fake's service account
    slack = FakeSlack().start()
    sheets = FakeSheets(key.public_key()).start()
    workers: list[Worker] = []
    store = None
    try:
        db = str(work / "relay.db")
        sa_json = service_account_json(key, sheets.token_url, sheets.email)
        sheets_sink = {
            "url_env": "ACME_SHEETS_URL", "env": {"spreadsheet_id": "ACME_SHEET_ID", "service_account": "ACME_SA_JSON"},
            "options": {"sheet": "Sheet1"}, "timeout_s": 5.0,
        }
        env_extra = {"ACME_SHEETS_URL": sheets.url, "ACME_SHEET_ID": SPREADSHEET_ID, "ACME_SA_JSON": sa_json, "ALERT_WEBHOOK": slack.url}
        cfg = make_cfg(slack.url, None, slack_timeout=SINK_TIMEOUT_S, extra_sinks={"sheets": sheets_sink}, env_extra=env_extra)
        env = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL", "DATABASE_URL_TEST", "RELAY_CONFIG", "ALERT_WEBHOOK")}
        env.update(env_for(slack.url, None, RELAY_DB_PATH=db, RELAY_WORKER_INTERVAL_S="0.2", RELAY_LEASE_MIN_S="1", PYTHONDONTWRITEBYTECODE="1", **env_extra))
        env["RELAY_CONFIG"] = config_json(slack=True, make=False, slack_timeout=SINK_TIMEOUT_S, extra_sinks={"sheets": sheets_sink})

        store = SqliteStore(db)
        store.init_schema()
        registry = {"slack": SlackSink(), "sheets": SheetsSink()}
        rig = make_rig(store, cfg, registry=registry)

        # ---------------------------------------------------------------------------------------------------------------
        step("1. The old system's export (invented people, a Windows-1252 CSV with the usual mess)")
        history, dnc, facts = write_exports(work)
        say(f"   {history.name}: {facts['history_rows']} rows (mixed phone formats, upper case emails, 12 repeats, 3 rows that are not contacts)")
        say(f"   {dnc.name}: {facts['dnc_rows']} rows, 5 of them also in the history file")

        step("2. Import the do-not-contact list, then the history, dry run first")
        code, out = run_cli("relay.import_history", [str(dnc), "--list", "suppression", "--apply"], env)
        show(out)
        expect(code == 0 and number(out, "inserted") == 25 - 0, "the do-not-contact list loaded (25 rows)")
        code, out = run_cli("relay.import_history", [str(history), "--kind", "ContactCreate"], env)
        show(out)
        expect(code == 1 and "DRY RUN" in out, "the dry run wrote nothing and said three rows are not contacts")
        dry_invalid = out.count("  line ")
        expect(dry_invalid == 3, f"the dry run named {dry_invalid} refused rows by line number, never by contact")
        code, out = run_cli("relay.import_history", [str(history), "--kind", "ContactCreate", "--apply"], env)
        expect(code == 1 and "nothing was written" in out, "--apply refuses a file with refused rows (all or nothing)")
        code, out = run_cli("relay.import_history", [str(history), "--kind", "ContactCreate", "--apply", "--skip-invalid"], env)
        show(out)
        history_in = number(out, "inserted")
        expect(code == 1 and history_in == 300, f"--skip-invalid wrote the {history_in} distinct people (12 repeats folded)")
        expect(number(out, "duplicates") == 12 and number(out, "suppressed") == 5, "12 repeats folded and 5 of the history people are on the do-not-contact list")
        code, out = run_cli("relay.import_history", [str(history), "--kind", "ContactCreate", "--apply", "--skip-invalid"], env)
        expect(number(out, "inserted") == 0, "importing the same file again adds nothing (idempotent)")

        # ---------------------------------------------------------------------------------------------------------------
        step("3. Replay leads through the real receiver and worker")
        known = [n for n in range(110, 160) if (n - 100) % 7 != 3][:10]  # ten history people that the file holds by phone
        already = [payload(n, phone=e164(n), email=f"fresh{n}@example.org") for n in known]
        suppressed = [payload(100 + k, phone=e164(500 + k), email=f"fresh{100 + k}@example.org") for k in range(6)]
        fresh = [payload(200 + k, phone=e164(700 + k), email=f"new{200 + k}@example.org") for k in range(18)]
        first = fresh[0]
        ingest_one(store, cfg, first)  # the first new lead warms the Sheets token, so the next 5xx is the append and nothing else
        drain(rig, store)
        base_slack, base_rows = slack.effects, sheets.effects
        expect(base_slack == 1 and base_rows == 1, "a brand new lead lands once in Slack and once in the sheet")
        for p in already + suppressed + fresh[1:12]:
            ingest_one(store, cfg, p)
        for p in fresh[1:4]:  # the same delivery posted again: a duplicate, stored once
            ingest_one(store, cfg, p)
        before = len(store.deliveries())
        sheets.queue("act_then_fail")  # the next append is written, then the answer is a 500
        for p in fresh[12:14]:
            ingest_one(store, cfg, p)
        drain(rig, store)
        dup_note = len(store.deliveries()) - before - 4  # 2 leads x 2 sinks were just added
        expect(dup_note == 0, "the three repeated deliveries created no new rows")
        for p in fresh[14:]:
            ingest_one(store, cfg, p)
        drain(rig, store)
        rows = store.deliveries()
        kinds: dict[str, int] = {}
        for d in rows:
            tag = d["effect_ref"] if d["effect_ref"] in ("suppressed", "already_sent") else d["state"]
            kinds[tag] = kinds.get(tag, 0) + 1
        sent_leads = len(fresh)
        say("   deliveries by outcome: " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items())))
        expect(slack.effects == sent_leads, f"Slack got exactly {sent_leads} messages, one per new lead")
        expect(sheets.effects == sent_leads, f"the sheet got exactly {sent_leads} rows, one per new lead, even with a 500 after a write")
        expect(kinds.get("already_sent") == 10 * 2, "10 people the old system already messaged were skipped on both destinations")
        expect(kinds.get("suppressed") == 6 * 2, "6 people on the do-not-contact list were refused on both destinations")
        why = {d["last_error"] for d in rows if d["effect_ref"] == "suppressed"}
        say("   reasons stored on the refused rows: " + ", ".join(sorted(str(w).replace("suppressed: ", "") for w in why)))
        expect(all(w and ("asked to stop" in w or "bounced" in w or "legal" in w) for w in map(str, why)), "each refusal carries its stored reason")
        reconciled = [d for d in rows if d["sink"] == "sheets" and d["effect_ref"] == "reconciled"]
        expect(len(reconciled) == 1, "the append that answered 500 after writing was read back, found, and not repeated")

        # ---------------------------------------------------------------------------------------------------------------
        step("4. Kill the worker while it holds a delivery, start another")
        slack.reset()
        sheets.reset()
        first_w = Worker(env, work / "worker1.log")
        workers.append(first_w)
        wait_for(lambda: store.heartbeat_age_s("worker") is not None and store.heartbeat_age_s("worker") < 2, timeout=30, what="the first worker")
        slack.queue("hang")  # the next Slack request is held open: the worker is inside the send, holding the claim
        victim = ingest_one(store, cfg, payload(900, phone=e164(900), email="new900@example.org"))
        wait_for(lambda: len(slack.requests) == 1, timeout=30, what="the worker to be inside the send")
        claimed = store.delivery(victim.event_id, "slack")
        expect(claimed["state"] == "pending" and claimed["claim_token"] is not None, "the delivery is claimed and not finished")
        first_w.kill()
        slack.release.set()
        expect(slack.effects == 0, "the worker died hard (TerminateProcess): nothing was sent")
        second_w = Worker(env, work / "worker2.log")
        workers.append(second_w)
        wait_for(lambda: store.heartbeat_age_s("worker") is not None and store.heartbeat_age_s("worker") < 2, timeout=30, what="the second worker")
        inside = store.delivery(victim.event_id, "slack")
        expect(inside["state"] == "pending" and slack.effects == 0, "inside the dead worker's lease nobody may take the row, so there is no duplicate")
        wait_for(lambda: store.delivery(victim.event_id, "slack")["state"] == "done", timeout=60, what="the lease to run out")
        expect(slack.effects == 1, "after the lease the new worker delivered it exactly once")
        expect(second_w.proc.poll() is None, "and it is still running")

        # ---------------------------------------------------------------------------------------------------------------
        step("5. Health check: healthy, then the worker stops")
        code, out = run_cli("relay.health_check", ["--max-heartbeat-age-s", "3"], env)
        show(out.splitlines()[-1] if out else "")
        expect(code == 0, "healthy while the worker is beating (exit 0)")
        second_w.kill()
        wait_for(lambda: (store.heartbeat_age_s("worker") or 0) > 3.5, timeout=30, what="the heartbeat to go stale")
        alerts_before = slack.effects
        code, out = run_cli("relay.health_check", ["--max-heartbeat-age-s", "3", "--alert"], env)
        show("\n".join(line for line in out.splitlines() if line.startswith(("FINDING", "alert"))))
        expect(code == 1 and "FINDING heartbeat" in out, "a stopped worker is a finding (exit 1)")
        expect(slack.effects == alerts_before + 1, "one alert was posted")
        code, out = run_cli("relay.health_check", ["--max-heartbeat-age-s", "3", "--alert"], env)
        show("\n".join(line for line in out.splitlines() if line.startswith(("FINDING", "alert"))))
        expect(code == 1 and slack.effects == alerts_before + 1, "the next run stays quiet inside the cooldown (still exit 1, no second alert)")
    finally:
        slack.release.set()
        for w in workers:
            w.close()
        if store is not None:
            store.close()
        slack.stop()
        sheets.stop()

    step("Result")
    seconds = time.monotonic() - started
    if FAILURES:
        say(f"FAIL: {len(FAILURES)} expectation(s) failed in {seconds:.0f}s")
        for f in FAILURES:
            say("   - " + f)
        return 1
    say(f"PASS: every expectation held ({seconds:.0f}s, fake destinations, invented data)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
