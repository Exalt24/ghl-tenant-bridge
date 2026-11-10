"""Offline test for tests/ghl_setup.py: request building, response checking, the rate limit helper
and the whole custom field, custom value and calendar flow, run against a stand-in HighLevel
server on 127.0.0.1. CI runs it with no token and no network.

The stand-in is written from the vendor pages and from the answers the sandbox gave on
2026-11-10 (shapes only: ids and data are invented). It refuses what the real API refused: no
token, no Version header, a booking on a slot that is not free, more than 25 requests in 10 seconds.
It runs on a fake clock that the throttle advances, so the test is fast and the budget is counted.

GHL_SETUP_PATH points the test at a mutated copy of ghl_setup.py (tests/ghl_setup_mutants.py).
"""
import importlib.util
import json
import os
import re
import sys
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
MODULE = os.environ.get("GHL_SETUP_PATH") or str(HERE / "ghl_setup.py")
spec = importlib.util.spec_from_file_location("ghl_setup_under_test", MODULE)
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)

LOC = "LOC000000000000000001"
USER = "USER00000000000000001"
TOKEN = "stand-in-token-not-a-secret"
TZ = timezone(timedelta(hours=-6))

passed = failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
    else:
        failed += 1
        print("FAIL  %s %s" % (name, detail))


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


class State:
    def __init__(self, clock):
        self.clock = clock
        self.n = 0
        self.fields, self.values, self.calendars, self.contacts, self.events = [], [], [], [], []
        self.posts = []          # (path) of every create
        self.stamps = []         # fake clock time of every request
        self.worst_window = 0
        self.refuse_prefix = None      # a path prefix answered with the scope message
        self.ignore_contact_fields = False
        self.offer_saturday = False
        self.seen_headers = []

    def new_id(self, kind):
        self.n += 1
        return "%s%018d" % (kind, self.n)


