"""The tier 3 doc fixtures held against the code: a number or a name in a fixture that the code does not agree with is a failure,
so the fixtures cannot drift from the pulls and the receiver, and the code cannot drift from the saved vendor pages."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from relay import fathom_hook
from relay.money import to_minor_from_number, to_minor_from_text
from relay.pulls import fathom as pull_fathom
from relay.pulls import meta_spend, windsor
from relay.sinks import capi

from .fakes.fathom import sign_webhook

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


def fixture(vendor: str, name: str):
    return json.loads((CONTRACTS / vendor / name).read_text(encoding="utf-8"))


# ---- Fathom ------------------------------------------------------------------------------------------------------------------


def test_the_documented_webhook_payload_is_reduced_to_a_title_a_summary_and_action_items_only():
    doc = fixture("fathom", "webhook_payload.doc.json")
    meeting = fathom_hook.normalise_meeting(doc)
    assert meeting["recording_id"] == "123456789" and meeting["title"] == "Quarterly Business Review"
    assert meeting["summary"].startswith("## Summary") and meeting["url"] == "https://fathom.video/xyz123"
    assert meeting["action_items"] == [{"description": "Email revised proposal to client", "completed": False, "assignee": "Jane Doe"}]
    kept = json.dumps(meeting)
    for dropped in ("Let's revisit the budget", "john.smith@client.com", "jane.doe@acme.com", "hubspot", "share_url"):
        assert dropped not in kept, dropped


def test_the_documented_example_secret_decodes_to_a_key_and_the_header_forms_are_parsed_as_documented():
    headers = fixture("fathom", "webhook_headers.doc.json")
    key = fathom_hook.parse_secret(headers["example_secret"])
    assert key == base64.b64decode(headers["example_secret"].removeprefix("whsec_")) and len(key) == 24
    assert len(headers["steps"]) == 7 and headers["signed_content"] == "${id}.${timestamp}.${body}"
    body = b'{"recording_id": 1}'
    mid, ts = headers["headers"]["webhook-id"]["example"], headers["headers"]["webhook-timestamp"]["example"]
    good = sign_webhook(headers["example_secret"], mid, ts, body)
    sig = good.split(",", 1)[1]
    now = float(ts)
    for header, ok in ((good, True), (f"v2,QVBJ v1,{sig}", True), (f"v1,RmF0aG9t v1,{sig}", True), ("v1,RmF0aG9t v2,QVBJ", False), (f"v2,{sig}", False)):
        verdict = fathom_hook.verify(key, {"webhook-id": mid, "webhook-timestamp": str(ts), "webhook-signature": header}, body, now, 300.0)
        assert (verdict is None) is ok, header


def test_the_signature_example_value_has_the_documented_shape():
    example = fixture("fathom", "webhook_headers.doc.json")["headers"]["webhook-signature"]["example"]
    version, _comma, value = example.partition(",")
    assert version == "v1" and len(base64.b64decode(value)) == 32  # an HMAC-SHA256 digest


def test_the_rate_limit_fixture_agrees_with_the_limiter_constants():
    doc = fixture("fathom", "rate_limits.doc.json")
    assert (pull_fathom.CALLS_PER_WINDOW, pull_fathom.WINDOW_S) == (doc["global"]["calls"], doc["global"]["window_seconds"])
    assert (pull_fathom.HEAVY_CALLS_PER_WINDOW, pull_fathom.WINDOW_S) == (doc["heavy"]["calls"], doc["heavy"]["window_seconds"])
    assert "Retry-After" in doc["headers"]


def test_the_list_fixture_names_every_parameter_the_puller_sends_and_the_reply_shape_the_fake_uses(fathom):
    from .fakes.fathom import API_KEY, make_meeting

    doc = fixture("fathom", "list_meetings.doc.json")
    fathom.meetings = [make_meeting(1), make_meeting(2)]
    import httpx

    body = httpx.get(f"{fathom.url}/meetings", params={"include_action_items": "true"}, headers={"X-Api-Key": API_KEY}).json()
    assert set(body) == set(doc["response"]) and doc["authentication"]["header"] == "X-Api-Key"
    assert {"include_action_items", "created_after", "cursor", "include_summary"} == set(doc["parameters_used_by_the_relay"])
    assert "transcript" not in " ".join(doc["parameters_used_by_the_relay"])


def test_the_delivery_fixture_matches_what_the_receiver_relies_on():
    doc = fixture("fathom", "delivery.doc.json")
    assert "webhook-id" in doc["duplicates"] and "deduplicate" in doc["duplicates"]
    assert "no post-call summary" in doc["no_summary_no_webhook"] and "destination URL" in doc["which_user"]


# ---- Windsor -----------------------------------------------------------------------------------------------------------------


def test_the_documented_windsor_example_numbers_become_exact_minor_units():
    doc = fixture("windsor", "response.doc.json")
    spends = [r["spend"] for r in doc["success"]["data"]]
    assert [to_minor_from_number(s, "USD") for s in spends] == [12545, 13467]
    assert all(isinstance(s, float) for s in spends)  # the page's spend is a JSON number


def test_the_windsor_fields_the_pull_asks_for_are_all_in_the_fields_fixture():
    asked = set(windsor.FIELDS.split(","))
    listed = {f["id"] for f in fixture("windsor", "fields.doc.json")["used_by_the_relay"]}
    assert asked == listed
    types = {f["id"]: f["type"] for f in fixture("windsor", "fields.doc.json")["used_by_the_relay"]}
    assert types["spend"] == "NUMERIC" and types["date"] == "DATE" and types["account_currency"] == "TEXT"


def test_the_windsor_error_table_and_auth_fixture_agree_with_how_the_pull_and_the_fake_behave(windsor):
    import httpx

    errors = fixture("windsor", "errors.doc.json")
    assert set(errors["statuses"]) == {"400", "401", "403", "404", "429", "500"}
    assert errors["limits"] == {"requests_per_minute": 600, "requests_per_day": 10000}
    auth = fixture("windsor", "auth.doc.json")
    assert auth["authentication"] == "the api_key query parameter only"
    refused = httpx.get(f"{windsor.url}/facebook", params={"fields": "date"}, headers={"X-Api-Key": "k"})
    assert refused.json() == auth["header_authentication"]["answer"]
    query = fixture("windsor", "query.doc.json")
    assert query["connector_used_by_the_relay"] == "facebook" and "date_from" in query and "date_to" in query


# ---- Meta Insights -----------------------------------------------------------------------------------------------------------


def test_the_insights_example_row_parses_with_a_string_spend_and_the_fields_match_the_pull():
    doc = fixture("meta", "insights.doc.json")
    row = doc["example_response"]["data"][0]
    assert isinstance(row["spend"], str) and to_minor_from_text(row["spend"], "USD") == 533950
    assert doc["field_types"]["spend"].startswith("numeric string") and doc["field_types"]["account_currency"].startswith("string")
    assert set(doc["parameters_used_by_the_relay"]["fields"].split(", ")) == set(meta_spend.FIELDS.split(","))
    assert "28 days" in doc["refresh"] and "ASSUMED" in doc["not_stated"]


def test_the_limits_fixture_agrees_with_the_codes_and_headers_the_pull_reads():
    doc = fixture("meta", "insights_limits.doc.json")
    assert "1487534" in doc["data_per_call"]["statement"] and meta_spend.TOO_MUCH_DATA == (100, 1487534)
    assert doc["token_error"]["code"] == 190
    assert set(doc["throttle_codes"]["documented"]) <= capi.THROTTLE_CODES and capi.is_throttle_code(80004)
    assert tuple(h.lower() for h in doc["usage_header"]["names"]) == capi.BUC_HEADERS
    sample = doc["usage_header"]["sample"]
    assert capi.regain_wait_s({"x-business-use-case-usage": json.dumps(sample)}) == 19 * 60.0  # 19 minutes in the page's sample


def test_the_paging_fixture_states_the_rule_the_pull_follows():
    doc = fixture("meta", "paging.doc.json")
    assert "empty but contain a next paging link" in doc["next"] and "Stop paging when the next link no longer appears" in doc["next"]
    assert "shouldn't store cursors" in doc["cursors"]
