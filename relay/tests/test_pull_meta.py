"""The Meta Insights pull against its fake, on SQLite and on Postgres. Every test runs after the fixture's positive control, so
a refusal here is the pull's doing and not a broken request."""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from relay.http import HttpConnectError, HttpTimeout
from relay.money import to_minor_from_text
from relay.pulls.common import PullAbort, PullError, PullThrottled
from relay.pulls.meta_spend import pull_meta_spend, parse_row

from .fakes.ads import ACCOUNT, day_rows
from .pull_helpers import FIXED_TODAY, StubHttp, insights_cfg, pull_env, reply

START = FIXED_TODAY - timedelta(days=27)


def seed(fake, campaigns=None, days=28, **kw):
    fake.rows = day_rows(campaigns or {"c1": "One", "c2": "Two"}, START, days, **kw)
    return fake.rows


def expected(rows, currency="USD"):
    return {(r["campaign_id"], r["date_start"]): to_minor_from_text(r["spend"], currency) for r in rows}


def stored(store):
    return {(r["campaign_id"], r["day"]): r["spend_minor"] for r in store.spend_rows("meta")}


def test_a_28_day_pull_stores_every_campaign_day_with_exact_minor_units_and_the_currency(store, insights):
    rows = seed(insights)
    env, _clock = pull_env()
    result = pull_meta_spend(store, insights_cfg(insights, limit=10), env)
    assert result.rows == 56 and result.pages == 6 and result.splits == 0
    assert stored(store) == expected(rows)
    got = store.spend_rows("meta")
    assert {r["currency"] for r in got} == {"USD"} and {r["account"] for r in got} == {ACCOUNT}
    assert got[0]["impressions"] == int(rows[0]["impressions"]) or got[0]["impressions"] is not None


def test_the_request_asks_for_campaign_level_one_day_buckets_over_the_last_28_days(insights, store):
    seed(insights)
    env, _clock = pull_env()
    pull_meta_spend(store, insights_cfg(insights), env)
    q = insights.queries[0]
    assert q["level"] == "campaign" and q["time_increment"] == "1" and q["limit"] == "500"
    assert json.loads(q["time_range"]) == {"since": START.isoformat(), "until": FIXED_TODAY.isoformat()}
    assert {"campaign_id", "spend", "account_currency"} <= set(q["fields"].split(","))


def test_the_window_follows_the_configured_number_of_days(insights, store):
    seed(insights)
    env, _clock = pull_env()
    pull_meta_spend(store, insights_cfg(insights, days=3), env)
    assert json.loads(insights.queries[0]["time_range"]) == {"since": (FIXED_TODAY - timedelta(days=2)).isoformat(), "until": FIXED_TODAY.isoformat()}
    assert {r["day"] for r in store.spend_rows("meta")} == {(FIXED_TODAY - timedelta(days=i)).isoformat() for i in range(3)}


def test_a_numeric_account_id_gets_the_act_prefix(insights, store):
    seed(insights, days=2)
    env, _clock = pull_env()
    pull_meta_spend(store, insights_cfg(insights, account=ACCOUNT.removeprefix("act_")), env)
    assert {r["account"] for r in store.spend_rows("meta")} == {ACCOUNT}


def test_spend_is_parsed_from_text_so_a_float_trap_value_is_exact(store, insights):
    insights.rows = [
        {"campaign_id": "c1", "campaign_name": "n", "date_start": "2026-10-20", "date_stop": "2026-10-20", "spend": "1.15", "impressions": "1", "clicks": "0", "account_currency": "USD"},
        {"campaign_id": "c2", "campaign_name": "n", "date_start": "2026-10-20", "date_stop": "2026-10-20", "spend": "0.29", "impressions": "1", "clicks": "0", "account_currency": "USD"},
        {"campaign_id": "c3", "campaign_name": "n", "date_start": "2026-10-20", "date_stop": "2026-10-20", "spend": "5339.5", "impressions": "1", "clicks": "0", "account_currency": "USD"},
    ]
    env, _clock = pull_env()
    pull_meta_spend(store, insights_cfg(insights), env)
    assert stored(store) == {("c1", "2026-10-20"): 115, ("c2", "2026-10-20"): 29, ("c3", "2026-10-20"): 533950}  # a float parse gives 114 and 28


