"""Slack and Make sinks against their fakes, plus the doc fixtures.

Every test that touches a fake gets it through a fixture that already ran a positive control (a valid request must be
accepted), so a broken request cannot pass for a refusal.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path

import httpx
import pytest

from relay import ghl
from relay.http import Http
from relay.sinks.base import AMBIGUOUS, DEAD, DONE, PAUSE, RETRY, Event, SinkContext
from relay.sinks.make import MAX_BODY_BYTES, MakeSink
from relay.sinks.slack import SlackSink, build_message, escape, truncate_escaped

from .fakes.make import FakeMake
from .fakes.slack import FakeSlack
from .helpers import payload

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"


def fixture(vendor: str, name: str):
    return json.loads((CONTRACTS / vendor / name).read_text(encoding="utf-8"))


def event_for(p: dict | None = None, event_id: str = "a1b2c3d4e5f60718") -> Event:
    p = p if p is not None else payload()
    return Event(event_id=event_id, tenant="acme", source="ingest", payload=p, lead=ghl.normalise_lead(p))


def ctx(url: str, timeout: float = 2.0, apikey: str | None = None) -> SinkContext:
    return SinkContext(tenant="acme", url=url, timeout_s=timeout, apikey=apikey)


def refused_url() -> str:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}/nothing-listens-here"


# ---- Slack: the fake against the documentation -----------------------------------------------------------------------


def test_slack_doc_examples_are_accepted_by_a_fake_without_the_relay_rules():
    fake = FakeSlack(strict=False).start()
    try:
        hello = fixture("slack", "hello.doc.json")
        assert httpx.post(fake.url, json=hello["request"]).text == hello["response"]["body"]
        res = httpx.post(fake.url, json=fixture("slack", "review_blocks.doc.json"))
        assert res.status_code == 200 and res.text == "ok"
        assert fake.effects == 2
    finally:
        fake.stop()


def test_slack_fake_refuses_what_the_relay_must_never_send(slack):
    good = {"text": "x", "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "fine", "verbatim": True}}]}
    assert httpx.post(slack.url, json=good).status_code == 200  # control inside the test, same payload shape
    bad_cases = {
        "raw less-than": {"text": "a <b> c"},
        "bare ampersand": {"text": "a & b"},
        "no verbatim": {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "fine"}}]},
        "section over 3000": {"blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "x" * 3001, "verbatim": True}}]},
        "header over 150": {"blocks": [{"type": "header", "text": {"type": "plain_text", "text": "x" * 151}}]},
        "51 blocks": {"blocks": [{"type": "divider"}] * 51},
        "empty": {},
    }
    for name, body in bad_cases.items():
        res = httpx.post(slack.url, json=body)
        assert res.status_code == 400, name


def test_slack_fake_records_an_effect_only_for_accepted_messages(slack):
    httpx.post(slack.url, json={"text": "ok one"})
    httpx.post(slack.url, json={"text": "a < b"})
    assert slack.effects == 1 and len(slack.requests) == 2


# ---- Slack: the sink --------------------------------------------------------------------------------------------------


def test_slack_send_is_done_and_the_message_is_well_formed(slack, http):
    out = SlackSink().send(event_for(), http, ctx(slack.url))
    assert out.kind == DONE and slack.effects == 1
    sent = slack.requests[0]["json"]
    blocks = sent["blocks"]
    assert blocks[0]["type"] == "header" and "Pat Lead1" in blocks[0]["text"]["text"]
    footer = blocks[-1]["elements"][0]["text"]
    assert "a1b2c3d4" in footer and "a1b2c3d4e5" not in footer  # first 8 characters of the event id, no more
    assert all(b["text"]["verbatim"] is True for b in blocks if b["type"] == "section")


def test_slack_untrusted_text_is_escaped(slack, http):
    p = payload(name="<!channel> & <https://evil.example|click>", email="a&b@example.com")
    out = SlackSink().send(event_for(p), http, ctx(slack.url))
    assert out.kind == DONE
    text = json.dumps(slack.requests[0]["json"]["blocks"][1])
    assert "&lt;!channel&gt;" in text and "&amp;" in text and "<!channel>" not in text


def test_escape_function_order_does_not_double_escape():
    assert escape("a & <b> &lt;") == "a &amp; &lt;b&gt; &amp;lt;"


def test_slack_long_text_is_cut_after_escaping_and_the_lead_is_still_delivered(slack, http):
    # 10,000 characters, every one of them an escape trigger. Cutting BEFORE escaping would send 4 times the limit.
    p = payload(question="<" * 10_000, other="x" + "&" * 10_000)
    out = SlackSink().send(event_for(p), http, ctx(slack.url))
    assert out.kind == DONE, out
    sections = [b["text"]["text"] for b in slack.requests[0]["json"]["blocks"] if b["type"] == "section"]
    assert all(len(s) <= 3000 for s in sections)
    assert any("truncated" in s for s in sections)


def test_truncate_escaped_cuts_after_escaping_and_never_leaves_half_an_entity():
    cut = truncate_escaped("x" + "&" * 10_000)
    body = cut.split("…")[0]
    assert len(body) <= 2800 and not body.endswith("&amp") and not body.endswith("&am") and not body.endswith("&a") and not body.endswith("&")
    short = truncate_escaped("a < b")
    assert short == "a &lt; b"  # under the limit it is only escaped


def test_slack_header_and_block_count_stay_inside_the_documented_limits(slack, http):
    p = payload(name="N" * 500, **{f"answer{i}": f"value {i}" for i in range(100)})
    msg = build_message(event_for(p))
    assert len(msg["blocks"]) <= 50
    assert len(msg["blocks"][0]["text"]["text"]) <= 150
    assert SlackSink().send(event_for(p), http, ctx(slack.url)).kind == DONE


@pytest.mark.parametrize("sink_cls,fake_fixture", [(SlackSink, "slack"), (MakeSink, "make")])
def test_a_lead_with_every_optional_field_missing_still_sends(request, http, sink_cls, fake_fixture):
    fake = request.getfixturevalue(fake_fixture)
    for p in ({}, {"type": "ContactCreate"}, {"name": None, "email": None, "tags": None, "customFields": None}, {"tags": [None, 3, {"a": 1}], "state": ["TX"]}):
        out = sink_cls().send(event_for(p), http, ctx(fake.url))
        assert out.kind == DONE, (p, out)


def test_slack_outcomes_by_vendor_behaviour(slack, http):
    sink = SlackSink()
    cases = [
        ("rate_limit", RETRY), ("server_error", AMBIGUOUS), ("act_then_fail", AMBIGUOUS), ("forbidden", PAUSE),
        ("gone", DEAD), ("invalid", DEAD), ("odd_ok", DEAD),
    ]
    for mode, want in cases:
        assert sink.send(event_for(), http, ctx(slack.url)).kind == DONE  # positive control before every refusal
        slack.queue(mode)
        out = sink.send(event_for(), http, ctx(slack.url))
        assert out.kind == want, (mode, out)


def test_slack_429_honours_retry_after_and_marks_the_channel_rate_limited(slack, http):
    slack.retry_after = 7
    slack.queue("rate_limit")
    out = SlackSink().send(event_for(), http, ctx(slack.url))
    assert (out.kind, out.after_s, out.rate_limited) == (RETRY, 7.0, True)
    slack.retry_after = 99999
    slack.queue("rate_limit")
    assert SlackSink().send(event_for(), http, ctx(slack.url)).after_s == 300.0  # capped
    slack.retry_after = "soon"  # type: ignore[assignment]
    slack.queue("rate_limit")
    assert SlackSink().send(event_for(), http, ctx(slack.url)).after_s == 30.0  # unreadable header, default


def test_slack_ambiguous_means_the_vendor_may_have_acted(slack, http):
    slack.queue("act_then_fail")
    out = SlackSink().send(event_for(), http, ctx(slack.url))
    assert out.kind == AMBIGUOUS and slack.effects == 1  # the message WAS posted and the answer was a 500
    assert out.reason == "http 500"


def test_slack_timeout_is_ambiguous_and_a_refused_connection_is_a_plain_retry(slack, http):
    slack.queue("hang")
    out = SlackSink().send(event_for(), http, ctx(slack.url, timeout=0.3))
    assert out.kind == AMBIGUOUS and out.reason == "ReadTimeout"
    slack.release.set()
    refused = SlackSink().send(event_for(), http, ctx(refused_url(), timeout=0.5))
    assert refused.kind == RETRY and refused.breaker_failure is True


def test_slack_error_reason_names_the_slack_error_code_but_never_the_body_or_url(slack, http):
    slack.queue("gone")
    out = SlackSink().send(event_for(), http, ctx(slack.url))
    assert out.reason == "http 404 no_service"
    assert slack.url not in (out.reason or "")


def test_slack_error_names_in_the_doc_fixture_are_all_known_to_the_sink():
    from relay.sinks.slack import SLACK_ERRORS

    doc = fixture("slack", "errors.doc.json")
    assert set(doc["errors"]) <= SLACK_ERRORS


# ---- Make --------------------------------------------------------------------------------------------------------------


def test_make_send_is_done_with_a_queue_reference_and_the_idempotency_header(make, http):
    out = MakeSink().send(event_for(), http, ctx(make.url))
    assert out.kind == DONE and out.effect_ref == "queue"
    req = make.requests[0]
    assert req["headers"]["idempotency-key"] == "a1b2c3d4e5f60718"
    body = req["json"]
    assert body["event_id"] == "a1b2c3d4e5f60718" and body["lead"]["email"] == "pat1@example.com" and body["payload"]["id"] == "contact00001"


def test_make_any_2xx_is_done_even_when_the_body_is_not_accepted(make, http):
    make.queue("accepted_custom")
    assert MakeSink().send(event_for(), http, ctx(make.url)).kind == DONE


def test_make_outcomes_by_vendor_behaviour(make, http):
    sink = MakeSink()
    cases = [
        ("queue_full", RETRY), ("rate_limit", RETRY), ("gone", PAUSE), ("forbidden", PAUSE), ("bad_request", DEAD),
        ("server_error", AMBIGUOUS), ("act_then_fail", AMBIGUOUS),
    ]
    for mode, want in cases:
        assert sink.send(event_for(), http, ctx(make.url)).kind == DONE  # positive control before every refusal
        make.queue(mode)
        out = sink.send(event_for(), http, ctx(make.url))
        assert out.kind == want, (mode, out)


def test_make_queue_full_is_a_retry_that_spends_no_budget_and_is_never_dead(make, http):
    make.queue("queue_full")
    out = MakeSink().send(event_for(), http, ctx(make.url))
    assert out.kind == RETRY and out.reason == "queue is full" and out.breaker_failure is False


def test_make_a_400_with_another_body_is_dead_not_a_queue_retry(make, http):
    make.queue("bad_request")
    assert MakeSink().send(event_for(), http, ctx(make.url)).kind == DEAD


def test_make_429_marks_the_hook_rate_limited(make, http):
    make.queue("rate_limit")
    out = MakeSink().send(event_for(), http, ctx(make.url))
    assert out.kind == RETRY and out.rate_limited is True and out.after_s is None


def test_make_ambiguous_means_the_vendor_may_have_acted(make, http):
    make.queue("act_then_fail")
    assert MakeSink().send(event_for(), http, ctx(make.url)).kind == AMBIGUOUS
    assert make.effects == 1


def test_make_timeout_is_ambiguous_and_refused_is_a_retry(make, http):
    make.queue("hang")
    assert MakeSink().send(event_for(), http, ctx(make.url, timeout=0.3)).kind == AMBIGUOUS
    make.release.set()
    assert MakeSink().send(event_for(), http, ctx(refused_url(), timeout=0.5)).kind == RETRY


def test_make_apikey_header_is_sent_when_configured_and_a_wrong_key_is_dead():
    fake = FakeMake(apikey="k" * 24).start()
    h = Http()
    try:
        assert MakeSink().send(event_for(), h, ctx(fake.url, apikey="k" * 24)).kind == DONE  # positive control
        wrong = MakeSink().send(event_for(), h, ctx(fake.url, apikey="w" * 24))
        assert wrong.kind == DEAD and wrong.reason == "http 401"
        none = MakeSink().send(event_for(), h, ctx(fake.url))
        assert none.kind == DEAD
        assert fake.requests[0]["headers"]["x-make-apikey"] == "k" * 24
    finally:
        h.close()
        fake.stop()


def test_make_payload_over_five_megabytes_is_dead_without_a_call(make, http):
    big = payload(question="x" * (MAX_BODY_BYTES + 10))
    out = MakeSink().send(event_for(big), http, ctx(make.url))
    assert out.kind == DEAD and out.reason == "payload over 5 MB"
    assert make.requests == []


def test_make_doc_fixtures_match_the_fake(make):
    docs = fixture("make", "responses.doc.json")
    res = httpx.post(make.url, json={"x": 1})
    assert (res.status_code, res.text) == (docs["accepted"]["status"], docs["accepted"]["body"])
    make.queue("queue_full")
    res = httpx.post(make.url, json={"x": 1})
    assert (res.status_code, res.text) == (docs["queue_full"]["status"], docs["queue_full"]["body"])
    make.queue("rate_limit")
    res = httpx.post(make.url, json={"x": 1})
    assert (res.status_code, res.text) == (docs["rate_limited"]["status"], docs["rate_limited"]["body"])
    assert fixture("make", "limits.doc.json")["max_payload_bytes"] == MAX_BODY_BYTES
