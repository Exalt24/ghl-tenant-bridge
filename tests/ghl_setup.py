"""Request building, response checking and the rate limit helper for the custom field,
custom value and calendar checks (docs/custom-fields-values-calendars.md).

Nothing in this file touches the network by itself. The live run in
tests/custom_fields_values_calendars.py and the offline test in tests/ghl_setup.test.py both
drive it: the first against a HighLevel sandbox, the second against a stand-in server that is
written from the vendor pages and from the answers the sandbox gave on 2026-11-10.
"""
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = "https://services.leadconnectorhq.com"
API_VERSION = "2021-07-28"
# the sandbox allows 25 requests per 10 seconds per location; stay well under it
WINDOW_SECONDS = 10.0
WINDOW_LIMIT = 20
MIN_GAP_SECONDS = 0.3
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

FIELD_NAME = "Lead source detail"
VALUE_NAME = "Welcome offer text"
VALUE_FIRST = "10 percent off your first lesson"
VALUE_SECOND = "15 percent off your first lesson, this month only"
CALENDAR_NAME = "Discovery call (API demo)"
CONTACT_EMAIL = "test.lead@example.com"
FIELD_TEXT = "Spring open house flyer"
APPOINTMENT_TITLE = "Discovery call (API demo)"


class CheckFailed(Exception):
    """A response did not say what the step needs it to say."""


class ScopeBlocked(Exception):
    """The API refused the call with a 401 or 403. The message is kept word for word."""

    def __init__(self, status, message, label):
        super().__init__("%s: HTTP %s %s" % (label, status, message))
        self.status = status
        self.message = message
        self.label = label


class Throttle:
    """Keeps a run under the request budget. The clock and the sleep are injected so a test can
    run it on a fake clock and count requests per window."""

    def __init__(self, clock=time.monotonic, sleep=time.sleep, limit=WINDOW_LIMIT,
                 window=WINDOW_SECONDS, gap=MIN_GAP_SECONDS):
        self.clock, self.sleep = clock, sleep
        self.limit, self.window, self.gap = limit, window, gap
        self.stamps = []

    def wait(self):
        now = self.clock()
        if self.stamps:
            pause = self.gap - (now - self.stamps[-1])
            if pause > 0:
                self.sleep(pause)
                now = self.clock()
        self.stamps = [t for t in self.stamps if now - t < self.window]
        if len(self.stamps) >= self.limit:
            pause = self.window - (now - self.stamps[0])
            if pause > 0:
                self.sleep(pause)
                now = self.clock()
            self.stamps = [t for t in self.stamps if now - t < self.window]
        self.stamps.append(now)


def _parse(text):
    try:
        return json.loads(text) if text else {}
    except ValueError:
        return {"raw": text[:300]}


class Client:
    """One HTTP call at a time, throttled, with a transcript of every call (no headers, so the
    token never lands in it)."""

    def __init__(self, token, base=BASE, version=API_VERSION, throttle=None):
        self._token = token
        self.base = base
        self.version = version
        self.throttle = throttle or Throttle()
        self.transcript = []

    def request(self, method, path, body=None):
        self.throttle.wait()
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Authorization", "Bearer " + self._token)
        req.add_header("Version", self.version)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", UA)
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                status, text = r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            status, text = e.code, e.read().decode("utf-8", "replace")
        parsed = _parse(text)
        self.transcript.append({"method": method, "path": path, "request": body,
                                "status": status, "response": parsed})
        return status, parsed


# ---------------------------------------------------------------- request building

def field_body(name=FIELD_NAME):
    return {"name": name, "dataType": "TEXT", "model": "contact",
            "placeholder": "Where the lead first heard of us"}


def value_body(name, value):
    return {"name": name, "value": value}


def contact_body(location_id, email=CONTACT_EMAIL):
    return {"locationId": location_id, "firstName": "Test", "lastName": "Lead",
            "email": email, "source": "api test"}


def contact_field_body(field_id, text):
    return {"customFields": [{"id": field_id, "field_value": text}]}


