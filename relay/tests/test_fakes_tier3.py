"""The tier 3 fakes against the vendor pages they were written from. A fake that accepts everything is a rubber stamp, so each
rule a fake enforces is proven here by a request that breaks exactly that rule, after a request that does not (the
fixtures already ran a positive control). Tests/mutants.py deletes each rule and requires one of these to go red."""

from __future__ import annotations

import base64
import datetime as dt
import json

import httpx
import pytest

from .conftest import FATHOM_KEY, INSIGHTS_TOKEN, WINDSOR_KEY
from .fakes.ads import ACCOUNT, day_rows, windsor_rows
from .fakes.fathom import LIMIT, WEBHOOK_SECRET, make_meeting, sign_webhook

# One client for the whole file: httpx.get builds a fresh TLS context per call, which costs about 150 ms on Windows.
CLIENT = httpx.Client(trust_env=False)

GOOD = {
    "access_token": INSIGHTS_TOKEN, "level": "campaign", "time_increment": "1", "limit": "25",
    "fields": "campaign_id,campaign_name,spend,impressions,clicks,account_currency",
    "time_range": '{"since":"2026-10-01","until":"2026-10-04"}',
}


def get(f, **over):
    params = {**GOOD, **over}
    return CLIENT.get(f"{f.url}/{ACCOUNT}/insights", params={k: v for k, v in params.items() if v is not None})


# ---- Insights ----------------------------------------------------------------------------------------------------------------


def test_insights_serves_string_spend_in_the_range_asked_with_the_account_currency(insights):
    insights.rows = day_rows({"c1": "One", "c2": "Two"}, dt.date(2026, 10, 1), 6, currency="EUR")
    body = get(insights).json()
    assert len(body["data"]) == 8  # 2 campaigns x 4 days inside the range
    row = body["data"][0]
    assert isinstance(row["spend"], str) and row["account_currency"] == "EUR" and row["date_start"] == row["date_stop"]
    assert {r["date_start"] for r in body["data"]} == {"2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04"}


@pytest.mark.parametrize(
    "rule,over",
    [
        ("token", {"access_token": "wrong"}),
        ("level", {"level": "ad"}),
        ("level", {"level": None}),
        ("time_increment", {"time_increment": "7"}),
        ("time_increment", {"time_increment": None}),
        ("time_range", {"time_range": "last_7d"}),
        ("time_range", {"time_range": '{"since":"2026-10-05","until":"2026-10-01"}'}),
        ("time_range", {"time_range": None}),
        ("fields", {"fields": "campaign_id,spend"}),
        ("fields", {"fields": None}),
        ("limit", {"limit": "0"}),
        ("limit", {"limit": "9999"}),
        ("limit", {"limit": "many"}),
    ],
)
def test_insights_refuses_each_request_that_breaks_one_rule(insights, rule, over):
    insights.rows = day_rows({"c1": "One"}, dt.date(2026, 10, 1), 4)
    assert get(insights).status_code == 200  # control, inside the test: the same request without the break
    res = get(insights, **over)
    assert res.status_code == 400, (rule, over)
    assert res.json()["error"]["code"] in (100, 190)


def test_insights_pages_by_next_and_the_last_page_has_none(insights):
    insights.rows = day_rows({"c1": "One", "c2": "Two"}, dt.date(2026, 10, 1), 4)  # 8 rows
    first = get(insights, limit="3").json()
    assert len(first["data"]) == 3 and "next" in first["paging"]
    second = CLIENT.get(first["paging"]["next"]).json()
    third = CLIENT.get(second["paging"]["next"]).json()
    assert [len(first["data"]), len(second["data"]), len(third["data"])] == [3, 3, 2] and "next" not in third["paging"]
    seen = [r["date_start"] + r["campaign_id"] for p in (first, second, third) for r in p["data"]]
    assert len(seen) == len(set(seen)) == 8


def test_insights_can_answer_an_empty_page_that_still_carries_next(insights):
    insights.rows = day_rows({"c1": "One"}, dt.date(2026, 10, 1), 4)
    insights.queue("empty_page")
    page = get(insights, limit="2").json()
    assert page["data"] == [] and "next" in page["paging"]
    real = CLIENT.get(page["paging"]["next"]).json()
    assert len(real["data"]) == 2 and "next" in real["paging"]


