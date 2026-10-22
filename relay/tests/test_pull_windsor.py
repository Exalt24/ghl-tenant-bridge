"""The Windsor.ai pull against its fake, on SQLite and on Postgres, and the proof that the API key (a query parameter, so part of
every URL) never reaches a log line, an exception message or a database column."""

from __future__ import annotations

import logging
import traceback
from datetime import timedelta

import pytest

from relay import redact
from relay.http import HttpConnectError
from relay.pulls.common import PullAbort, PullError, PullThrottled
from relay.pulls.windsor import parse_row, pull_windsor

from .fakes.ads import WINDSOR_KEY, windsor_rows
from .pull_helpers import FIXED_TODAY, StubHttp, pull_env, reply, windsor_cfg

START = FIXED_TODAY - timedelta(days=27)


def seed(fake, days=28, campaigns=None, **kw):
    fake.rows = windsor_rows(campaigns or {"c1": "One", "c2": "Two"}, START, days, **kw)
    return fake.rows


def test_a_pull_stores_every_row_with_exact_minor_units_from_the_number_text(store, windsor):
    windsor.rows = [
        {"date": "2026-10-20", "campaign_id": "c1", "campaign": "n", "spend": 1.005, "impressions": 10, "clicks": 1, "account_currency": "USD"},
        {"date": "2026-10-20", "campaign_id": "c2", "campaign": "n", "spend": 0.29, "impressions": 10, "clicks": 1, "account_currency": "USD"},
        {"date": "2026-10-20", "campaign_id": "c3", "campaign": "n", "spend": 1234567.89, "impressions": 10, "clicks": 1, "account_currency": "USD"},
        {"date": "2026-10-20", "campaign_id": "c4", "campaign": "n", "spend": 7, "impressions": 10, "clicks": 1, "account_currency": "USD"},
    ]
    env, _clock = pull_env()
    result = pull_windsor(store, windsor_cfg(windsor, label="acct-a"), env)
    got = {r["campaign_id"]: r["spend_minor"] for r in store.spend_rows("windsor")}
    assert got == {"c1": 101, "c2": 29, "c3": 123456789, "c4": 700}  # Decimal(1.005) would give 100
    assert result.rows == 4 and {r["account"] for r in store.spend_rows("windsor")} == {"acct-a"}
    assert {r["currency"] for r in store.spend_rows("windsor")} == {"USD"} and store.spend_rows("meta") == []


def test_the_request_carries_the_key_as_a_query_parameter_and_no_auth_header_for_the_window(store, windsor):
    seed(windsor)
    env, _clock = pull_env()
    pull_windsor(store, windsor_cfg(windsor), env)
    q = windsor.queries[0]
    assert q["api_key"] == WINDSOR_KEY and q["date_from"] == START.isoformat() and q["date_to"] == FIXED_TODAY.isoformat()
    assert {"date", "campaign_id", "spend", "account_currency"} <= set(q["fields"].split(","))
    sent = windsor.requests[0]["headers"]
    assert "authorization" not in sent and "x-api-key" not in sent  # header auth is refused by Windsor
    assert len(store.spend_rows("windsor")) == 56


def test_the_window_follows_the_configured_days(store, windsor):
    seed(windsor)
    env, _clock = pull_env()
    pull_windsor(store, windsor_cfg(windsor, days=2), env)
    assert windsor.queries[0]["date_from"] == (FIXED_TODAY - timedelta(days=1)).isoformat()
    assert {r["day"] for r in store.spend_rows("windsor")} == {FIXED_TODAY.isoformat(), (FIXED_TODAY - timedelta(days=1)).isoformat()}


def test_a_repull_replaces_the_days_and_stacks_nothing(store, windsor):
    seed(windsor, days=4)
    env, _clock = pull_env()
    cfg = windsor_cfg(windsor)
    pull_windsor(store, cfg, env)
    assert len(store.spend_rows("windsor")) == 8
    for r in windsor.rows:
        r["spend"] = 50.5
    pull_windsor(store, cfg, env)
    again = store.spend_rows("windsor")
    assert len(again) == 8 and {r["spend_minor"] for r in again} == {5050}


def test_meta_and_windsor_rows_for_the_same_campaign_and_day_live_side_by_side(store, windsor):
    store.upsert_spend([{"source": "meta", "account": "default", "campaign_id": "c1", "day": START.isoformat(), "spend_minor": 1, "currency": "USD"}])
    seed(windsor, days=1, campaigns={"c1": "One"})
    env, _clock = pull_env()
    pull_windsor(store, windsor_cfg(windsor), env)
    assert {r["source"] for r in store.spend_rows()} == {"meta", "windsor"} and len(store.spend_rows()) == 2


