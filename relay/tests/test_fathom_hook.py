"""Standard Webhooks verification for Fathom, as a pure function, with signatures built by code that does not import the relay
(tests/fakes/fathom.py). Each rule here has a mutant."""

from __future__ import annotations

import base64
import hmac
import json
import random

import pytest

from relay import fathom_hook
from relay.fathom_hook import normalise_meeting, parse_secret, sign, verify

from .fakes.fathom import WEBHOOK_SECRET, make_meeting, sign_webhook

def _key() -> bytes:
    return parse_secret(WEBHOOK_SECRET)  # called inside each test, so a broken parse_secret fails a test and not the whole import

NOW = 1_760_000_000.0
BODY = json.dumps({"recording_id": 5001, "title": "Weekly sync"}).encode()


def headers(body: bytes = BODY, *, mid: str = "msg_1", ts: int | None = None, secret: str = WEBHOOK_SECRET, signature: str | None = None) -> dict[str, str]:
    ts = int(NOW) if ts is None else ts
    return {"webhook-id": mid, "webhook-timestamp": str(ts), "webhook-signature": signature if signature is not None else sign_webhook(secret, mid, ts, body)}


def ok(h, body=BODY, tolerance=300.0, now=NOW):
    return verify(_key(), h, body, now, tolerance)


def test_a_correctly_signed_delivery_is_accepted():
    assert ok(headers()) is None


def test_the_signature_the_relay_makes_equals_the_independent_signer():
    for i in range(20):
        body = bytes(random.Random(i).randrange(256) for _ in range(random.Random(i).randrange(0, 400)))
        mid, ts = f"msg_{i}", 1_700_000_000 + i
        assert "v1," + sign(_key(), mid, str(ts), body) == sign_webhook(WEBHOOK_SECRET, mid, ts, body)


def test_header_names_are_read_case_insensitively():
    h = {k.title(): v for k, v in headers().items()}
    assert ok(h) is None
    assert ok({k.upper(): v for k, v in headers().items()}) is None


@pytest.mark.parametrize("missing", ["webhook-id", "webhook-timestamp", "webhook-signature"])
def test_a_missing_header_is_refused(missing):
    assert ok(headers()) is None  # control
    h = headers()
    del h[missing]
    assert ok(h) == "missing-header"
    assert ok({**headers(), missing: ""}) == "missing-header"


def test_an_overlong_webhook_id_is_refused():
    assert ok(headers(mid="m" * 128)) is None
    assert ok(headers(mid="m" * 129)) == "missing-header"


def test_a_forged_signature_is_refused():
    assert ok(headers()) is None  # control
    wrong_key = "whsec_" + base64.b64encode(b"another-secret-of-32-bytes-long!!").decode()
    assert ok(headers(secret=wrong_key)) == "bad-signature"
    assert ok(headers(signature="v1," + base64.b64encode(b"x" * 32).decode())) == "bad-signature"
    assert ok(headers(signature="v1,")) == "bad-signature"
    assert ok(headers(signature="garbage")) == "bad-signature"


def test_the_signature_covers_the_body_the_id_and_the_timestamp():
    h = headers()
    assert ok(h) is None  # control
    assert ok(h, body=BODY + b" ") == "bad-signature"
    assert ok({**h, "webhook-id": "msg_2"}) == "bad-signature"
    assert ok({**h, "webhook-timestamp": str(int(NOW) + 1)}) == "bad-signature"


def test_the_signature_is_over_the_raw_bytes_not_a_reserialised_body():
    spaced = b'{ "recording_id" : 5001,\n  "title" : "Weekly sync" }'
    compact = json.dumps(json.loads(spaced)).encode()
    assert spaced != compact and json.loads(spaced) == json.loads(compact)
    h = headers(spaced)
    assert ok(h, body=spaced) is None  # the raw bytes that were signed
    assert ok(h, body=compact) == "bad-signature"  # the same JSON, different bytes: a re-serialising receiver would accept this
    assert ok(headers(compact), body=spaced) == "bad-signature"


def test_the_signed_text_is_not_decoded_and_encoded_again():
    body = '{"title": "café ☃ \U0001F600"}'.encode("utf-8")
    assert ok(headers(body), body=body) is None
    assert ok(headers(body), body=body.decode("utf-8").encode("utf-16")) == "bad-signature"


def test_a_body_that_is_not_valid_utf8_is_verified_byte_for_byte():
    body = b"\xff\xfe{\x00}\x80"
    assert ok(headers(body), body=body) is None  # a receiver that decoded the body to text and back would change these bytes