def calendar_body(location_id, user_id, name=CALENDAR_NAME):
    weekdays = [{"daysOfTheWeek": [d], "hours": [{"openHour": 9, "openMinute": 0,
                                                  "closeHour": 17, "closeMinute": 0}]}
                for d in (1, 2, 3, 4, 5)]
    return {
        "locationId": location_id,
        "name": name,
        "description": "Thirty minute discovery call, weekdays 9 to 5. Made by the API for a demo.",
        "calendarType": "personal",
        "isActive": True,
        "autoConfirm": True,
        "slotDuration": 30,
        "slotDurationUnit": "mins",
        "slotInterval": 30,
        "slotIntervalUnit": "mins",
        "allowBookingAfter": 0,
        "allowBookingAfterUnit": "mins",
        "allowBookingFor": 30,
        "allowBookingForUnit": "days",
        "teamMembers": [{"userId": user_id, "priority": 0.5, "selected": True}],
        "openHours": weekdays,
    }


def free_slots_path(calendar_id, start, days=7, tz=None):
    """Epoch milliseconds; the end is exclusive and the range may not pass 31 days."""
    if days > 31:
        raise ValueError("free slot range may not pass 31 days")
    start_ms = int(start.timestamp() * 1000)
    end_ms = int((start + timedelta(days=days)).timestamp() * 1000)
    q = {"startDate": start_ms, "endDate": end_ms}
    if tz:
        q["timezone"] = tz
    return "/calendars/%s/free-slots?%s" % (calendar_id, urllib.parse.urlencode(q))


