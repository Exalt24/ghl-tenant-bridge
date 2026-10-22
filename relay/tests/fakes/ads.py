"""Fakes for the two ad spend sources, written from saved vendor pages (retrieved 2026-10-22, see contracts/meta/SOURCE.md
and contracts/windsor/SOURCE.md).

FakeInsights: GET /{version}/act_<id>/insights, the Meta Marketing API Insights edge.
  Documented: `spend` and the other metrics are numeric STRINGS; `account_currency` is its own field; `level` and
  `time_increment` (an integer 1 to 90 gives one row per N days) and `time_range` {"since","until"}; a response holds
  `data` and `paging` and `paging.next` is absent on the last page; a page can be EMPTY and still carry `next`, so a
  client must stop only when `next` is gone; error 190 is an expired token, the throttle codes (4, 17, 613, 80000
  series) come with an X-Business-Use-Case-Usage header whose `estimated_time_to_regain_access` is in MINUTES, and error
  code 100 with subcode 1487534 means the request asked for too much data per call, to be cut into smaller date ranges.
  ASSUMED (not in the evidence): that `spend` is in `account_currency`, and the HTTP status that goes with each code.

FakeWindsor: GET /facebook, the Windsor.ai connectors API.
  Documented: the key is the `api_key` QUERY parameter and nothing else (a header gets {"error": "Not authorized"});
  `fields`, `date_from` and `date_to`; the reply is {"data": [...]} with `spend` a JSON NUMBER; 429 above 600 requests a
  minute; the error body {"error": {"code", "message"}}.

Stricter than the vendors on purpose, each rule named in `self.rules` so a test and a mutant can switch it off:
  insights:  token, level, time_increment, time_range, fields, limit
  windsor:   api_key (query only), fields, dates
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any, Callable
from urllib.parse import urlencode

from werkzeug.wrappers import Request, Response

from ._base import FakeVendor

VERSION = "v26.0"
ACCOUNT = "act_fake01"  # made up, deliberately not a real shaped id
TOKEN = "fake-insights-token-0123456789"
WINDSOR_KEY = "fake-windsor-key-0123456789abcdef"
INSIGHT_FIELDS_NEEDED = {"campaign_id", "spend", "account_currency"}
WINDSOR_FIELDS_NEEDED = {"date", "campaign_id", "spend", "account_currency"}
MAX_LIMIT = 5000


def iso(d: date) -> str:
    return d.isoformat()


def day_rows(
    campaigns: dict[str, str], start: date, days: int, *, currency: str = "USD", spend: Callable[[int, int], str] | None = None
) -> list[dict[str, Any]]:
    """One row per campaign per day, as the Insights edge returns them with level=campaign and time_increment=1."""
    make = spend or (lambda c, d: f"{10 + c + d}.{(c * 7 + d) % 100:02d}")
    rows = []
    for d in range(days):
        day = start + timedelta(days=d)
        for c, (cid, name) in enumerate(sorted(campaigns.items())):
            rows.append(
                {
                    "campaign_id": cid, "campaign_name": name, "date_start": iso(day), "date_stop": iso(day),
                    "spend": make(c, d), "impressions": str(1000 + c * 10 + d), "clicks": str(10 + c + d % 5), "account_currency": currency,
                }
            )
    return rows


# Modes (a script entry or the default)
#   ok              the page the query asks for
#   empty_page      an EMPTY page that still carries paging.next, pointing at the page it should have returned
#   throttle        400 code 4 with X-Business-Use-Case-Usage (`regain_minutes`, header name in `header_name`)
#   throttle_80004  400 code 80004, with the header
#   no_header       400 code 4 with no usage header at all
#   token_expired   400 code 190
#   server_error    500 with a Graph error object (code 2)
#   bad_request     400 code 100 with no subcode
#   hang            holds the request until released


class FakeInsights(FakeVendor):
    def __init__(self, token: str = TOKEN, account: str = ACCOUNT) -> None:
        super().__init__()
        self.rules = {"token", "level", "time_increment", "time_range", "fields", "limit"}
        self.token = token
        self.account = account
        self.rows: list[dict[str, Any]] = []
        self.max_days: int | None = None  # a range longer than this answers code 100 subcode 1487534
        self.regain_minutes = 5
        self.header_name = "X-Business-Use-Case-Usage"
        self.spend_type = "string"  # "number" makes spend a JSON number, to prove the relay refuses it
        self.next_base: str | None = None  # a paging.next that points at another host, to prove the relay will not follow it
        self.queries: list[dict[str, str]] = []

    def reset(self) -> None:
        super().reset()
        self.rows = []
        self.max_days = None
        self.regain_minutes = 5
        self.header_name = "X-Business-Use-Case-Usage"
        self.spend_type = "string"
        self.next_base = None
        self.queries = []

    @property
    def url(self) -> str:
        """The base the relay is configured with (the Graph version is part of it)."""
        return f"{self.base}/{VERSION}"

    def _error(self, status: int, code: int, message: str, *, subcode: int | None = None, headers: dict[str, str] | None = None) -> Response:
        err: dict[str, Any] = {"message": message, "type": "OAuthException" if code in (190, 102) else "GraphMethodException", "code": code, "fbtrace_id": "FAKETRACE"}
        if subcode is not None:
            err["error_subcode"] = subcode
        return Response(json.dumps({"error": err}), status=status, mimetype="application/json", headers=headers or {})

    def _validate(self, args: dict[str, str]) -> tuple[str | None, date | None, date | None, int]:
        """(problem, since, until, limit). A problem is a readable text."""
        limit = 25
        if "limit" in args:
            if not args["limit"].isdigit() or not (1 <= int(args["limit"]) <= MAX_LIMIT):
                if "limit" in self.rules:
                    return "limit must be an integer from 1 to 5000", None, None, limit
            else:
                limit = int(args["limit"])
        if "level" in self.rules and args.get("level") != "campaign":
            return "level must be campaign", None, None, limit
        if "time_increment" in self.rules and args.get("time_increment") != "1":
            return "time_increment must be 1", None, None, limit
        if "fields" in self.rules and not INSIGHT_FIELDS_NEEDED <= set(args.get("fields", "").split(",")):
            return "fields must include campaign_id, spend and account_currency", None, None, limit
        since = until = None
        if "time_range" in self.rules:
            try:
                tr = json.loads(args.get("time_range", ""))
                since, until = date.fromisoformat(tr["since"]), date.fromisoformat(tr["until"])
            except (ValueError, KeyError, TypeError):
                return "time_range must be {\"since\": YYYY-MM-DD, \"until\": YYYY-MM-DD}", None, None, limit
            if since > until:
                return "time_range since is after until", None, None, limit
        return None, since, until, limit

    def handle(self, request: Request) -> Response:
        mode = self.next_mode()
        args = {k: v for k, v in request.args.items()}
        self.queries.append(args)
        expected = f"/{VERSION}/{self.account}/insights"
        if request.method != "GET" or request.path != expected:
            self.record(request, None, mode, False)
            return self._error(404, 100, "Unsupported get request")
        if "token" in self.rules and args.get("access_token") != self.token:
            self.record(request, None, mode, False)
            return self._error(400, 190, "Invalid OAuth access token")
        if mode == "hang":
            self.record(request, None, mode, False)
            self.hang()
            return Response("{}", status=200, mimetype="application/json")
        usage = {self.account: [{"type": "ads_insights", "call_count": 100, "total_cputime": 25, "total_time": 25, "estimated_time_to_regain_access": self.regain_minutes, "ads_api_access_tier": "development_access"}]}
        if mode in ("throttle", "throttle_80004", "no_header"):
            self.record(request, None, mode, False)
            headers = {} if mode == "no_header" else {self.header_name: json.dumps(usage)}
            return self._error(400, 80004 if mode == "throttle_80004" else 4, "Application request limit reached", headers=headers)
        if mode == "token_expired":
            self.record(request, None, mode, False)
            return self._error(400, 190, "Error validating access token: Session has expired")
        if mode == "server_error":
            self.record(request, None, mode, False)
            return self._error(500, 2, "Service temporarily unavailable")
        if mode == "bad_request":
            self.record(request, None, mode, False)
            return self._error(400, 100, "Invalid parameter")
        problem, since, until, limit = self._validate(args)
        if problem:
            self.record(request, None, mode, False)
            return self._error(400, 100, problem)
        if self.max_days is not None and since is not None and until is not None and (until - since).days + 1 > self.max_days:
            self.record(request, None, mode, False)
            return self._error(400, 100, "Please reduce the amount of data you're asking for, then retry your request", subcode=1487534)
        matching = [r for r in self.rows if since is None or until is None or since <= date.fromisoformat(r["date_start"]) <= until]
        after = args.get("after", "")
        offset = int(after[1:]) if after.startswith("c") and after[1:].isdigit() else 0
        page = matching[offset : offset + limit]
        paging: dict[str, Any] = {"cursors": {"before": "MAZDZD", "after": "MAZDZD"}}
        next_offset = offset + limit
        if mode == "empty_page":
            self.record(request, None, mode, False)
            page = []
            next_offset = offset  # the same page again: the empty one carried a next link and the real page follows it
        if next_offset < len(matching) or mode == "empty_page":
            nargs = {k: v for k, v in args.items() if k != "after"}
            nargs["after"] = f"c{next_offset}"
            if mode == "empty_page":
                # A real cursor differs from the last one. Without this the empty page's next would be the very URL just
                # asked for, which a client is right to refuse as a loop.
                nargs["_e"] = str(int(args.get("_e", "0")) + 1)
            paging["next"] = f"{self.next_base or self.base}/{VERSION}/{self.account}/insights?{urlencode(nargs)}"
        body_rows = [dict(r) for r in page]
        if self.spend_type == "number":
            for r in body_rows:
                r["spend"] = float(r["spend"])
        if mode != "empty_page":
            self.record(request, None, mode, True)
        return Response(json.dumps({"data": body_rows, "paging": paging}), status=200, mimetype="application/json")


# Windsor modes
#   ok             the rows for the dates asked
#   rate_limit     429 with {"error": {"code": "rate_limit_exceeded"}} and Retry-After when `retry_after` is set
#   unauthorized   401 {"error": {"code": "authentication_error"}}
#   server_error   500
#   bad_request    400 invalid_request
#   echo_400       400 whose body echoes the full request URL, api_key included
#   echo_url       500 whose BODY echoes the full request URL, api_key included (a vendor error page can do that)
#   hang


class FakeWindsor(FakeVendor):
    def __init__(self, key: str = WINDSOR_KEY) -> None:
        super().__init__()
        self.rules = {"api_key", "fields", "dates"}
        self.key = key
        self.rows: list[dict[str, Any]] = []
        self.retry_after: int | None = 2
        self.spend_type = "number"  # "string" makes spend text, to prove the relay refuses it
        self.queries: list[dict[str, str]] = []

    def reset(self) -> None:
        super().reset()
        self.rows = []
        self.retry_after = 2
        self.spend_type = "number"
        self.queries = []

    @property
    def url(self) -> str:
        return self.base

    @staticmethod
    def _error(status: int, code: str, message: str, headers: dict[str, str] | None = None) -> Response:
        return Response(json.dumps({"error": {"code": code, "message": message}}), status=status, mimetype="application/json", headers=headers or {})

    def handle(self, request: Request) -> Response:
        mode = self.next_mode()
        args = {k: v for k, v in request.args.items()}
        self.queries.append(args)
        if request.method != "GET" or request.path != "/facebook":
            self.record(request, None, mode, False)
            return self._error(404, "not_found", "The requested connector or resource doesn't exist")
        if "api_key" in self.rules and args.get("api_key") != self.key:
            # Documented: header authentication is not supported and answers {"error":"Not authorized"}.
            self.record(request, None, mode, False)
            return Response(json.dumps({"error": "Not authorized"}), status=401, mimetype="application/json")
        if mode == "hang":
            self.record(request, None, mode, False)
            self.hang()
            return Response("{}", status=200, mimetype="application/json")
        if mode == "unauthorized":
            self.record(request, None, mode, False)
            return self._error(401, "authentication_error", "Invalid API key provided")
        if mode == "rate_limit":
            self.record(request, None, mode, False)
            headers = {"Retry-After": str(self.retry_after)} if self.retry_after is not None else {}
            return self._error(429, "rate_limit_exceeded", "Too many requests in a given amount of time", headers)
        if mode == "server_error":
            self.record(request, None, mode, False)
            return self._error(500, "server_error", "An unexpected server error occurred")
        if mode == "echo_400":
            self.record(request, None, mode, False)
            return Response(f"bad request for {request.url}", status=400)
        if mode == "bad_request":
            self.record(request, None, mode, False)
            return self._error(400, "invalid_request", "The request is malformed or missing required parameters")
        if mode == "echo_url":
            self.record(request, None, mode, False)
            return Response(f"upstream failed for {request.url}", status=500)
        if "fields" in self.rules and not WINDSOR_FIELDS_NEEDED <= set(args.get("fields", "").split(",")):
            self.record(request, None, mode, False)
            return self._error(400, "invalid_request", "fields must include date, campaign_id, spend and account_currency")
        since = until = None
        if "dates" in self.rules:
            try:
                since, until = date.fromisoformat(args["date_from"]), date.fromisoformat(args["date_to"])
            except (KeyError, ValueError):
                self.record(request, None, mode, False)
                return self._error(400, "invalid_request", "date_from and date_to must be YYYY-MM-DD")
        out = []
        for r in self.rows:
            d = date.fromisoformat(r["date"])
            if since is not None and until is not None and not (since <= d <= until):
                continue
            row = dict(r)
            row["spend"] = str(r["spend"]) if self.spend_type == "string" else r["spend"]
            out.append(row)
        self.record(request, None, mode, True)
        return Response(json.dumps({"data": out}), status=200, mimetype="application/json")


def windsor_rows(campaigns: dict[str, str], start: date, days: int, *, currency: str = "USD") -> list[dict[str, Any]]:
    rows = []
    for d in range(days):
        day = start + timedelta(days=d)
        for c, (cid, name) in enumerate(sorted(campaigns.items())):
            rows.append(
                {"date": iso(day), "campaign_id": cid, "campaign": name, "spend": round(12.5 + c * 3.25 + d * 0.5, 2), "impressions": 1000 + c * 10 + d, "clicks": 10 + c + d % 5, "account_currency": currency}
            )
    return rows

