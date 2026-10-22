"""Store contract, run on SQLite and Postgres with the same expectations.

Each test names the guard it protects; tests/mutants.py removes that guard and requires this file to go red.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

from relay.store.base import FinishPlan

SINKS = ["make", "slack"]


def put(store, key="k1", sinks=None, window_s=None, basis="native", tenant="acme", payload='{"a":1}'):
    return store.ingest(key=key, tenant=tenant, source="t", basis=basis, payload=payload, sinks=SINKS if sinks is None else sinks, window_s=window_s)


def done_plan(**kw):
    return FinishPlan(state="done", attempts_inc=1, ok=True, status=200, **kw)


def test_schema_has_every_table_including_the_later_tier_ones(store):
    names = set()
    with store.tx(write=False) as t:
        names = set(store._table_names(t))
    assert {"events", "deliveries", "attempts", "suppressed", "sink_state", "spend_daily", "fathom_meetings", "alerts", "alert_state"} <= names


def test_ingest_writes_one_event_and_one_delivery_per_sink(store):
    r = put(store)
    assert r.duplicate is False and r.event_id == "k1"
    assert [e["event_id"] for e in store.events()] == ["k1"]
    assert sorted(d["sink"] for d in store.deliveries("k1")) == SINKS
    assert all(d["state"] == "pending" and d["attempts"] == 0 for d in store.deliveries("k1"))


def test_same_key_many_times_gives_one_event_and_one_delivery_per_sink(store):
    first = put(store)
    assert first.duplicate is False
    for _ in range(9):
        assert put(store).duplicate is True
    assert len(store.events()) == 1
    assert len(store.deliveries()) == len(SINKS)
    assert store.health_counts()["suppressed"] == 9


def test_same_key_concurrently_gives_one_event_and_one_delivery_per_sink(store):
    n = 12
    barrier = threading.Barrier(n)
    results: list = []
    errors: list = []

    def go():
        barrier.wait()
        try:
            results.append(put(store, key="race"))
        except Exception as exc:  # recorded so the assertion below can name it
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=go) for _ in range(n)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert errors == []
    assert sum(1 for r in results if not r.duplicate) == 1
    assert len(store.events()) == 1
    assert len(store.deliveries()) == len(SINKS)
    assert store.health_counts()["suppressed"] == n - 1


@pytest.mark.parametrize("where", ["ingest_after_event", "ingest_mid_deliveries"])
def test_ingest_is_one_transaction_a_crash_part_way_leaves_nothing(store, where):
    def boom():
        raise RuntimeError("process died part way through the ingest")

    store.faults[where] = boom
    with pytest.raises(RuntimeError):
        put(store, key="atomic")
    store.faults.clear()
    assert store.events() == []
    assert store.deliveries() == []
    assert store.health_counts()["suppressed"] == 0
    # Positive control: the same call without the fault works, so the empty result above came from the rollback.
    assert put(store, key="atomic").duplicate is False
    assert len(store.deliveries("atomic")) == len(SINKS)


def test_a_native_key_dedupes_forever(store):
    put(store, key="n1")
    store.advance(100 * 86400)
    assert put(store, key="n1").duplicate is True


def test_derived_key_inside_window_is_suppressed_and_after_window_is_a_new_event(store):
    window = 3600.0
    a = put(store, key="h1", basis="body-hash", window_s=window)
    assert (a.event_id, a.duplicate) == ("h1", False)
    store.advance(600)  # t = 10 minutes
    b = put(store, key="h1", basis="body-hash", window_s=window)
    assert b.duplicate is True
    store.advance(2 * 3600)  # t = 2 hours 10 minutes, past the window
    c = put(store, key="h1", basis="body-hash", window_s=window)
    assert (c.event_id, c.duplicate) == ("h1|1", False)
    store.advance(600)
    assert put(store, key="h1", basis="body-hash", window_s=window).duplicate is True
    store.advance(2 * 3600)
    d = put(store, key="h1", basis="body-hash", window_s=window)
    assert d.event_id == "h1|2" and d.duplicate is False
    assert sorted(e["event_id"] for e in store.events()) == ["h1", "h1|1", "h1|2"]
    assert store.health_counts()["suppressed"] == 2


def test_a_different_derived_key_is_never_suppressed(store):
    put(store, key="x", basis="body-hash", window_s=3600.0)
    assert put(store, key="y", basis="body-hash", window_s=3600.0).duplicate is False


def test_derived_key_concurrently_gives_one_event(store):
    n = 8
    barrier = threading.Barrier(n)
    results: list = []

    def go():
        barrier.wait()
        results.append(put(store, key="dr", basis="workflow-key", window_s=3600.0))

    threads = [threading.Thread(target=go) for _ in range(n)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert sum(1 for r in results if not r.duplicate) == 1
    assert len(store.events()) == 1


def test_claim_takes_one_row_and_hands_out_a_token(store):
    put(store, key="a")
    put(store, key="b")
    put(store, key="c")
    claim = store.claim("slack", lease_s=300)
    assert claim is not None and claim.sink == "slack" and claim.token
    left = [d for d in store.deliveries() if d["sink"] == "slack" and d["claim_token"] is None]
    assert len(left) == 2  # ONE row per claim


def test_a_claimed_row_is_not_claimable_until_its_lease_expires(store):
    put(store, key="l1", sinks=["slack"])
    first = store.claim("slack", lease_s=300)
    assert first is not None
    assert store.claim("slack", lease_s=300) is None
    store.advance(299)
    assert store.claim("slack", lease_s=300) is None
    store.advance(2)
    second = store.claim("slack", lease_s=300)
    assert second is not None and second.event_id == "l1" and second.token != first.token


def test_a_row_not_yet_due_is_not_claimed(store):
    put(store, key="nd", sinks=["slack"])
    claim = store.claim("slack", lease_s=300)
    plan = FinishPlan(state="pending", retries_inc=1, delay_s=120.0, status=429)
    assert store.finish(claim, plan) is True
    assert store.claim("slack", lease_s=300) is None
    store.advance(121)
    assert store.claim("slack", lease_s=300) is not None


def test_claim_only_takes_pending_rows_for_the_asked_sink(store):
    put(store, key="s1", sinks=["slack"])
    assert store.claim("make", lease_s=300) is None
    c = store.claim("slack", lease_s=300)
    store.finish(c, done_plan())
    store.advance(10_000)
    assert store.claim("slack", lease_s=300) is None  # done rows are never claimed again


def test_the_final_write_is_conditional_on_the_claim_token(store):
    put(store, key="st", sinks=["slack"])
    stale = store.claim("slack", lease_s=300)
    store.advance(301)
    fresh = store.claim("slack", lease_s=300)
    assert fresh is not None and fresh.token != stale.token
    assert store.finish(fresh, done_plan()) is True
    # The stale worker finishes late with a different verdict. It must not overwrite the newer result.
    assert store.finish(stale, FinishPlan(state="dead", attempts_inc=1, error="late")) is False
    row = store.delivery("st", "slack")
    assert row["state"] == "done" and row["last_error"] is None
    assert len(store.attempts_for("st", "slack")) == 1  # the stale write left no attempts row either


def test_a_stale_worker_cannot_finish_a_row_a_newer_worker_still_holds(store):
    """The token check on its own: the row is still pending (the newer worker has not finished), so only the token tells
    the stale worker's write from the holder's."""
    put(store, key="held", sinks=["slack"])
    stale = store.claim("slack", lease_s=300)
    store.advance(301)
    fresh = store.claim("slack", lease_s=300)
    assert store.finish(stale, done_plan()) is False
    row = store.delivery("held", "slack")
    assert row["state"] == "pending" and row["claim_token"] == fresh.token and store.attempts_for("held", "slack") == []
    assert store.finish(fresh, done_plan()) is True


def test_the_final_write_ignores_a_row_that_is_no_longer_pending(store):
    put(store, key="np", sinks=["slack"])
    c = store.claim("slack", lease_s=300)
    assert store.finish(c, done_plan()) is True
    assert store.finish(c, done_plan()) is False  # token was cleared and the state is done


def test_state_and_attempt_row_are_written_in_one_transaction(store):
    put(store, key="tx", sinks=["slack"])
    c = store.claim("slack", lease_s=300)

    def boom():
        raise RuntimeError("process died between the state change and the attempts row")

    store.faults["finish_mid"] = boom
    with pytest.raises(RuntimeError):
        store.finish(c, done_plan())
    store.faults.clear()
    row = store.delivery("tx", "slack")
    assert row["state"] == "pending" and row["claim_token"] == c.token  # the state change rolled back with the log
    assert store.attempts_for("tx", "slack") == []
    assert store.finish(c, done_plan()) is True  # positive control: same call, no fault
    assert len(store.attempts_for("tx", "slack")) == 1


def test_attempts_and_retries_are_counted_apart(store):
    put(store, key="cnt", sinks=["slack"])
    c = store.claim("slack", lease_s=300)
    store.finish(c, FinishPlan(state="pending", retries_inc=1, delay_s=1.0, status=429))
    store.advance(2)
    c = store.claim("slack", lease_s=300)
    assert (c.attempts, c.retries) == (0, 1)
    store.finish(c, FinishPlan(state="pending", attempts_inc=1, delay_s=1.0, status=500))
    store.advance(2)
    c = store.claim("slack", lease_s=300)
    assert (c.attempts, c.retries) == (1, 1)
    assert [a["n"] for a in store.attempts_for("cnt", "slack")] == [1, 2]


def test_a_pause_keeps_a_tenants_rows_unclaimable_then_releases_them(store):
    put(store, key="p1", sinks=["slack"])
    put(store, key="p2", sinks=["slack"])
    c = store.claim("slack", lease_s=300)
    store.finish(c, FinishPlan(state="pending", retries_inc=1, delay_s=1.0, pause_s=900.0, pause_reason="forbidden (403)", status=403))
    store.advance(5)
    assert store.claim("slack", lease_s=300) is None  # paused for this tenant, both leads wait
    assert store.sink_state("acme", "slack")["paused"]
    store.advance(900)
    assert store.claim("slack", lease_s=300) is not None


def test_pause_is_per_tenant(store):
    put(store, key="t1", sinks=["slack"], tenant="acme")
    put(store, key="t2", sinks=["slack"], tenant="other")
    c = store.claim("slack", lease_s=300)
    paused_tenant = c.tenant
    store.finish(c, FinishPlan(state="pending", retries_inc=1, delay_s=1.0, pause_s=900.0, pause_reason="x"))
    store.advance(5)
    other = store.claim("slack", lease_s=300)
    assert other is not None and other.tenant != paused_tenant


def test_the_circuit_breaker_opens_at_the_threshold_and_half_opens_later(store):
    for i in range(7):
        put(store, key=f"b{i}", sinks=["slack"])
    for _ in range(5):
        c = store.claim("slack", lease_s=300)
        assert c is not None
        store.finish(c, FinishPlan(state="pending", attempts_inc=0, retries_inc=1, delay_s=1.0, breaker="fail", breaker_threshold=5, breaker_open_s=60.0))
        store.advance(0.001)
    assert store.sink_state("acme", "slack")["breaker_open"]
    assert store.claim("slack", lease_s=300) is None
    store.advance(61)
    probe = store.claim("slack", lease_s=300)
    assert probe is not None
    store.finish(probe, FinishPlan(state="done", attempts_inc=1, ok=True, breaker="ok"))
    st = store.sink_state("acme", "slack")
    assert st["consecutive_failures"] == 0 and not st["breaker_open"]


def test_four_failures_do_not_open_the_breaker(store):
    for i in range(6):
        put(store, key=f"f{i}", sinks=["slack"])
    for _ in range(4):
        c = store.claim("slack", lease_s=300)
        store.finish(c, FinishPlan(state="pending", retries_inc=1, delay_s=0.0, breaker="fail", breaker_threshold=5, breaker_open_s=60.0))
    assert not store.sink_state("acme", "slack")["breaker_open"]
    assert store.claim("slack", lease_s=300) is not None


def test_a_minimum_interval_is_a_database_time_not_a_sleep(store, monkeypatch):
    def no_sleep(_s):
        raise AssertionError("the store must never sleep to enforce an interval")

    monkeypatch.setattr(time, "sleep", no_sleep)
    put(store, key="m1", sinks=["slack"])
    put(store, key="m2", sinks=["slack"])
    c = store.claim("slack", lease_s=300)
    store.finish(c, FinishPlan(state="done", attempts_inc=1, ok=True, min_interval_s=1.0))
    assert store.claim("slack", lease_s=300) is None  # the second row waits for next_send_at
    store.advance(1.01)
    assert store.claim("slack", lease_s=300) is not None


def test_expire_old_kills_only_pending_rows_past_the_limit_and_alerts(store):
    put(store, key="old", sinks=["slack"])
    store.advance(10 * 3600)
    put(store, key="new", sinks=["slack"])
    killed = store.expire_old({"slack": 5 * 3600.0})
    assert [k["event_id"] for k in killed] == ["old"]
    assert store.delivery("old", "slack")["state"] == "dead"
    assert store.delivery("new", "slack")["state"] == "pending"
    assert [a["event_id"] for a in store.alerts_unsent("slack")] == ["old"]


def test_expire_old_leaves_a_row_a_worker_holds(store):
    put(store, key="held", sinks=["slack"])
    store.claim("slack", lease_s=20 * 3600)
    store.advance(10 * 3600)  # older than the limit, but a worker still holds the lease
    assert store.expire_old({"slack": 5 * 3600.0}) == []
    assert store.delivery("held", "slack")["state"] == "pending"
    store.advance(11 * 3600)  # the lease ran out, so nobody holds it any more
    assert [k["event_id"] for k in store.expire_old({"slack": 5 * 3600.0})] == ["held"]


def test_replay_resets_only_dead_rows_by_default(store):
    put(store, key="r1", sinks=["slack", "make"])
    a = store.claim("slack", lease_s=300)
    store.finish(a, FinishPlan(state="dead", attempts_inc=1, error="http 400"))
    b = store.claim("make", lease_s=300)
    store.finish(b, done_plan())
    assert store.replay("r1") == 1
    assert store.delivery("r1", "slack")["state"] == "pending"
    assert store.delivery("r1", "make")["state"] == "done"


def test_replay_of_a_done_row_needs_force_and_resets_the_age_clock(store):
    put(store, key="r2", sinks=["slack"])
    c = store.claim("slack", lease_s=300)
    store.finish(c, done_plan())
    assert store.replay("r2", "slack") == 0
    store.advance(7 * 86400)
    assert store.replay("r2", "slack", force=True) == 1
    row = store.delivery("r2", "slack")
    assert row["state"] == "pending" and row["attempts"] == 0
    # created_at was reset, so the age limit does not kill the replayed row on its first claim
    again = store.claim("slack", lease_s=300)
    assert again is not None and again.age_s < 5


def test_backfill_inserts_only_the_missing_rows_since_a_time(store):
    put(store, key="e1", sinks=["slack"])
    store.advance(3600)
    put(store, key="e2", sinks=["slack"])
    since = datetime.now(timezone.utc) + timedelta(seconds=store.offset_s - 1800)  # between the two events
    inserted = store.backfill("acme", "make", since)
    assert inserted == 1
    assert store.delivery("e2", "make") is not None and store.delivery("e1", "make") is None
    assert store.backfill("acme", "make", since) == 0  # nothing new the second time


def test_backfill_leaves_an_existing_delivery_untouched(store):
    put(store, key="bf", sinks=["slack"])
    c = store.claim("slack", lease_s=300)
    store.finish(c, done_plan())
    far_past = datetime(2000, 1, 1, tzinfo=timezone.utc)
    assert store.backfill("acme", "slack", far_past) == 0
    assert store.delivery("bf", "slack")["state"] == "done"


def test_purge_nulls_old_payloads_and_dedupe_still_holds(store):
    put(store, key="pg", payload='{"secret":"lead data"}')
    store.advance(29 * 86400)
    assert store.purge(30) == 0
    store.advance(2 * 86400)
    assert store.purge(30) == 1
    assert store.get_event("pg")["payload"] is None
    assert put(store, key="pg").duplicate is True


def test_the_lease_uses_the_database_clock_not_the_process_clock(store, monkeypatch):
    put(store, key="clk", sinks=["slack"])
    assert store.claim("slack", lease_s=300) is not None
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 10 * 365 * 86400)  # a wildly wrong process clock
    assert store.claim("slack", lease_s=300) is None  # still leased: the database says only a moment has passed
    store.advance(301)
    assert store.claim("slack", lease_s=300) is not None


def test_concurrent_claims_never_hand_out_a_row_twice(store):
    total = 24
    for i in range(total):
        put(store, key=f"c{i:02d}", sinks=["slack"])
    claimed: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        while True:
            c = store.claim("slack", lease_s=600)
            if c is None:
                return
            with lock:
                claimed.append(c.event_id)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert sorted(claimed) == sorted(f"c{i:02d}" for i in range(total))  # every row exactly once


def test_overlapping_claims_skip_a_row_another_worker_is_taking_instead_of_coming_back_empty(store):
    n = 8
    for i in range(n + 4):
        put(store, key=f"o{i:02d}", sinks=["slack"])
    barrier = threading.Barrier(n)
    got: list = []
    lock = threading.Lock()

    def one_claim():
        barrier.wait()
        c = store.claim("slack", lease_s=600)
        with lock:
            got.append(c)

    threads = [threading.Thread(target=one_claim) for _ in range(n)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert all(c is not None for c in got), "a claimer found nothing although unclaimed rows remained"
    assert len({c.event_id for c in got}) == n


def test_a_claim_skips_a_row_locked_by_another_connection_without_waiting_for_it(store):
    """Deterministic version of the SKIP LOCKED guarantee: a second connection holds the first row's lock open and a
    claim must come back with a DIFFERENT row at once. Without SKIP LOCKED the claim queues behind the lock; the
    lock_timeout turns that into an error in about a second instead of a hang."""
    if store.dialect != "postgres":
        pytest.skip("row locks that a claim must skip are a Postgres behaviour (SQLite serialises writers)")
    import psycopg

    for key in ("lk1", "lk2", "lk3"):
        put(store, key=key, sinks=["slack"])
    holder = psycopg.connect(store.url, options=f"-c search_path={store.schema}")
    try:
        holder.execute("BEGIN")
        holder.execute("SELECT 1 FROM deliveries WHERE event_id = %s FOR UPDATE", ("lk1",))  # the row a claim would pick first
        with store.tx() as t:
            t.execute("SET LOCAL lock_timeout = '1500ms'")
            claim = store.claim("slack", lease_s=300)
        assert claim is not None and claim.event_id != "lk1"
    finally:
        holder.rollback()
        holder.close()
    assert store.delivery("lk1", "slack")["claim_token"] is None  # the locked row was left alone, not claimed


def test_due_in_s_reports_none_zero_and_the_wait(store):
    assert store.due_in_s() is None
    put(store, key="d1", sinks=["slack"])
    assert store.due_in_s() == 0.0
    c = store.claim("slack", lease_s=300)
    assert 290 < store.due_in_s() <= 300.5  # leased, so due when the lease ends
    store.finish(c, FinishPlan(state="pending", retries_inc=1, delay_s=120.0))
    assert 110 < store.due_in_s() <= 120
    store.advance(130)
    assert store.due_in_s() == 0.0


def test_due_in_s_accounts_for_a_pause(store):
    put(store, key="dp", sinks=["slack"])
    c = store.claim("slack", lease_s=300)
    store.finish(c, FinishPlan(state="pending", retries_inc=1, delay_s=1.0, pause_s=900.0, pause_reason="x"))
    assert 890 < store.due_in_s() <= 900


def test_alert_gate_lets_exactly_one_caller_through_per_interval(store):
    store.enqueue_alert("slack", "acme", "dead", "http 400")
    assert store.alert_sinks_waiting() == ["slack"]
    wins = []
    barrier = threading.Barrier(6)

    def go():
        barrier.wait()
        wins.append(store.alert_gate("slack", 900.0))

    threads = [threading.Thread(target=go) for _ in range(6)]
    [t.start() for t in threads]
    [t.join(30) for t in threads]
    assert sum(wins) == 1
    assert store.alert_gate("slack", 900.0) is False
    store.advance(901)
    assert store.alert_gate("slack", 900.0) is True


def test_alert_gate_release_reopens_after_a_short_retry_delay(store):
    store.enqueue_alert("slack", "acme", "dead", "r")
    assert store.alert_gate("slack", 900.0) is True
    store.alert_gate_release("slack", 900.0, retry_in_s=60.0)
    assert store.alert_gate("slack", 900.0) is False
    store.advance(61)
    assert store.alert_gate("slack", 900.0) is True


def test_unsent_alerts_are_capped_per_sink(store):
    for i in range(250):
        store.enqueue_alert("slack", "acme", "dead", f"r{i}")
    assert len(store.alerts_unsent("slack", limit=1000)) == 200


def test_health_counts(store):
    put(store, key="h1")
    put(store, key="h1")
    counts = store.health_counts()
    assert counts["events"] == 1 and counts["suppressed"] == 1 and counts["deliveries"] == {"pending": 2}


def test_dump_all_text_reads_every_column(store):
    put(store, key="dump-me", payload='{"x":"visible-marker"}')
    text = "\n".join(store.dump_all_text())
    assert "dump-me" in text and "visible-marker" in text


def test_a_delivery_needs_its_event(store):
    with pytest.raises(Exception):
        with store.tx() as t:
            t.execute(
                f"INSERT INTO deliveries (event_id, sink, tenant, created_at, next_at) VALUES ('ghost', 'slack', 'acme', {store.now()}, {store.now()})"
            )


def test_several_processes_opening_a_fresh_database_at_once_all_succeed(tmp_path):
    """gunicorn starts N workers together and each one creates the tables on a brand new file."""
    from relay.store import open_store

    path = str(tmp_path / "fresh.db")
    n = 8
    barrier = threading.Barrier(n)
    errors: list[str] = []
    stores: list = []

    def start():
        barrier.wait()
        try:
            stores.append(open_store(None, path))
        except Exception as exc:
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=start) for _ in range(n)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    assert errors == [] and len(stores) == n
    for s in stores:
        s.close()


def test_a_schema_failure_that_persists_is_raised_not_swallowed(monkeypatch, tmp_path):
    from relay.store import open_store, sqlite

    calls = []

    def always_fails(self):
        calls.append(1)
        raise RuntimeError("cannot create tables")

    monkeypatch.setattr(sqlite.SqliteStore, "init_schema", always_fails)
    monkeypatch.setattr("relay.store.SCHEMA_RETRY_WAIT_S", 0.0)
    with pytest.raises(RuntimeError):
        open_store(None, str(tmp_path / "x.db"))
    assert len(calls) == 8