def slots_for(state, cal, start_ms, end_ms):
    days = {d for h in cal.get("openHours", []) for d in h["daysOfTheWeek"]}
    if state.offer_saturday:
        days.add(6)
    booked = {parse(e["startTime"]) for e in state.events if e["calendarId"] == cal["id"] and e["state"] == "active"}
    out = {}
    day = datetime.fromtimestamp(start_ms / 1000, TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    last = datetime.fromtimestamp(end_ms / 1000, TZ)
    while day < last:
        dow = (day.weekday() + 1) % 7   # Monday 1 ... Sunday 0
        if dow in days:
            for k in range(16):
                s = day + timedelta(hours=9, minutes=30 * k)
                ms = s.timestamp() * 1000
                if start_ms <= ms < end_ms and s not in booked:
                    out.setdefault(day.strftime("%Y-%m-%d"), {"slots": []})["slots"].append(s.isoformat())
        day += timedelta(days=1)
    return out


def parse(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(TZ)


def make_handler(state):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, status, body):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def handle_any(self, method):
            url = urllib.parse.urlparse(self.path)
            path, q = url.path, urllib.parse.parse_qs(url.query)
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n)) if n else None
            now = state.clock.now()
            state.stamps.append(now)
            state.worst_window = max(state.worst_window, sum(1 for t in state.stamps if now - t < 10.0))
            state.seen_headers.append((self.headers.get("Authorization"), self.headers.get("Version")))
            if state.worst_window > 25:
                return self.send(429, {"message": "Too many requests"})
            if self.headers.get("Authorization") != "Bearer " + TOKEN:
                return self.send(401, {"message": "Unauthorized"})
            if not self.headers.get("Version"):
                return self.send(400, {"message": "Version header missing"})
            if state.refuse_prefix and path.startswith(state.refuse_prefix):
                return self.send(401, {"message": "The token is not authorized for this scope."})
            return self.route(method, path, q, body)

        def route(self, method, path, q, body):
            P = path.rstrip("/")
            m = re.fullmatch(r"/locations/([^/]+)/customFields", P)
            if m:
                if method == "GET":
                    return self.send(200, {"customFields": state.fields})
                f = {"id": state.new_id("F"), "model": body.get("model"), "name": body["name"],
                     "fieldKey": "contact." + re.sub(r"\W+", "_", body["name"].lower()),
                     "dataType": body["dataType"], "locationId": m.group(1)}
                state.fields.append(f)
                state.posts.append(P)
                return self.send(201, {"customField": f, "backfillTriggered": False})
            m = re.fullmatch(r"/locations/([^/]+)/customValues(?:/([^/]+))?", P)
            if m:
                if method == "GET":
                    return self.send(200, {"customValues": state.values})
                if method == "POST":
                    v = {"id": state.new_id("V"), "name": body["name"], "value": body["value"], "locationId": m.group(1),
                         "fieldKey": "{{ custom_values.%s }}" % re.sub(r"\W+", "_", body["name"].lower())}
                    state.values.append(v)
                    state.posts.append(P)
                    return self.send(201, {"customValue": v})
                v = next((x for x in state.values if x["id"] == m.group(2)), None)
                if v is None:
                    return self.send(404, {"message": "not found"})
                v["value"] = body["value"]
                return self.send(200, {"customValue": v})
            if P == "/contacts/upsert":
                c = next((x for x in state.contacts if x["email"] == body["email"]), None)
                new = c is None
                if new:
                    c = {"id": state.new_id("C"), "email": body["email"], "firstName": body["firstName"],
                         "lastName": body["lastName"], "locationId": body["locationId"], "customFields": []}
                    state.contacts.append(c)
                    state.posts.append(P)
                return self.send(201 if new else 200, {"new": new, "succeeded": True, "contact": c})
            m = re.fullmatch(r"/contacts/([^/]+)", P)
            if m:
                c = next((x for x in state.contacts if x["id"] == m.group(1)), None)
                if c is None:
                    return self.send(404, {"message": "not found"})
                if method == "PUT":
                    if not state.ignore_contact_fields:
                        for cf in body.get("customFields", []):
                            c["customFields"] = [x for x in c["customFields"] if x["id"] != cf["id"]]
                            c["customFields"].append({"id": cf["id"], "value": cf["field_value"]})
                    return self.send(200, {"succeeded": True, "contact": c})
                return self.send(200, {"contact": c})
            m = re.fullmatch(r"/contacts/([^/]+)/appointments", P)
            if m:
                evs = [{k: e[k] for k in ("id", "calendarId", "contactId", "title", "startTime", "appointmentStatus")}
                       for e in state.events if e["contactId"] == m.group(1) and e["state"] == "active"]
                return self.send(200, {"events": evs})
            if P == "/calendars":
                if method == "GET":
                    return self.send(200, {"calendars": state.calendars})
                cal = dict(body)
                cal["id"] = state.new_id("K")
                state.calendars.append(cal)
                state.posts.append(P)
                return self.send(201, {"calendar": cal})
            m = re.fullmatch(r"/calendars/([^/]+)/free-slots", P)
            if m:
                cal = next((x for x in state.calendars if x["id"] == m.group(1)), None)
                if cal is None:
                    return self.send(404, {"message": "not found"})
                a, b = int(q["startDate"][0]), int(q["endDate"][0])
                if b - a > 31 * 86400 * 1000:
                    return self.send(400, {"message": "range over 31 days"})
                return self.send(200, slots_for(state, cal, a, b))
            if P == "/calendars/events/appointments" and method == "POST":
                cal = next((x for x in state.calendars if x["id"] == body["calendarId"]), None)
                if cal is None:
                    return self.send(404, {"message": "calendar not found"})
                day = parse(body["startTime"])
                free = slots_for(state, cal, int((day - timedelta(hours=1)).timestamp() * 1000),
                                 int((day + timedelta(hours=1)).timestamp() * 1000))
                offered = {parse(s) for d in free.values() for s in d["slots"]}
                if not body.get("ignoreFreeSlotValidation") and day not in offered:
                    return self.send(400, {"message": "slot not available (stand-in)"})
                e = {"id": state.new_id("E"), "calendarId": cal["id"], "contactId": body["contactId"],
                     "locationId": body["locationId"], "title": body.get("title"), "startTime": body["startTime"],
                     "endTime": body["endTime"], "appointmentStatus": body.get("appointmentStatus"),
                     "state": "active", "toNotify": body.get("toNotify", True)}
                state.events.append(e)
                state.posts.append(P)
                return self.send(201, {k: e[k] for k in ("id", "calendarId", "locationId", "contactId", "title", "state")})
            m = re.fullmatch(r"/calendars/events/appointments/([^/]+)", P)
            if m and method == "GET":
                e = next((x for x in state.events if x["id"] == m.group(1)), None)
                return self.send(200, {"appointment": e}) if e else self.send(404, {"message": "not found"})
            m = re.fullmatch(r"/calendars/events/([^/]+)", P)
            if m and method == "DELETE":
                e = next((x for x in state.events if x["id"] == m.group(1)), None)
                if e is None:
                    return self.send(404, {"message": "not found"})
                e["state"] = "deleted"
                return self.send(200, {"succeeded": True})
            m = re.fullmatch(r"/calendars/([^/]+)", P)
            if m and method == "GET":
                cal = next((x for x in state.calendars if x["id"] == m.group(1)), None)
                return self.send(200, {"calendar": cal}) if cal else self.send(404, {"message": "not found"})
            return self.send(404, {"message": "no route " + P})

        def do_GET(self):
            self.handle_any("GET")

        def do_POST(self):
            self.handle_any("POST")

        def do_PUT(self):
            self.handle_any("PUT")

        def do_DELETE(self):
            self.handle_any("DELETE")

    return H