def test_a_429_waits_the_retry_after_the_reply_gives(store, windsor):
    seed(windsor, days=2)
    windsor.retry_after = 11
    windsor.queue("rate_limit", "rate_limit")
    env, clock = pull_env()
    pull_windsor(store, windsor_cfg(windsor), env)
    assert clock.sleeps == [11.0, 11.0] and len(store.spend_rows("windsor")) == 4


def test_a_429_without_retry_after_backs_off_exponentially(store, windsor):
    seed(windsor, days=2)
    windsor.retry_after = None
    windsor.queue("rate_limit", "rate_limit", "rate_limit")
    env, clock = pull_env()
    pull_windsor(store, windsor_cfg(windsor), env)
    first, second, third = clock.sleeps
    assert 2.5 <= first <= 5 and 5 <= second <= 10 and 10 <= third <= 20


def test_a_retry_after_longer_than_the_run_will_hold_ends_the_run_without_waiting(store, windsor):
    seed(windsor, days=2)
    windsor.retry_after = 3600
    windsor.queue("rate_limit")
    env, clock = pull_env()
    with pytest.raises(PullThrottled) as err:
        pull_windsor(store, windsor_cfg(windsor), env)
    assert err.value.after_s == 3600 and clock.sleeps == [] and store.spend_rows() == []


def test_a_vendor_that_keeps_saying_429_ends_the_run_after_six_tries(store, windsor):
    seed(windsor, days=2)
    windsor.queue(*["rate_limit"] * 12)
    env, clock = pull_env()
    with pytest.raises(PullThrottled, match="gave up"):
        pull_windsor(store, windsor_cfg(windsor), env)
    assert len(windsor.requests) == 6 and store.spend_rows() == []


@pytest.mark.parametrize("mode", ["unauthorized"])
def test_a_refused_key_stops_the_run_and_is_not_retried(store, windsor, mode):
    seed(windsor, days=2)
    windsor.queue(mode)
    env, clock = pull_env()
    with pytest.raises(PullAbort, match="refused"):
        pull_windsor(store, windsor_cfg(windsor), env)
    assert clock.sleeps == [] and len(windsor.requests) == 1 and store.spend_rows() == []


def test_a_wrong_key_is_the_documented_not_authorized_and_also_stops_the_run(store, windsor):
    seed(windsor, days=2)
    env, _clock = pull_env()
    assert pull_windsor(store, windsor_cfg(windsor), env).rows == 4  # control: the right key works
    with pytest.raises(PullAbort):
        pull_windsor(store, windsor_cfg(windsor, key="wrong-key-value"), env)


def test_a_server_error_is_retried_and_a_bad_request_is_not(store, windsor):
    rows = seed(windsor, days=2)
    windsor.queue("server_error")
    env, clock = pull_env()
    pull_windsor(store, windsor_cfg(windsor), env)
    assert len(clock.sleeps) == 1 and len(store.spend_rows("windsor")) == len(rows)
    windsor.queue("bad_request")
    with pytest.raises(PullError, match="refused the request"):
        pull_windsor(store, windsor_cfg(windsor), env)
    assert len(clock.sleeps) == 1  # the 400 was not waited on or retried


def test_a_spend_that_is_text_is_refused_because_windsor_documents_a_number(store, windsor):
    seed(windsor, days=2)
    env, _clock = pull_env()
    assert pull_windsor(store, windsor_cfg(windsor), env).rows == 4  # control
    windsor.spend_type = "string"
    with pytest.raises(PullError, match="^spend is not a JSON number"):
        pull_windsor(store, windsor_cfg(windsor), env)


GOOD = {"date": "2026-10-20", "campaign_id": "c1", "campaign": "n", "spend": 12.34, "impressions": 100, "clicks": 5, "account_currency": "USD"}


@pytest.mark.parametrize(
    "change,match",
    [
        ({"spend": "12.34"}, "^spend is not a JSON number"),
        ({"spend": None}, "^spend is not a JSON number"),
        ({"spend": True}, "^spend is not a JSON number"),
        ({"spend": -1.5}, "negative"),
        ({"spend": float("nan")}, "refused"),
        ({"account_currency": "ZZZ"}, "refused"),
        ({"account_currency": None}, "refused"),
        ({"date": "20/10/2026"}, "no valid date"),
        ({"date": None}, "no valid date"),
        ({"campaign_id": None}, "campaign_id"),
        ({"impressions": 1.5}, "whole number"),
        ({"clicks": "5"}, "whole number"),
    ],
)
def test_a_malformed_row_is_refused(change, match):
    assert parse_row(GOOD, "x")["spend_minor"] == 1234  # control
    with pytest.raises(PullError, match=match):
        parse_row({**GOOD, **change}, "x")