@pytest.mark.parametrize("ts", ["", "abc", "-5", "+5", "1.5", " 1760000000", "1760000000 ", "١٢٣", "0x10", "1e9"])
def test_a_timestamp_that_is_not_plain_digits_is_refused(ts):
    h = headers()
    assert ok(h) is None  # control
    assert ok({**h, "webhook-timestamp": ts}) in ("bad-timestamp", "missing-header")


def test_the_timestamp_must_be_inside_the_tolerance_in_both_directions():
    assert ok(headers(ts=int(NOW) - 300)) is None and ok(headers(ts=int(NOW) + 300)) is None  # exactly on the edge
    assert ok(headers(ts=int(NOW) - 301)) == "timestamp-outside-window"
    assert ok(headers(ts=int(NOW) + 301)) == "timestamp-outside-window"  # a timestamp from the future is a replay too
    assert ok(headers(ts=int(NOW) - 100), tolerance=60.0) == "timestamp-outside-window"
    assert ok(headers(ts=int(NOW) - 100), tolerance=120.0) is None
    assert ok(headers(ts=0)) == "timestamp-outside-window"


def test_every_v1_signature_in_the_header_is_checked_not_only_the_first():
    good = sign_webhook(WEBHOOK_SECRET, "msg_1", int(NOW), BODY)
    bad = "v1," + base64.b64encode(b"y" * 32).decode()
    assert ok(headers(signature=good)) is None  # control
    for header in (f"{good} {bad}", f"{bad} {good}", f"{bad} {bad} {bad} {good}", f"{bad} {good} {bad}"):
        assert ok(headers(signature=header)) is None, header
    assert ok(headers(signature=f"{bad} {bad}")) == "bad-signature"


def test_only_v1_entries_count():
    good = sign_webhook(WEBHOOK_SECRET, "msg_1", int(NOW), BODY).split(",", 1)[1]
    assert ok(headers(signature=f"v1,{good}")) is None  # control
    for header in (f"v2,{good}", f"v1a,{good}", f"V1,{good}", good, f"v1{good}", f",{good}", f"v1;{good}"):
        assert ok(headers(signature=header)) == "bad-signature", header
    assert ok(headers(signature=f"v2,{good} v1,{good}")) is None


def test_odd_whitespace_and_non_ascii_entries_neither_raise_nor_match():
    good = sign_webhook(WEBHOOK_SECRET, "msg_1", int(NOW), BODY)
    assert ok(headers(signature=f"  {good}   ")) is None  # extra spaces make empty entries, which are skipped
    assert ok(headers(signature="v1,café v1,☃")) == "bad-signature"
    assert ok(headers(signature="v1,café " + good)) is None


def test_each_v1_entry_is_compared_in_constant_time_with_no_early_exit(monkeypatch):
    calls: list[tuple[bytes, bytes]] = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(fathom_hook.hmac, "compare_digest", spy)
    good = sign_webhook(WEBHOOK_SECRET, "msg_1", int(NOW), BODY)
    bad = "v1," + base64.b64encode(b"z" * 32).decode()
    assert ok(headers(signature=f"{good} {bad} {bad}")) is None
    assert len(calls) == 3  # the first entry already matched and the other two were still compared
    calls.clear()
    assert ok(headers(signature=f"v2,{good.split(',')[1]} {bad}")) == "bad-signature"
    assert len(calls) == 1  # the v2 entry is not compared at all
    assert all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)


# ---- the secret ---------------------------------------------------------------------------------------------------------------


def test_parse_secret_strips_the_prefix_and_decodes_base64():
    assert parse_secret(WEBHOOK_SECRET) == b"fake-fathom-hook-secret-bytes-32!"
    assert parse_secret("whsec_" + base64.b64encode(b"s" * 16).decode()) == b"s" * 16


@pytest.mark.parametrize("bad", ["", "abc", "whsec_", "whsec_!!!", "whsec_" + base64.b64encode(b"s" * 15).decode(), "WHSEC_" + base64.b64encode(b"s" * 20).decode(), None, 5])
def test_parse_secret_refuses_anything_else_without_echoing_it(bad):
    parse_secret(WEBHOOK_SECRET)  # control
    with pytest.raises(ValueError) as err:
        parse_secret(bad)
    assert str(bad) not in str(err.value) or str(bad) in ("", "None", "5")


# ---- the reduced record --------------------------------------------------------------------------------------------------------


