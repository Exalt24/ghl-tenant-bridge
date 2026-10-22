"""Meta Conversions API sink. Contract: contracts/meta/SOURCE.md (pages retrieved 2026-10-22).

One event per request: POST {base}/{pixel id}/events?access_token=... with {"data": [event], "test_event_code": ...}.

Delivery here is safe to repeat: the page says a server to server event with the same `event_id` and `event_name` is kept
once ("That makes batch retries safe"), so a 5xx or a timeout is a plain RETRY and never AMBIGUOUS. The page gives a 48
hour window only for the Pixel and server pairing; for two server copies it gives no window (the README says so).

What the relay will NOT send, and why each one is visible instead of silent:
  SKIPPED  an event type with no mapping, an opportunity that is not won, a Purchase with no currency or no value from
           config, and (by default) a lead with no click id. Terminal, with the reason stored.
  DEAD     an event older than the 7 day rule at SEND time (Meta rejects the whole request), and a lead with no field
           Meta could match on.
  PAUSE    codes 190 and 102 (a token Meta no longer accepts): the lead stays pending, the sink pauses, an alert goes out.

`test_event_code` is on unless the tenant sets live AND the process has RELAY_LIVE=1. It is not a sandbox: Meta says events
sent with it flow into Events Manager and are used for targeting and measurement.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any, Callable
from urllib.parse import quote

from .. import attribution
from ..http import HttpConnectError, HttpFailure, HttpTimeout
from .base import Event, HttpLike, Outcome, Sink, SinkContext

DEFAULT_EVENT_MAP = {
    "ContactCreate": "Lead",
    "Lead": "Lead",
    "AppointmentCreate": "Schedule",
    "OpportunityStatusUpdate": "Purchase",  # ASSUMED: "won" is the status of this event, the pages show only open
}
SEVEN_DAYS_S = 7 * 86400.0
MARGIN_S = 3600.0
MAX_EVENT_AGE_S = SEVEN_DAYS_S - MARGIN_S  # judged at SEND time, with an hour of margin for the call itself
MAX_THROTTLE_WAIT_S = 3600.0
# ASSUMED table (the CAPI handling-errors page was a 404 in the evidence, the generic Graph error table is the source).
RETRY_CODES = frozenset({1, 2, 4, 17, 341, 613})
THROTTLE_CODES = frozenset({4, 17, 341, 613})
PAUSE_CODES = frozenset({190, 102})
BUC_HEADERS = ("x-business-use-case-usage", "x-business-use-case")
MATCH_KEYS = ("em", "ph", "fbc", "fbp")  # at least one must be present or Meta has nothing to match on
TEST_CODE_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")
CURRENCY_RE = re.compile(r"[A-Z]{3}")
OPTION_KEYS = {
    "event_map", "capi_require_click_id", "click_id_sources", "default_country_code", "currency", "live", "test_event_code",
}


def in_80000_series(code: int) -> bool:
    """The page names 80000, 80003, 80004 and 80014 as ad account throttles. The whole 80000 series is treated alike
    (ASSUMED beyond those four)."""
    return 80000 <= code < 81000


def is_retry_code(code: int) -> bool:
    return code in RETRY_CODES or in_80000_series(code)


def is_throttle_code(code: int) -> bool:
    return code in THROTTLE_CODES or in_80000_series(code)


def regain_wait_s(headers: dict[str, str]) -> float | None:
    """Seconds from `estimated_time_to_regain_access` (minutes, per the page) in X-Business-Use-Case(-Usage). The header
    is optional and its name is read case insensitively (the Http seam lowercases every name). The value is a JSON object
    of account id to a list of usage objects; the longest wait among them wins."""
    for name in BUC_HEADERS:
        raw = headers.get(name)
        if not raw:
            continue
        try:
            doc = json.loads(raw)
        except ValueError:
            continue
        waits: list[float] = []
        entries = doc.values() if isinstance(doc, dict) else []
        for usage in entries:
            for item in usage if isinstance(usage, list) else []:
                minutes = item.get("estimated_time_to_regain_access") if isinstance(item, dict) else None
                if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and minutes > 0:
                    waits.append(float(minutes) * 60.0)
        if waits:
            return min(max(waits), MAX_THROTTLE_WAIT_S)
    return None


def error_code(text: str) -> int | None:
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    err = doc.get("error") if isinstance(doc, dict) else None
    code = err.get("code") if isinstance(err, dict) else None
    return code if isinstance(code, int) and not isinstance(code, bool) else None


def _event_id_part(event: Event, name: str) -> str | None:
    """The id of the thing the event is about: the opportunity for a Purchase, the appointment for a Schedule, the contact
    for everything else."""
    payload = event.payload
    kind = event.lead.get("event_type") or ""
    if name == "Purchase" and kind.startswith("Opportunity"):
        value = payload.get("id")
    elif kind.startswith("Appointment"):
        inner = payload.get("appointment")
        value = inner.get("id") if isinstance(inner, dict) else None
    else:
        value = event.lead.get("contact_id")
    return value if isinstance(value, str) and value else None


def make_event_id(event: Event, name: str) -> str:
    """A page side pixel eventID (carried as fbEventId or event_id) wins, because only that value can match the
    browser's copy. Otherwise sha256(tenant|entity id|event name): the same submission arriving by the app webhook and by
    the workflow webhook gets one id, so Meta keeps one copy, and a re-won deal does not count twice (amendments 12, 13)."""
    given = (event.lead.get("attribution") or {}).get("fb_event_id")
    if isinstance(given, str) and given:
        return given
    part = _event_id_part(event, name)
    if part is None:
        return event.event_id
    return attribution.sha256_hex(f"{event.tenant}|{part}|{name}")


def event_time_of(event: Event, now: float) -> float:
    """When the thing happened. A create event uses dateAdded; a status change uses dateUpdated or the envelope's
    `timestamp`; the fallback is when the relay stored the event (amendment 12)."""
    lead = event.lead
    kind = lead.get("event_type") or ""
    if kind.endswith("Create") or kind == "Lead":
        candidates = [lead.get("date_added")]
    else:
        candidates = [lead.get("date_updated"), event.payload.get("timestamp")]
    for c in candidates:
        t = attribution.parse_time(c)
        if t is not None:
            return t
    return event.received_epoch if event.received_epoch is not None else now


class CapiSink(Sink):
    name = "capi"
    default_timeout_s = 5.0  # Meta recommends 1.5 s; 5 s is the relay's own default and is configurable per sink
    default_min_interval_s = 0.1
    default_max_age_s = 6 * 86400.0  # leaves the 7 day rule room to judge before the age limit kills the row
    required_env = ("pixel_id",)
    needs_apikey = True

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock

    # ---- config ---------------------------------------------------------------------------------------------------------
    @classmethod
    def check_options(cls, options: dict[str, Any], env_values: dict[str, str], live: bool) -> dict[str, Any]:
        unknown = sorted(set(options) - OPTION_KEYS)
        if unknown:
            raise ValueError(f"unknown option `{unknown[0]}`")
        event_map = dict(DEFAULT_EVENT_MAP)
        custom = options.get("event_map", {})
        if not isinstance(custom, dict) or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in custom.items()):
            raise ValueError("event_map must map an event type to an event name")
        event_map.update(custom)
        require = options.get("capi_require_click_id", True)
        if not isinstance(require, bool):
            raise ValueError("capi_require_click_id must be true or false")
        sources = options.get("click_id_sources", [])
        if not isinstance(sources, list) or not all(isinstance(s, str) and s for s in sources):
            raise ValueError("click_id_sources must be a list of lead source names")
        cc = options.get("default_country_code")
        if cc is not None and not (isinstance(cc, str) and re.fullmatch(r"[1-9][0-9]{0,2}", cc)):
            raise ValueError("default_country_code must be 1 to 3 digits with no plus and no leading zero")
        currency = options.get("currency")
        if currency is not None and not (isinstance(currency, str) and CURRENCY_RE.fullmatch(currency)):
            raise ValueError("currency must be a 3 letter upper case ISO code")
        wants_live = options.get("live", False)
        if not isinstance(wants_live, bool):
            raise ValueError("live must be true or false")
        if wants_live and not live:
            raise ValueError("live is true but RELAY_LIVE is not 1")
        code = options.get("test_event_code")
        if code is not None and not (isinstance(code, str) and TEST_CODE_RE.fullmatch(code)):
            raise ValueError("test_event_code must be letters, digits, - or _")
        if not wants_live and not code:
            raise ValueError("test_event_code is required unless live is true (the default is test mode)")
        return {
            "event_map": event_map,
            "require_click_id": require,
            "click_id_sources": list(sources),
            "default_country_code": cc,
            "currency": currency,
            "send_live": wants_live and live,
            "test_event_code": code,
        }

    # ---- building the event ---------------------------------------------------------------------------------------------
    def _decide(self, event: Event, ctx: SinkContext) -> tuple[dict[str, Any] | None, Outcome | None]:
        """(event dict, None) to send it, or (None, the outcome that explains why not)."""
        extra = ctx.extra
        lead = event.lead
        kind = lead.get("event_type") or ""
        name = extra["event_map"].get(kind)
        if not name:
            return None, Outcome.skipped(f"event type {kind or 'none'} is not mapped")
        if name == "Purchase":
            if kind.startswith("Opportunity") and (lead.get("status") or "").lower() != "won":
                return None, Outcome.skipped("opportunity is not won")
            if not extra.get("currency"):
                return None, Outcome.skipped("no currency")
            if lead.get("monetary_value") is None:
                return None, Outcome.skipped("no value")
        if extra["require_click_id"] and not attribution.has_click_id(lead, extra["click_id_sources"]):
            return None, Outcome.skipped("no click id (fbc, fbp, fbclid or an allowlisted source)")

        now = self.clock()
        when = event_time_of(event, now)
        if now - when > MAX_EVENT_AGE_S:
            return None, Outcome.dead("older than 7 days")
        when = min(when, now)  # ASSUMED: Meta's pages give no rule for the future, so never send one

        user_data = {
            **attribution.hashed_user_data(lead, extra["default_country_code"]),
            **attribution.plain_user_data(lead, event.received_epoch, now),
        }
        if not any(k in user_data for k in MATCH_KEYS):
            return None, Outcome.dead("no matchable user data (email, phone, fbc or fbp)")

        attr = lead.get("attribution") or {}
        url = attribution.sanitise_url(attr.get("page_url"))
        data: dict[str, Any] = {
            "event_name": name,
            "event_time": int(when),
            "event_id": make_event_id(event, name),
            # Honest about where it happened: only a lead with both a browser user agent and a page URL is a website
            # event, and Meta requires client_user_agent (and the URL) for those.
            "action_source": "website" if ("client_user_agent" in user_data and url) else "system_generated",
            "user_data": user_data,
        }
        if data["action_source"] == "website":
            data["event_source_url"] = url
        custom = attribution.custom_data_utms(lead)
        if name == "Purchase":
            custom["currency"] = extra["currency"]
            custom["value"] = lead["monetary_value"]
        if custom:
            data["custom_data"] = custom
        return data, None

    def build_request(self, event: Event, ctx: SinkContext) -> tuple[bytes, str] | Outcome:
        data, why = self._decide(event, ctx)
        if why is not None:
            return why
        body: dict[str, Any] = {"data": [data]}
        if not ctx.extra["send_live"]:
            body["test_event_code"] = ctx.extra["test_event_code"]
        url = f"{ctx.url.rstrip('/')}/{quote(ctx.extra['pixel_id'], safe='')}/events?access_token={quote(ctx.apikey or '', safe='')}"
        return json.dumps(body, ensure_ascii=False).encode("utf-8"), url

    # ---- sending --------------------------------------------------------------------------------------------------------
    def send(self, event: Event, http: HttpLike, ctx: SinkContext) -> Outcome:
        built = self.build_request(event, ctx)
        if isinstance(built, Outcome):
            return built
        body, url = built
        try:
            res = http.request("POST", url, headers={"Content-Type": "application/json"}, body=body, timeout=ctx.timeout_s)
        except HttpConnectError as exc:
            return Outcome.retry(reason=exc.kind, breaker_failure=True)
        except (HttpTimeout, HttpFailure) as exc:
            # Safe to repeat: the same event_id and event_name is kept once, so this is a RETRY, not AMBIGUOUS.
            return Outcome.retry(reason=exc.kind, breaker_failure=True)

        status = res.status
        if 200 <= status < 300:
            return Outcome.done(status=status)
        code = error_code(res.text) if status >= 400 else None
        label = f"http {status}" + (f" code {code}" if code is not None else "")
        if code in PAUSE_CODES:
            return Outcome.pause(f"token rejected by Meta ({label})", status=status)
        if code is not None and is_throttle_code(code) or status == 429:
            return Outcome.retry(after_s=regain_wait_s(res.headers), status=status, reason=label, rate_limited=True)
        if code is not None and is_retry_code(code):
            return Outcome.retry(status=status, reason=label, breaker_failure=True)
        if status >= 500:
            return Outcome.retry(status=status, reason=label, breaker_failure=True)
        return Outcome.dead(label, status=status)
