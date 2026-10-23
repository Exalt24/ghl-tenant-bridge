"""The Google Sheets sink against its fake, and the fake against the documentation.

The `sheets` fixture ran a positive control (a correctly signed assertion earns a token, a valid append lands) before any
test, so a malformed request cannot pass for a refusal. The sink runs on a fixed clock where time matters, and the
worker level tests move the DATABASE clock instead of sleeping.
"""

from __future__ import annotations

import base64
import json
import logging
import random
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec as elliptic
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from relay import ghl, redact
from relay.config import ConfigError, load_config
from relay.fanout import Hooks
from relay.http import Http, HttpResult
from relay.sinks import sheets as sheets_mod
from relay.sinks.base import AMBIGUOUS, DEAD, DONE, PAUSE, RETRY, Event, SinkContext
from relay.sinks.sheets import ABSENT_AFTER_S, MAX_CELL_CHARS, SheetsSink, _is_unparseable_range, backoff_429, b64url, cut_units, make_assertion, parse_account, table_start_column

from .fakes.sheets import GRANT_TYPE, SCOPE, SPREADSHEET_ID, FakeSheets, col_letters, col_num, service_account_json, utf16_units
from .helpers import SimulatedCrash, ingest_one, make_cfg, make_rig, payload, wait_for

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts" / "google"
NOW = 1_800_000_000.0


def make_event(p: dict | None = None, event_id: str = "evt-0001") -> Event:
    p = p if p is not None else payload()
    return Event(event_id=event_id, tenant="acme", source="ingest", payload=p, lead=ghl.normalise_lead(p), received_epoch=NOW - 30)


def sctx(sheets: FakeSheets, sa_json: str, store, timeout: float = 2.0, options: dict | None = None, **kw) -> SinkContext:
    env = {"spreadsheet_id": SPREADSHEET_ID, "service_account": sa_json}
    extra = {**SheetsSink.check_options({"sheet": "Sheet1", **(options or {})}, env, False), **env}
    return SinkContext(tenant="acme", url=sheets.url, timeout_s=timeout, extra=extra, lock=store.append_lock, **kw)


def fixed(sheets: FakeSheets, store, sa_json: str, **kw):
    """(sink, http, ctx) on the fixed clock, with the fake on the same clock."""
    sheets.clock = lambda: NOW
    return SheetsSink(clock=lambda: NOW, rng=random.Random(1)), Http(), sctx(sheets, sa_json, store, **kw)


def column_a(sheets: FakeSheets) -> list:
    return sheets.column_values(1)


# ---- the fake against the documentation -----------------------------------------------------------------------------------------


def jwt(key, claims: dict | None = None, header: dict | None = None, pad: bool = False, sign: bool = True) -> str:
    def enc(b: bytes) -> str:
        out = base64.urlsafe_b64encode(b).decode()
        return out if pad else out.rstrip("=")

    base = {"iss": "relay-test@fake-project.iam.gserviceaccount.com", "scope": SCOPE, "aud": None, "iat": int(NOW), "exp": int(NOW) + 3600}
    base.update(claims or {})
    head = enc(json.dumps(header or {"alg": "RS256", "typ": "JWT"}).encode())
    body = enc(json.dumps(base).encode())
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256()) if sign else b"unsigned"
    return f"{head}.{body}.{enc(sig)}"


def exchange(sheets: FakeSheets, assertion: str, grant: str = GRANT_TYPE) -> httpx.Response:
    return httpx.post(sheets.token_url, data={"grant_type": grant, "assertion": assertion})


def test_the_token_endpoint_refuses_every_malformed_assertion_the_documentation_forbids(sheets, rsa_key):
    sheets.clock = lambda: NOW
    good = jwt(rsa_key, {"aud": sheets.token_url})
    assert exchange(sheets, good).status_code == 200  # positive control inside the test
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cases = {
        "wrong signing key": (jwt(other_key, {"aud": sheets.token_url}), GRANT_TYPE),
        "unsigned": (jwt(rsa_key, {"aud": sheets.token_url}, sign=False), GRANT_TYPE),
        "padded base64": (jwt(rsa_key, {"aud": sheets.token_url}, pad=True), GRANT_TYPE),
        "wrong grant type": (good, "password"),
        "wrong audience": (jwt(rsa_key, {"aud": "https://elsewhere.example/token"}), GRANT_TYPE),
        "wrong issuer": (jwt(rsa_key, {"aud": sheets.token_url, "iss": "someone@else.example"}), GRANT_TYPE),
        "wrong scope": (jwt(rsa_key, {"aud": sheets.token_url, "scope": "https://www.googleapis.com/auth/drive"}), GRANT_TYPE),
        "lifetime over 65 minutes": (jwt(rsa_key, {"aud": sheets.token_url, "exp": int(NOW) + 66 * 60}), GRANT_TYPE),
        "exp before iat": (jwt(rsa_key, {"aud": sheets.token_url, "exp": int(NOW) - 5}), GRANT_TYPE),
        "issued in the far future": (jwt(rsa_key, {"aud": sheets.token_url, "iat": int(NOW) + 7200, "exp": int(NOW) + 9000}), GRANT_TYPE),
        "not three parts": ("abc.def", GRANT_TYPE),
        "wrong alg": (jwt(rsa_key, {"aud": sheets.token_url}, header={"alg": "HS256", "typ": "JWT"}), GRANT_TYPE),
    }
    for name, (assertion, grant) in cases.items():
        res = exchange(sheets, assertion, grant)
        assert res.status_code == 400 and res.json()["error"] in ("invalid_grant", "invalid_scope", "unsupported_grant_type"), name
    assert exchange(sheets, jwt(rsa_key, {"aud": sheets.token_url, "exp": int(NOW) + 65 * 60})).status_code == 200  # exactly 65 minutes is fine
    assert sheets.token_count == 2


def test_the_signature_rule_names_invalid_jwt_signature(sheets, rsa_key):
    sheets.clock = lambda: NOW
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    res = exchange(sheets, jwt(other, {"aud": sheets.token_url}))
    assert res.json() == {"error": "invalid_grant", "error_description": "Invalid JWT Signature."}


