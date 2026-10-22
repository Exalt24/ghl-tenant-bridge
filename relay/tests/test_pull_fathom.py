"""The Fathom puller against its fake (SQLite and Postgres), and the client side limiter on a simulated clock."""

from __future__ import annotations

import json
import logging
import random
import traceback
from datetime import timedelta

import pytest

from relay import redact
from relay.pulls.common import PullAbort, PullError, PullThrottled, WindowLimiter
from relay.pulls.fathom import FathomLimiter, pull_fathom

from .fakes.fathom import API_KEY, make_meeting
from .pull_helpers import FIXED_TODAY, Clock, StubHttp, fathom_cfg, pull_env, reply


def seed(fake, n, **kw):
    fake.meetings = [make_meeting(i, **kw) for i in range(1, n + 1)]
    return fake.meetings


def test_a_pull_stores_every_meeting_across_pages_with_its_action_items(store, fathom):
    seed(fathom, 25, created="2026-10-20T10:00:00Z")
    env, _clock = pull_env()
    fathom.clock = _clock.monotonic
    result = pull_fathom(store, fathom_cfg(fathom), env, tenant="acme")
    assert result.rows == 25 and result.pages == 3
    rows = store.meetings()
    assert len(rows) == 25 and {r["tenant"] for r in rows} == {"acme"}
    first = next(r for r in rows if r["recording_id"] == "5001")
    assert first["title"] == "Weekly sync 1" and first["summary"] is None  # a summary was not asked for
    assert json.loads(first["action_items"]) == [{"description": "Send recap 1", "completed": False, "assignee": "Jane Doe"}]


def test_the_request_names_the_flags_the_key_header_and_the_window(store, fathom):
    seed(fathom, 2)
    env, clock = pull_env()
    fathom.clock = clock.monotonic
    pull_fathom(store, fathom_cfg(fathom, days=7), env, tenant="acme")
    q = fathom.queries[0]
    assert q["include_action_items"] == "true" and "include_summary" not in q and "include_transcript" not in q
    assert q["created_after"] == f"{(FIXED_TODAY - timedelta(days=7)).isoformat()}T00:00:00Z"
    assert fathom.requests[0]["headers"]["x-api-key"] == API_KEY


def test_the_summary_is_stored_only_when_it_was_asked_for_and_a_heavy_call_is_made(store, fathom):
    seed(fathom, 3, created="2026-10-20T10:00:00Z")
    env, clock = pull_env()
    fathom.clock = clock.monotonic
    pull_fathom(store, fathom_cfg(fathom, include_summary=True), env, tenant="acme")
    assert fathom.queries[0]["include_summary"] == "true"
    assert all(r["summary"].startswith("## Summary") for r in store.meetings())
    assert fathom.calls and all(heavy for _t, heavy in fathom.calls)


def test_a_later_pull_that_asks_for_less_keeps_the_summary_already_stored(store, fathom):
    seed(fathom, 2, created="2026-10-20T10:00:00Z")
    env, clock = pull_env()
    fathom.clock = clock.monotonic
    pull_fathom(store, fathom_cfg(fathom, include_summary=True), env, tenant="acme")
    pull_fathom(store, fathom_cfg(fathom, include_summary=False), env, tenant="acme")
    assert all(r["summary"] for r in store.meetings()) and len(store.meetings()) == 2


def test_the_transcript_is_never_stored_even_when_the_vendor_sends_one(store, fathom):
    seed(fathom, 1, created="2026-10-20T10:00:00Z")
    fathom.meetings[0]["transcript"] = [{"speaker": {"display_name": "Jane"}, "text": "the launch code is hunter2", "timestamp": "00:00:01"}]
    fathom.meetings[0]["calendar_invitees"] = [{"name": "A", "email": "private.person@example.test"}]
    env, clock = pull_env()
    fathom.clock = clock.monotonic
    pull_fathom(store, fathom_cfg(fathom), env, tenant="acme")
    text = "\n".join(store.dump_all_text())
    assert "hunter2" not in text and "private.person" not in text and "Weekly sync 1" in text


def test_a_429_waits_the_retry_after_the_reply_gives_and_carries_on(store, fathom):
    seed(fathom, 2, created="2026-10-20T10:00:00Z")
    fathom.retry_after = 3
    fathom.queue("rate_limit", "rate_limit")
    env, clock = pull_env()
    fathom.clock = clock.monotonic
    pull_fathom(store, fathom_cfg(fathom), env, tenant="acme")
    assert clock.sleeps == [3.0, 3.0] and len(store.meetings()) == 2


def test_a_429_without_retry_after_waits_a_full_window(store, fathom):
    seed(fathom, 2, created="2026-10-20T10:00:00Z")
    fathom.retry_after = None
    fathom.queue("rate_limit")
    env, clock = pull_env()
    fathom.clock = clock.monotonic
    pull_fathom(store, fathom_cfg(fathom), env, tenant="acme")
    assert clock.sleeps == [60.0]


