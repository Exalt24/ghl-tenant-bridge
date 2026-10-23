"""The Sheets fake and sink against answers observed from the REAL Sheets API on 2026-10-23.

The `*.observed.json` fixtures in contracts/google were captured with a USER OAuth token on a scratch spreadsheet (trashed
the same day). They say nothing about the service account JWT sign-in at oauth2.googleapis.com/token, and the relay's own
HTTP client never ran against real Google. Each test replays what the real API did against the fake first (so the fake
cannot drift from it) and then checks what the sink does with it.
"""

from __future__ import annotations

import json
import re

import httpx
import pytest

from relay.sinks.base import DEAD, DONE, PAUSE
from relay.sinks.sheets import _is_unparseable_range, cut_units

from .fakes.sheets import SPREADSHEET_ID, FakeSheets, utf16_units
from .helpers import payload
from .test_sheets import CONTRACTS, append_raw, column_a, fixed, make_event, token_for


def observed(name: str) -> dict:
    doc = json.loads((CONTRACTS / f"{name}.observed.json").read_text(encoding="utf-8"))
    assert doc["provenance"] == "observed" and doc["observed_on"] == "2026-10-23" and "NOT a service account JWT" in doc["token_kind"], name
    return doc


def values_get(sheets: FakeSheets, token: str, rng: str, **params) -> httpx.Response:
    return httpx.get(f"{sheets.url}/v4/spreadsheets/{SPREADSHEET_ID}/values/{rng}", params=params or None, headers={"Authorization": f"Bearer {token}"})


def test_the_fake_lands_every_row_where_the_real_api_put_it(sheets, rsa_key):
    doc = observed("append_layout")
    token = token_for(sheets, rsa_key)
    assert sorted(doc["scenarios"]) == ["f7_Bare", "f7_ColA", "mixed_MixBare", "mixed_MixColA"]
    for key, sc in doc["scenarios"].items():
        sheets.cells.clear()
        sheets.sheet = sc["tab"]
        for step in sc["appends"]:
            res = append_raw(sheets, token, rng=step["request_range"], row=step["row"])
            assert res.status_code == step["status"] == 200, key
            got = res.json()
            assert got.get("tableRange") == step["tableRange"], (key, step["request_range"], step["row"][0])
            want = step["answer"]["updates"]
            assert {k: v for k, v in got["updates"].items() if k != "spreadsheetId"} == {k: v for k, v in want.items() if k != "spreadsheetId"}, key
        assert values_get(sheets, token, sc["tab"]).json()["values"] == sc["readback_of_whole_tab"]["values"], key
    # the fixture itself records the defect the column A range prevents: a bare tab name scattered the rows
    bare, col_a = doc["scenarios"]["f7_Bare"]["appends"], doc["scenarios"]["f7_ColA"]["appends"]
    assert [a["updatedRange"] for a in bare] == ["Bare!A1:L1", "Bare!L2:W2", "Bare!W3:AH3"]
    assert [a["updatedRange"] for a in col_a] == ["ColA!A1:L1", "ColA!A2:L2", "ColA!A3:L3"]


def test_the_fake_reads_back_a_column_the_way_the_real_api_does(sheets, rsa_key):
    doc = observed("values_get")
    token = token_for(sheets, rsa_key)
    sheets.sheet = "Empty"
    for key, rng in (("empty_tab_A_colon_A", "Empty!A:A"), ("empty_tab_bare", "Empty")):
        got = values_get(sheets, token, rng).json()
        assert got == doc[key]["body"] and "values" not in got, key  # no `values` key at all for an empty sheet
    sheets.sheet = "Trail"
    sheets.seed(doc["seed_rows"])
    for key, rng, params in (("trail_A_colon_A", "Trail!A:A", {}), ("trail_bare", "Trail", {}), ("trail_A_colon_A_unformatted", "Trail!A:A", {"valueRenderOption": "UNFORMATTED_VALUE"})):
        assert values_get(sheets, token, rng, **params).json() == doc[key]["body"], key
    assert doc["trail_A_colon_A"]["body"]["values"] == [["evt-1"], [], ["evt-3"], ["12345"], ["evt-5"], [], ["evt-7"]]  # a blank cell is [], not [""]