def test_the_exponent_follows_the_account_currency(store, insights):
    seed(insights, {"c1": "One"}, days=2, currency="JPY", spend=lambda c, d: "1500")
    env, _clock = pull_env()
    pull_meta_spend(store, insights_cfg(insights), env)
    rows = store.spend_rows("meta")
    assert {r["spend_minor"] for r in rows} == {1500} and {r["currency"] for r in rows} == {"JPY"}  # yen have no minor unit


def test_paging_follows_next_until_it_is_absent_and_an_empty_page_with_next_is_not_the_end(store, insights):
    rows = seed(insights, {"c1": "One"}, days=12)
    insights.queue("ok", "empty_page", "ok", "empty_page")  # an empty page that still carries next, twice, mid way
    env, _clock = pull_env()
    result = pull_meta_spend(store, insights_cfg(insights, limit=4), env)
    assert stored(store) == expected(rows) and len(rows) == 12
    assert result.pages == 5  # 3 real pages and 2 empty ones that carried next


def test_an_empty_first_page_with_next_still_reaches_the_data(store, insights):
    rows = seed(insights, {"c1": "One"}, days=3)
    insights.queue("empty_page")
    env, _clock = pull_env()
    pull_meta_spend(store, insights_cfg(insights, limit=10), env)
    assert stored(store) == expected(rows)


def test_a_next_link_to_another_host_is_not_followed_and_nothing_is_written(store, insights):
    from .fakes.ads import FakeInsights

    other = FakeInsights().start()
    try:
        seed(insights, {"c1": "One"}, days=6)
        other.rows = list(insights.rows)
        insights.next_base = other.base
        env, _clock = pull_env()
        with pytest.raises(PullError, match="away from the configured host"):
            pull_meta_spend(store, insights_cfg(insights, limit=3), env)
        assert other.requests == [] and store.spend_rows() == []  # the token never went there
    finally:
        other.stop()


def test_a_next_link_to_the_same_host_is_followed(store, insights):
    rows = seed(insights, {"c1": "One"}, days=6)
    env, _clock = pull_env()
    pull_meta_spend(store, insights_cfg(insights, limit=3), env)  # control for the test above: the same data, same host
    assert stored(store) == expected(rows)


def test_a_repeated_cursor_ends_the_run_instead_of_looping(store):
    row = {"campaign_id": "c1", "date_start": "2026-10-20", "date_stop": "2026-10-20", "spend": "1.00", "account_currency": "USD"}
    page = reply(200, json.dumps({"data": [row], "paging": {"next": "http://127.0.0.1:9/v26.0/act_fake01/insights?after=c1"}}))
    http = StubHttp(page, page, page)
    env, _clock = pull_env(http)
    cfg = insights_cfg(type("F", (), {"url": "http://127.0.0.1:9/v26.0"})())
    with pytest.raises(PullError, match="repeated"):
        pull_meta_spend(store, cfg, env)
    assert len(http.urls) == 2 and store.spend_rows() == []


def test_a_repull_upserts_the_same_days_and_restated_figures_replace_the_old_ones(store, insights):
    rows = seed(insights, {"c1": "One"}, days=5)
    env, _clock = pull_env()
    cfg = insights_cfg(insights)
    pull_meta_spend(store, cfg, env)
    assert stored(store) == expected(rows)
    for r in insights.rows:  # Meta restates recent days
        r["spend"] = "99.99"
    pull_meta_spend(store, cfg, env)
    again = store.spend_rows("meta")
    assert len(again) == 5 and {r["spend_minor"] for r in again} == {9999}
    pull_meta_spend(store, cfg, env)
    assert len(store.spend_rows("meta")) == 5  # a third pull stacks nothing


def test_error_190_stops_the_run_writes_nothing_and_is_not_retried(store, insights):
    seed(insights, {"c1": "One"}, days=9)
    insights.queue("ok", "token_expired")  # the second page is refused: the first page's rows must not be kept
    env, clock = pull_env()
    with pytest.raises(PullAbort, match="190"):
        pull_meta_spend(store, insights_cfg(insights, limit=3), env)
    assert store.spend_rows() == [] and clock.sleeps == []
    assert len(insights.requests) == 2