def parse_when(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def appointment_body(location_id, calendar_id, contact_id, user_id, slot_start, minutes=30):
    """startTime and endTime keep the offset the slot came with. toNotify is false so no
    automation runs and nothing is sent."""
    start = parse_when(slot_start)
    end = start + timedelta(minutes=minutes)
    return {
        "calendarId": calendar_id,
        "locationId": location_id,
        "contactId": contact_id,
        "assignedUserId": user_id,
        "title": APPOINTMENT_TITLE,
        "appointmentStatus": "confirmed",
        "startTime": start.isoformat(),
        "endTime": end.isoformat(),
        "toNotify": False,
        "ignoreFreeSlotValidation": False,
    }


# ---------------------------------------------------------------- response checking

def expect(status, body, ok, label):
    """Raise ScopeBlocked for 401 or 403, CheckFailed for any other status not in ok."""
    if status in (401, 403):
        msg = body.get("message") if isinstance(body, dict) else None
        raise ScopeBlocked(status, msg if isinstance(msg, str) else json.dumps(body)[:200], label)
    if status not in ok:
        raise CheckFailed("%s: HTTP %s %s" % (label, status, json.dumps(body)[:300]))
    return body


def find_by_name(items, name):
    wanted = name.strip().lower()
    hits = [i for i in items if isinstance(i, dict) and str(i.get("name", "")).strip().lower() == wanted]
    if len(hits) > 1:
        raise CheckFailed("%d objects are named %r, cannot pick one" % (len(hits), name))
    return hits[0] if hits else None


def check_field(obj, name, location_id):
    if not obj or obj.get("name") != name:
        raise CheckFailed("custom field %r not found in the answer" % name)
    if obj.get("dataType") != "TEXT":
        raise CheckFailed("custom field %r has dataType %r" % (name, obj.get("dataType")))
    if obj.get("model") not in (None, "contact"):
        raise CheckFailed("custom field %r is on model %r" % (name, obj.get("model")))
    if not str(obj.get("fieldKey", "")).startswith("contact."):
        raise CheckFailed("custom field %r has fieldKey %r" % (name, obj.get("fieldKey")))
    if obj.get("locationId") not in (None, location_id):
        raise CheckFailed("custom field %r belongs to location %r" % (name, obj.get("locationId")))
    return obj


def check_contact_field(contact, field_id, text):
    got = [f for f in contact.get("customFields", []) if f.get("id") == field_id]
    if len(got) != 1:
        raise CheckFailed("contact carries %d entries for the field" % len(got))
    value = got[0].get("value", got[0].get("field_value"))
    if value != text:
        raise CheckFailed("contact field value is %r, wanted %r" % (value, text))
    return value


def check_value(obj, name, text, location_id):
    if not obj or obj.get("name") != name:
        raise CheckFailed("custom value %r not found in the answer" % name)
    if obj.get("value") != text:
        raise CheckFailed("custom value %r reads %r, wanted %r" % (name, obj.get("value"), text))
    if not str(obj.get("fieldKey", "")).replace(" ", "").startswith("{{custom_values."):
        raise CheckFailed("custom value %r has fieldKey %r" % (name, obj.get("fieldKey")))
    if obj.get("locationId") not in (None, location_id):
        raise CheckFailed("custom value %r belongs to location %r" % (name, obj.get("locationId")))
    return obj


def check_calendar(obj, name, location_id, user_id):
    if not obj or obj.get("name") != name:
        raise CheckFailed("calendar %r not found in the answer" % name)
    if obj.get("locationId") != location_id:
        raise CheckFailed("calendar belongs to location %r" % obj.get("locationId"))
    if obj.get("slotDuration") != 30:
        raise CheckFailed("calendar slotDuration is %r" % obj.get("slotDuration"))
    days = sorted(d for h in obj.get("openHours", []) for d in h.get("daysOfTheWeek", []))
    if days != [1, 2, 3, 4, 5]:
        raise CheckFailed("calendar open days are %r, wanted weekdays 1 to 5" % days)
    if obj.get("isActive") is False:
        raise CheckFailed("calendar is not active")
    members = [m.get("userId") for m in obj.get("teamMembers", [])]
    if user_id not in members:
        raise CheckFailed("calendar team members are %r" % members)
    return obj


def slot_times(resp):
    """Every slot as a string, soonest first, from the answer keyed by date. A date with no
    free slots is left out by HighLevel, so an empty answer means no slot at all."""
    out = []
    for key, day in resp.items():
        if not (isinstance(key, str) and len(key) == 10 and key[4] == "-" and key[7] == "-"):
            continue
        slots = day.get("slots") if isinstance(day, dict) else day
        for s in slots or []:
            out.append(s)
    return sorted(out, key=parse_when)


def first_weekday_slot(resp):
    times = slot_times(resp)
    if not times:
        raise CheckFailed("the calendar has no free slot in the range")
    for t in times:
        if parse_when(t).weekday() < 5:
            return t
    raise CheckFailed("no free slot falls on a weekday")


def check_event(event, calendar_id, contact_id, slot_start, location_id):
    if event.get("calendarId") != calendar_id:
        raise CheckFailed("event is on calendar %r" % event.get("calendarId"))
    if event.get("contactId") != contact_id:
        raise CheckFailed("event is for contact %r" % event.get("contactId"))
    if event.get("locationId") not in (None, location_id):
        raise CheckFailed("event belongs to location %r" % event.get("locationId"))
    if parse_when(event["startTime"]) != parse_when(slot_start):
        raise CheckFailed("event starts %r, the slot was %r" % (event.get("startTime"), slot_start))
    return event


def active_events(events, calendar_id):
    """Appointments on our calendar that are not cancelled."""
    return [e for e in events
            if e.get("calendarId") == calendar_id
            and str(e.get("appointmentStatus", e.get("status", ""))).lower() != "cancelled"]


def slot_is_taken(resp, slot_start):
    want = parse_when(slot_start)
    return not any(parse_when(t) == want for t in slot_times(resp))


# ---------------------------------------------------------------- the three parts

def run_field(c, loc, log):
    st, body = c.request("GET", "/locations/%s/customFields?model=contact" % loc)
    fields = expect(st, body, (200,), "list custom fields").get("customFields", [])
    existing = find_by_name(fields, FIELD_NAME)
    if existing:
        log("custom field already exists: %s" % existing["id"])
        created = False
    else:
        st, body = c.request("POST", "/locations/%s/customFields" % loc, field_body())
        existing = expect(st, body, (200, 201), "create custom field").get("customField")
        check_field(existing, FIELD_NAME, loc)
        created = True
        log("custom field created: %s" % existing["id"])
    fid = existing["id"]
    st, body = c.request("GET", "/locations/%s/customFields?model=contact" % loc)
    again = find_by_name(expect(st, body, (200,), "read custom fields back").get("customFields", []), FIELD_NAME)
    check_field(again, FIELD_NAME, loc)
    log("custom field read back: key %s" % again["fieldKey"])

    st, body = c.request("POST", "/contacts/upsert", contact_body(loc))
    contact = expect(st, body, (200, 201), "upsert test contact").get("contact") or {}
    cid = contact.get("id")
    if not cid:
        raise CheckFailed("upsert returned no contact id")
    if contact.get("email") != CONTACT_EMAIL:
        raise CheckFailed("upsert returned contact %r" % contact.get("email"))
    st, body = c.request("PUT", "/contacts/%s" % cid, contact_field_body(fid, FIELD_TEXT))
    expect(st, body, (200, 201), "set the field on the contact")
    st, body = c.request("GET", "/contacts/%s" % cid)
    got = expect(st, body, (200,), "read the contact back").get("contact") or {}
    check_contact_field(got, fid, FIELD_TEXT)
    log("contact %s carries %r in field %s" % (cid, FIELD_TEXT, fid))
    return {"fieldId": fid, "fieldKey": again["fieldKey"], "created": created, "contactId": cid}


def run_value(c, loc, log):
    st, body = c.request("GET", "/locations/%s/customValues" % loc)
    values = expect(st, body, (200,), "list custom values").get("customValues", [])
    existing = find_by_name(values, VALUE_NAME)
    if existing is None:
        st, body = c.request("POST", "/locations/%s/customValues" % loc, value_body(VALUE_NAME, VALUE_FIRST))
        existing = expect(st, body, (200, 201), "create custom value").get("customValue")
        created = True
        log("custom value created: %s" % existing["id"])
    else:
        created = False
        log("custom value already exists: %s" % existing["id"])
        if existing.get("value") != VALUE_FIRST:
            st, body = c.request("PUT", "/locations/%s/customValues/%s" % (loc, existing["id"]),
                                 value_body(VALUE_NAME, VALUE_FIRST))
            existing = expect(st, body, (200,), "reset custom value").get("customValue")
    vid = existing["id"]
    check_value(existing, VALUE_NAME, VALUE_FIRST, loc)
    st, body = c.request("GET", "/locations/%s/customValues" % loc)
    back = find_by_name(expect(st, body, (200,), "read custom values back").get("customValues", []), VALUE_NAME)
    check_value(back, VALUE_NAME, VALUE_FIRST, loc)
    log("custom value read back: %s = %r" % (back["fieldKey"], back["value"]))
    st, body = c.request("PUT", "/locations/%s/customValues/%s" % (loc, vid), value_body(VALUE_NAME, VALUE_SECOND))
    expect(st, body, (200,), "update custom value")
    st, body = c.request("GET", "/locations/%s/customValues" % loc)
    back2 = find_by_name(expect(st, body, (200,), "read custom values after update").get("customValues", []), VALUE_NAME)
    check_value(back2, VALUE_NAME, VALUE_SECOND, loc)
    log("custom value after update: %r" % back2["value"])
    return {"valueId": vid, "fieldKey": back2["fieldKey"], "created": created}


def run_calendar(c, loc, user_id, contact_id, log, now=None, tz=None):
    st, body = c.request("GET", "/calendars/?locationId=%s" % loc)
    cals = expect(st, body, (200,), "list calendars").get("calendars", [])
    existing = find_by_name(cals, CALENDAR_NAME)
    if existing:
        log("calendar already exists: %s" % existing["id"])
        created = False
    else:
        st, body = c.request("POST", "/calendars/", calendar_body(loc, user_id))
        existing = expect(st, body, (200, 201), "create calendar").get("calendar")
        created = True
        log("calendar created: %s" % existing["id"])
    cal_id = existing["id"]
    st, body = c.request("GET", "/calendars/%s" % cal_id)
    cal = expect(st, body, (200,), "read the calendar back").get("calendar")
    check_calendar(cal, CALENDAR_NAME, loc, user_id)
    log("calendar read back: slotDuration %s, open days %s" % (
        cal["slotDuration"], sorted(d for h in cal["openHours"] for d in h["daysOfTheWeek"])))

    # clear a leftover from an aborted run: only appointments on OUR calendar for OUR contact
    st, body = c.request("GET", "/contacts/%s/appointments" % contact_id)
    for ev in active_events(expect(st, body, (200,), "list contact appointments").get("events", []), cal_id):
        st, body = c.request("DELETE", "/calendars/events/%s" % ev["id"])
        expect(st, body, (200, 201), "delete a leftover appointment")
        log("deleted leftover appointment %s" % ev["id"])

    start = now or datetime.now(timezone.utc)
    start = start.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    st, body = c.request("GET", free_slots_path(cal_id, start, 7, tz))
    slots = expect(st, body, (200,), "read free slots")
    slot = first_weekday_slot(slots)
    log("free slots: %d in the next 7 days, booking %s" % (len(slot_times(slots)), slot))

    st, body = c.request("POST", "/calendars/events/appointments",
                         appointment_body(loc, cal_id, contact_id, user_id, slot))
    made = expect(st, body, (200, 201), "create appointment")
    event_id = made.get("id") or (made.get("event") or {}).get("id")
    if not event_id:
        raise CheckFailed("create appointment returned no id: %s" % json.dumps(made)[:200])
    log("appointment created: %s" % event_id)
    st, body = c.request("GET", "/calendars/events/appointments/%s" % event_id)
    # the docs name the key "event"; the sandbox answers with "appointment", so both are read
    got = expect(st, body, (200,), "read the appointment back")
    event = got.get("appointment") or got.get("event") or {}
    check_event(event, cal_id, contact_id, slot, loc)
    log("appointment read back: %s to %s" % (event.get("startTime"), event.get("endTime")))
    st, body = c.request("GET", "/contacts/%s/appointments" % contact_id)
    listed = [e for e in expect(st, body, (200,), "list contact appointments").get("events", [])
              if e.get("id") == event_id]
    if len(listed) != 1:
        raise CheckFailed("the contact's appointment list holds %d copies of the new event" % len(listed))
    log("contact appointment list holds it (status %s)" % listed[0].get("appointmentStatus", listed[0].get("status")))
    st, body = c.request("GET", free_slots_path(cal_id, start, 7, tz))
    after = expect(st, body, (200,), "read free slots after booking")
    if not slot_is_taken(after, slot):
        raise CheckFailed("the booked slot is still offered as free")
    log("the booked slot no longer appears as free")

    st, body = c.request("DELETE", "/calendars/events/%s" % event_id)
    expect(st, body, (200, 201), "delete the appointment")
    st, body = c.request("GET", "/contacts/%s/appointments" % contact_id)
    left = [e for e in expect(st, body, (200,), "list contact appointments after delete").get("events", [])
            if e.get("id") == event_id and str(e.get("appointmentStatus", e.get("status", ""))).lower() != "cancelled"]
    if left:
        raise CheckFailed("the appointment is still active after the delete")
    log("appointment %s deleted, the contact's list no longer shows it as active" % event_id)
    return {"calendarId": cal_id, "created": created, "eventId": event_id, "slot": slot}


def run_all(c, loc, user_id, log=print, now=None, tz=None):
    """Each part is tried on its own, so a missing scope on one does not hide the others. The
    calendar part needs the contact the field part makes; with no contact it is reported."""
    result = {}
    for name, fn in (("customField", lambda: run_field(c, loc, log)),
                     ("customValue", lambda: run_value(c, loc, log))):
        try:
            result[name] = fn()
        except ScopeBlocked as e:
            result[name] = {"blocked": str(e)}
            log("BLOCKED %s" % e)
    contact_id = (result.get("customField") or {}).get("contactId")
    if not contact_id:
        try:
            st, body = c.request("POST", "/contacts/upsert", contact_body(loc))
            contact_id = (expect(st, body, (200, 201), "upsert test contact").get("contact") or {}).get("id")
        except ScopeBlocked as e:
            result["calendar"] = {"blocked": str(e)}
            log("BLOCKED %s" % e)
            return result
    try:
        result["calendar"] = run_calendar(c, loc, user_id, contact_id, log, now, tz)
    except ScopeBlocked as e:
        result["calendar"] = {"blocked": str(e)}
        log("BLOCKED %s" % e)
    return result