def test_insights_answers_error_100_subcode_1487534_for_a_range_over_max_days(insights):
    insights.rows = day_rows({"c1": "One"}, dt.date(2026, 10, 1), 4)
    insights.max_days = 4
    assert get(insights).status_code == 200  # a 4 day range is allowed
    insights.max_days = 3
    res = get(insights)
    err = res.json()["error"]
    assert res.status_code == 400 and (err["code"], err["error_subcode"]) == (100, 1487534)
    assert get(insights, time_range='{"since":"2026-10-01","until":"2026-10-03"}').status_code == 200


def test_insights_throttle_carries_the_usage_header_in_minutes_under_either_name(insights):
    insights.queue("throttle")
    res = get(insights)
    usage = json.loads(res.headers["x-business-use-case-usage"])
    assert res.status_code == 400 and res.json()["error"]["code"] == 4
    assert usage[ACCOUNT][0]["estimated_time_to_regain_access"] == 5
    insights.header_name = "X-Business-Use-Case"
    insights.regain_minutes = 2
    insights.queue("throttle")
    res = get(insights)
    assert "x-business-use-case" in res.headers and "x-business-use-case-usage" not in res.headers
    insights.queue("no_header")
    assert "x-business-use-case" not in get(insights).headers
    insights.queue("throttle_80004")
    assert get(insights).json()["error"]["code"] == 80004
    insights.queue("token_expired")
    assert get(insights).json()["error"]["code"] == 190


def test_insights_can_serve_spend_as_a_number_and_can_point_next_at_another_host(insights):
    insights.rows = day_rows({"c1": "One"}, dt.date(2026, 10, 1), 4)
    insights.spend_type = "number"
    assert isinstance(get(insights).json()["data"][0]["spend"], float)
    insights.spend_type = "string"
    insights.next_base = "http://other.invalid:9"
    assert get(insights, limit="2").json()["paging"]["next"].startswith("http://other.invalid:9/")


# ---- Windsor -----------------------------------------------------------------------------------------------------------------


WGOOD = {"api_key": WINDSOR_KEY, "fields": "date,campaign_id,campaign,spend,impressions,clicks,account_currency", "date_from": "2026-10-01", "date_to": "2026-10-02"}


def wget(f, headers=None, **over):
    params = {**WGOOD, **over}
    return CLIENT.get(f"{f.url}/facebook", params={k: v for k, v in params.items() if v is not None}, headers=headers or {})


def test_windsor_serves_number_spend_for_the_dates_asked(windsor):
    windsor.rows = windsor_rows({"c1": "One"}, dt.date(2026, 10, 1), 5)
    rows = wget(windsor).json()["data"]
    assert [r["date"] for r in rows] == ["2026-10-01", "2026-10-02"] and isinstance(rows[0]["spend"], float)
    windsor.spend_type = "string"
    assert isinstance(wget(windsor).json()["data"][0]["spend"], str)


def test_windsor_takes_the_key_from_the_query_parameter_only(windsor):
    windsor.rows = windsor_rows({"c1": "One"}, dt.date(2026, 10, 1), 2)
    assert wget(windsor).status_code == 200  # control
    res = wget(windsor, api_key=None, headers={"X-Api-Key": WINDSOR_KEY, "Authorization": "Bearer " + WINDSOR_KEY})
    assert res.status_code == 401 and res.json() == {"error": "Not authorized"}  # the documented answer to header auth
    assert wget(windsor, api_key="wrong").status_code == 401


@pytest.mark.parametrize("over", [{"fields": "date,spend"}, {"fields": None}, {"date_from": None}, {"date_to": "soon"}])
def test_windsor_refuses_a_request_without_the_fields_or_dates_it_needs(windsor, over):
    windsor.rows = windsor_rows({"c1": "One"}, dt.date(2026, 10, 1), 2)
    assert wget(windsor).status_code == 200  # control
    res = wget(windsor, **over)
    assert res.status_code == 400 and res.json()["error"]["code"] == "invalid_request"


def test_windsor_modes_answer_the_documented_errors(windsor):
    windsor.queue("rate_limit")
    res = wget(windsor)
    assert res.status_code == 429 and res.headers["retry-after"] == "2" and res.json()["error"]["code"] == "rate_limit_exceeded"
    windsor.retry_after = None
    windsor.queue("rate_limit")
    assert "retry-after" not in wget(windsor).headers
    windsor.queue("unauthorized")
    assert wget(windsor).status_code == 401
    windsor.queue("server_error")
    assert wget(windsor).status_code == 500
    windsor.queue("echo_url")
    res = wget(windsor)
    assert res.status_code == 500 and WINDSOR_KEY in res.text  # the vendor page that echoes the URL, key and all