class Rig:
    def __init__(self, token=TOKEN):
        self.clock = FakeClock()
        self.state = State(self.clock)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.state))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.client = g.Client(token, base=self.base, throttle=g.Throttle(self.clock.now, self.clock.sleep))

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def run(self, now=datetime(2026, 11, 10, 7, 0, tzinfo=timezone.utc)):
        logs = []
        return g.run_all(self.client, LOC, USER, logs.append, now=now), logs


# ------------------------------------------------------------------ pure pieces

b = g.field_body()
check("field body is a contact TEXT field", b["dataType"] == "TEXT" and b["model"] == "contact" and b["name"] == "Lead source detail")
cb = g.calendar_body(LOC, USER)
check("calendar body: weekdays only, 30 minutes, 9 to 5",
      sorted(d for h in cb["openHours"] for d in h["daysOfTheWeek"]) == [1, 2, 3, 4, 5]
      and cb["slotDuration"] == 30 and cb["openHours"][0]["hours"][0]["openHour"] == 9 and cb["openHours"][0]["hours"][0]["closeHour"] == 17)
ab = g.appointment_body(LOC, "CAL", "CON", USER, "2026-11-10T09:00:00-06:00")
check("appointment keeps the slot offset", ab["startTime"] == "2026-11-10T09:00:00-06:00")
check("appointment ends 30 minutes later", ab["endTime"] == "2026-11-10T09:30:00-06:00", ab["endTime"])
check("appointment never notifies", ab["toNotify"] is False)
p = g.free_slots_path("CAL", datetime(2026, 11, 10, tzinfo=timezone.utc), 7)
check("free slots path has epoch milliseconds", "startDate=1794268800000" in p and "endDate=1794873600000" in p, p)
try:
    g.free_slots_path("CAL", datetime(2026, 11, 10, tzinfo=timezone.utc), 32)
    check("free slot range over 31 days is refused", False)
except ValueError:
    check("free slot range over 31 days is refused", True)

fake = {"2026-11-14": {"slots": ["2026-11-14T10:00:00-06:00"]}, "2026-11-16": {"slots": ["2026-11-16T09:00:00-06:00", "2026-11-16T09:30:00-06:00"]}, "traceId": "x"}
check("slot times ignore the trace id and sort", g.slot_times(fake)[0].startswith("2026-11-14"))
check("a Saturday slot is skipped for the next weekday", g.first_weekday_slot(fake) == "2026-11-16T09:00:00-06:00")
check("a slot missing from the answer counts as taken", g.slot_is_taken(fake, "2026-11-16T10:00:00-06:00") and not g.slot_is_taken(fake, "2026-11-16T09:00:00-06:00"))
try:
    g.first_weekday_slot({})
    check("no slots is a failure", False)
except g.CheckFailed:
    check("no slots is a failure", True)

for label, fn in (
    ("a wrong contact field value is refused", lambda: g.check_contact_field({"customFields": [{"id": "F1", "value": "x"}]}, "F1", "y")),
    ("a missing contact field is refused", lambda: g.check_contact_field({"customFields": []}, "F1", "y")),
    ("a wrong custom value text is refused", lambda: g.check_value({"name": g.VALUE_NAME, "value": "a", "fieldKey": "{{ custom_values.x }}"}, g.VALUE_NAME, "b", LOC)),
    ("an event at another time is refused", lambda: g.check_event({"calendarId": "C", "contactId": "P", "startTime": "2026-11-10T10:00:00-06:00"}, "C", "P", "2026-11-10T09:00:00-06:00", LOC)),
    ("a field on the wrong data type is refused", lambda: g.check_field({"name": g.FIELD_NAME, "dataType": "NUMERICAL", "fieldKey": "contact.x"}, g.FIELD_NAME, LOC)),
    ("a calendar open on weekends is refused", lambda: g.check_calendar({"name": g.CALENDAR_NAME, "locationId": LOC, "slotDuration": 30, "isActive": True,
                                                                        "openHours": [{"daysOfTheWeek": [0, 1, 2, 3, 4, 5]}], "teamMembers": [{"userId": USER}]}, g.CALENDAR_NAME, LOC, USER)),
    ("two objects with one name are refused", lambda: g.find_by_name([{"name": "a"}, {"name": "A"}], "a")),
):
    try:
        fn()
        check(label, False)
    except g.CheckFailed:
        check(label, True)
