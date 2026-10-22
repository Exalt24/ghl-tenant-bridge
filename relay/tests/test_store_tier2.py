"""The store queries tier 2 added, run on SQLite and on Postgres with the same expectations: the claim counter that shows a
claim nobody finished, the mark of a try that may have reached the vendor, the event's received time, and the one
append in flight per spreadsheet lock.

Each test names the guard it protects; tests/mutants.py removes that guard and requires a file here to go red.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from relay.store.base import REPLAY_MARK_AGE_S, FinishPlan, to_epoch

from .helpers import wait_for
from .test_store_contract import put


def retry_plan(**kw):
    return FinishPlan(state="pending", retries_inc=1, delay_s=0.0, **kw)


def ambiguous_plan(**kw):
    return FinishPlan(state="pending", attempts_inc=1, delay_s=0.0, maybe_acted=True, **kw)


def claim(store, sink="slack", lease_s=300.0):
    c = store.claim(sink, lease_s)
    assert c is not None
    return c


# ---- the event's received time --------------------------------------------------------------------------------------------------


def test_the_event_received_time_is_epoch_seconds_on_the_database_clock(store):
    put(store, sinks=["slack"])
    ev = store.get_event("k1")
    assert isinstance(ev["received_epoch"], float)
    assert abs(ev["received_epoch"] - time.time()) < 600  # the database clock and this process share a machine
    store.advance(3600)
    put(store, key="k2", sinks=["slack"])
    assert store.get_event("k2")["received_epoch"] - ev["received_epoch"] == pytest.approx(3600, abs=30)
    assert store.get_event("nope") is None


def test_to_epoch_reads_a_datetime_and_the_sqlite_text_form():
    assert to_epoch("2021-11-26 12:41:02.193") == pytest.approx(1637930462.193)
    assert to_epoch(datetime(2021, 11, 26, 12, 41, 2, 193000, tzinfo=timezone.utc)) == pytest.approx(1637930462.193)
    assert to_epoch(datetime(2021, 11, 26, 12, 41, 2, 193000)) == pytest.approx(1637930462.193)  # naive means UTC
    assert to_epoch(datetime(2021, 11, 26, 18, 41, 2, tzinfo=timezone(timedelta(hours=6)))) == pytest.approx(1637930462.0)


# ---- claims that nobody finished -------------------------------------------------------------------------------------------------


def test_a_first_claim_is_not_a_lost_one(store):
    put(store, sinks=["slack"])
    c = claim(store)
    assert (c.prior_claim_lost, c.maybe_acted_before, c.since_maybe_acted_s) == (False, False, None)


def test_a_claim_whose_lease_ran_out_with_no_final_write_shows_up_as_lost_on_the_next_claim(store):
    put(store, sinks=["slack"])
    first = claim(store)
    assert store.claim("slack", 300) is None  # inside the lease nobody may take it
    store.advance(301)
    second = claim(store)
    assert second.token != first.token and second.prior_claim_lost is True
    assert store.finish(second, done_plan_state()) is True


def done_plan_state():
    return FinishPlan(state="done", attempts_inc=1, ok=True, status=200)


def test_a_finished_try_brings_the_counter_back_so_a_lost_claim_is_not_flagged_for_ever(store):
    put(store, sinks=["slack"])
    claim(store)  # lost
    store.advance(301)
    second = claim(store)
    assert second.prior_claim_lost is True
    assert store.finish(second, retry_plan()) is True
    third = claim(store)
    assert third.prior_claim_lost is False  # one lost claim is remembered once, by the maybe_acted mark, not by an offset
    assert third.maybe_acted_before is True  # the lost claim left that mark when the next claim finished
    assert store.finish(third, retry_plan()) is True
    assert claim(store).prior_claim_lost is False


def test_two_lost_claims_in_a_row_are_both_noticed(store):
    put(store, sinks=["slack"])
    claim(store)
    store.advance(301)
    claim(store)
    store.advance(301)
    third = claim(store)
    assert third.prior_claim_lost is True


def test_a_plain_retry_or_pause_try_is_not_a_lost_claim(store):
    put(store, sinks=["slack"])
    for _ in range(3):
        c = claim(store)
        assert c.prior_claim_lost is False
        assert store.finish(c, retry_plan()) is True


def test_replay_restarts_the_counters_but_keeps_the_mark_that_an_earlier_try_may_have_acted(store):
    put(store, sinks=["slack"])
    claim(store)
    store.advance(301)
    c = claim(store)
    assert store.finish(c, FinishPlan(state="dead", attempts_inc=1, error="boom", status=500, maybe_acted=True)) is True
    store.advance(120)
    assert store.replay("k1", "slack") == 1
    row = store.delivery("k1", "slack")
    assert (row["attempts"], row["retries"], row["claims"]) == (0, 0, 0)
    fresh = claim(store)
    # The earlier try is remembered as it was (about two minutes ago), never forgotten, so a sink with a read back looks first.
    assert fresh.prior_claim_lost is False and fresh.maybe_acted_before is True
    assert 120 <= fresh.since_maybe_acted_s < 600


def test_replay_marks_a_row_that_never_had_a_mark_as_tried_long_ago_so_the_read_back_is_believed_at_once(store):
    put(store, sinks=["slack"])
    c = claim(store)
    assert store.finish(c, FinishPlan(state="done", attempts_inc=1, ok=True, status=200)) is True
    assert store.delivery("k1", "slack")["maybe_acted_at"] is None  # a clean 200 leaves no mark
    assert store.replay("k1", "slack", force=True) == 1
    fresh = claim(store)
    assert fresh.maybe_acted_before is True and fresh.prior_claim_lost is False
    assert fresh.since_maybe_acted_s == pytest.approx(REPLAY_MARK_AGE_S, abs=30)


# ---- the mark of a try that may have reached the vendor ------------------------------------------------------------------------------


def test_an_ambiguous_try_leaves_a_mark_with_its_age_on_the_database_clock(store):
    put(store, sinks=["slack"])
    c = claim(store)
    assert store.finish(c, ambiguous_plan()) is True
    store.advance(42)
    nxt = claim(store)
    assert nxt.maybe_acted_before is True and nxt.since_maybe_acted_s == pytest.approx(42, abs=5)


def test_a_try_that_certainly_did_not_act_leaves_no_mark(store):
    put(store, sinks=["slack"])
    c = claim(store)
    assert store.finish(c, retry_plan(status=429)) is True
    nxt = claim(store)
    assert nxt.maybe_acted_before is False and nxt.since_maybe_acted_s is None


def test_the_mark_keeps_its_first_time_until_another_try_may_have_acted(store):
    put(store, sinks=["slack"])
    store.finish(claim(store), ambiguous_plan())
    store.advance(100)
    c = claim(store)
    assert c.since_maybe_acted_s == pytest.approx(100, abs=5)
    store.finish(c, retry_plan())  # a clean retry does not refresh the mark
    store.advance(10)
    assert claim(store).since_maybe_acted_s == pytest.approx(110, abs=5)


# ---- the append lock ------------------------------------------------------------------------------------------------------------------


def in_thread(fn):
    box: dict = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001
            box["error"] = exc

    t = threading.Thread(target=run)
    t.start()
    return t, box


def try_lock_elsewhere(store, key: str, ttl: float = 60.0) -> bool:
    def go():
        with store.append_lock(key, ttl) as got:
            return got

    t, box = in_thread(go)
    t.join(30)
    assert "error" not in box, box
    return box["value"]


def test_a_second_holder_cannot_take_the_lock_and_the_first_can_release_it(store):
    with store.append_lock("sheets:one", 60) as got:
        assert got is True
        assert try_lock_elsewhere(store, "sheets:one") is False  # another connection, same key
    assert try_lock_elsewhere(store, "sheets:one") is True  # released when the with block ended


def test_different_spreadsheets_never_block_each_other(store):
    with store.append_lock("sheets:one", 60) as a:
        assert a is True
        assert try_lock_elsewhere(store, "sheets:two") is True


def test_the_lock_is_released_when_the_holder_raises(store):
    with pytest.raises(RuntimeError):
        with store.append_lock("sheets:one", 60) as got:
            assert got is True
            raise RuntimeError("append blew up")
    assert try_lock_elsewhere(store, "sheets:one") is True


def test_a_second_caller_that_did_not_get_the_lock_does_not_release_the_holders_lock(store):
    with store.append_lock("sheets:one", 60) as got:
        assert got is True

        def loser():
            with store.append_lock("sheets:one", 60) as g:
                return g

        t, box = in_thread(loser)
        t.join(30)
        assert box["value"] is False
        assert try_lock_elsewhere(store, "sheets:one") is False  # the loser's exit did not free it


def test_a_lock_row_left_by_a_dead_holder_expires_on_the_database_clock(store):
    if store.dialect != "sqlite":
        pytest.skip("Postgres advisory locks belong to a connection and vanish with it, there is no row to expire")
    lock = store.append_lock("sheets:one", 60)
    holder = lock.__enter__()  # take it and never release it, as a killed worker would
    assert holder is True
    assert try_lock_elsewhere(store, "sheets:one", 60) is False
    store.advance(59)
    assert try_lock_elsewhere(store, "sheets:one", 60) is False
    store.advance(2)
    assert try_lock_elsewhere(store, "sheets:one", 60) is True  # the ttl ran out


def test_concurrent_callers_never_hold_the_same_lock_together(store):
    n = 8
    barrier = threading.Barrier(n)
    state = {"tried": 0, "holders": 0}
    mutex = threading.Lock()

    def go():
        barrier.wait()
        with store.append_lock("sheets:race", 60) as got:
            with mutex:
                state["tried"] += 1
                state["holders"] += 1 if got else 0
            if got:  # hold it until every other caller has tried and been told no
                wait_for(lambda: state["tried"] == n, what="every caller to try the lock")

    threads = [in_thread(go)[0] for _ in range(n)]
    for t in threads:
        t.join(60)
    assert state == {"tried": n, "holders": 1}