# ---- Fathom ------------------------------------------------------------------------------------------------------------------


def fget(f, key=FATHOM_KEY, **params):
    return CLIENT.get(f"{f.url}/meetings", params=params, headers={"X-Api-Key": key} if key else {})


def test_fathom_pages_by_next_cursor_ten_at_a_time_and_ends_with_null(fathom):
    fathom.meetings = [make_meeting(i) for i in range(1, 26)]
    seen, cursor, pages = [], None, 0
    while True:
        body = fget(fathom, include_action_items="true", **({"cursor": cursor} if cursor else {})).json()
        seen += [m["recording_id"] for m in body["items"]]
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert pages == 3 and len(seen) == len(set(seen)) == 25


def test_fathom_serves_action_items_and_summary_only_when_asked(fathom):
    fathom.meetings = [make_meeting(1)]
    plain = fget(fathom).json()["items"][0]
    assert plain["action_items"] is None and plain["default_summary"] is None
    with_items = fget(fathom, include_action_items="true").json()["items"][0]
    assert with_items["action_items"][0]["description"] == "Send recap 1" and with_items["default_summary"] is None
    assert fget(fathom, include_summary="true").json()["items"][0]["default_summary"]["markdown_formatted"].startswith("## Summary")


def test_fathom_filters_by_created_after(fathom):
    fathom.meetings = [make_meeting(1, created="2026-09-01T10:00:00Z"), make_meeting(2, created="2026-10-10T10:00:00Z")]
    assert len(fget(fathom).json()["items"]) == 2  # control
    assert [m["recording_id"] for m in fget(fathom, created_after="2026-10-01T00:00:00Z").json()["items"]] == [5002]


def test_fathom_needs_the_api_key_header(fathom):
    assert fget(fathom).status_code == 200  # control
    assert fget(fathom, key="wrong").status_code == 401 and fget(fathom, key=None).status_code == 401
    assert fathom.effects == 1


def test_fathom_answers_429_with_retry_after_and_enforces_its_own_window(fathom):
    fathom.queue("rate_limit")
    res = fget(fathom)
    assert res.status_code == 429 and res.headers["retry-after"] == "3" and res.headers["ratelimit-limit"] == "60"
    fathom.retry_after = None
    fathom.queue("rate_limit")
    assert "retry-after" not in fget(fathom).headers
    clock = [0.0]
    fathom.clock = lambda: clock[0]
    answers = [fget(fathom).status_code for _ in range(LIMIT + 5)]
    assert answers.count(200) == LIMIT and answers.count(429) == 5  # the 61st call inside the window is refused
    clock[0] += 61
    assert fget(fathom).status_code == 200  # the window moved on


def test_fathom_counts_a_heavy_call_against_its_own_smaller_window(fathom):
    clock = [0.0]
    fathom.clock = lambda: clock[0]
    answers = [fget(fathom, include_summary="true").status_code for _ in range(35)]
    assert answers.count(200) == 30 and answers.count(429) == 5
    assert fget(fathom).status_code == 200  # a light call still fits the global window


def test_fathom_modes_unauthorized_and_server_error(fathom):
    fathom.queue("unauthorized", "server_error")
    assert (fget(fathom).status_code, fget(fathom).status_code) == (401, 500)


def test_the_independent_signer_matches_the_standard_webhooks_recipe():
    """The signer in the fake is written without the relay. Here it is held against a signature computed by hand from the
    recipe on Fathom's page, so the fake cannot drift from the page without this going red."""
    import hashlib
    import hmac

    body = b'{"recording_id": 1}'
    key = base64.b64decode(WEBHOOK_SECRET[len("whsec_"):])
    expected = base64.b64encode(hmac.new(key, b"msg_1.1712345678." + body, hashlib.sha256).digest()).decode()
    assert sign_webhook(WEBHOOK_SECRET, "msg_1", 1712345678, body) == "v1," + expected
    assert sign_webhook(WEBHOOK_SECRET, "msg_1", 1712345678, body, versions=("v1", "v2")) == f"v1,{expected} v2,{expected}"
    assert sign_webhook(WEBHOOK_SECRET, "msg_1", 1712345679, body) != "v1," + expected  # the timestamp is part of what is signed