def test_a_meeting_is_reduced_and_bounded():
    full = make_meeting(1)
    full["transcript"] = [{"text": "secret words"}]
    m = normalise_meeting(full)
    assert m["recording_id"] == "5001" and m["title"] == "Weekly sync 1" and m["summary"].startswith("## Summary")
    assert "transcript" not in m and "secret words" not in json.dumps(m)
    assert m["url"] == "https://fathom.video/calls/fake1"
    long = normalise_meeting({"recording_id": 1, "title": "t" * 1000, "default_summary": {"markdown_formatted": "s" * 50000}})
    assert len(long["title"]) == 300 and len(long["summary"]) == 20000


def test_a_meeting_needs_a_usable_recording_id():
    assert normalise_meeting({"recording_id": 7})["recording_id"] == "7"
    assert normalise_meeting({"recording_id": " abc "})["recording_id"] == "abc"
    for bad in ({}, {"recording_id": None}, {"recording_id": ""}, {"recording_id": "  "}, {"recording_id": True}, {"recording_id": [1]}, {"recording_id": 1.5}, [], "x", None):
        assert normalise_meeting(bad) is None, bad


def test_a_url_is_kept_only_when_it_is_https_and_short():
    assert normalise_meeting({"recording_id": 1, "url": "https://fathom.video/x"})["url"] == "https://fathom.video/x"
    for bad in ("http://fathom.video/x", "javascript:alert(1)", "https://" + "a" * 600, 5, None):
        assert normalise_meeting({"recording_id": 1, "url": bad})["url"] is None, bad


def test_action_items_keep_a_description_a_flag_and_a_first_name_only():
    items = [
        {"description": "do it", "completed": True, "assignee": {"name": "Jane", "email": "jane@example.test", "team": "Ops"}},
        {"description": "", "completed": False},
        {"description": 5},
        "not an object",
        {"description": "no assignee", "assignee": None},
    ] + [{"description": f"item {i}"} for i in range(80)]
    m = normalise_meeting({"recording_id": 1, "action_items": items})
    assert m["action_items"][0] == {"description": "do it", "completed": True, "assignee": "Jane"}
    assert m["action_items"][1] == {"description": "no assignee", "completed": False, "assignee": None}
    assert len(m["action_items"]) == 50
    assert "jane@example.test" not in json.dumps(m)
    assert normalise_meeting({"recording_id": 1, "action_items": None})["action_items"] is None
    assert fathom_hook.action_items_text({"action_items": None}) is None
    assert json.loads(fathom_hook.action_items_text(m))[0]["assignee"] == "Jane"


# ---- seeded property test ----------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(40))
def test_property_only_a_delivery_signed_with_the_secret_inside_the_window_is_ever_accepted(seed):
    """Random bodies, ids, timestamps, secrets and signature lists. The test decides what SHOULD pass from its own model
    (a v1 entry equal to the independent signer's output, a timestamp within tolerance) and compares the verdict."""
    rng = random.Random(seed)
    other = "whsec_" + base64.b64encode(bytes(rng.randrange(256) for _ in range(24))).decode()
    for _ in range(30):
        body = bytes(rng.randrange(32, 127) for _ in range(rng.randrange(0, 200)))
        mid = f"msg_{rng.randrange(10**6)}"
        skew = rng.choice([0, 0, 5, 299, 300, 301, -299, -300, -301, 4000])
        ts = int(NOW) + skew
        entries = []
        should_pass = abs(skew) <= 300
        valid_present = False
        for _e in range(rng.randrange(1, 5)):
            kind = rng.choice(["good", "wrong_secret", "wrong_body", "v2", "junk"])
            if kind == "good":
                entries.append(sign_webhook(WEBHOOK_SECRET, mid, ts, body))
                valid_present = True
            elif kind == "wrong_secret":
                entries.append(sign_webhook(other, mid, ts, body))
            elif kind == "wrong_body":
                entries.append(sign_webhook(WEBHOOK_SECRET, mid, ts, body + b"x"))
            elif kind == "v2":
                entries.append(sign_webhook(WEBHOOK_SECRET, mid, ts, body, versions=("v2",)))
            else:
                entries.append("v1,junk" + str(rng.randrange(100)))
        rng.shuffle(entries)
        h = {"webhook-id": mid, "webhook-timestamp": str(ts), "webhook-signature": " ".join(entries)}
        verdict = verify(_key(), h, body, NOW, 300.0)
        assert (verdict is None) == (should_pass and valid_present), (seed, skew, entries)