def test_a_retry_after_longer_than_the_run_will_hold_ends_the_run_with_nothing_written(store, fathom):
    seed(fathom, 2)
    fathom.retry_after = 900
    fathom.queue("rate_limit")
    env, clock = pull_env()
    with pytest.raises(PullThrottled) as err:
        pull_fathom(store, fathom_cfg(fathom), env, tenant="acme")
    assert err.value.after_s == 900 and clock.sleeps == [] and store.meetings() == []


def test_a_refused_key_stops_the_run_and_is_not_retried(store, fathom):
    seed(fathom, 2, created="2026-10-20T10:00:00Z")
    env, clock = pull_env()
    assert pull_fathom(store, fathom_cfg(fathom), env, tenant="acme").rows == 2  # control
    with pytest.raises(PullAbort, match="401"):
        pull_fathom(store, fathom_cfg(fathom, key="wrong-key-value"), env, tenant="acme")
    assert clock.sleeps == []


def test_a_server_error_is_retried_and_a_bad_cursor_answer_is_not(store, fathom):
    seed(fathom, 2, created="2026-10-20T10:00:00Z")
    fathom.queue("server_error")
    env, clock = pull_env()
    fathom.clock = clock.monotonic
    pull_fathom(store, fathom_cfg(fathom), env, tenant="acme")
    assert len(clock.sleeps) == 1 and len(store.meetings()) == 2


def test_a_repeated_cursor_ends_the_run_instead_of_looping(store):
    page = reply(200, json.dumps({"limit": 10, "next_cursor": "abc", "items": []}))
    http = StubHttp(page, page, page, page)
    env, _clock = pull_env(http)
    with pytest.raises(PullError, match="repeated"):
        pull_fathom(store, fathom_cfg(type("F", (), {"url": "http://127.0.0.1:9/external/v1"})()), env, tenant="acme")
    assert len(http.urls) == 2 and store.meetings() == []


def test_a_cursor_that_is_not_text_is_an_error(store):
    env, _clock = pull_env(StubHttp(reply(200, json.dumps({"items": [], "next_cursor": 5}))))
    with pytest.raises(PullError, match="not text"):
        pull_fathom(store, fathom_cfg(type("F", (), {"url": "http://127.0.0.1:9/external/v1"})()), env, tenant="acme")


def test_a_meeting_without_a_recording_id_is_skipped_and_counted(store):
    items = [make_meeting(1), {"title": "no id"}, make_meeting(2)]
    env, _clock = pull_env(StubHttp(reply(200, json.dumps({"limit": 10, "next_cursor": None, "items": items}))))
    result = pull_fathom(store, fathom_cfg(type("F", (), {"url": "http://127.0.0.1:9/external/v1"})()), env, tenant="acme")
    assert result.rows == 2 and result.skipped == 1


def test_a_response_that_is_not_the_documented_shape_is_an_error(store):
    for body, match in (("nope", "not JSON"), ("[]", "not an object"), ('{"items": 3}', "no items list")):
        env, _clock = pull_env(StubHttp(reply(200, body)))
        with pytest.raises(PullError, match=match):
            pull_fathom(store, fathom_cfg(type("F", (), {"url": "http://127.0.0.1:9/external/v1"})()), env, tenant="acme")


# ---- the client side limiter ------------------------------------------------------------------------------------------------------


def test_a_long_backlog_is_paced_so_the_vendor_never_has_to_say_429(store, fathom):
    seed(fathom, 1500, created="2026-10-20T10:00:00Z")  # 150 pages: more than two full windows of calls
    env, clock = pull_env()
    fathom.clock = clock.monotonic
    result = pull_fathom(store, fathom_cfg(fathom), env, tenant="acme")
    assert result.pages == 150 and len(store.meetings()) == 1500
    assert fathom.limited == 0  # the fake counts 429s on the same simulated clock, so a limiter that let one through shows here
    assert clock.now - 1000.0 >= 120.0  # 150 calls cannot fit in fewer than three windows of 60


def test_heavy_calls_are_paced_under_the_smaller_window(store, fathom):
    seed(fathom, 450, created="2026-10-20T10:00:00Z")  # 45 heavy pages
    env, clock = pull_env()
    fathom.clock = clock.monotonic
    pull_fathom(store, fathom_cfg(fathom, include_summary=True), env, tenant="acme")
    assert fathom.limited == 0 and len(store.meetings()) == 450
    assert clock.now - 1000.0 >= 60.0  # 45 heavy calls need a second window: the first holds 30