def test_raw_text_and_the_cell_limit_in_the_fake_match_what_the_real_api_did(sheets, rsa_key):
    doc = observed("values_raw_and_cell_limit")
    token = token_for(sheets, rsa_key)
    assert append_raw(sheets, token, row=doc["raw_values_sent"], params={"valueInputOption": "RAW"}).status_code == 200
    assert sheets.row_values(1) == doc["raw_values_sent"] == doc["raw_read_back"]["formatted"][0]  # RAW keeps "=1+2", "'x", "007" as sent
    assert doc["raw_read_back"]["formula"] == doc["raw_read_back"]["unformatted"] == doc["raw_read_back"]["formatted"]
    assert doc["contrast_user_entered"]["formatted"] == [["3", "apostrophe", "7"]]  # USER_ENTERED is what would have been a formula
    glyph = {"ascii": "a", "astral": "\U0001F600", "two_byte": "é", "bmp_cjk": "中"}
    seen = set()
    for case in doc["cell_limit"]["cases"]:
        label = case["label"]
        kind = next(k for k in glyph if label.startswith(k))
        text = "\U0001F600" * 24_999 + "a" if label == "astral_24999_plus_a" else glyph[kind] * case["chars_sent"]
        assert len(text) == case["chars_sent"] and utf16_units(text) == case["utf16_units_sent"], label
        res = append_raw(sheets, token, rng="Sheet1!A:A", row=[text], params={"valueInputOption": "RAW"})
        assert res.status_code == case["status"], label
        if case["status"] == 400:
            assert res.json()["error"] == case["answer"]["error"], label
        seen.add(case["status"])
    assert seen == {200, 400}
    over = {c["label"] for c in doc["cell_limit"]["cases"] if c["status"] == 400}
    assert {"astral_49000_codepoints", "ascii_50001"} <= over  # 49,000 characters were refused because they are 98,000 units