def test_one_bad_row_writes_nothing_at_all(store, windsor):
    seed(windsor, days=3)
    env, _clock = pull_env()
    windsor.rows[-1]["account_currency"] = "ZZZ"
    with pytest.raises(PullError):
        pull_windsor(store, windsor_cfg(windsor), env)
    assert store.spend_rows() == []


def test_two_rows_for_one_campaign_and_day_are_refused_not_merged(store, windsor):
    seed(windsor, days=1, campaigns={"c1": "One"})
    windsor.rows.append(dict(windsor.rows[0]))
    env, _clock = pull_env()
    with pytest.raises(PullError, match="share a campaign and a day"):
        pull_windsor(store, windsor_cfg(windsor), env)
    assert store.spend_rows() == []


def test_a_response_that_is_not_the_documented_shape_is_an_error(store):
    for body, match in (("not json", "not JSON"), ("[1]", "not an object"), ('{"rows": []}', "no data list")):
        env, _clock = pull_env(StubHttp(reply(200, body)))
        with pytest.raises(PullError, match=match):
            pull_windsor(store, windsor_cfg(type("F", (), {"url": "http://127.0.0.1:9"})()), env)


# ---- the key is a secret that sits in the URL -----------------------------------------------------------------------------------


def test_the_api_key_never_reaches_a_log_line_an_exception_or_a_database_column(store, windsor, caplog):
    redact.uninstall()
    redact.install()
    redact.register(WINDSOR_KEY)
    seed(windsor, days=2)
    seen_text: list[str] = []
    cfg = windsor_cfg(windsor)
    scenarios = ["ok", "rate_limit", "unauthorized", "server_error", "echo_url", "echo_400", "bad_request"]
    with caplog.at_level(logging.DEBUG):
        for mode in scenarios:
            windsor.reset()
            seed(windsor, days=2)
            windsor.retry_after = 1
            windsor.queue(mode, *(["ok"] if mode in ("rate_limit", "server_error") else []))
            env, _clock = pull_env()
            try:
                pull_windsor(store, cfg, env)
            except (PullError, PullAbort, PullThrottled) as exc:
                seen_text.append(f"{exc!s} {exc!r} {''.join(traceback.format_exception(exc))}")
        # a transport failure on every try (a stub, because a refused connection on Windows takes seconds to give up)
        dead = windsor_cfg(type("F", (), {"url": "http://127.0.0.1:9"})(), key=WINDSOR_KEY)
        env, _clock = pull_env(StubHttp(*[HttpConnectError("ConnectError")] * 6))
        with pytest.raises(PullThrottled) as err:
            pull_windsor(store, dead, env)
        seen_text.append(f"{err.value!s} {''.join(traceback.format_exception(err.value))}")
    log_text = "\n".join(f"{r.name} {r.getMessage()} {r.exc_text or ''}" for r in caplog.records if not r.name.startswith("werkzeug"))
    everything = "\n".join(seen_text) + "\n" + log_text + "\n" + "\n".join(store.dump_all_text())
    # positive controls: the key really was on the wire, a vendor body really did echo it, and the run produced output
    windsor.reset()
    windsor.queue("echo_url")
    import httpx

    echoed = httpx.get(f"{windsor.url}/facebook", params={"api_key": WINDSOR_KEY})
    assert WINDSOR_KEY in echoed.text and WINDSOR_KEY in str(echoed.request.url)
    assert seen_text and len(store.spend_rows("windsor")) > 0
    assert any(r.name.startswith("werkzeug") for r in caplog.records)  # the fake's own access log exists, is not the relay's, and is masked anyway
    assert WINDSOR_KEY not in everything and "api_key=" not in everything


def test_the_masking_filter_would_catch_a_leaked_url_if_one_were_logged(caplog):
    """Control for the test above: a line that does carry the URL is masked, so the sweep above is able to see a leak."""
    redact.uninstall()
    redact.install()
    redact.register(WINDSOR_KEY)
    with caplog.at_level(logging.INFO):
        logging.getLogger("relay.test").warning("GET https://connectors.windsor.ai/facebook?fields=date&api_key=%s", WINDSOR_KEY)
    assert all(WINDSOR_KEY not in r.getMessage() for r in caplog.records) and any("api_key=***" in r.getMessage() for r in caplog.records)