for st in (401, 403):
    try:
        g.expect(st, {"message": "The token is not authorized for this scope."}, (200,), "probe")
        check("HTTP %d becomes a scope block" % st, False)
    except g.ScopeBlocked as e:
        check("HTTP %d becomes a scope block with the exact text" % st, e.message == "The token is not authorized for this scope." and e.status == st)
try:
    g.expect(500, {}, (200,), "probe")
    check("HTTP 500 is a failed check", False)
except g.CheckFailed:
    check("HTTP 500 is a failed check", True)
except g.ScopeBlocked:
    check("HTTP 500 is a failed check", False)

# throttle: 300 calls on a fake clock never put more than 20 in any 10 second window
clock = FakeClock()
th = g.Throttle(clock.now, clock.sleep)
stamps = []
for _ in range(300):
    th.wait()
    stamps.append(clock.now())
worst = max(sum(1 for t in stamps if 0 <= s - t < 10.0) for s in stamps)
check("throttle keeps every 10 second window at 20 or fewer", worst <= 20, "worst %d" % worst)
check("throttle keeps a gap between calls", all(b2 - a2 >= 0.299 for a2, b2 in zip(stamps, stamps[1:])))

# ------------------------------------------------------------------ whole flow on the stand-in

rig = Rig()
res, logs = rig.run()
s = rig.state
check("positive control: all three parts finish", all("blocked" not in v for v in res.values()) and set(res) == {"customField", "customValue", "calendar"}, json.dumps(res)[:300])
check("one custom field, named and typed", len(s.fields) == 1 and s.fields[0]["name"] == "Lead source detail")
check("one custom value, holding the second text", len(s.values) == 1 and s.values[0]["value"] == g.VALUE_SECOND)
check("one calendar", len(s.calendars) == 1 and s.calendars[0]["name"] == g.CALENDAR_NAME)
check("the test contact carries the field value", len(s.contacts) == 1 and s.contacts[0]["customFields"] == [{"id": s.fields[0]["id"], "value": g.FIELD_TEXT}])
check("one appointment was booked and then deleted", len(s.events) == 1 and s.events[0]["state"] == "deleted")
check("the booked appointment sent no notification", s.events[0]["toNotify"] is False)
check("the booking is for a weekday morning slot at 9", parse(s.events[0]["startTime"]).hour == 9 and parse(s.events[0]["startTime"]).weekday() < 5)
check("never more than 25 requests in 10 seconds", s.worst_window <= 25, "worst %d" % s.worst_window)
check("every call carried the bearer token and the Version header",
      all(a == "Bearer " + TOKEN and v == g.API_VERSION for a, v in s.seen_headers))
check("the transcript holds no token", TOKEN not in json.dumps(rig.client.transcript))
first_posts = list(s.posts)

res2, _ = rig.run()
check("re-run: only the booking is created again, no field, value, calendar or contact",
      s.posts[len(first_posts):] == ["/calendars/events/appointments"], str(s.posts[len(first_posts):]))
check("re-run: still one field, one value, one calendar, one contact",
      (len(s.fields), len(s.values), len(s.calendars), len(s.contacts)) == (1, 1, 1, 1))
check("re-run: reports nothing as created", not any(res2[k].get("created") for k in res2))
check("re-run: the value ends on the second text again", s.values[0]["value"] == g.VALUE_SECOND)
check("re-run: the contact holds one entry for the field", len(s.contacts[0]["customFields"]) == 1)
rig.close()