def test_a_throttle_waits_the_minutes_the_usage_header_gives_then_carries_on(store, insights):
    rows = seed(insights, {"c1": "One"}, days=4)
    insights.regain_minutes = 2
    insights.queue("throttle")
    env, clock = pull_env()
    pull_meta_spend(store, insights_cfg(insights), env)
    assert clock.sleeps == [120.0] and stored(store) == expected(rows)


@pytest.mark.parametrize("header_name", ["X-Business-Use-Case-Usage", "X-Business-Use-Case"])
def test_both_header_names_are_read(store, insights, header_name):
    seed(insights, {"c1": "One"}, days=2)
    insights.header_name = header_name
    insights.regain_minutes = 3
    insights.queue("throttle")
    env, clock = pull_env()
    pull_meta_spend(store, insights_cfg(insights), env)
    assert clock.sleeps == [180.0]


def test_a_throttle_without_the_header_backs_off_exponentially_with_jitter(store, insights):
    seed(insights, {"c1": "One"}, days=2)
    insights.queue("no_header", "no_header", "no_header")
    env, clock = pull_env()
    pull_meta_spend(store, insights_cfg(insights), env)
    first, second, third = clock.sleeps
    assert 2.5 <= first <= 5.0 and 5.0 <= second <= 10.0 and 10.0 <= third <= 20.0  # base 5 s doubling, half to full jitter
    assert len(store.spend_rows("meta")) == 2


def test_the_80000_series_is_a_throttle_too(store, insights):
    seed(insights, {"c1": "One"}, days=2)
    insights.queue("throttle_80004")
    env, clock = pull_env()
    pull_meta_spend(store, insights_cfg(insights), env)
    assert len(clock.sleeps) == 1 and len(store.spend_rows("meta")) == 2


def test_a_wait_longer_than_the_run_will_hold_ends_the_run_with_nothing_written(store, insights):
    seed(insights, {"c1": "One"}, days=2)
    insights.regain_minutes = 19
    insights.queue("throttle")
    env, clock = pull_env()
    with pytest.raises(PullThrottled) as err:
        pull_meta_spend(store, insights_cfg(insights), env)
    assert err.value.after_s == 19 * 60 and clock.sleeps == [] and store.spend_rows() == []


def test_the_total_wait_in_one_run_is_bounded(store, insights):
    seed(insights, {"c1": "One"}, days=2)
    insights.regain_minutes = 5  # 300 s each, allowed one by one
    insights.queue(*["throttle"] * 10)
    env, clock = pull_env()
    with pytest.raises(PullThrottled):
        pull_meta_spend(store, insights_cfg(insights), env)
    assert sum(clock.sleeps) <= 900 and len(clock.sleeps) == 3 and store.spend_rows() == []


def test_a_server_error_is_retried_and_a_persistent_one_ends_the_run(store, insights):
    rows = seed(insights, {"c1": "One"}, days=2)
    insights.queue("server_error", "server_error")
    env, clock = pull_env()
    pull_meta_spend(store, insights_cfg(insights), env)
    assert len(clock.sleeps) == 2 and stored(store) == expected(rows)
    insights.reset()
    seed(insights, {"c1": "One"}, days=2)
    insights.queue(*["server_error"] * 20)
    env, clock = pull_env()
    with pytest.raises(PullThrottled, match="gave up"):
        pull_meta_spend(store, insights_cfg(insights), env)