def append_raw(sheets: FakeSheets, token: str, rng: str = "Sheet1", row=None, params=None) -> httpx.Response:
    q = {"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"} if params is None else params
    return httpx.post(f"{sheets.url}/v4/spreadsheets/{SPREADSHEET_ID}/values/{rng}:append", params=q, json={"values": [row or ["evt", "x"]]}, headers={"Authorization": f"Bearer {token}"})


def token_for(sheets: FakeSheets, key) -> str:
    sheets.clock = lambda: NOW
    return exchange(sheets, jwt(key, {"aud": sheets.token_url})).json()["access_token"]


def test_the_values_endpoints_enforce_auth_the_required_option_and_the_range(sheets, rsa_key):
    token = token_for(sheets, rsa_key)
    assert append_raw(sheets, token).status_code == 200  # control
    assert append_raw(sheets, "ya29.nope").status_code == 401
    assert append_raw(sheets, token, params={"insertDataOption": "INSERT_ROWS"}).status_code == 400  # valueInputOption is required
    assert append_raw(sheets, token, rng="Elsewhere").status_code == 400
    assert append_raw(sheets, token, rng="Sheet1!ZZZ$$").status_code == 400
    res = httpx.post(f"{sheets.url}/v4/spreadsheets/other-id/values/Sheet1:append", params={"valueInputOption": "RAW"}, json={"values": [["x"]]}, headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 404
    sheets.clock = lambda: NOW + 7200  # the token has expired
    assert append_raw(sheets, token).status_code == 401
    assert sheets.effects == 1


def test_raw_keeps_a_leading_equals_as_text_and_user_entered_would_make_it_a_formula(sheets, rsa_key):
    token = token_for(sheets, rsa_key)
    append_raw(sheets, token, row=["=1+2"], params={"valueInputOption": "RAW"})
    assert sheets.cells[(1, 1)] == "=1+2"
    append_raw(sheets, token, row=["=1+2"], params={"valueInputOption": "USER_ENTERED"})
    assert sheets.cells[(2, 1)] == "#FORMULA"  # the fake's picture of what the vendor would do


def test_the_fake_finds_tables_and_starts_a_row_where_the_documented_example_says(sheets, rsa_key):
    doc = json.loads((CONTRACTS / "table_detection.doc.json").read_text(encoding="utf-8"))
    token = token_for(sheets, rsa_key)

    def place():
        sheets.cells.clear()
        sheets.seed([["a", "b", "c"], ["d", "e", "f"]], "A1")  # table A1:C2
        sheets.seed([["g", "h", "i"], ["j", "k", "l"], ["m", "n", "o"]], "B4")  # table B4:D6

    place()
    assert [f"{col_letters(c1)}{r1}:{col_letters(c2)}{r2}" for r1, c1, r2, c2 in sheets.tables()] == doc["tables"]
    for rng in doc["appends_begin_at_B7_for_these_ranges"]:
        place()
        res = append_raw(sheets, token, rng=f"Sheet1!{rng}" if rng != "Sheet1" else "Sheet1", row=["NEW"])
        assert res.status_code == 200 and sheets.cells[(7, 2)] == "NEW", rng
    for rng, start in doc["other_ranges"].items():
        place()
        append_raw(sheets, token, rng=f"Sheet1!{rng}", row=["NEW"])
        col = col_num("".join(ch for ch in start if ch.isalpha()))
        row = int("".join(ch for ch in start if ch.isdigit()))
        assert sheets.cells[(row, col)] == "NEW", rng


def test_the_append_answer_carries_the_table_before_the_append_and_is_empty_for_an_empty_sheet(sheets, rsa_key):
    token = token_for(sheets, rsa_key)
    first = append_raw(sheets, token, row=["event_id", "name"]).json()
    assert "tableRange" not in first and first["updates"]["updatedRange"] == "Sheet1!A1:B1"
    second = append_raw(sheets, token, row=["e1", "Pat"]).json()
    assert second["tableRange"] == "Sheet1!A1:B1" and second["updates"]["updatedRange"] == "Sheet1!A2:B2"


def test_the_read_back_returns_the_column_without_trailing_empties_and_no_values_key_when_empty(sheets, rsa_key):
    token = token_for(sheets, rsa_key)
    url = f"{sheets.url}/v4/spreadsheets/{SPREADSHEET_ID}/values/Sheet1!A:A"
    headers = {"Authorization": f"Bearer {token}"}
    assert httpx.get(url, headers=headers).json() == {"range": "Sheet1!A1:A1000", "majorDimension": "ROWS"}  # observed: the range is expanded
    append_raw(sheets, token, row=["one"])
    append_raw(sheets, token, row=["two"])
    assert httpx.get(url, headers=headers).json()["values"] == [["one"], ["two"]]


# ---- the JWT and the token --------------------------------------------------------------------------------------------------


def split_assertion(sheets: FakeSheets) -> tuple[str, dict, dict, bytes]:
    form = sheets.token_requests[-1]["form"]
    head, body, sig = form["assertion"].split(".")
    dec = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))
    return form["assertion"], json.loads(dec(head)), json.loads(dec(body)), dec(sig)


def test_the_assertion_is_rs256_with_the_documented_claims_and_verifies_with_the_public_key(sheets, sa_json, rsa_key, store):
    sink, http, ctx = fixed(sheets, store, sa_json)
    assert sink.send(make_event(), http, ctx).kind == DONE
    assertion, header, claims, sig = split_assertion(sheets)
    doc = json.loads((CONTRACTS / "jwt.doc.json").read_text(encoding="utf-8"))
    assert {k: header[k] for k in ("alg", "typ")} == doc["header"] and header["kid"] == "testkey01"
    assert set(claims) == set(doc["claims"]) and claims["iss"] == sheets.email and claims["scope"] == SCOPE == doc["claims"]["scope"]
    assert claims["aud"] == sheets.token_url and claims["iat"] == int(NOW) and claims["exp"] == claims["iat"] + doc["lifetime_s"] == claims["iat"] + 3600
    assert claims["exp"] - claims["iat"] <= doc["max_lifetime_s"] == 65 * 60
    assert "=" not in assertion and "\n" not in assertion  # base64url, no padding, no newlines
    head_part, body_part, _ = assertion.split(".")
    rsa_key.public_key().verify(sig, f"{head_part}.{body_part}".encode(), padding.PKCS1v15(), hashes.SHA256())  # raises if wrong
    assert sheets.token_requests[-1]["form"]["grant_type"] == GRANT_TYPE
    assert sheets.token_requests[-1]["headers"]["content-type"].startswith("application/x-www-form-urlencoded")