# only our own appointment is ever deleted
rig = Rig()
res, _ = rig.run()
s = rig.state
st, body = rig.client.request("POST", "/contacts/upsert", {"locationId": LOC, "firstName": "Other", "lastName": "Person", "email": "other@example.com"})
other = body["contact"]["id"]
cal_id = s.calendars[0]["id"]
rig.client.request("POST", "/calendars/events/appointments",
                   {"calendarId": cal_id, "locationId": LOC, "contactId": other, "title": "Not ours", "startTime": "2026-11-11T09:00:00-06:00",
                    "endTime": "2026-11-11T09:30:00-06:00", "appointmentStatus": "confirmed"})
st, body = rig.client.request("POST", "/calendars/", {"locationId": LOC, "name": "Someone else's calendar", "openHours": []})
rig.state.events.append({"id": "EXTERNAL", "calendarId": body["calendar"]["id"], "contactId": s.contacts[0]["id"], "locationId": LOC,
                         "title": "Same contact, other calendar", "startTime": "2026-11-12T09:00:00-06:00", "endTime": "2026-11-12T09:30:00-06:00",
                         "appointmentStatus": "confirmed", "state": "active"})
rig.run()
alive = {e["title"] for e in s.events if e["state"] == "active"}
check("another contact's appointment on our calendar survives", "Not ours" in alive)
check("our contact's appointment on another calendar survives", "Same contact, other calendar" in alive)
check("a calendar we did not make is untouched", any(c["name"] == "Someone else's calendar" for c in s.calendars))
rig.close()

# a leftover from an aborted run is cleared, and only that one
rig = Rig()
rig.run()
s = rig.state
s.events.append({"id": "LEFTOVER", "calendarId": s.calendars[0]["id"], "contactId": s.contacts[0]["id"], "locationId": LOC,
                 "title": g.APPOINTMENT_TITLE, "startTime": "2026-11-13T09:00:00-06:00", "endTime": "2026-11-13T09:30:00-06:00",
                 "appointmentStatus": "confirmed", "state": "active"})
rig.run()
check("a leftover booking of ours is cleared on the next run", next(e for e in s.events if e["id"] == "LEFTOVER")["state"] == "deleted")
rig.close()

# a missing scope on one part does not hide the others, and its text is kept
for prefix, part in (("/calendars", "calendar"), ("/locations/%s/customFields" % LOC, "customField"), ("/locations/%s/customValues" % LOC, "customValue")):
    rig = Rig()
    rig.state.refuse_prefix = prefix
    res, logs = rig.run()
    others = [k for k in ("customField", "customValue", "calendar") if k != part]
    if part == "customField":
        others = ["customValue"]   # the calendar part needs the contact, which still exists through the upsert
    check("%s blocked: reported with the exact scope text" % part, "The token is not authorized for this scope." in res[part].get("blocked", ""), json.dumps(res)[:200])
    check("%s blocked: the others still ran" % part, all("blocked" not in res[k] for k in others), json.dumps(res)[:300])
    rig.close()

# negative control: a contact write that is silently ignored must fail the read back
rig = Rig()
rig.state.ignore_contact_fields = True
try:
    rig.run()
    check("a contact write that did not stick fails the read back", False)
except g.CheckFailed:
    check("a contact write that did not stick fails the read back", True)
rig.close()

# the calendar refuses a slot it does not offer, and the helper turns that into a failed check
rig = Rig()
rig.run()
s = rig.state
try:
    st, body = rig.client.request("POST", "/calendars/events/appointments",
                                  g.appointment_body(LOC, s.calendars[0]["id"], s.contacts[0]["id"], USER, "2026-11-14T10:00:00-06:00"))
    g.expect(st, body, (200, 201), "book a Saturday")
    check("a Saturday booking is refused by the calendar", False)
except g.CheckFailed:
    check("a Saturday booking is refused by the calendar", True)
rig.close()

# the stand-in offers a Saturday slot first: the flow must book a weekday
rig = Rig()
rig.state.offer_saturday = True
rig.run(now=datetime(2026, 11, 14, 14, 0, tzinfo=timezone.utc))   # a Saturday
check("a Saturday offer is skipped, a weekday is booked", rig.state.events and parse(rig.state.events[0]["startTime"]).weekday() < 5)
rig.close()

# a wrong token is an auth failure, not a silent pass
rig = Rig(token="not-the-token")
res, _ = rig.run()
check("a bad token blocks every part", all("blocked" in v for v in res.values()), json.dumps(res)[:200])
rig.close()

print("%d checks passed, %d failed" % (passed, failed))
sys.exit(0 if failed == 0 else 1)
