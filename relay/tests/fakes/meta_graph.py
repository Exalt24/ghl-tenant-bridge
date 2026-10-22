"""Fake Meta Conversions API (Graph) endpoint, written from the saved Meta pages (retrieved 2026-10-22, see
contracts/meta/SOURCE.md).

Documented: POST /{version}/{pixel id}/events?access_token=..., body {"data": [events], "test_event_code": "..."};
required event_name, event_time (Unix seconds, an integer, at most 7 days old or the WHOLE request is rejected),
user_data, action_source; website events need event_source_url and client_user_agent; the customer fields em, ph, fn, ln,
ct, st, zp and country must be SHA-256 hashed while client_ip_address, client_user_agent, fbc and fbp must not be; the
same event_id and event_name is kept once; the Graph error object {"error": {"message", "type", "code", "error_subcode",
"fbtrace_id"}}; the X-Business-Use-Case header with estimated_time_to_regain_access in minutes.

ASSUMED (not in the evidence, tagged in SOURCE.md): the body of a success, `{"events_received": n, "messages": [],
"fbtrace_id": ...}`, and the HTTP status that goes with each error code. A 2xx is the only documented success signal.

Stricter than Meta on purpose, each with a rule name in `self.rules` so a test and a mutant can switch it off:
  event_id         a missing event_id is refused (Meta marks it optional but recommended; the relay always sends one)
  hashed_em_ph     an unhashed em or ph is refused (the page: "our systems are designed to not accept ... unhashed")
  hashed_other     an unhashed fn, ln, ct, st, zp or country is refused
  hashed_fbc       a hashed fbc is refused (fbc must not be hashed)
  hashed_never     a hashed client_ip_address, client_user_agent or fbp is refused
  event_time_int   a non integer event_time is refused (seconds as an integer, never a float or milliseconds)
  event_time_age   an event_time older than 7 days is refused for the whole request
  action_source    a missing or unknown action_source is refused
  website_ua       a website event without client_user_agent or event_source_url is refused
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from werkzeug.wrappers import Request, Response

from ._base import FakeVendor

VERSION = "v26.0"
PIXEL_ID = "fake-pixel-01"  # made up, and deliberately not shaped like a real 16 digit id
TOKEN = "fake-meta-token-0123456789abcdef"
HEX64 = re.compile(r"[0-9a-f]{64}")
ACTION_SOURCES = {"email", "website", "app", "phone_call", "chat", "physical_store", "system_generated", "business_messaging", "other"}
DEDUPE_WINDOW_S = 48 * 3600.0
SEVEN_DAYS_S = 7 * 86400.0
HASHED_OTHER = ("fn", "ln", "ct", "st", "zp", "country")

# Modes
#   ok             2xx with the plausible body
#   throttle       400 code 4 with an X-Business-Use-Case header (`regain_minutes`)
#   throttle_80004 400 code 80004
#   token_expired  400 code 190
#   session        400 code 102
#   temp_400       400 code 2 (a temporary code that arrives as a client error status)
#   limit_341      400 code 341
#   temporary      500 code 1
#   bad_param      400 code 100
#   server_error   500, nothing recorded as an effect
#   act_then_fail  the event IS accepted and the answer is a 500
#   hang           holds the request until released
#   no_header      like throttle but without the usage header


class FakeMeta(FakeVendor):
    def __init__(self, token: str = TOKEN, pixel: str = PIXEL_ID, clock: Callable[[], float] = time.time) -> None:
        super().__init__()
        self.rules = {"event_id", "hashed_em_ph", "hashed_other", "hashed_fbc", "hashed_never", "event_time_int", "event_time_age", "action_source", "website_ua"}
        self.token = token
        self.pixel = pixel
        self.clock = clock
        self.regain_minutes = 5
        self.header_name = "X-Business-Use-Case-Usage"
        self.seen: dict[tuple[str, str], float] = {}
        self.accepted_requests = 0
        self.events: list[dict[str, Any]] = []  # the unique events kept, in order
        self.test_codes: list[str | None] = []

    def reset(self) -> None:
        super().reset()
        self.regain_minutes = 5
        self.header_name = "X-Business-Use-Case-Usage"
        self.seen.clear()
        self.accepted_requests = 0
        self.events.clear()
        self.test_codes.clear()

    @property
    def url(self) -> str:
        """The base the relay is configured with (the Graph version is part of it)."""
        return f"{self.base}/{VERSION}"

    # ---- validation ----------------------------------------------------------------------------------------------------
    def _values(self, v: Any) -> list[Any]:
        return v if isinstance(v, list) else [v]

    def validate_event(self, ev: Any) -> str | None:
        """A reason text, or None when the event is acceptable."""
        if not isinstance(ev, dict):
            return "event is not an object"
        for required in ("event_name", "event_time", "user_data", "action_source"):
            if required not in ev:
                if required == "action_source" and "action_source" not in self.rules:
                    continue
                return f"missing {required}"
        if not isinstance(ev["event_name"], str) or not ev["event_name"]:
            return "bad event_name"
        if "event_id" in self.rules and not (isinstance(ev.get("event_id"), str) and ev["event_id"]):
            return "missing event_id"
        t = ev["event_time"]
        if "event_time_int" in self.rules and (isinstance(t, bool) or not isinstance(t, int)):
            return "event_time must be an integer number of seconds"
        if "event_time_age" in self.rules and isinstance(t, (int, float)) and self.clock() - t > SEVEN_DAYS_S:
            return "event_time is more than 7 days old"
        src = ev.get("action_source")
        if "action_source" in self.rules and src not in ACTION_SOURCES:
            return "bad action_source"
        ud = ev["user_data"]
        if not isinstance(ud, dict) or not ud:
            return "user_data is empty"
        if "hashed_em_ph" in self.rules:
            for key in ("em", "ph"):
                for val in self._values(ud.get(key)) if key in ud else []:
                    if not (isinstance(val, str) and HEX64.fullmatch(val)):
                        return f"{key} must be SHA-256 hashed"
        if "hashed_other" in self.rules:
            for key in HASHED_OTHER:
                for val in self._values(ud.get(key)) if key in ud else []:
                    if not (isinstance(val, str) and HEX64.fullmatch(val)):
                        return f"{key} must be SHA-256 hashed"
        if "hashed_fbc" in self.rules and isinstance(ud.get("fbc"), str) and HEX64.fullmatch(ud["fbc"]):
            return "fbc must not be hashed"
        if "hashed_never" in self.rules:
            for key in ("client_ip_address", "client_user_agent", "fbp"):
                if isinstance(ud.get(key), str) and HEX64.fullmatch(ud[key]):
                    return f"{key} must not be hashed"
        if "website_ua" in self.rules and src == "website":
            if not ud.get("client_user_agent"):
                return "client_user_agent is required for website events"
            if not ev.get("event_source_url"):
                return "event_source_url is required for website events"
        if ev["event_name"] == "Purchase":
            cd = ev.get("custom_data") or {}
            if not isinstance(cd, dict) or "currency" not in cd or "value" not in cd:
                return "Purchase needs currency and value"
        return None

    # ---- the endpoint ---------------------------------------------------------------------------------------------------
    def _error(self, status: int, code: int, message: str, *, headers: dict[str, str] | None = None, subcode: int | None = None) -> Response:
        err: dict[str, Any] = {"message": message, "type": "OAuthException" if code in (190, 102) else "GraphMethodException", "code": code, "fbtrace_id": "FAKETRACE"}
        if subcode is not None:
            err["error_subcode"] = subcode
        return Response(json.dumps({"error": err}), status=status, mimetype="application/json", headers=headers or {})

    def handle(self, request: Request) -> Response:
        mode = self.next_mode()
        parsed = self.parse_json(request)
        expected = f"/{VERSION}/{self.pixel}/events"
        if request.method != "POST" or request.path != expected:
            self.record(request, parsed, mode, False)
            return self._error(404, 100, "Unsupported request")
        if request.args.get("access_token") != self.token:
            self.record(request, parsed, mode, False)
            return self._error(400, 190, "Invalid OAuth access token")
        if mode == "hang":
            self.record(request, parsed, mode, False)
            self.hang()
            return Response("{}", status=200, mimetype="application/json")
        if mode in ("throttle", "no_header"):
            self.record(request, parsed, mode, False)
            usage = {"act_1": [{"type": "ads_management", "call_count": 100, "total_cputime": 25, "total_time": 25, "estimated_time_to_regain_access": self.regain_minutes}]}
            headers = {} if mode == "no_header" else {self.header_name: json.dumps(usage)}
            return self._error(400, 4, "Application request limit reached", headers=headers)
        simple = {"throttle_80004": (400, 80004, "Too many calls from this ad account"), "token_expired": (400, 190, "Error validating access token: Session has expired"),
                  "session": (400, 102, "Session key invalid"), "temporary": (500, 1, "An unknown error occurred"), "bad_param": (400, 100, "Invalid parameter"),
                  "temp_400": (400, 2, "Service temporarily unavailable"), "plain_502": (502, None, ""), "limit_341": (400, 341, "Application limit reached"),
                  "server_error": (500, 2, "Service temporarily unavailable")}
        if mode in simple:
            self.record(request, parsed, mode, False)
            status, code, message = simple[mode]
            if code is None:  # a gateway answer with no Graph error object at all
                return Response("Bad gateway", status=status)
            return self._error(status, code, message)
        if not isinstance(parsed, dict) or not isinstance(parsed.get("data"), list) or not parsed["data"]:
            self.record(request, parsed, mode, False)
            return self._error(400, 100, "data must be a non-empty list")
        events = parsed["data"]
        for ev in events:
            problem = self.validate_event(ev)
            if problem:  # one invalid event rejects the request; a too old event_time rejects it whole, by Meta's page
                self.record(request, parsed, mode, False)
                return self._error(400, 100, problem)
        new = 0
        with self.lock:
            for ev in events:
                key = (str(ev.get("event_id")), ev["event_name"])
                kept = self.seen.get(key)
                if kept is not None and self.clock() - kept < DEDUPE_WINDOW_S:
                    continue  # documented: the same event_id and event_name is kept once
                self.seen[key] = self.clock()
                self.events.append(ev)
                new += 1
            self.accepted_requests += 1
            self.test_codes.append(parsed.get("test_event_code"))
            self.requests.append({"method": request.method, "path": request.path, "headers": {k.lower(): v for k, v in request.headers.items()},
                                  "json": parsed, "mode": mode, "accepted": True, "new_events": new, "query": dict(request.args)})
            self.effects += new
        if mode == "act_then_fail":
            return self._error(500, 2, "Service temporarily unavailable")
        return Response(json.dumps({"events_received": len(events), "messages": [], "fbtrace_id": "FAKETRACE"}), status=200, mimetype="application/json")