def test_a_transport_failure_is_retried_with_a_backoff(store, insights):
    rows = seed(insights, {"c1": "One"}, days=2)
    from relay.http import Http

    real = Http()
    calls = {"n": 0}

    class Flaky:
        def request(self, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise HttpConnectError("ConnectError")
            if calls["n"] == 2:
                raise HttpTimeout("ReadTimeout")
            return real.request(*a, **kw)

    env, clock = pull_env(Flaky())
    pull_meta_spend(store, insights_cfg(insights), env)
    assert len(clock.sleeps) == 2 and stored(store) == expected(rows)


def test_error_100_with_subcode_1487534_splits_the_range_until_each_part_is_small_enough(store, insights):
    rows = seed(insights, days=28)
    insights.max_days = 7
    env, _clock = pull_env()
    result = pull_meta_spend(store, insights_cfg(insights), env)
    assert stored(store) == expected(rows) and len(store.spend_rows("meta")) == 56  # every day once, none lost to the split
    assert result.splits >= 3
    ranges = [json.loads(q["time_range"]) for q in insights.queries]
    assert ranges[0] == {"since": START.isoformat(), "until": FIXED_TODAY.isoformat()}  # the whole window was tried first
    ok_ranges = [r for r in ranges if (date.fromisoformat(r["until"]) - date.fromisoformat(r["since"])).days + 1 <= 7]
    assert ok_ranges and all(date.fromisoformat(r["since"]) >= START for r in ranges)


def test_a_split_range_is_cut_in_two_without_a_gap_or_an_overlap(store, insights):
    seed(insights, {"c1": "One"}, days=10)
    insights.max_days = 5
    env, _clock = pull_env()
    pull_meta_spend(store, insights_cfg(insights, days=10), env)
    ranges = [(date.fromisoformat(r["since"]), date.fromisoformat(r["until"])) for r in (json.loads(q["time_range"]) for q in insights.queries)]
    good = sorted(r for r in ranges if (r[1] - r[0]).days + 1 <= 5)
    assert good[0][0] == FIXED_TODAY - timedelta(days=9) and good[-1][1] == FIXED_TODAY
    for (a_from, a_to), (b_from, b_to) in zip(good, good[1:]):
        assert b_from == a_to + timedelta(days=1)


def test_one_day_that_is_still_too_much_ends_the_run(store, insights):
    seed(insights, {"c1": "One"}, days=4)
    insights.max_days = 0
    env, _clock = pull_env()
    with pytest.raises(PullError, match="one day"):
        pull_meta_spend(store, insights_cfg(insights, days=4), env)
    assert store.spend_rows() == []


def test_a_bad_request_is_an_error_not_a_retry(store, insights):
    seed(insights, {"c1": "One"}, days=2)
    insights.queue("bad_request")
    env, clock = pull_env()
    with pytest.raises(PullError, match="refused the request"):
        pull_meta_spend(store, insights_cfg(insights), env)
    assert clock.sleeps == [] and len(insights.requests) == 1


# ---- what a row must look like ------------------------------------------------------------------------------------------------


GOOD_ROW = {"campaign_id": "c1", "date_start": "2026-10-20", "date_stop": "2026-10-20", "spend": "12.34", "impressions": "100", "clicks": "5", "account_currency": "USD"}


def test_a_good_row_parses(store):
    row = parse_row(GOOD_ROW, ACCOUNT)
    assert (row["spend_minor"], row["currency"], row["impressions"], row["clicks"], row["day"]) == (1234, "USD", 100, 5, "2026-10-20")


@pytest.mark.parametrize(
    "change,match",
    [
        ({"spend": 12.34}, "numeric string"),
        ({"spend": 12}, "numeric string"),
        ({"spend": None}, "numeric string"),
        ({"spend": "1e3"}, "refused"),
        ({"spend": "-5.00"}, "negative"),
        ({"spend": "abc"}, "refused"),
        ({"account_currency": None}, "refused"),
        ({"account_currency": "ZZZ"}, "refused"),
        ({"account_currency": "usd"}, "refused"),
        ({"date_stop": "2026-10-21"}, "more than one day"),
        ({"date_start": "yesterday"}, "no valid date"),
        ({"campaign_id": None}, "campaign_id"),
        ({"campaign_id": ""}, "campaign_id"),
        ({"impressions": "many"}, "count"),
        ({"clicks": True}, "count"),
    ],
)
def test_a_malformed_row_is_refused_with_a_reason(change, match):
    assert parse_row(GOOD_ROW, ACCOUNT)["spend_minor"] == 1234  # control
    with pytest.raises(PullError, match=match):
        parse_row({**GOOD_ROW, **change}, ACCOUNT)


def test_missing_counts_are_stored_as_null_and_an_integer_campaign_id_is_text():
    row = parse_row({**GOOD_ROW, "campaign_id": 12345, "impressions": None, "clicks": None}, ACCOUNT)
    assert row["campaign_id"] == "12345" and row["impressions"] is None and row["clicks"] is None


def test_a_response_that_is_not_the_documented_shape_is_an_error(store):
    for body, match in (("not json", "not JSON"), ("[]", "not an object"), ('{"data": 5}', "no data list")):
        env, _clock = pull_env(StubHttp(reply(200, body)))
        with pytest.raises(PullError, match=match):
            pull_meta_spend(store, insights_cfg(type("F", (), {"url": "http://127.0.0.1:9/v26.0"})()), env)
    assert store.spend_rows() == []


def test_a_row_the_fake_serves_with_a_number_for_spend_ends_the_run(store, insights):
    seed(insights, {"c1": "One"}, days=2)
    env, _clock = pull_env()
    assert pull_meta_spend(store, insights_cfg(insights), env).rows == 2  # control: string spend is accepted
    insights.spend_type = "number"
    with pytest.raises(PullError, match="numeric string"):
        pull_meta_spend(store, insights_cfg(insights), env)


def test_a_page_chain_that_never_ends_is_cut_off(store, insights, monkeypatch):
    from relay.pulls import meta_spend

    seed(insights, {"c1": "One"}, days=12)
    monkeypatch.setattr(meta_spend, "MAX_PAGES", 3)
    env, _clock = pull_env()
    with pytest.raises(PullError, match="did not end"):
        pull_meta_spend(store, insights_cfg(insights, limit=2), env)  # 6 pages of 2 against a ceiling of 3
    assert store.spend_rows() == []


def test_a_gateway_error_with_no_graph_error_object_is_retried(store):
    row = {"campaign_id": "c1", "date_start": "2026-10-20", "date_stop": "2026-10-20", "spend": "1.00", "account_currency": "USD"}
    http = StubHttp(reply(502, "Bad gateway"), reply(200, json.dumps({"data": [row]})))
    env, clock = pull_env(http)
    result = pull_meta_spend(store, insights_cfg(type("F", (), {"url": "http://127.0.0.1:9/v26.0"})()), env)
    assert result.rows == 1 and len(clock.sleeps) == 1 and len(http.urls) == 2


def test_the_access_token_never_reaches_a_log_line_an_exception_or_a_database_column(store, insights, caplog):
    import logging
    import traceback

    from relay import redact

    from .fakes.ads import TOKEN

    redact.uninstall()
    redact.install()
    redact.register(TOKEN)
    seen: list[str] = []
    echoes = [reply(400, f"bad request, token {TOKEN} was in https://graph.example.test/v26.0/act_1/insights?access_token={TOKEN}"), reply(403, TOKEN), reply(200, TOKEN)]
    with caplog.at_level(logging.DEBUG):
        for mode in ("token_expired", "bad_request", "throttle", "server_error"):
            insights.reset()
            seed(insights, {"c1": "One"}, days=2)
            insights.queue(mode, *(["ok"] if mode in ("throttle", "server_error") else []))
            env, _clock = pull_env()
            try:
                pull_meta_spend(store, insights_cfg(insights), env)
            except (PullError, PullAbort, PullThrottled) as exc:
                seen.append(f"{exc!s} {exc!r} {''.join(traceback.format_exception(exc))}")
        for body in echoes:
            env, _clock = pull_env(StubHttp(body))
            try:
                pull_meta_spend(store, insights_cfg(type("F", (), {"url": "http://127.0.0.1:9/v26.0"})()), env)
            except PullError as exc:
                seen.append(f"{exc!s} {exc!r} {''.join(traceback.format_exception(exc))}")
    log_text = "\n".join(f"{r.name} {r.getMessage()} {r.exc_text or ''}" for r in caplog.records if not r.name.startswith("werkzeug"))
    everything = "\n".join(seen) + "\n" + log_text + "\n" + "\n".join(store.dump_all_text())
    assert len(seen) >= 5 and any("refused the request" in s for s in seen)  # control: failures happened and their messages were read
    assert len(store.spend_rows("meta")) == 2  # and a pull succeeded between them
    assert TOKEN not in everything and "access_token=" not in everything