def test_the_fake_answers_the_error_cases_with_the_text_the_real_api_used(sheets, rsa_key):
    doc = observed("errors")
    token = token_for(sheets, rsa_key)

    def same(res: httpx.Response, want: dict, whole: bool = True) -> None:
        assert res.status_code == want["status"]
        assert res.headers["content-type"] == want["headers"]["content-type"]
        got, exp = res.json()["error"], want["body"]["error"]
        keys = ("code", "status", "message")
        assert (got == exp) if whole else {k: got[k] for k in keys} == {k: exp[k] for k in keys}

    url = f"{sheets.url}/v4/spreadsheets/{SPREADSHEET_ID}/values"
    wrong = {"Authorization": "Bearer ya29.this-is-not-a-real-token"}
    body = {"values": [["x"]]}
    q = {"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}
    sheets.sheet = "Err"
    assert append_raw(sheets, token, rng="Err!A:A").status_code == 200  # control: the same call with a good token lands
    sheets.cells.clear()
    same(httpx.post(f"{url}/Err!A:A:append", params=q, json=body, headers=wrong), doc["wrong_bearer"]["append"])
    same(httpx.get(f"{url}/Err!A:A", headers=wrong), doc["wrong_bearer"]["get"])
    same(httpx.post(f"{url}/Err!A:A:append", params=q, json=body), doc["no_authorization"]["append"], whole=False)  # the real answer also names CREDENTIALS_MISSING
    same(httpx.get(f"{url}/Err!A:A"), doc["no_authorization"]["get"], whole=False)  # an anonymous read is a 403, an anonymous write a 401
    same(httpx.post(f"{sheets.url}/v4/spreadsheets/some-other-id/values/Sheet1!A:A:append", params=q, json=body, headers={"Authorization": f"Bearer {token}"}), doc["missing_spreadsheet"]["append"])
    same(append_raw(sheets, token, rng="NoSuchTab!A:A"), doc["missing_tab"]["append"])
    same(values_get(sheets, token, "NoSuchTab!A:A"), doc["missing_tab"]["get"])
    same(append_raw(sheets, token, rng="Err!not a range"), doc["bad_range_not_a_range"]["append"])
    same(append_raw(sheets, token, rng="Err!A:A", params={"insertDataOption": "INSERT_ROWS"}), doc["rejected_bodies"]["missing_valueInputOption"])
    same(append_raw(sheets, token, rng="Err!A:A", params={"valueInputOption": "BOGUS", "insertDataOption": "INSERT_ROWS"}), doc["rejected_bodies"]["bogus_valueInputOption"], whole=False)
    for kind, sent in (("empty_values_list", {"values": []}), ("empty_object", {})):  # a 200 that writes nothing
        res = httpx.post(f"{url}/Err!A:A:append", params=q, json=sent, headers={"Authorization": f"Bearer {token}"})
        assert res.status_code == 200 and res.json()["updates"]["updatedRange"] == doc["rejected_bodies"][kind]["body"]["updates"]["updatedRange"], kind
    assert sheets.cells == {}
    sheets.queue("forbidden")
    same(append_raw(sheets, token, rng="Err!A:A"), doc["view_only_sheet"]["append_empty_values"])


def test_the_fake_429_has_the_real_body_and_no_retry_after(sheets, rsa_key):
    doc = observed("rate_limit")
    token = token_for(sheets, rsa_key)
    assert append_raw(sheets, token).status_code == 200  # control
    sheets.queue("rate_limit")
    res = append_raw(sheets, token)
    assert res.status_code == 429 and "retry-after" not in res.headers and doc["first_429"]["has_retry_after"] is False
    got, want = res.json()["error"], doc["first_429"]["body"]["error"]
    assert (got["code"], got["status"]) == (want["code"], want["status"]) == (429, "RESOURCE_EXHAUSTED")
    assert re.sub(r"project_number:\d+", "project_number:N", got["message"]) == want["message"].replace("{n}", "N").replace("project_number:N", "project_number:N")
    assert got["details"][0]["reason"] == want["details"][0]["reason"] == "RATE_LIMIT_EXCEEDED"
    assert got["details"][0]["metadata"]["quota_limit_value"] == want["details"][0]["metadata"]["quota_limit_value"] == "60"
    # what the real quota did: more than the documented 60 got through in one burst, and the wait was longer than the 32 s backoff cap
    assert doc["loop"]["ok_before_first_429"] > 60 and doc["recovery"]["seconds_until_a_write_succeeded"] > 32.0


@pytest.mark.parametrize("units", [0, 1, 48_999, 49_000, 49_001])
def test_cut_units_keeps_at_most_the_limit_in_utf16_units_and_never_splits_an_emoji(units):
    for text in ("a" * units, "\U0001F600" * units, "a" + "\U0001F600" * units, "é" * units):
        cut = cut_units(text, 49_000)
        assert utf16_units(cut) <= 49_000 and text.startswith(cut)
        cut.encode("utf-16-le")  # strict: a split pair would be a lone surrogate and raise
        if utf16_units(text) <= 49_000:
            assert cut == text
        else:
            assert utf16_units(cut) >= 48_999  # no more than one emoji is lost


def test_a_cell_of_emoji_is_cut_to_the_limit_in_utf16_units_so_sheets_does_not_refuse_the_row(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json, options={"columns": ["event_id", "name", "email"]})
    assert sink.send(make_event(payload(name="N" * 49_000), event_id="control"), http, ctx).kind == DONE  # the fake takes a full ASCII cell
    out = sink.send(make_event(payload(name="\U0001F600" * 49_000), event_id="emoji"), http, ctx)  # 98,000 units: the real API answered 400
    assert out.kind == DONE, out
    stored = sheets.row_values(2)
    assert utf16_units(stored[1]) == 49_000 and stored[1] == "\U0001F600" * 24_500 and stored[2] == "pat1@example.com"
    out = sink.send(make_event(payload(name="a" * 48_999 + "\U0001F600" * 5), event_id="edge"), http, ctx)
    assert out.kind == DONE and sheets.row_values(3)[1] == "a" * 48_999  # the emoji that would cross the limit is dropped whole


def test_a_missing_tab_pauses_the_sink_and_any_other_400_is_dead(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json)
    assert sink.send(make_event(event_id="control"), http, ctx).kind == DONE
    gone = fixed(sheets, store, sa_json, options={"sheet": "Renamed"})[2]  # the tenant renamed the tab; the real API answers 400 Unable to parse range
    out = sink.send(make_event(event_id="e1"), http, gone)
    assert out.kind == PAUSE and out.status == 400 and "options.sheet" in out.reason and SPREADSHEET_ID not in out.reason
    assert column_a(sheets) == ["control"]
    sheets.queue("bad_request")
    assert sink.send(make_event(event_id="e2"), http, ctx).kind == DEAD  # a 400 that is not about the range cannot be fixed by the tenant


def test_the_unparseable_range_check_reads_the_observed_error_bodies():
    doc = observed("errors")
    assert _is_unparseable_range(json.dumps(doc["missing_tab"]["append"]["body"]))
    assert _is_unparseable_range(json.dumps(doc["bad_range_not_a_range"]["append"]["body"]))
    for body in (doc["rejected_bodies"]["invalid_json"]["body"], doc["rejected_bodies"]["bogus_valueInputOption"]["body"], doc["missing_spreadsheet"]["append"]["body"]):
        assert not _is_unparseable_range(json.dumps(body))
    cell = observed("values_raw_and_cell_limit")["cell_limit"]["cases"][2]["answer"]
    assert cell["error"]["code"] == 400 and not _is_unparseable_range(json.dumps(cell))
    assert not _is_unparseable_range("not json") and not _is_unparseable_range("[1]") and not _is_unparseable_range('{"error": "Unable to parse range"}')
