"""The Meta Conversions API sink against its fake, and the fake against the documentation.

Every test that touches the fake gets it through the `meta` fixture, which ran a positive control first (a valid event is
accepted), so a malformed request cannot pass for a refusal. The sink runs on a fixed clock, so the 7 day rule is tested
at exact boundaries without waiting for anything.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from werkzeug.wrappers import Response

from relay import attribution, ghl
from relay.config import ConfigError, load_config
from relay.http import Http
from relay.sinks.base import DEAD, DONE, PAUSE, RETRY, SKIPPED, Event, Outcome, SinkContext
from relay.sinks.capi import DEFAULT_EVENT_MAP, MAX_EVENT_AGE_S, CapiSink, regain_wait_s

from .fakes.meta_graph import PIXEL_ID, TOKEN, FakeMeta
from .helpers import ingest_one, make_cfg, make_rig, payload

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
NOW = 1_800_000_000.0
DAY = 86400.0


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ad_payload(i: int = 1, **over):
    """A ContactCreate from an ad click: a click id, a browser user agent, a page URL carrying personal data."""
    base = {
        "fbclid": "IwAR2Ab_c-D", "userAgent": "Mozilla/5.0 (X11; Linux x86_64)", "ip": "203.0.113.7",
        "page_url": "https://shop.example.com/lp?utm_source=facebook&email=pat%40example.com&fbclid=IwAR2Ab_c-D",
        "utm_source": "facebook", "utm_medium": "cpc", "utm_campaign": "spring",
        "dateAdded": iso(NOW - 120), "dateUpdated": iso(NOW - 60),
    }
    return payload(i, **{**base, **over})


def make_event(p: dict | None = None, event_id: str = "a1b2c3d4e5f60718", received: float | None = NOW - 30.0) -> Event:
    p = p if p is not None else ad_payload()
    return Event(event_id=event_id, tenant="acme", source="ingest", payload=p, lead=ghl.normalise_lead(p), received_epoch=received)


def ctx_for(meta: FakeMeta, timeout: float = 2.0, process_live: bool = False, **opts) -> SinkContext:
    options = {"test_event_code": "TEST1234", **opts}
    extra = {**CapiSink.check_options(options, {"pixel_id": PIXEL_ID}, process_live), "pixel_id": PIXEL_ID}
    return SinkContext(tenant="acme", url=meta.url, timeout_s=timeout, apikey=TOKEN, extra=extra)


@pytest.fixture
def at_now(meta):
    meta.clock = lambda: NOW
    return meta


def sink() -> CapiSink:
    return CapiSink(clock=lambda: NOW)


def last_event(meta: FakeMeta) -> dict:
    return meta.requests[-1]["json"]["data"][0]


def send(meta: FakeMeta, p: dict | None = None, http: Http | None = None, **kw):
    h = http or Http()
    try:
        return sink().send(make_event(p, **{k: v for k, v in kw.items() if k in ("event_id", "received")}), h, ctx_for(meta, **{k: v for k, v in kw.items() if k not in ("event_id", "received")}))
    finally:
        if http is None:
            h.close()


def raw_post(meta: FakeMeta, event: dict, **extra) -> httpx.Response:
    return httpx.post(f"{meta.url}/{PIXEL_ID}/events", params={"access_token": TOKEN}, json={"data": [event], **extra})


def good_event(**over) -> dict:
    ev = {
        "event_name": "Lead", "event_time": int(NOW) - 60, "event_id": "evt-1", "action_source": "system_generated",
        "user_data": {"em": sha("pat@example.com"), "fbc": "fb.1.1554763741205.AbCdEf"},
    }
    ev.update(over)
    return ev


# ---- the fake against the documentation -----------------------------------------------------------------------------------------


def test_the_fake_refuses_every_malformed_event_the_documentation_forbids(at_now):
    assert raw_post(at_now, good_event()).status_code == 200  # positive control inside the test, same shape
    ud = good_event()["user_data"]
    cases = {
        "missing event_id": {k: v for k, v in good_event().items() if k != "event_id"},
        "unhashed email": good_event(user_data={**ud, "em": "pat@example.com"}),
        "unhashed phone": good_event(user_data={**ud, "ph": "14155550100"}),
        "unhashed email in a list": good_event(user_data={**ud, "em": [sha("a@b.co"), "pat@example.com"]}),
        "unhashed first name": good_event(user_data={**ud, "fn": "pat"}),
        "unhashed country": good_event(user_data={**ud, "country": "us"}),
        "hashed fbc": good_event(user_data={**ud, "fbc": sha("fb.1.1554763741205.AbCdEf")}),
        "hashed ip": good_event(user_data={**ud, "client_ip_address": sha("203.0.113.7")}),
        "hashed user agent": good_event(user_data={**ud, "client_user_agent": sha("Mozilla")}),
        "hashed fbp": good_event(user_data={**ud, "fbp": sha("fb.1.1596403881668.1116446470")}),
        "float event_time": good_event(event_time=float(int(NOW) - 60)),
        "string event_time": good_event(event_time=str(int(NOW) - 60)),
        "event_time older than 7 days": good_event(event_time=int(NOW - 7 * DAY - 1)),
        "missing action_source": {k: v for k, v in good_event().items() if k != "action_source"},
        "unknown action_source": good_event(action_source="carrier_pigeon"),
        "website without a user agent": good_event(action_source="website", event_source_url="https://x.example/lp"),
        "website without a url": good_event(action_source="website", user_data={**ud, "client_user_agent": "Mozilla/5.0"}),
        "empty user_data": good_event(user_data={}),
        "Purchase without currency and value": good_event(event_name="Purchase"),
    }
    for name, ev in cases.items():
        res = raw_post(at_now, ev)
        assert res.status_code == 400 and res.json()["error"]["code"] == 100, name
    assert at_now.effects == 1  # only the control event got in


def test_the_fake_accepts_a_system_generated_event_without_a_user_agent_and_a_complete_website_event(at_now):
    assert raw_post(at_now, good_event()).status_code == 200  # no client_user_agent, system_generated: fine
    site = good_event(event_id="evt-2", action_source="website", event_source_url="https://x.example/lp",
                      user_data={**good_event()["user_data"], "client_user_agent": "Mozilla/5.0"})
    assert raw_post(at_now, site).status_code == 200
    purchase = good_event(event_id="evt-3", event_name="Purchase", custom_data={"currency": "USD", "value": 40})
    assert raw_post(at_now, purchase).status_code == 200
    assert at_now.effects == 3


def test_the_fake_keeps_the_first_copy_of_an_event_id_and_name_and_a_second_name_is_a_new_event(at_now):
    assert raw_post(at_now, good_event()).status_code == 200
    again = raw_post(at_now, good_event())
    assert again.status_code == 200 and again.json()["events_received"] == 1  # accepted, and dropped as a duplicate
    assert at_now.effects == 1 and at_now.accepted_requests == 2
    assert raw_post(at_now, good_event(event_name="Schedule")).status_code == 200  # same id, other name: not a duplicate
    assert raw_post(at_now, good_event(event_id="evt-other")).status_code == 200
    assert at_now.effects == 3


def test_the_fake_dedupe_window_is_48_hours(at_now):
    assert raw_post(at_now, good_event()).status_code == 200
    at_now.clock = lambda: NOW + 47 * 3600
    raw_post(at_now, good_event(event_time=int(NOW) + 47 * 3600 - 60))
    assert at_now.effects == 1
    at_now.clock = lambda: NOW + 49 * 3600
    raw_post(at_now, good_event(event_time=int(NOW) + 49 * 3600 - 60))
    assert at_now.effects == 2


def test_the_fake_checks_the_token_and_the_path(at_now):
    assert raw_post(at_now, good_event()).status_code == 200
    bad = httpx.post(f"{at_now.url}/{PIXEL_ID}/events", params={"access_token": "wrong"}, json={"data": [good_event(event_id="z")]})
    assert bad.status_code == 400 and bad.json()["error"]["code"] == 190
    assert httpx.post(f"{at_now.url}/other-pixel/events", params={"access_token": TOKEN}, json={"data": [good_event(event_id="y")]}).status_code == 404


# ---- the sink sends a well formed event -------------------------------------------------------------------------------------------


def test_a_lead_from_an_ad_is_sent_hashed_with_the_plain_fields_plain_and_is_done(at_now):
    out = send(at_now)
    assert out.kind == DONE and at_now.effects == 1
    req = at_now.requests[0]
    assert req["path"] == f"/v26.0/{PIXEL_ID}/events" and req["query"]["access_token"] == TOKEN
    body = req["json"]
    assert body["test_event_code"] == "TEST1234" and len(body["data"]) == 1
    ev = body["data"][0]
    assert ev["event_name"] == "Lead" and isinstance(ev["event_time"], int) and ev["event_time"] == int(NOW) - 120  # dateAdded
    assert ev["action_source"] == "website" and ev["event_source_url"] == "https://shop.example.com/lp?utm_source=facebook&fbclid=IwAR2Ab_c-D"
    ud = ev["user_data"]
    assert ud["em"] == sha("pat1@example.com") and ud["ph"] == sha("14155550100") and ud["fn"] == sha("pat") and ud["ln"] == sha("lead1")
    assert ud["ct"] == sha("austin") and ud["st"] == sha("tx")
    assert ud["client_ip_address"] == "203.0.113.7" and ud["client_user_agent"] == "Mozilla/5.0 (X11; Linux x86_64)"
    assert ud["fbc"] == f"fb.1.{int((NOW - 120) * 1000)}.IwAR2Ab_c-D" and "fbp" not in ud
    assert ev["custom_data"] == {"utm_source": "facebook", "utm_medium": "cpc", "utm_campaign": "spring"}
    assert "email" not in json.dumps(body).replace("pat1@example", "")  # no raw email anywhere in the payload


def test_no_personal_data_from_the_page_url_reaches_meta(at_now):
    send(at_now, ad_payload(page_url="https://shop.example.com/lp?email=pat%40example.com&phone=%2B14155550100&utm_term=x%40y.com&fbclid=Ab1"))
    wire = json.dumps(last_event(at_now))
    assert "pat%40example.com" not in wire and "pat@example.com" not in wire and "14155550100" not in wire.replace(sha("14155550100"), "")
    assert last_event(at_now)["event_source_url"] == "https://shop.example.com/lp?fbclid=Ab1"


def test_a_secret_never_appears_in_the_outcome(at_now):
    for mode in ("ok", "token_expired", "throttle", "server_error", "bad_param"):
        at_now.queue(mode)
        out = send(at_now, ad_payload(i=int(hashlib.md5(mode.encode()).hexdigest()[:4], 16)))
        text = " ".join(str(v) for v in (out.reason, out.status, out.kind))
        assert TOKEN not in text and at_now.base not in text and PIXEL_ID not in text


def test_test_event_code_is_sent_by_default_and_dropped_only_when_live(at_now):
    send(at_now)
    assert at_now.test_codes[-1] == "TEST1234" and "test_event_code" in at_now.requests[-1]["json"]
    out = send(at_now, ad_payload(i=2), process_live=True, live=True)
    assert out.kind == DONE and "test_event_code" not in at_now.requests[-1]["json"] and at_now.test_codes[-1] is None


def test_live_needs_the_process_flag_and_test_mode_needs_a_code():
    with pytest.raises(ValueError, match="RELAY_LIVE"):
        CapiSink.check_options({"live": True}, {"pixel_id": "x"}, live=False)
    with pytest.raises(ValueError, match="test_event_code is required"):
        CapiSink.check_options({}, {"pixel_id": "x"}, live=True)  # the process is live, the tenant has not said so: still test mode
    ok = CapiSink.check_options({"live": True}, {"pixel_id": "x"}, live=True)
    assert ok["send_live"] is True and ok["test_event_code"] is None
    still_test = CapiSink.check_options({"test_event_code": "T1"}, {"pixel_id": "x"}, live=True)
    assert still_test["send_live"] is False


@pytest.mark.parametrize(
    "opts,fragment",
    [
        ({"nope": 1}, "unknown option"),
        ({"event_map": []}, "event_map"),
        ({"event_map": {"A": ""}}, "event_map"),
        ({"capi_require_click_id": "yes"}, "capi_require_click_id"),
        ({"click_id_sources": "facebook"}, "click_id_sources"),
        ({"default_country_code": "+63"}, "default_country_code"),
        ({"default_country_code": "063"}, "default_country_code"),
        ({"currency": "usd"}, "currency"),
        ({"test_event_code": "has space"}, "test_event_code"),
        ({"live": "true"}, "live"),
    ],
)
def test_option_validation_names_the_problem(opts, fragment):
    with pytest.raises(ValueError, match=fragment):
        CapiSink.check_options({"test_event_code": "T1", **opts}, {"pixel_id": "x"}, False)
    assert CapiSink.check_options({"test_event_code": "T1"}, {"pixel_id": "x"}, False)["require_click_id"] is True  # control


# ---- event_time ------------------------------------------------------------------------------------------------------------------------------


def test_event_time_comes_from_date_added_for_a_create_and_from_date_updated_or_the_envelope_for_a_status_change(at_now):
    send(at_now, ad_payload(dateAdded=iso(NOW - 500), dateUpdated=iso(NOW - 100)))
    assert last_event(at_now)["event_time"] == int(NOW) - 500
    won = ad_payload(3, type="OpportunityStatusUpdate", status="won", monetaryValue=250, dateAdded=iso(NOW - 900), dateUpdated=iso(NOW - 200))
    send(at_now, won, currency="USD")
    assert last_event(at_now)["event_time"] == int(NOW) - 200
    envelope = ad_payload(4, type="OpportunityStatusUpdate", status="won", monetaryValue=250, dateUpdated=None, timestamp=iso(NOW - 300))
    send(at_now, envelope, currency="USD")
    assert last_event(at_now)["event_time"] == int(NOW) - 300


def test_event_time_falls_back_to_the_time_the_event_was_received(at_now):
    send(at_now, ad_payload(dateAdded=None), received=NOW - 77)
    assert last_event(at_now)["event_time"] == int(NOW) - 77
    send(at_now, ad_payload(5, dateAdded="not a date"), received=NOW - 88)
    assert last_event(at_now)["event_time"] == int(NOW) - 88
    won = ad_payload(6, type="OpportunityStatusUpdate", status="won", monetaryValue=5, dateUpdated=None)
    send(at_now, won, received=NOW - 99, currency="USD")
    assert last_event(at_now)["event_time"] == int(NOW) - 99


def test_a_create_event_does_not_borrow_the_updated_stamp(at_now):
    send(at_now, ad_payload(dateAdded=None, dateUpdated=iso(NOW - 10)), received=NOW - 55)
    assert last_event(at_now)["event_time"] == int(NOW) - 55


def test_an_event_6_days_23_hours_old_is_sent_and_an_older_one_is_dead_at_send_time(at_now):
    assert MAX_EVENT_AGE_S == 7 * DAY - 3600
    edge = NOW - (7 * DAY - 3600)
    assert send(at_now, ad_payload(dateAdded=iso(edge))).kind == DONE  # exactly 6 days 23 hours: sent
    assert last_event(at_now)["event_time"] == int(edge)
    n = len(at_now.requests)
    out = send(at_now, ad_payload(2, dateAdded=iso(edge - 1)))
    assert out.kind == DEAD and out.reason == "older than 7 days"
    assert len(at_now.requests) == n  # no call was made for it


def test_the_age_is_judged_at_send_time_not_at_ingest_time(at_now):
    p = ad_payload(dateAdded=iso(NOW - 3 * DAY))
    assert sink().send(make_event(p), Http(), ctx_for(at_now)).kind == DONE
    later = CapiSink(clock=lambda: NOW + 4 * DAY)  # the same lead, four days later: now 7 days old
    at_now.clock = lambda: NOW + 4 * DAY
    out = later.send(make_event(p, event_id="other-id"), Http(), ctx_for(at_now))
    assert out.kind == DEAD and out.reason == "older than 7 days"


def test_an_event_time_in_the_future_is_clamped_to_now(at_now):
    send(at_now, ad_payload(dateAdded=iso(NOW + 3600)))
    assert last_event(at_now)["event_time"] == int(NOW)


# ---- action_source, URL, user agent -----------------------------------------------------------------------------------------------------


def test_action_source_is_website_only_with_both_a_user_agent_and_a_page_url(at_now):
    send(at_now)
    assert last_event(at_now)["action_source"] == "website"
    no_ua = ad_payload(2)
    del no_ua["userAgent"]
    assert send(at_now, no_ua).kind == DONE  # the fake would refuse website without a user agent: it is system_generated instead
    assert last_event(at_now)["action_source"] == "system_generated" and "client_user_agent" not in last_event(at_now)["user_data"]
    assert "event_source_url" not in last_event(at_now)
    no_url = ad_payload(3)
    del no_url["page_url"]
    assert send(at_now, no_url).kind == DONE
    assert last_event(at_now)["action_source"] == "system_generated" and "event_source_url" not in last_event(at_now)
    assert last_event(at_now)["user_data"]["client_user_agent"]  # the user agent is still sent when there is one
    junk_url = ad_payload(4, page_url="javascript:alert(1)")
    assert send(at_now, junk_url).kind == DONE and last_event(at_now)["action_source"] == "system_generated"


# ---- event names and ids --------------------------------------------------------------------------------------------------------------------


def test_the_default_map_and_a_custom_map(at_now):
    assert DEFAULT_EVENT_MAP["ContactCreate"] == "Lead" and DEFAULT_EVENT_MAP["Lead"] == "Lead" and DEFAULT_EVENT_MAP["AppointmentCreate"] == "Schedule"
    send(at_now, ad_payload(type="Lead"))
    assert last_event(at_now)["event_name"] == "Lead"
    appt = {"type": "AppointmentCreate", "locationId": "LOC1", "email": "pat@example.com", "fbclid": "Ab1",
            "appointment": {"id": "apt-77", "contactId": "c-9", "dateAdded": iso(NOW - 60), "dateUpdated": iso(NOW - 30), "title": "Roof check"}}
    send(at_now, appt)
    assert last_event(at_now)["event_name"] == "Schedule"
    send(at_now, ad_payload(8, type="ContactUpdate"), event_map={"ContactUpdate": "CompleteRegistration"})
    assert last_event(at_now)["event_name"] == "CompleteRegistration"


def test_a_lead_event_id_is_a_hash_of_tenant_contact_and_name_so_two_webhooks_for_one_submission_dedupe_at_meta(at_now):
    app_hook = ad_payload(type="ContactCreate", id="contact-777")
    workflow_hook = {k: v for k, v in ad_payload(id="contact-777").items() if k != "type"}
    workflow_hook["type"] = "Lead"
    workflow_hook["contactId"] = "contact-777"
    send(at_now, app_hook, event_id="relay-id-A")
    send(at_now, workflow_hook, event_id="relay-id-B")
    first, second = at_now.requests[0]["json"]["data"][0], at_now.requests[1]["json"]["data"][0]
    assert first["event_id"] == second["event_id"] == sha("acme|contact-777|Lead")
    assert at_now.effects == 1 and at_now.accepted_requests == 2  # Meta kept one copy


def test_a_page_event_id_wins_because_only_that_value_can_match_the_browsers_copy(at_now):
    send(at_now, ad_payload(fbEventId="pixel-evt-42"))
    assert last_event(at_now)["event_id"] == "pixel-evt-42"
    send(at_now, ad_payload(2, event_id="pixel-evt-43"))
    assert last_event(at_now)["event_id"] == "pixel-evt-43"


def test_a_purchase_id_is_a_hash_of_tenant_opportunity_and_name_so_a_re_won_deal_counts_once(at_now):
    won = ad_payload(type="OpportunityStatusUpdate", id="opp-5", contactId="contact-1", status="won", monetaryValue=1200)
    send(at_now, won, currency="PHP", event_id="relay-1")
    send(at_now, {**won, "dateUpdated": iso(NOW - 10)}, currency="PHP", event_id="relay-2")  # the deal flips to won a second time
    ids = {r["json"]["data"][0]["event_id"] for r in at_now.requests}
    assert ids == {sha("acme|opp-5|Purchase")} and at_now.effects == 1
    assert last_event(at_now)["custom_data"]["currency"] == "PHP" and last_event(at_now)["custom_data"]["value"] == 1200


def test_an_event_without_an_entity_id_falls_back_to_the_relay_event_id(at_now):
    send(at_now, ad_payload(id=None, contactId=None, type="Lead"), event_id="relay-fallback-1")
    assert last_event(at_now)["event_id"] == "relay-fallback-1"


# ---- skipped, dead --------------------------------------------------------------------------------------------------------------------------------


def test_an_unmapped_event_type_is_skipped_with_a_reason_and_no_call(at_now):
    for kind in ("ContactUpdate", "InvoicePaid"):
        out = send(at_now, ad_payload(type=kind))
        assert out.kind == SKIPPED and "not mapped" in out.reason and out.alert is False, kind  # a designed skip stays quiet
    assert at_now.requests == []
    assert send(at_now, ad_payload()).kind == DONE  # control: the mapped type goes through


def test_an_event_with_no_type_at_all_is_skipped_and_asks_for_an_alert_naming_the_field(at_now):
    out = send(at_now, {k: v for k, v in ad_payload().items() if k != "type"})
    assert out.kind == SKIPPED and out.alert is True and "type" in out.reason and "Lead" in out.reason
    assert send(at_now, ad_payload(type="")).alert is True  # a blank type is the same mistake
    # No other outcome, and a skip built without the flag, asks for an alert: a designed skip stays quiet.
    assert Outcome.skipped("no click id").alert is False and Outcome(SKIPPED, reason="x").alert is False
    assert Outcome.done().alert is False and Outcome.dead("x").alert is False and Outcome.retry().alert is False
    assert at_now.requests == []


def test_an_opportunity_that_is_not_won_is_skipped_and_a_purchase_needs_a_currency_and_a_value(at_now):
    open_opp = ad_payload(type="OpportunityStatusUpdate", status="open", monetaryValue=100)
    assert send(at_now, open_opp, currency="USD").reason == "opportunity is not won"
    won = ad_payload(2, type="OpportunityStatusUpdate", status="won", monetaryValue=100)
    assert send(at_now, won).reason == "no currency"
    no_value = ad_payload(3, type="OpportunityStatusUpdate", status="won")
    assert send(at_now, no_value, currency="USD").reason == "no value"
    assert at_now.requests == []
    assert send(at_now, won, currency="USD").kind == DONE  # control
    assert last_event(at_now)["event_name"] == "Purchase"


def test_the_won_status_is_matched_case_insensitively(at_now):
    assert send(at_now, ad_payload(type="OpportunityStatusUpdate", status="Won", monetaryValue=1), currency="USD").kind == DONE


def test_a_lead_with_no_click_id_is_skipped_by_default_and_sent_when_the_tenant_says_so(at_now):
    organic = ad_payload(source="manual entry")
    for k in ("fbclid", "fbc", "fbp"):
        organic.pop(k, None)
    out = send(at_now, organic)
    assert out.kind == SKIPPED and "no click id" in out.reason and at_now.requests == []
    assert send(at_now, organic, capi_require_click_id=False).kind == DONE
    assert send(at_now, organic, click_id_sources=["Manual Entry"]).kind == DONE  # an allowlisted source, any case


def test_a_lead_with_nothing_to_match_on_is_dead_with_a_readable_reason(at_now):
    bare = {"type": "ContactCreate", "locationId": "LOC1", "id": "c1", "fbclid": "x", "dateAdded": iso(NOW - 5)}
    bare.pop("fbclid")
    out = send(at_now, bare, capi_require_click_id=False)
    assert out.kind == DEAD and "no matchable user data" in out.reason and at_now.requests == []
    assert send(at_now, {**bare, "email": "pat@example.com"}, capi_require_click_id=False).kind == DONE  # control


def test_a_lead_with_every_optional_field_missing_never_raises(at_now):
    for p in ({}, {"type": "ContactCreate"}, {"type": "ContactCreate", "email": None, "phone": None, "tags": None}, {"type": "Lead", "fbp": "fb.1.1.2"}):
        out = send(at_now, p, capi_require_click_id=False)
        assert out.kind in (DONE, DEAD, SKIPPED), (p, out)


# ---- outcomes ---------------------------------------------------------------------------------------------------------------------------------


def test_any_2xx_is_done_whatever_the_body_says(at_now, monkeypatch):
    assert send(at_now).kind == DONE  # control: the plausible body
    for status in (200, 201, 202, 204):
        monkeypatch.setattr(FakeMeta, "handle", lambda self, request, s=status: Response("whatever", status=s))
        out = send(at_now, ad_payload(i=status))
        assert out.kind == DONE and out.status == status


def test_throttle_retries_after_the_estimated_regain_time_from_the_usage_header(at_now):
    at_now.regain_minutes = 5
    at_now.queue("throttle")
    out = send(at_now)
    assert out.kind == RETRY and out.after_s == 300.0 and out.rate_limited is True and out.status == 400
    assert out.reason == "http 400 code 4" and out.breaker_failure is False


def test_the_usage_header_is_optional_and_its_name_is_read_case_insensitively(at_now):
    at_now.queue("no_header")
    out = send(at_now)
    assert out.kind == RETRY and out.after_s is None and out.rate_limited is True  # no header: the default exponential backoff
    for name in ("x-business-use-case-usage", "X-BUSINESS-USE-CASE", "X-Business-Use-Case"):
        at_now.header_name = name
        at_now.regain_minutes = 2
        at_now.queue("throttle")
        assert send(at_now, ad_payload(i=len(name))).after_s == 120.0, name


def test_the_regain_wait_is_capped_and_garbage_in_the_header_is_ignored():
    usage = {"a": [{"estimated_time_to_regain_access": 3}], "b": [{"estimated_time_to_regain_access": 90}, {"x": 1}, "junk"]}
    assert regain_wait_s({"x-business-use-case-usage": json.dumps(usage)}) == 3600.0  # 90 minutes capped to an hour
    assert regain_wait_s({"x-business-use-case-usage": json.dumps({"a": [{"estimated_time_to_regain_access": 4}]})}) == 240.0
    for junk in ("not json", "[]", '{"a": 5}', '{"a": [{"estimated_time_to_regain_access": "soon"}]}', '{"a": [{"estimated_time_to_regain_access": -3}]}', '{"a": [{"estimated_time_to_regain_access": true}]}'):
        assert regain_wait_s({"x-business-use-case-usage": junk}) is None, junk
    assert regain_wait_s({}) is None
    assert regain_wait_s({"x-business-use-case": json.dumps({"a": [{"estimated_time_to_regain_access": 1}]})}) == 60.0


def test_the_80000_series_and_temporary_codes_retry_and_the_token_codes_pause(at_now):
    expected = {
        "throttle_80004": (RETRY, True), "temporary": (RETRY, False), "server_error": (RETRY, False), "temp_400": (RETRY, False),
        "limit_341": (RETRY, True), "token_expired": (PAUSE, None), "session": (PAUSE, None), "bad_param": (DEAD, None),
    }
    for i, (mode, (kind, rate_limited)) in enumerate(expected.items()):
        assert send(at_now, ad_payload(i=100 + i)).kind == DONE  # control before every refusal
        at_now.queue(mode)
        out = send(at_now, ad_payload(i=200 + i))
        assert out.kind == kind, (mode, out)
        if rate_limited is not None:
            assert out.rate_limited is rate_limited, mode
    at_now.queue("bad_param")
    assert send(at_now, ad_payload(i=300)).reason == "http 400 code 100"  # the code is in the reason, the message is not


def test_a_5xx_and_a_timeout_are_plain_retries_never_ambiguous_because_a_repeat_is_safe(at_now):
    at_now.queue("act_then_fail")
    out = send(at_now)
    assert out.kind == RETRY and out.status == 500 and at_now.effects == 1  # Meta kept the event and answered 500
    again = send(at_now)  # the retry: same event_id and name
    assert again.kind == DONE and at_now.effects == 1  # one event at Meta after two sends
    at_now.queue("plain_502")  # no Graph error object, so only the status can say what to do
    out = send(at_now, ad_payload(i=8))
    assert out.kind == RETRY and out.status == 502 and out.reason == "http 502"
    at_now.queue("hang")
    out = send(at_now, ad_payload(i=9), timeout=0.3)
    assert out.kind == RETRY and out.breaker_failure is True and out.reason == "ReadTimeout"
    at_now.release.set()


def test_a_refused_connection_is_a_retry():
    dead = FakeMeta().start()
    base = dead.url
    dead.stop()
    ctx = SinkContext(tenant="acme", url=base, timeout_s=0.5, apikey=TOKEN, extra={**CapiSink.check_options({"test_event_code": "T1"}, {"pixel_id": PIXEL_ID}, False), "pixel_id": PIXEL_ID})
    h = Http()
    try:
        out = sink().send(make_event(), h, ctx)
    finally:
        h.close()
    assert out.kind == RETRY and out.breaker_failure is True and TOKEN not in (out.reason or "")


def test_the_timeout_is_the_configured_one(at_now):
    at_now.queue("hang")
    out = send(at_now, timeout=0.25)
    assert out.kind == RETRY and out.reason == "ReadTimeout"  # the configured 0.25 s ran out, not the 5 s default
    at_now.release.set()
    assert CapiSink.default_timeout_s == 5.0


def test_a_429_with_no_error_body_is_a_rate_limited_retry(at_now, monkeypatch):
    monkeypatch.setattr(FakeMeta, "handle", lambda self, request: Response("slow down", status=429))
    out = send(at_now)
    assert out.kind == RETRY and out.rate_limited is True and out.status == 429


# ---- through the worker ------------------------------------------------------------------------------------------------------------------------------


def capi_cfg(meta: FakeMeta, **opts):
    sink_cfg = {
        "url_env": "ACME_META_URL", "apikey_env": "ACME_META_TOKEN", "env": {"pixel_id": "ACME_PIXEL_ID"},
        "options": {"test_event_code": "TEST1234", **opts},
    }
    return make_cfg(None, None, extra_sinks={"capi": sink_cfg}, env_extra={"ACME_META_URL": meta.url, "ACME_META_TOKEN": TOKEN, "ACME_PIXEL_ID": PIXEL_ID})


def real_payload(i: int = 1, **over):
    now = time.time()
    return ad_payload(i, dateAdded=iso(now - 120), dateUpdated=iso(now - 60), **over)


def test_a_delivery_reaches_meta_once_through_the_worker_and_a_duplicate_ingest_adds_nothing(store, meta):
    cfg = capi_cfg(meta)
    rig = make_rig(store, cfg, registry={"capi": CapiSink()})
    meta.clock = time.time
    r = ingest_one(store, cfg, real_payload())
    assert ingest_one(store, cfg, real_payload()).duplicate is True
    rig.settle()
    d = store.delivery(r.event_id, "capi")
    assert d["state"] == "done" and meta.effects == 1
    ev = meta.requests[0]["json"]["data"][0]
    assert ev["event_id"] == sha("acme|contact00001|Lead") and ev["user_data"]["em"] == sha("pat1@example.com")


def test_a_skipped_lead_is_terminal_and_visible_in_the_store(store, meta):
    cfg = capi_cfg(meta)
    rig = make_rig(store, cfg, registry={"capi": CapiSink()})
    r = ingest_one(store, cfg, payload())  # no click id
    rig.settle()
    d = store.delivery(r.event_id, "capi")
    assert d["state"] == "skipped" and "no click id" in d["last_error"] and meta.requests == []


def test_a_rejected_token_pauses_the_sink_and_keeps_every_lead_pending_then_they_all_arrive(store, meta):
    cfg = capi_cfg(meta)
    rig = make_rig(store, cfg, registry={"capi": CapiSink()})
    meta.clock = time.time
    ids = [ingest_one(store, cfg, real_payload(i)).event_id for i in range(1, 6)]
    meta.mode = "token_expired"
    rig.worker.run_once(wait_horizon_s=0)
    assert {store.delivery(i, "capi")["state"] for i in ids} == {"pending"}  # none died
    state = store.sink_state("acme", "capi")
    assert state["paused"] and "token rejected" in state["reason"]  # and the digest already went out as a RELAY_ALERT
    meta.mode = "ok"  # the operator replaces the token
    rig.settle(jump_s=1000)
    assert {store.delivery(i, "capi")["state"] for i in ids} == {"done"} and meta.effects == 5


def test_a_throttled_burst_waits_and_then_delivers_every_lead_once(store, meta):
    cfg = capi_cfg(meta)
    rig = make_rig(store, cfg, registry={"capi": CapiSink()})
    meta.clock = time.time
    ids = [ingest_one(store, cfg, real_payload(i)).event_id for i in range(1, 9)]
    meta.queue("throttle", "throttle", "throttle_80004", "act_then_fail", "temporary")
    rig.settle(jump_s=700)
    assert {store.delivery(i, "capi")["state"] for i in ids} == {"done"}
    assert meta.effects == 8 and all(store.delivery(i, "capi")["attempts"] == 1 for i in ids)  # retries spent no attempt budget


def test_the_access_token_is_in_the_query_and_in_no_stored_column_or_log_line(store, meta, caplog):
    import logging

    cfg = capi_cfg(meta)
    from relay import redact
    from relay.app import Deps, create_app

    redact.uninstall()
    create_app(cfg, Deps(store=store))
    rig = make_rig(store, cfg, registry={"capi": CapiSink()})
    meta.clock = time.time
    meta.queue("throttle", "token_expired", "server_error")
    with caplog.at_level(logging.DEBUG):
        r = ingest_one(store, cfg, real_payload())
        rig.worker.run_once(wait_horizon_s=0)
        meta.mode = "ok"
        rig.settle(jump_s=1000)
    assert store.delivery(r.event_id, "capi")["state"] == "done"
    assert meta.requests[-1]["query"]["access_token"] == TOKEN  # it really was on the wire
    logs = "\n".join(f"{r.name} {r.getMessage()} {r.exc_text or ''}" for r in caplog.records if not r.name.startswith("werkzeug"))
    db = "\n".join(store.dump_all_text())
    for secret in (TOKEN, PIXEL_ID):
        assert secret not in logs and secret not in db


# ---- configuration -------------------------------------------------------------------------------------------------------------------------------------


def base_env(meta: FakeMeta, **extra) -> dict:
    sink_cfg = {"url_env": "ACME_META_URL", "apikey_env": "ACME_META_TOKEN", "env": {"pixel_id": "ACME_PIXEL_ID"}, "options": {"test_event_code": "T1"}}
    env = {"RELAY_ENV": "test", "RELAY_INGEST_TOKEN": "i" * 20, "ACME_WORKFLOW_SECRET": "w" * 20, "ACME_META_URL": meta.url, "ACME_META_TOKEN": TOKEN, "ACME_PIXEL_ID": PIXEL_ID}
    env["RELAY_CONFIG"] = json.dumps({"locations": {"L": "acme"}, "tenants": {"acme": {"workflow_secret_env": "ACME_WORKFLOW_SECRET", "sinks": {"capi": sink_cfg}}}})
    env.update(extra)
    return env


def test_the_capi_sink_loads_from_config_and_its_values_are_registered_as_secrets(meta):
    cfg = load_config(base_env(meta))
    s = cfg.tenants["acme"].sinks["capi"]
    assert s.timeout_s == 5.0 and s.max_age_s == 6 * 86400.0 and s.apikey == TOKEN and s.extra["pixel_id"] == PIXEL_ID
    assert PIXEL_ID in cfg.secret_values() and TOKEN in cfg.secret_values()


def mutate(env: dict, edit) -> dict:
    doc = json.loads(env["RELAY_CONFIG"])
    edit(doc["tenants"]["acme"]["sinks"]["capi"])
    return {**env, "RELAY_CONFIG": json.dumps(doc)}


def test_config_refuses_a_capi_sink_without_its_token_pixel_id_or_test_code_and_a_literal_identifier(meta):
    env = base_env(meta)
    load_config(env)  # control
    with pytest.raises(ConfigError, match="apikey_env is required"):
        load_config(mutate(env, lambda s: s.pop("apikey_env")))
    with pytest.raises(ConfigError, match="env.pixel_id is required"):
        load_config(mutate(env, lambda s: s.update(env={})))
    with pytest.raises(ConfigError, match="may only name"):
        load_config(mutate(env, lambda s: s["env"].update(spreadsheet_id="X")))
    with pytest.raises(ConfigError, match="test_event_code is required"):
        load_config(mutate(env, lambda s: s["options"].pop("test_event_code")))
    with pytest.raises(ConfigError, match="would hold an identifier"):
        load_config(mutate(env, lambda s: s["options"].update(pixel_id="123456789")))
    with pytest.raises(ConfigError, match="would hold an identifier"):
        load_config(mutate(env, lambda s: s["options"].update(access_token="abc")))
    with pytest.raises(ConfigError, match="RELAY_LIVE"):
        load_config(mutate(env, lambda s: s["options"].update(live=True)))
    with pytest.raises(ConfigError, match="options must be an object"):
        load_config(mutate(env, lambda s: s.update(options=[1])))
    with pytest.raises(ConfigError, match="ACME_NOPE is not set"):
        load_config(mutate(env, lambda s: s["env"].update(pixel_id="ACME_NOPE")))


def test_a_real_graph_url_is_refused_without_relay_live(meta):
    env = base_env(meta, ACME_META_URL="https://graph.facebook.com/v26.0")
    with pytest.raises(ConfigError, match="RELAY_LIVE"):
        load_config(env)
    cfg = load_config({**env, "RELAY_LIVE": "1"})  # control: the same config loads when live is declared
    assert cfg.tenants["acme"].sinks["capi"].url == "https://graph.facebook.com/v26.0"


# ---- the doc fixtures agree with the code and the fake ----------------------------------------------------------------------------------------------


def fixture(name: str):
    return json.loads((CONTRACTS / "meta" / name).read_text(encoding="utf-8"))


def test_the_hashing_fixture_matches_the_normalisers_and_the_printed_digests():
    doc = fixture("hashing.doc.json")
    assert tuple(doc["never_hash"]) == attribution.NEVER_HASH
    norm = {"em": attribution.norm_email, "ph": lambda v: attribution.norm_phone(v), "fn": attribution.norm_name, "ln": attribution.norm_name,
            "ct": attribution.norm_city, "st": attribution.norm_state, "zp": attribution.norm_zip, "country": attribution.norm_country}
    printed = 0
    for v in doc["vectors"]:
        field = v["field"]
        source = v.get("relay_input", v["input"])
        got = norm[field](source, v["country"]) if field in ("zp", "st") and "country" in v else norm[field](source)
        assert got == v["normalised"], v
        digest = v["sha256"] or sha(v["normalised"])
        assert sha(got) == digest, v
        printed += bool(v["sha256"])
    assert printed == 6  # the six digests the page prints


def test_the_fbc_fixture_matches_the_builder():
    doc = fixture("fbc.doc.json")
    assert attribution.build_fbc(doc["example_fbclid"], doc["example_creation_time_ms"]) == doc["example_fbc"]
    assert doc["subdomain_index_on_a_server"] == attribution.FBC_SUBDOMAIN_INDEX and len(str(doc["example_creation_time_ms"])) == 13


def test_the_server_event_fixture_matches_the_fake_and_the_sink():
    doc = fixture("server_event.doc.json")
    from .fakes.meta_graph import ACTION_SOURCES, SEVEN_DAYS_S

    assert set(doc["action_source_values"]) == ACTION_SOURCES and doc["event_time"]["max_age_days"] * 86400 == SEVEN_DAYS_S
    assert MAX_EVENT_AGE_S == SEVEN_DAYS_S - 3600 and doc["recommended_timeout_ms"] == 1500


def test_the_error_fixture_codes_are_the_ones_the_sink_knows():
    from relay.sinks.capi import PAUSE_CODES, RETRY_CODES, in_80000_series

    doc = fixture("errors.doc.json")
    named = {int(k) for k in doc["codes"]}
    assert PAUSE_CODES | RETRY_CODES | {80000} == named
    assert in_80000_series(80000) and in_80000_series(80004) and in_80000_series(80014) and not in_80000_series(79999) and not in_80000_series(81000)
    assert doc["error_object"]["error"]["code"] == 190
    assert fixture("response.assumed.json")["provenance"] == "ASSUMED"
