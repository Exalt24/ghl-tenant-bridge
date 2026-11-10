#!/usr/bin/env python3
# LIFESPAN: permanent
"""
Mutation check for tests/ghl_setup.py: each mutant breaks ONE guard in a temp copy of the module and
tests/ghl_setup.test.py (pointed at the copy with GHL_SETUP_PATH) must go red. A mutant whose anchor is not
found exactly once is STALE and fails the run, so a reworded line cannot turn this into a quiet pass.

Usage:  python tests/ghl_setup_mutants.py [name filter]
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TARGET = os.path.join(HERE, "ghl_setup.py")

MUTANTS = [
    ("the bearer token is not sent", 'req.add_header("Authorization", "Bearer " + self._token)', "pass"),
    ("the Version header is not sent", 'req.add_header("Version", self.version)', "pass"),
    ("the throttle allows a thousand requests per window", "limit=WINDOW_LIMIT,", "limit=1000,"),
    ("the throttle keeps no gap between calls", "MIN_GAP_SECONDS = 0.3", "MIN_GAP_SECONDS = 0.0"),
    ("a 401 or 403 is no longer a scope block", "if status in (401, 403):", "if False:"),
    ("any status is accepted", "if status not in ok:", "if False:"),
    ("the free slot range is not capped", 'if days > 31:', "if False:"),
    ("the appointment end ignores the slot length", "end = start + timedelta(minutes=minutes)", "end = start"),
    ("the appointment notifies the contact", '"toNotify": False,', '"toNotify": True,'),
    ("the appointment drops the slot offset", "start = parse_when(slot_start)", "start = parse_when(slot_start).astimezone(timezone.utc)"),
    ("a Saturday slot can be booked", "if parse_when(t).weekday() < 5:", "if True:"),
    ("the contact field value is not compared", "if value != text:", "if False:"),
    ("the custom value text is not compared", "if obj.get(\"value\") != text:", "if False:"),
    ("the event start is not compared", "if parse_when(event[\"startTime\"]) != parse_when(slot_start):", "if False:"),
    ("a booked slot may still be offered", "return not any(parse_when(t) == want for t in slot_times(resp))", "return True"),
    ("the calendar open days are not checked", "if days != [1, 2, 3, 4, 5]:", "if False:"),
    ("the field is created again on every run", "    if existing:\n        log(\"custom field already exists", "    if False:\n        log(\"custom field already exists"),
    ("the value is created again on every run", "    if existing is None:\n        st, body = c.request(\"POST\", \"/locations/%s/customValues\"", "    if True:\n        st, body = c.request(\"POST\", \"/locations/%s/customValues\""),
    ("the calendar is created again on every run", "    if existing:\n        log(\"calendar already exists", "    if False:\n        log(\"calendar already exists"),
    ("a leftover cleanup deletes any calendar's appointment", "if e.get(\"calendarId\") == calendar_id\n", "if True\n"),
    ("the leftover cleanup is skipped", "for ev in active_events(expect(", "for ev in [] and active_events(expect("),
    ("the appointment is never deleted", "st, body = c.request(\"DELETE\", \"/calendars/events/%s\" % event_id)\n    expect(st, body, (200, 201), \"delete the appointment\")", "pass"),
    ("a blocked part stops the whole run", "        except ScopeBlocked as e:\n            result[name] = {\"blocked\": str(e)}\n            log(\"BLOCKED %s\" % e)\n    contact_id", "        except ZeroDivisionError as e:\n            result[name] = {\"blocked\": str(e)}\n            log(\"BLOCKED %s\" % e)\n    contact_id"),
    ("the second value text is never written", "value_body(VALUE_NAME, VALUE_SECOND))\n    expect(st, body, (200,), \"update custom value\")", "value_body(VALUE_NAME, VALUE_FIRST))\n    expect(st, body, (200,), \"update custom value\")"),
    ("the transcript records the token", "self.transcript.append({\"method\": method, \"path\": path, \"request\": body,", "self.transcript.append({\"method\": method, \"path\": path + self._token, \"request\": body,"),
]


def main():
    only = [a for a in sys.argv[1:] if not a.startswith("-")]
    base = open(TARGET, encoding="utf-8", newline="").read()
    bad = ran = 0
    for i, (name, old, new) in enumerate(MUTANTS):
        if only and not any(o.lower() in name.lower() for o in only):
            continue
        if base.count(old) != 1:
            print("STALE    %s (anchor found %d times)" % (name, base.count(old)))
            bad += 1
            continue
        ran += 1
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "ghl_setup.py")
            with open(path, "w", encoding="utf-8", newline="") as fh:
                fh.write(base.replace(old, new))
            env = dict(os.environ, GHL_SETUP_PATH=path)
            res = subprocess.run([sys.executable, "-I", os.path.join(HERE, "ghl_setup.test.py")],
                                 capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=120)
            survived = res.returncode == 0
            print("%s %s" % ("SURVIVED" if survived else "caught  ", name))
            bad += survived
    print("GHL SETUP MUTANTS: %s (%d run, %d problem(s))" % ("ALL CAUGHT" if not bad else "PROBLEMS", ran, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