def test_the_assertion_function_alone_signs_pkcs1v15_sha256_and_b64url_has_no_padding(rsa_key, sheets):
    acct = parse_account(service_account_json(rsa_key, sheets.token_url))
    a = make_assertion(acct, NOW)
    head, body, sig = a.split(".")
    for part in (head, body, sig):
        assert "=" not in part and "+" not in part and "/" not in part
    pad = lambda s: s + "=" * (-len(s) % 4)
    rsa_key.public_key().verify(base64.urlsafe_b64decode(pad(sig)), f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    assert b64url(b"\xff\xfe\xfd\xfc") == "__79_A" and b64url(b"a") == "YQ"
    assert GRANT_TYPE == sheets_mod.GRANT_TYPE == json.loads((CONTRACTS / "token_request.rfc7523.json").read_text(encoding="utf-8"))["grant_type"]
    assert sheets_mod.JWT_LIFETIME_S == 3600 and sheets_mod.JWT_LIFETIME_S <= sheets_mod.MAX_JWT_LIFETIME_S == 65 * 60
    assert sheets_mod.SCOPE == SCOPE


def test_a_token_is_cached_until_five_minutes_before_it_expires(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json)
    for i in range(3):
        assert sink.send(make_event(event_id=f"e{i}"), http, ctx).kind == DONE
    assert sheets.token_count == 1
    sink.clock = lambda: NOW + 3299
    sheets.clock = lambda: NOW + 3299
    assert sink.send(make_event(event_id="e-late"), http, ctx).kind == DONE
    assert sheets.token_count == 1  # 3299 s: still inside the cache
    sink.clock = lambda: NOW + 3300
    sheets.clock = lambda: NOW + 3300
    assert sink.send(make_event(event_id="e-later"), http, ctx).kind == DONE
    assert sheets.token_count == 2  # 5 minutes before the 3600 s expiry: refreshed


def test_a_401_refreshes_the_token_once_and_a_second_401_is_dead(sheets, sa_json, store, monkeypatch):
    sink, http, ctx = fixed(sheets, store, sa_json)
    assert sink.send(make_event(), http, ctx).kind == DONE  # control
    sheets.tokens.clear()  # the vendor forgot our token
    out = sink.send(make_event(event_id="e2"), http, ctx)
    assert out.kind == DONE and sheets.token_count == 2  # one refresh and the retry went through
    monkeypatch.setattr(FakeSheets, "_authorised", lambda self, request: False)  # now even a fresh token is refused
    out = sink.send(make_event(event_id="e3"), http, ctx)
    assert out.kind == DEAD and out.status == 401 and out.reason == "http 401"
    assert sheets.token_count == 3  # one refresh was tried, then the second 401 stood


def test_the_token_endpoint_failures_never_write_a_row_and_map_to_pause_or_retry(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json)
    sheets.queue_token("invalid_grant")
    out = sink.send(make_event(), http, ctx)
    assert out.kind == PAUSE and "invalid_grant" in out.reason and out.status == 400
    sheets.queue_token("server_error")
    out = sink.send(make_event(), http, ctx)
    assert out.kind == RETRY and out.breaker_failure is True
    assert column_a(sheets) == []  # nothing was ever written
    assert sink.send(make_event(), http, ctx).kind == DONE  # control: the next token call works


# ---- the append -------------------------------------------------------------------------------------------------------------------


def test_the_append_request_is_raw_insert_rows_on_a_bare_sheet_name_with_a_bearer_token(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json)
    out = sink.send(make_event(), http, ctx)
    assert out.kind == DONE and out.effect_ref == "Sheet1!A1:L1" and sheets.effects == 1
    req = [r for r in sheets.requests if r["method"] == "POST"][0]
    assert req["path"] == f"/v4/spreadsheets/{SPREADSHEET_ID}/values/Sheet1!A:A:append"  # column A, which is never empty
    assert req["headers"]["authorization"].startswith("Bearer ya29.fake-token-")
    body = req["json"]
    assert list(body) == ["values"] and len(body["values"]) == 1 and all(isinstance(c, str) for c in body["values"][0])
    row = body["values"][0]
    assert row[0] == "evt-0001" and row[2] == "ContactCreate" and row[3] == "Pat Lead1" and row[4] == "pat1@example.com"


def test_the_query_carries_raw_and_insert_rows(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json)
    seen = []
    real = http.request

    def spy(method, url, **kw):
        seen.append((method, url))
        return real(method, url, **kw)

    http.request = spy  # type: ignore[method-assign]
    sink.send(make_event(), http, ctx)
    append_url = [u for m, u in seen if m == "POST" and "append" in u][0]
    assert "valueInputOption=RAW" in append_url and "insertDataOption=INSERT_ROWS" in append_url and "USER_ENTERED" not in append_url


def test_a_value_that_starts_with_an_equals_sign_is_stored_as_text(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json)
    p = payload(name="=HYPERLINK(\"http://evil.example\",\"click\")", email="=1+2")
    assert sink.send(make_event(p), http, ctx).kind == DONE
    stored = sheets.row_values(1)
    assert stored[3] == '=HYPERLINK("http://evil.example","click")' and stored[4] == "=1+2"  # text, never a formula
    assert "#FORMULA" not in stored


def test_a_cell_is_cut_to_49000_characters_and_the_lead_is_still_delivered(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json, options={"columns": ["event_id", "name", "email"]})
    p = payload(name="N" * 120_000)
    assert sink.send(make_event(p), http, ctx).kind == DONE
    assert len(sheets.row_values(1)[1]) == MAX_CELL_CHARS == 49_000
    assert sheets.row_values(1)[2] == "pat1@example.com"


def test_column_a_is_the_event_id_and_columns_are_configurable(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json, options={"columns": ["event_id", "email", "utm_campaign", "tags"]})
    p = payload(utm_campaign="spring", tags=["a", "b"])
    sink.send(make_event(p, event_id="evt-xyz"), http, ctx)
    assert sheets.row_values(1) == ["evt-xyz", "pat1@example.com", "spring", "a, b"]


def test_a_lead_with_every_optional_field_missing_still_appends(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json)
    for i, p in enumerate(({}, {"type": "ContactCreate"}, {"name": None, "email": None, "tags": None}, {"tags": [None, 3], "state": ["TX"]})):
        assert sink.send(make_event(p, event_id=f"e{i}"), http, ctx).kind == DONE, p
    assert column_a(sheets) == ["e0", "e1", "e2", "e3"]


# ---- the table must start at column A -----------------------------------------------------------------------------------------------


class MisplacedTable:
    """Wraps the Http seam and rewrites the answer of an append so it reports a table that starts at another column (the guard
    is defence in depth: the append range is column A, so the fake itself can no longer place a row anywhere else)."""

    def __init__(self, inner, table_range: str) -> None:
        self.inner, self.table_range = inner, table_range

    def request(self, method, url, **kw):
        res = self.inner.request(method, url, **kw)
        if method == "POST" and "append" in url and res.status == 200:
            doc = json.loads(res.text)
            doc["tableRange"] = self.table_range
            return HttpResult(status=200, headers=res.headers, text=json.dumps(doc))
        return res


@pytest.mark.parametrize("origin", ["C1", "B1"])
def test_a_table_that_does_not_start_at_column_a_is_refused_and_the_sink_stops_writing(sheets, sa_json, store, origin):
    sink, http, ctx = fixed(sheets, store, sa_json)
    http = MisplacedTable(http, f"Sheet1!{origin}:D5")  # the answer says the table begins at column B or C
    out = sink.send(make_event(event_id="e1"), http, ctx)
    assert out.kind == DEAD and out.reason == "table does not start at column A"
    writes = sheets.effects
    again = sink.send(make_event(event_id="e2"), http, ctx)
    assert again.kind == PAUSE and "column A" in again.reason and sheets.effects == writes  # no second misplaced row
    sink.clock = lambda: NOW + 901
    sheets.clock = lambda: NOW + 901
    third = sink.send(make_event(event_id="e3"), http, ctx)
    assert third.kind == DEAD and sheets.effects == writes + 1  # after 15 minutes it looks again


def test_a_table_that_starts_at_column_a_and_an_empty_sheet_are_accepted(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json)
    assert sink.send(make_event(event_id="e1"), http, ctx).kind == DONE  # empty sheet: no tableRange in the answer
    assert sink.send(make_event(event_id="e2"), http, ctx).kind == DONE  # tableRange begins at A
    assert table_start_column("Sheet1!A1:D5") == "A" and table_start_column("Sheet1!C1:D5") == "C" and table_start_column("") is None
    assert table_start_column("'My Sheet'!B2:C3") == "B" and table_start_column("garbage") == "?" and table_start_column(None) is None
    assert table_start_column("Sheet1!AB1:AC9") == "AB"


# ---- statuses ---------------------------------------------------------------------------------------------------------------------------


def test_outcomes_by_vendor_behaviour(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json)
    cases = [
        ("rate_limit", RETRY), ("server_error", AMBIGUOUS), ("act_then_fail", AMBIGUOUS), ("forbidden", PAUSE),
        ("notfound", DEAD), ("bad_range", PAUSE), ("bad_request", DEAD),
    ]
    for i, (mode, want) in enumerate(cases):
        assert sink.send(make_event(event_id=f"ok{i}"), http, ctx).kind == DONE  # positive control before every refusal
        sheets.queue(mode)
        out = sink.send(make_event(event_id=f"bad{i}"), http, ctx)
        assert out.kind == want, (mode, out)


def test_a_429_backs_off_with_the_documented_formula_and_marks_the_service_rate_limited(sheets, sa_json, store):
    for n in range(8):
        got = backoff_429(n, random.Random(n))
        assert min(2**n, 32.0) <= got <= min(2**n + 1, 32.0) and got <= 32.0
    assert len({backoff_429(3, random.Random(s)) for s in range(5)}) > 1  # there is jitter, not a fixed wait
    assert backoff_429(5000, random.Random(1)) == 32.0  # a rate limit that lasts for hours stays at the cap and never overflows
    sink, http, ctx = fixed(sheets, store, sa_json)
    ctx.retries = 3
    sheets.queue("rate_limit")
    out = sink.send(make_event(), http, ctx)
    assert out.kind == RETRY and out.rate_limited is True and 8.0 <= out.after_s <= 9.0 and out.status == 429
    ctx.retries = 20
    sheets.queue("rate_limit")
    assert sink.send(make_event(), http, ctx).after_s == 32.0  # capped


def test_a_5xx_and_a_timeout_are_ambiguous_and_a_refused_connection_is_a_retry(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json, timeout=0.4)
    sheets.queue("act_then_fail")
    out = sink.send(make_event(event_id="e1"), http, ctx)
    assert out.kind == AMBIGUOUS and out.status == 500 and sheets.effects == 1  # the row WAS written
    sheets.queue("hang")
    out = sink.send(make_event(event_id="e2"), http, ctx)
    assert out.kind == AMBIGUOUS and out.reason == "ReadTimeout" and out.maybe_acted is True
    sheets.release.set()
    dead = FakeSheets(sheets.public_key).start()
    base = dead.url
    dead.stop()
    refused = SinkContext(tenant="acme", url=base, timeout_s=0.5, extra=ctx.extra, lock=store.append_lock)
    out = SheetsSink(clock=lambda: NOW).send(make_event(), http, refused)
    assert out.kind == RETRY and out.breaker_failure is True


def test_a_secret_never_appears_in_an_outcome(sheets, sa_json, store, rsa_key):
    sink, http, ctx = fixed(sheets, store, sa_json)
    pem_line = json.loads(sa_json)["private_key"].splitlines()[1]
    for mode in ("rate_limit", "server_error", "forbidden", "notfound", "bad_range", "bad_request"):
        sheets.queue(mode)
        out = sink.send(make_event(event_id=f"e-{mode}"), http, ctx)
        text = f"{out.reason} {out.kind} {out.status}"
        assert pem_line not in text and SPREADSHEET_ID not in text and sheets.base not in text and "ya29" not in text


# ---- reading back --------------------------------------------------------------------------------------------------------------------------


def test_reconcile_finds_a_row_an_ambiguous_send_left_and_reports_absent_and_unknown_honestly(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json)
    ev = make_event(event_id="e-target")
    sheets.queue("act_then_fail")
    assert sink.send(ev, http, ctx).kind == AMBIGUOUS
    assert sink.reconcile(ev, http, ctx) == "found"
    assert sink.reconcile(make_event(event_id="e-other"), http, ctx) == "absent"
    sheets.queue("forbidden")
    assert sink.reconcile(ev, http, ctx) == "unknown"  # a failed read proves nothing
    sheets.queue("server_error")
    assert sink.reconcile(ev, http, ctx) == "unknown"


def test_reconcile_matches_the_whole_event_id_in_column_a_only(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json)
    sheets.seed([["evt-0001-extra"], ["prefix-evt-0001"]], "A1")
    sheets.seed([["evt-0001"]], "B9")  # right value, wrong column
    assert sink.reconcile(make_event(event_id="evt-0001"), http, ctx) == "absent"
    sheets.seed([["evt-0001"]], "A20")
    assert sink.reconcile(make_event(event_id="evt-0001"), http, ctx) == "found"


# ---- one append in flight per spreadsheet ---------------------------------------------------------------------------------------------------


def in_thread(fn):
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on join below
            box["error"] = exc

    t = threading.Thread(target=run)
    t.start()
    return t, box


def test_only_one_append_is_in_flight_per_spreadsheet_and_the_second_caller_waits_without_acting(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json, timeout=5.0)
    sheets.queue("late")  # held until released, then written and answered: a slow but healthy append
    first, box1 = in_thread(lambda: sink.send(make_event(event_id="e1"), Http(), sctx(sheets, sa_json, store, timeout=5.0)))
    try:
        wait_for(lambda: sheets.append_in_flight == 1, what="the first append to be in flight")
        out = sink.send(make_event(event_id="e2"), http, ctx)
        assert out.kind == RETRY and out.after_s == 2.0 and "another append" in out.reason and out.breaker_failure is False
        assert sheets.max_in_flight == 1
    finally:
        sheets.release.set()
        first.join(30)
    assert box1["value"].kind == DONE
    assert sink.send(make_event(event_id="e2"), http, ctx).kind == DONE  # the lock was released
    assert column_a(sheets) == ["e1", "e2"] and sheets.max_in_flight == 1


def test_the_lock_is_released_even_when_the_append_raises(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json)

    class Boom(Http):
        def request(self, method, url, **kw):
            if method == "POST" and "append" in url:
                raise RuntimeError("boom")
            return super().request(method, url, **kw)

    with pytest.raises(RuntimeError):
        sink.send(make_event(event_id="e1"), Boom(), ctx)
    assert sink.send(make_event(event_id="e2"), http, ctx).kind == DONE


def test_the_sink_refuses_to_run_without_the_append_lock(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json)
    ctx.lock = None
    with pytest.raises(RuntimeError, match="append lock"):
        sink.send(make_event(), http, ctx)


# ---- looking again before sending ------------------------------------------------------------------------------------------------------------


def test_a_try_after_an_ambiguous_one_finds_the_row_and_does_not_append_again(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json)
    sheets.queue("act_then_fail")
    sink.send(make_event(event_id="e1"), http, ctx)
    again = sink.send(make_event(event_id="e1"), http, sctx(sheets, sa_json, store, maybe_acted_before=True, since_maybe_acted_s=5.0))
    assert again.kind == DONE and again.effect_ref == "reconciled" and column_a(sheets) == ["e1"]


def test_an_absent_read_back_counts_only_after_sixty_seconds(sheets, sa_json, store):
    sink, http, _ = fixed(sheets, store, sa_json)
    assert ABSENT_AFTER_S == 60.0
    for since, appends in ((10.0, False), (59.9, False), (60.0, True)):
        sheets.reset()
        sheets.clock = lambda: NOW
        out = sink.send(make_event(event_id="e1"), http, sctx(sheets, sa_json, store, maybe_acted_before=True, since_maybe_acted_s=since))
        if appends:
            assert out.kind == DONE and column_a(sheets) == ["e1"], since
        else:
            assert out.kind == RETRY and 0.0 < out.after_s <= 50.1 and column_a(sheets) == [], since
            assert "waiting" in out.reason
    waited = sink.send(make_event(event_id="e2"), http, sctx(sheets, sa_json, store, maybe_acted_before=True, since_maybe_acted_s=20.0))
    assert waited.after_s == pytest.approx(40.0)


def test_a_read_back_that_fails_blocks_the_append_and_a_forbidden_one_pauses(sheets, sa_json, store):
    sink, http, _ = fixed(sheets, store, sa_json)
    c = lambda: sctx(sheets, sa_json, store, maybe_acted_before=True, since_maybe_acted_s=120.0)
    assert sink.send(make_event(event_id="e0"), http, c()).kind == DONE  # control: absent for 120 s means append
    for mode, want in (("server_error", RETRY), ("rate_limit", RETRY), ("notfound", RETRY), ("forbidden", PAUSE)):
        sheets.queue(mode)
        out = sink.send(make_event(event_id="e1"), http, c())
        assert out.kind == want, mode
    assert column_a(sheets) == ["e0"]  # never appended on a read it could not trust


def test_a_row_that_lands_while_waiting_for_the_lock_is_noticed_by_the_second_look(sheets, sa_json, store):
    sink, http, _ = fixed(sheets, store, sa_json)
    ctx = sctx(sheets, sa_json, store, maybe_acted_before=True, since_maybe_acted_s=120.0)
    reads = {"n": 0}
    real = http.request

    def spy(method, url, **kw):
        res = real(method, url, **kw)
        if method == "GET":
            reads["n"] += 1
            if reads["n"] == 1:  # after the first look came back empty and before the append, the late request lands
                sheets.seed([["e1", "late"]], "A1")
        return res

    http.request = spy  # type: ignore[method-assign]
    out = sink.send(make_event(event_id="e1"), http, ctx)
    assert out.kind == DONE and out.effect_ref == "reconciled" and column_a(sheets) == ["e1"] and reads["n"] == 2


def test_a_first_try_does_not_read_the_sheet_at_all(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json)
    sink.send(make_event(), http, ctx)
    assert [r["method"] for r in sheets.requests] == ["POST"]


def test_the_lock_is_taken_per_spreadsheet(sheets, sa_json, store):
    sink, http, ctx = fixed(sheets, store, sa_json)
    keys = []
    real = store.append_lock

    def spy(key, ttl):
        keys.append((key, ttl))
        return real(key, ttl)

    ctx.lock = spy
    assert sink.send(make_event(), http, ctx).kind == DONE
    assert keys == [(f"sheets:{SPREADSHEET_ID}", ctx.timeout_s * 4 + 10)]


def test_a_lost_claim_beats_the_sixty_second_wait_because_its_lease_already_outlasted_it(sheets, sa_json, store):
    sink, http, _ = fixed(sheets, store, sa_json)
    ctx = sctx(sheets, sa_json, store, prior_claim_lost=True, maybe_acted_before=True, since_maybe_acted_s=5.0)
    out = sink.send(make_event(event_id="e9"), http, ctx)
    assert out.kind == DONE and column_a(sheets) == ["e9"]  # the earlier ambiguous mark was 5 s old, the dead worker's lease was not


def test_a_worker_that_died_mid_call_counts_as_a_try_that_may_have_written(sheets, sa_json, store):
    sink, http, _ = fixed(sheets, store, sa_json)
    sheets.seed([["e1"]], "A1")
    out = sink.send(make_event(event_id="e1"), http, sctx(sheets, sa_json, store, prior_claim_lost=True))
    assert out.kind == DONE and out.effect_ref == "reconciled" and column_a(sheets) == ["e1"]
    # and when the row is not there, the crash's own lease already outlasted 60 seconds, so it appends
    gone = sink.send(make_event(event_id="e2"), http, sctx(sheets, sa_json, store, prior_claim_lost=True))
    assert gone.kind == DONE and gone.effect_ref != "reconciled" and column_a(sheets) == ["e1", "e2"]


# ---- through the worker: crash and late landing ----------------------------------------------------------------------------------------------------------


def sheets_cfg(sheets: FakeSheets, sa_json: str, timeout: float = 5.0, **opts):
    sink_cfg = {
        "url_env": "ACME_SHEETS_URL", "env": {"spreadsheet_id": "ACME_SHEET_ID", "service_account": "ACME_SA_JSON"},
        "options": {"sheet": "Sheet1", **opts}, "timeout_s": timeout,
    }
    return make_cfg(None, None, extra_sinks={"sheets": sink_cfg}, env_extra={"ACME_SHEETS_URL": sheets.url, "ACME_SHEET_ID": SPREADSHEET_ID, "ACME_SA_JSON": sa_json})


def test_a_delivery_lands_one_row_through_the_worker(store, sheets, sa_json):
    cfg = sheets_cfg(sheets, sa_json)
    rig = make_rig(store, cfg, registry={"sheets": SheetsSink()})
    r = ingest_one(store, cfg)
    ingest_one(store, cfg)  # a duplicate delivery
    rig.settle()
    d = store.delivery(r.event_id, "sheets")
    assert d["state"] == "done" and column_a(sheets) == [r.event_id] and sheets.effects == 1


def test_a_crash_after_the_append_and_before_the_commit_leaves_exactly_one_row(store, sheets, sa_json):
    cfg = sheets_cfg(sheets, sa_json)
    fired = {"n": 0}

    def crash(claim, outcome):
        if fired["n"] == 0:
            fired["n"] += 1
            raise SimulatedCrash("worker died after the append")

    rig = make_rig(store, cfg, registry={"sheets": SheetsSink()}, hooks=Hooks(after_send=crash))
    r = ingest_one(store, cfg)
    with pytest.raises(SimulatedCrash):
        rig.worker.run_once(wait_horizon_s=0)
    assert sheets.effects == 1 and store.delivery(r.event_id, "sheets")["state"] == "pending"
    store.advance(301)  # the lease ran out
    restarted = make_rig(store, cfg, registry={"sheets": SheetsSink()})
    restarted.settle()
    assert store.delivery(r.event_id, "sheets")["state"] == "done" and store.delivery(r.event_id, "sheets")["effect_ref"] == "reconciled"
    assert column_a(sheets) == [r.event_id] and sheets.effects == 1  # exactly one row


def test_a_crash_before_the_append_still_lands_exactly_one_row(store, sheets, sa_json):
    cfg = sheets_cfg(sheets, sa_json)
    fired = {"n": 0}

    def crash(claim):
        if fired["n"] == 0:
            fired["n"] += 1
            raise SimulatedCrash("worker died before the append")

    rig = make_rig(store, cfg, registry={"sheets": SheetsSink()}, hooks=Hooks(before_send=crash))
    r = ingest_one(store, cfg)
    with pytest.raises(SimulatedCrash):
        rig.worker.run_once(wait_horizon_s=0)
    store.advance(301)
    make_rig(store, cfg, registry={"sheets": SheetsSink()}).settle()
    assert column_a(sheets) == [r.event_id] and store.delivery(r.event_id, "sheets")["state"] == "done"


def test_a_timeout_and_then_a_late_landing_append_still_leaves_exactly_one_row(store, sheets, sa_json):
    cfg = sheets_cfg(sheets, sa_json, timeout=0.4)
    rig = make_rig(store, cfg, registry={"sheets": SheetsSink()})
    r = ingest_one(store, cfg)
    sheets.queue("late")
    rig.worker.run_once(wait_horizon_s=0)
    d = store.delivery(r.event_id, "sheets")
    assert d["state"] == "pending" and column_a(sheets) == [] and d["last_error"] == "reconciled absent"  # the read-back found nothing yet
    sheets.release.set()  # the slow append now lands
    wait_for(lambda: column_a(sheets) == [r.event_id], what="the late append to land")
    store.advance(10)  # well inside the 60 second window
    rig.settle()
    assert store.delivery(r.event_id, "sheets")["state"] == "done" and column_a(sheets) == [r.event_id]
    assert sheets.effects == 1


def test_a_timeout_where_the_append_never_lands_is_retried_only_after_sixty_seconds(store, sheets, sa_json):
    cfg = sheets_cfg(sheets, sa_json, timeout=0.4)
    rig = make_rig(store, cfg, registry={"sheets": SheetsSink()})
    r = ingest_one(store, cfg)
    sheets.queue("hang")
    rig.worker.run_once(wait_horizon_s=0)
    assert store.delivery(r.event_id, "sheets")["state"] == "pending" and column_a(sheets) == []
    store.advance(store.due_in_s("sheets") + 0.01)  # the retry is due 30 to 60 seconds after the ambiguous try
    rig.worker.run_once(wait_horizon_s=0)
    d = store.delivery(r.event_id, "sheets")
    assert d["state"] == "pending" and d["last_error"] and "waiting" in d["last_error"] and d["retries"] == 2
    assert column_a(sheets) == [] and sheets.effects == 0  # under 60 seconds: the earlier append could still land, so nothing is sent
    store.advance(store.due_in_s("sheets") + 0.01)
    rig.settle()
    sheets.release.set()
    assert store.delivery(r.event_id, "sheets")["state"] == "done" and column_a(sheets) == [r.event_id]


def test_many_leads_against_a_failing_sheet_each_land_exactly_once(store, sheets, sa_json):
    cfg = sheets_cfg(sheets, sa_json)
    rig = make_rig(store, cfg, seed=3, registry={"sheets": SheetsSink(rng=random.Random(3))})
    ids = [ingest_one(store, cfg, payload(i)).event_id for i in range(6)]
    sheets.queue("rate_limit", "act_then_fail", "server_error", "act_then_fail", "rate_limit")
    rig.settle(jump_s=200)
    assert {store.delivery(i, "sheets")["state"] for i in ids} == {"done"}
    assert sorted(column_a(sheets)) == sorted(ids)  # one row per lead even though two appends answered 500 after writing


# ---- configuration --------------------------------------------------------------------------------------------------------------------------------


def base_env(sheets: FakeSheets, sa_json: str, **opts) -> dict:
    sink_cfg = {"url_env": "ACME_SHEETS_URL", "env": {"spreadsheet_id": "ACME_SHEET_ID", "service_account": "ACME_SA_JSON"}, "options": {"sheet": "Sheet1", **opts}}
    env = {"RELAY_ENV": "test", "RELAY_INGEST_TOKEN": "i" * 20, "ACME_WORKFLOW_SECRET": "w" * 20, "ACME_SHEETS_URL": sheets.url, "ACME_SHEET_ID": SPREADSHEET_ID, "ACME_SA_JSON": sa_json}
    env["RELAY_CONFIG"] = json.dumps({"locations": {"L": "acme"}, "tenants": {"acme": {"workflow_secret_env": "ACME_WORKFLOW_SECRET", "sinks": {"sheets": sink_cfg}}}})
    return env


def edit_cfg(env: dict, fn) -> dict:
    doc = json.loads(env["RELAY_CONFIG"])
    fn(doc["tenants"]["acme"]["sinks"]["sheets"])
    return {**env, "RELAY_CONFIG": json.dumps(doc)}


def test_the_sheets_sink_loads_and_registers_the_key_material_as_secrets(sheets, sa_json):
    cfg = load_config(base_env(sheets, sa_json))
    s = cfg.tenants["acme"].sinks["sheets"]
    assert s.timeout_s == 10.0 and s.min_interval_s == 1.0 and s.max_age_s == 72 * 3600.0
    assert s.extra["sheet"] == "Sheet1" and s.extra["columns"][0] == "event_id" and s.extra["token_uri"] == sheets.token_url
    secrets = cfg.secret_values()
    pem = json.loads(sa_json)["private_key"]
    assert sa_json in secrets and pem in secrets and SPREADSHEET_ID in secrets
    body_lines = [l for l in pem.splitlines() if "PRIVATE KEY" not in l and len(l) >= 16]
    assert body_lines and all(l in secrets for l in body_lines)


@pytest.mark.parametrize("title", ["My Sheet", "'Sheet1'", 'Sheet"1', "Sheet 1", "Sheet-1", "1Sheet", "", "Sheet1!A1", "Sheet1:append"])
def test_a_sheet_title_with_a_space_a_quote_or_other_characters_is_refused(sheets, sa_json, title):
    load_config(base_env(sheets, sa_json))  # control: a bare name loads
    with pytest.raises(ConfigError, match="bare sheet name"):
        load_config(base_env(sheets, sa_json, sheet=title))


def test_config_validates_columns_the_service_account_and_literal_identifiers(sheets, sa_json):
    env = base_env(sheets, sa_json)
    load_config(env)
    with pytest.raises(ConfigError, match="must start with event_id"):
        load_config(base_env(sheets, sa_json, columns=["email", "event_id"]))
    with pytest.raises(ConfigError, match="options.columns must be a list"):
        load_config(base_env(sheets, sa_json, columns=["event_id", "ssn"]))
    with pytest.raises(ConfigError, match="names a column twice"):
        load_config(base_env(sheets, sa_json, columns=["event_id", "email", "email"]))
    with pytest.raises(ConfigError, match="unknown option"):
        load_config(base_env(sheets, sa_json, tab="x"))
    with pytest.raises(ConfigError, match="would hold an identifier"):
        load_config(edit_cfg(env, lambda s: s["options"].update(spreadsheet_id="1AbCdEf")))
    with pytest.raises(ConfigError, match="would hold an identifier"):
        load_config(edit_cfg(env, lambda s: s["options"].update(service_account="{}")))
    with pytest.raises(ConfigError, match="env.service_account is required"):
        load_config(edit_cfg(env, lambda s: s["env"].pop("service_account")))
    for bad in ("not json", "[]", json.dumps({"client_email": "a@b.c"}), json.dumps({"client_email": "a@b.c", "private_key": "nope"})):
        with pytest.raises(ConfigError, match="env.service_account"):
            load_config({**env, "ACME_SA_JSON": bad})
    ec = elliptic.generate_private_key(elliptic.SECP256R1())
    ec_pem = ec.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    with pytest.raises(ConfigError, match="must be RSA"):
        load_config({**env, "ACME_SA_JSON": json.dumps({"client_email": "a@b.c", "private_key": ec_pem})})


def test_a_real_google_token_uri_or_base_url_is_refused_without_relay_live(sheets, rsa_key):
    real_sa = service_account_json(rsa_key, "https://oauth2.googleapis.com/token")
    env = base_env(sheets, real_sa)
    with pytest.raises(ConfigError, match="RELAY_LIVE"):
        load_config(env)
    with pytest.raises(ConfigError, match="RELAY_LIVE"):
        load_config({**base_env(sheets, service_account_json(rsa_key, sheets.token_url)), "ACME_SHEETS_URL": "https://sheets.googleapis.com"})
    live = load_config({**env, "ACME_SHEETS_URL": "https://sheets.googleapis.com", "RELAY_LIVE": "1"})  # control: both load when live is declared
    assert live.tenants["acme"].sinks["sheets"].extra["token_uri"] == "https://oauth2.googleapis.com/token"


def test_the_real_token_uri_is_the_aud_claim_a_real_key_file_would_carry(rsa_key):
    acct = parse_account(service_account_json(rsa_key, sheets_mod.DEFAULT_TOKEN_URI))
    assert sheets_mod.build_claims(acct, NOW)["aud"] == "https://oauth2.googleapis.com/token"
    no_uri = json.loads(service_account_json(rsa_key, "x"))
    del no_uri["token_uri"]
    assert parse_account(json.dumps(no_uri)).token_uri == sheets_mod.DEFAULT_TOKEN_URI


# ---- secrets --------------------------------------------------------------------------------------------------------------------------------------------


def test_no_key_material_token_or_spreadsheet_id_reaches_a_log_line_or_a_database_column(store, sheets, sa_json, caplog):
    from relay.app import Deps, create_app

    cfg = sheets_cfg(sheets, sa_json)
    redact.uninstall()
    create_app(cfg, Deps(store=store))
    rig = make_rig(store, cfg, registry={"sheets": SheetsSink()})
    sheets.queue_token("server_error", "invalid_grant")
    with caplog.at_level(logging.DEBUG):
        r = ingest_one(store, cfg)
        rig.worker.run_once(wait_horizon_s=0)  # server_error at the token endpoint
        rig.worker.run_once(wait_horizon_s=0)
        sheets.queue("rate_limit", "act_then_fail")
        rig.settle(jump_s=120)
    assert store.delivery(r.event_id, "sheets")["state"] == "done"
    pem = json.loads(sa_json)["private_key"]
    logs = "\n".join(f"{x.name} {x.getMessage()} {x.exc_text or ''}" for x in caplog.records if not x.name.startswith("werkzeug"))
    db = "\n".join(store.dump_all_text())
    assert any(x.name == "relay.fanout" for x in caplog.records) and r.event_id in db  # both sweeps saw real output
    for secret in [sa_json, pem, SPREADSHEET_ID, *[l for l in pem.splitlines() if len(l) >= 16 and "PRIVATE KEY" not in l]] + [t for t in sheets.tokens]:
        assert secret not in logs and secret not in db


# ---- the doc fixtures agree with the code ------------------------------------------------------------------------------------------------------------------


def test_the_append_fixture_matches_the_sink():
    doc = json.loads((CONTRACTS / "append.doc.json").read_text(encoding="utf-8"))
    assert doc["scope"] == SCOPE and doc["quotas_per_minute"]["user_per_project"] == 60 and SheetsSink.default_min_interval_s == 1.0
    assert doc["cell_character_limit"] == 50_000 and MAX_CELL_CHARS < doc["cell_character_limit"]
    assert "valueInputOption" in doc["required_query"] and "=1+2" in doc["value_input_option"]["RAW"]


def test_the_statuses_fixture_says_which_codes_are_assumed():
    doc = json.loads((CONTRACTS / "statuses.assumed.json").read_text(encoding="utf-8"))
    assert set(doc["assumed"]) == {"401", "403"} and "429" in doc["documented"] and doc["provenance"].startswith("ASSUMED")
    assert set(doc["observed_2026_10_23"]) == {"400", "401", "403", "404", "429"}  # what moved out of ASSUMED, each backed by errors.observed.json


def test_parse_account_never_puts_key_material_in_its_errors(rsa_key):
    fake_pem = "-----BEGIN " + "PRIVATE KEY-----\\nSECRETSECRET\\n-----END " + "PRIVATE KEY-----"  # assembled, so this file holds no key block
    for bad in ("{not json", '{"client_email": "a@b.c", "private_key": "' + fake_pem + '"}'):
        with pytest.raises(ValueError) as exc:
            parse_account(bad)
        assert "SECRETSECRET" not in str(exc.value)