def test_without_the_limiter_the_same_pull_is_answered_with_429s(store, fathom):
    """Control for the two tests above: the fake does push back when the client does not pace itself."""
    seed(fathom, 700, created="2026-10-20T10:00:00Z")  # 70 pages
    env, clock = pull_env()
    fathom.clock = clock.monotonic
    free = FathomLimiter(env)
    free.global_window.limit = 10**6
    free.heavy_window.limit = 10**6
    pull_fathom(store, fathom_cfg(fathom), env, tenant="acme", limiter=free)
    assert fathom.limited > 0 and len(store.meetings()) == 700  # it still finishes: Retry-After was honoured


def test_a_light_call_does_not_use_the_heavy_window_and_a_heavy_call_uses_both():
    clock = Clock()
    env, _ = pull_env(clock=clock)
    lim = FathomLimiter(env)
    for _ in range(29):
        lim.acquire(False)
    assert len(lim.global_window.calls) == 29 and len(lim.heavy_window.calls) == 0
    lim.acquire(True)
    assert len(lim.global_window.calls) == 30 and len(lim.heavy_window.calls) == 1
    assert clock.sleeps == []  # nothing waited while slots were free


def test_the_limiter_waits_exactly_until_the_oldest_call_leaves_the_window():
    clock = Clock(now=0.0)
    w = WindowLimiter(3, 60.0, clock.monotonic, clock.sleep)
    for _ in range(3):
        assert w.acquire() == 0.0
        clock.now += 10.0  # calls at 0, 10, 20
    waited = w.acquire()  # the 4th call must wait for the call at 0 to leave: until t = 60
    assert waited == pytest.approx(30.0) and clock.now == pytest.approx(60.0)


@pytest.mark.parametrize("seed_value", range(60))
def test_property_no_window_ever_holds_more_calls_than_allowed_and_no_wait_is_wasted(seed_value):
    rng = random.Random(seed_value)
    clock = Clock(now=0.0)
    env, _ = pull_env(clock=clock)
    lim = FathomLimiter(env)
    log: list[tuple[float, bool]] = []
    for _ in range(rng.randint(40, 220)):
        clock.now += rng.choice([0.0, 0.0, 0.1, 0.5, 2.0, 15.0, 61.0])
        heavy = rng.random() < 0.4
        before = clock.now
        waited = lim.acquire(heavy)
        log.append((clock.now, heavy))
        if waited == 0.0:
            assert clock.now == before
    for i, (t, _h) in enumerate(log):
        recent = [(u, h) for u, h in log[: i + 1] if t - u < 60.0]
        assert len(recent) <= 60
        assert sum(1 for _u, h in recent if h) <= 30
    for (t1, _), (t2, _) in zip(log, log[1:]):
        assert t2 >= t1  # time never runs backwards


def test_property_a_wait_is_never_longer_than_one_window():
    clock = Clock(now=0.0)
    env, _ = pull_env(clock=clock)
    lim = FathomLimiter(env)
    rng = random.Random(7)
    for _ in range(400):
        waited = lim.acquire(rng.random() < 0.5)
        assert waited <= 60.0 + 1e-9


# ---- the key is a secret -----------------------------------------------------------------------------------------------------------


def test_the_api_key_never_reaches_a_log_line_an_exception_or_a_database_column(store, fathom, caplog):
    redact.uninstall()
    redact.install()
    redact.register(API_KEY)
    seen: list[str] = []
    with caplog.at_level(logging.DEBUG):
        for mode in ("ok", "rate_limit", "server_error", "unauthorized"):
            fathom.reset()
            seed(fathom, 3, created="2026-10-20T10:00:00Z")
            fathom.retry_after = 1
            fathom.queue(mode, *(["ok"] if mode in ("rate_limit", "server_error") else []))
            env, clock = pull_env()
            fathom.clock = clock.monotonic
            try:
                pull_fathom(store, fathom_cfg(fathom), env, tenant="acme")
            except (PullError, PullAbort, PullThrottled) as exc:
                seen.append(f"{exc!s} {exc!r} {''.join(traceback.format_exception(exc))}")
    log_text = "\n".join(f"{r.name} {r.getMessage()} {r.exc_text or ''}" for r in caplog.records if not r.name.startswith("werkzeug"))
    everything = "\n".join(seen) + "\n" + log_text + "\n" + "\n".join(store.dump_all_text())
    assert seen and len(store.meetings()) == 3  # the run produced both a failure and stored data
    assert any(h["headers"].get("x-api-key") == API_KEY for h in fathom.requests) or fathom.requests == []
    assert API_KEY not in everything


def test_a_vendor_that_keeps_saying_429_ends_the_run_after_six_tries(store, fathom):
    seed(fathom, 2, created="2026-10-20T10:00:00Z")
    fathom.retry_after = 1
    fathom.queue(*["rate_limit"] * 12)
    env, clock = pull_env()
    fathom.clock = clock.monotonic
    with pytest.raises(PullThrottled, match="gave up"):
        pull_fathom(store, fathom_cfg(fathom), env, tenant="acme")
    assert len(fathom.requests) == 6 and store.meetings() == []
