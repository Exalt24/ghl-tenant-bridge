"""Fan-out behaviour end to end: ingest, claim, send to the fakes, final write, retry accounting, pauses, the breaker,
independence between sinks, alerts and the operator actions."""

from __future__ import annotations

import json
import random
from datetime import datetime
import threading
import time

import pytest

from relay import ghl
from relay.fanout import MAX_ATTEMPTS, Hooks, Unrouted, ambiguous_backoff_s, backoff_s, ingest, plan_for
from relay.sinks.base import Outcome
from relay.sinks.make import MakeSink
from relay.store.base import Claim

from .fakes.slack import FakeSlack
from .helpers import ScriptSink, TENANT, ingest_one, make_cfg, make_rig, payload, wait_for


@pytest.fixture(autouse=True)
def record_sleeps(monkeypatch):
    """Nothing in the relay may sleep to rate limit. Real sleeps are recorded here so a test can assert there were none
    (the polling helper in helpers.py waits on a threading.Event, so it is not recorded here)."""
    calls: list[float] = []
    real = time.sleep
    monkeypatch.setattr(time, "sleep", lambda s: calls.append(s))
    yield calls
    monkeypatch.setattr(time, "sleep", real)


def alert_kinds(store, sink: str = "slack") -> list[str]:
    """Every alert row for a sink, sent or not, so a test can see what the worker's digest step already consumed."""
    with store.tx(write=False) as t:
        return [r["kind"] for r in t.execute("SELECT kind FROM alerts WHERE sink = ? ORDER BY id", (sink,)).fetchall()]


def claim(attempts=0, retries=0) -> Claim:
    return Claim(event_id="e", sink="slack", tenant=TENANT, attempts=attempts, retries=retries, token="t", age_s=1.0)


def scfg(cfg):
    return cfg.tenants[TENANT].sinks["slack"]


# ---- ingest through the fan-out layer ----------------------------------------------------------------------------------


def test_ingest_freezes_the_enabled_sink_set_and_skips_disabled_sinks(store, slack, make):
    cfg = make_cfg(slack.url, make.url, slack_extra={"enabled": False})
    r = ingest_one(store, cfg)
    assert [d["sink"] for d in store.deliveries(r.event_id)] == ["make"]


def test_a_tenant_with_no_enabled_sink_is_unrouted(store, slack, make):
    cfg = make_cfg(slack.url, make.url, slack_extra={"enabled": False}, make_extra={"enabled": False})
    with pytest.raises(Unrouted):
        ingest_one(store, cfg)
    assert store.events() == []


def test_a_native_key_has_no_window_and_a_derived_key_uses_the_tenant_window(store, slack, make):
    cfg = make_cfg(slack.url, make.url, tenant_extra={"dedupe_window_s": 120})
    p = payload()
    for basis in (ghl.BASIS_WORKFLOW_KEY, ghl.BASIS_SYNTHESISED, ghl.BASIS_BODY_HASH):
        key = f"key-{basis}"
        a = ingest(store, cfg, tenant=TENANT, key=key, basis=basis, source="t", payload_json=json.dumps(p))
        store.advance(60)
        b = ingest(store, cfg, tenant=TENANT, key=key, basis=basis, source="t", payload_json=json.dumps(p))
        store.advance(120)
        c = ingest(store, cfg, tenant=TENANT, key=key, basis=basis, source="t", payload_json=json.dumps(p))
        assert (a.duplicate, b.duplicate, c.duplicate) == (False, True, False), basis
    n = ingest(store, cfg, tenant=TENANT, key="nat", basis=ghl.BASIS_NATIVE, source="t", payload_json="{}")
    store.advance(10_000)
    assert ingest(store, cfg, tenant=TENANT, key="nat", basis=ghl.BASIS_NATIVE, source="t", payload_json="{}").duplicate is True and not n.duplicate


# ---- the retry arithmetic, pure -----------------------------------------------------------------------------------------


def test_backoff_is_exponential_capped_and_jittered():
    rng = random.Random(3)
    for n, hi in ((0, 60), (1, 120), (2, 240), (5, 1920), (6, 3600), (20, 3600)):
        for _ in range(50):
            v = backoff_s(n, rng)
            assert hi / 2 <= v <= hi, (n, v)
    assert len({backoff_s(3, rng) for _ in range(20)}) > 1  # jitter, not a constant


def test_ambiguous_backoff_starts_at_30_minutes_caps_at_an_hour_and_never_shrinks_below_its_base():
    rng = random.Random(3)
    for n, base in ((0, 1800), (1, 3600), (2, 3600), (4, 3600), (20, 3600)):
        for _ in range(50):
            v = ambiguous_backoff_s(n, rng)
            assert base <= v <= base * 1.25, (n, v)  # jitter only adds, so the floor below is a guarantee
    assert len({ambiguous_backoff_s(3, rng) for _ in range(20)}) > 1


def test_the_five_waits_between_six_ambiguous_tries_total_at_least_four_hours_whatever_the_jitter():
    class Lowest:  # the draw that gives the least jitter
        @staticmethod
        def random():
            return 0.0

    floor = sum(ambiguous_backoff_s(n, Lowest()) for n in range(MAX_ATTEMPTS - 1))
    assert floor >= 4 * 3600 and floor < 72 * 3600


def test_plan_retry_spends_no_attempt_and_ambiguous_spends_one(slack, make):
    cfg = make_cfg(slack.url, make.url)
    rng = random.Random(1)
    retry = plan_for(Outcome.retry(after_s=7, status=429, rate_limited=True), claim(), scfg(cfg), rng, sent=True)
    assert (retry.state, retry.attempts_inc, retry.retries_inc, retry.delay_s, retry.rate_limit_s) == ("pending", 0, 1, 7, 7)
    amb = plan_for(Outcome.ambiguous(status=500), claim(attempts=2), scfg(cfg), rng, sent=True)
    assert (amb.state, amb.attempts_inc, amb.retries_inc, amb.breaker) == ("pending", 1, 0, "fail")
    assert 3600 <= amb.delay_s <= 4500  # the third ambiguous try waits an hour (plus up to 25 percent), not 4 minutes
    assert plan_for(Outcome.ambiguous(status=500), claim(attempts=0), scfg(cfg), rng, sent=True).delay_s >= 1800


def test_plan_ambiguous_goes_dead_when_the_sixth_try_is_spent(slack, make):
    cfg = make_cfg(slack.url, make.url)
    rng = random.Random(1)
    assert MAX_ATTEMPTS == 6
    fifth = plan_for(Outcome.ambiguous(status=500), claim(attempts=4), scfg(cfg), rng, sent=True)
    sixth = plan_for(Outcome.ambiguous(status=500), claim(attempts=5), scfg(cfg), rng, sent=True)
    assert fifth.state == "pending" and sixth.state == "dead"
    assert "attempt budget" in sixth.error and sixth.alerts and sixth.alerts[0][0] == "dead"


def test_plan_many_retries_never_exhaust_the_budget(slack, make):
    cfg = make_cfg(slack.url, make.url)
    plan = plan_for(Outcome.retry(), claim(attempts=0, retries=500), scfg(cfg), random.Random(1), sent=True)
    assert plan.state == "pending" and plan.attempts_inc == 0 and plan.delay_s <= 3600


def test_plan_terminal_outcomes(slack, make):
    cfg = make_cfg(slack.url, make.url)
    rng = random.Random(1)
    done = plan_for(Outcome.done(), claim(), scfg(cfg), rng, sent=True)
    assert (done.state, done.ok, done.breaker, done.min_interval_s, done.alerts) == ("done", True, "ok", 1.0, [])
    dead = plan_for(Outcome.dead("http 400", status=400), claim(), scfg(cfg), rng, sent=True)
    assert dead.state == "dead" and dead.alerts == [("dead", "http 400")]
    skipped = plan_for(Outcome.skipped("not a lead"), claim(), scfg(cfg), rng, sent=False)
    assert skipped.state == "skipped" and skipped.alerts == [] and skipped.min_interval_s is None
    loud = plan_for(Outcome.skipped("event has no type field", alert=True), claim(), scfg(cfg), rng, sent=False)
    assert loud.state == "skipped" and loud.alerts == [("skipped", "event has no type field")]  # a skip that asks to be heard is
    assert plan_for(Outcome.skipped("", alert=True), claim(), scfg(cfg), rng, sent=False).alerts == [("skipped", "skipped")]
    pause = plan_for(Outcome.pause("forbidden (403)"), claim(), scfg(cfg), rng, sent=True)
    assert pause.state == "pending" and pause.attempts_inc == 0 and pause.pause_s == 900.0 and pause.alerts == [("pause", "forbidden (403)")]


def test_the_alert_digest_counts_skipped_alerts_apart_from_dead_and_paused_ones():
    from relay.alerts import digest_text

    rows = [{"kind": "skipped", "tenant": "acme", "reason": "event has no type field"} for _ in range(3)] + [
        {"kind": "dead", "tenant": "acme", "reason": "http 400"}, {"kind": "pause", "tenant": "beta", "reason": "forbidden"},
        {"kind": "routing", "tenant": None, "reason": "unmapped location"},
    ]
    text = digest_text("capi", rows)
    assert "6 item(s)" in text and "1 dead, 1 paused, 3 skipped, 1 other" in text and "tenants acme, beta" in text
    assert digest_text("capi", rows[:3]) == "relay alert, capi: 3 item(s). 3 skipped. tenants acme. reasons: event has no type field"


def test_an_unknown_outcome_kind_is_dead_never_a_silent_success(slack, make):
    cfg = make_cfg(slack.url, make.url)
    plan = plan_for(Outcome("MYSTERY"), claim(), scfg(cfg), random.Random(1), sent=True)
    assert plan.state == "dead"


# ---- end to end through the fakes ------------------------------------------------------------------------------------------


def test_a_lead_reaches_both_sinks_once_and_the_log_says_so(store, slack, make):
    cfg = make_cfg(slack.url, make.url)
    rig = make_rig(store, cfg)
    r = ingest_one(store, cfg)
    assert rig.settle() == 2
    assert (slack.effects, make.effects) == (1, 1)
    for sink in ("slack", "make"):
        d = store.delivery(r.event_id, sink)
        assert d["state"] == "done" and d["attempts"] == 1 and d["retries"] == 0 and d["claim_token"] is None
        assert [a["ok"] for a in store.attempts_for(r.event_id, sink)] == [1]
    assert store.delivery(r.event_id, "make")["effect_ref"] == "queue"


def test_one_sink_failing_leaves_the_others_done_with_a_positive_control(store, slack, make):
    cfg = make_cfg(slack.url, make.url)
    rig = make_rig(store, cfg)
    # positive control: with both fakes healthy the lead is delivered everywhere
    control = ingest_one(store, cfg, payload(1))
    rig.settle()
    assert (slack.effects, make.effects) == (1, 1)
    assert {store.delivery(control.event_id, s)["state"] for s in ("slack", "make")} == {"done"}
    # now Make is broken for good: Slack must still finish, once, and Make must not drag it down
    make.mode = "bad_request"
    broken = ingest_one(store, cfg, payload(2))
    rig.settle()
    assert store.delivery(broken.event_id, "slack")["state"] == "done" and slack.effects == 2
    assert store.delivery(broken.event_id, "make")["state"] == "dead"
    assert make.effects == 1  # the dead call recorded no effect


def test_a_dead_sink_does_not_cause_the_done_sink_to_be_sent_again(store, slack, make):
    cfg = make_cfg(slack.url, make.url)
    rig = make_rig(store, cfg)
    make.mode = "bad_request"
    ingest_one(store, cfg)
    rig.settle()
    rig.settle()
    store.advance(7200)
    rig.settle()
    assert slack.effects == 1  # done once, never touched again by anything that happened to Make


def test_an_exception_in_one_sinks_pass_does_not_stop_the_other_sink(store, slack, make, monkeypatch):
    cfg = make_cfg(slack.url, make.url)
    rig = make_rig(store, cfg)
    ingest_one(store, cfg)
    real_claim = store.claim

    def claim_that_breaks_for_make(sink, lease_s):
        if sink == "make":
            raise RuntimeError("database hiccup for one sink")
        return real_claim(sink, lease_s)

    monkeypatch.setattr(store, "claim", claim_that_breaks_for_make)
    rig.worker.run_once(wait_horizon_s=0)  # must not raise
    assert slack.effects == 1 and make.effects == 0


# ---- retry accounting ------------------------------------------------------------------------------------------------------


def test_many_leads_against_a_rate_limiting_vendor_all_arrive_and_none_die(store, slack, make):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    rig = make_rig(store, cfg)
    n = 500
    ids = [ingest_one(store, cfg, payload(i)).event_id for i in range(n)]
    slack.mode = "rate_limit"
    slack.retry_after = 1
    for _ in range(8):
        rig.worker.run_once(wait_horizon_s=60)  # every send is answered 429
        store.advance(2)
    assert slack.effects == 0
    assert {d["state"] for d in store.deliveries()} == {"pending"}  # a 429 never kills a lead
    slack.mode = "ok"  # the vendor recovers
    rig.settle(max_rounds=2000, jump_s=2)
    assert slack.effects == n and {d["state"] for d in store.deliveries()} == {"done"}
    assert sorted(ids) == sorted(d["event_id"] for d in store.deliveries())


def test_a_lead_can_be_rate_limited_more_than_the_attempt_budget_and_still_arrive(store, slack, make):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    rig = make_rig(store, cfg)
    r = ingest_one(store, cfg)
    slack.queue(*["rate_limit"] * (MAX_ATTEMPTS + 4))
    rig.settle(max_rounds=200, jump_s=10)
    d = store.delivery(r.event_id, "slack")
    assert d["state"] == "done" and d["retries"] == MAX_ATTEMPTS + 4 and d["attempts"] == 1
    assert slack.effects == 1


def test_an_ambiguous_vendor_spends_the_budget_and_the_lead_dies_visibly(store, slack, make):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    rig = make_rig(store, cfg)
    r = ingest_one(store, cfg)
    slack.mode = "server_error"
    rig.settle(max_rounds=100, jump_s=3600)
    d = store.delivery(r.event_id, "slack")
    assert d["state"] == "dead" and d["attempts"] == MAX_ATTEMPTS and "attempt budget" in d["last_error"]
    assert len(store.attempts_for(r.event_id, "slack")) == MAX_ATTEMPTS
    assert alert_kinds(store) == ["dead"]


def test_a_lead_that_a_vendor_keeps_rate_limiting_dies_by_age_not_by_attempts(store, slack, make):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    rig = make_rig(store, cfg)
    r = ingest_one(store, cfg)
    slack.mode = "rate_limit"
    slack.retry_after = 300
    rig.settle(max_rounds=400, jump_s=3600)  # an hour per round, about 72 rounds to pass the 72 hour limit
    d = store.delivery(r.event_id, "slack")
    assert d["state"] == "dead" and d["last_error"] == "expired: older than 72 hours"
    assert d["attempts"] == 0 and d["retries"] > 10


def test_run_one_itself_refuses_a_row_older_than_the_sink_limit(store, slack, make):
    from relay.fanout import RunContext, process_next
    from relay.sinks.registry import build_registry

    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    r = ingest_one(store, cfg)
    store.advance(73 * 3600)  # nothing has run expire_old, only the claim path can notice the age
    ctx = RunContext(store=store, cfg=cfg, registry=build_registry(), http=None)
    assert process_next(ctx, "slack") is True
    d = store.delivery(r.event_id, "slack")
    assert d["state"] == "dead" and d["last_error"] == "expired: older than 72 hours" and slack.requests == []


def test_a_rate_limit_holds_back_the_whole_channel_not_only_the_row_that_hit_it(store, slack, make):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    rig = make_rig(store, cfg)
    ingest_one(store, cfg, payload(1))
    ingest_one(store, cfg, payload(2))
    slack.retry_after = 30
    slack.queue("rate_limit")
    rig.worker.run_once(wait_horizon_s=0)
    assert len(slack.requests) == 1
    store.advance(5)  # past the one second interval, inside Slack's 30 second request
    rig.worker.run_once(wait_horizon_s=0)
    assert len(slack.requests) == 1  # the second lead did not go out into a channel that said wait
    store.advance(30)
    rig.settle()
    assert slack.effects == 2


def test_a_permanent_4xx_goes_dead_on_the_first_try_with_an_alert(store, slack, make):
    cfg = make_cfg(slack.url, make.url, slack_extra={"enabled": False}, make_extra={})
    rig = make_rig(store, cfg)
    make.mode = "bad_request"
    r = ingest_one(store, cfg)
    rig.settle()
    d = store.delivery(r.event_id, "make")
    assert d["state"] == "dead" and d["attempts"] == 1 and d["last_status"] == 400
    assert len(make.requests) == 1


def test_queue_full_retries_and_then_delivers(store, slack, make):
    cfg = make_cfg(slack.url, make.url, slack_extra={"enabled": False})
    rig = make_rig(store, cfg)
    r = ingest_one(store, cfg)
    make.queue("queue_full", "queue_full")
    rig.settle(max_rounds=50, jump_s=120)
    d = store.delivery(r.event_id, "make")
    assert d["state"] == "done" and d["retries"] == 2 and make.effects == 1


def test_a_refused_connection_retries_and_spends_no_budget(store, slack, make):
    cfg = make_cfg("http://127.0.0.1:9/dead", make.url, make_extra={"enabled": False}, slack_timeout=0.5)
    rig = make_rig(store, cfg)
    r = ingest_one(store, cfg)
    for _ in range(3):
        rig.worker.run_once(wait_horizon_s=0)
        store.advance(4000)
    d = store.delivery(r.event_id, "slack")
    assert d["state"] == "pending" and d["attempts"] == 0 and d["retries"] >= 2


# ---- pauses, the breaker, intervals ---------------------------------------------------------------------------------------


def test_a_revoked_hook_pauses_the_sink_and_the_leads_wait_instead_of_dying(store, slack, make):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    rig = make_rig(store, cfg)
    first = ingest_one(store, cfg, payload(1))
    rig.settle()
    assert slack.effects == 1  # positive control: it worked before the fault
    slack.mode = "forbidden"
    ids = [ingest_one(store, cfg, payload(i)).event_id for i in range(2, 6)]
    store.advance(2)  # past the one second minimum interval after the first lead
    rig.worker.run_once(wait_horizon_s=0)
    assert slack.requests[-1]["mode"] == "forbidden"
    seen = len(slack.requests)
    rig.worker.run_once(wait_horizon_s=0)
    assert len(slack.requests) == seen  # paused: no further calls while the pause lasts
    assert {store.delivery(i, "slack")["state"] for i in ids} == {"pending"}
    assert store.sink_state(TENANT, "slack")["paused"]
    slack.mode = "ok"
    store.advance(901)  # the pause ends and one probe goes through, then the rest
    rig.settle()
    assert slack.effects == 1 + len(ids) and all(store.delivery(i, "slack")["state"] == "done" for i in ids)
    assert store.delivery(first.event_id, "slack")["state"] == "done"


def test_a_pause_raises_one_alert_not_one_per_lead(store, slack, make):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    rig = make_rig(store, cfg)
    ingest_one(store, cfg, payload(1))
    slack.mode = "forbidden"
    for i in range(2, 6):
        ingest_one(store, cfg, payload(i))
    rig.worker.run_once(wait_horizon_s=0)
    assert alert_kinds(store) == ["pause"]


def test_make_410_pauses_and_the_lead_is_delivered_after_the_hook_is_reactivated(store, slack, make):
    cfg = make_cfg(slack.url, make.url, slack_extra={"enabled": False})
    rig = make_rig(store, cfg)
    make.mode = "gone"
    r = ingest_one(store, cfg)
    rig.worker.run_once(wait_horizon_s=0)
    assert store.delivery(r.event_id, "make")["state"] == "pending" and store.sink_state(TENANT, "make")["paused"]
    make.mode = "accepted"
    store.advance(901)
    rig.settle()
    assert store.delivery(r.event_id, "make")["state"] == "done" and make.effects == 1


def test_the_circuit_breaker_stops_hammering_a_failing_vendor_then_probes_once(store, slack, make):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False}, slack_extra={"min_interval_s": 0.01})
    rig = make_rig(store, cfg)
    for i in range(20):
        ingest_one(store, cfg, payload(i))
    slack.mode = "server_error"
    rig.worker.run_once(wait_horizon_s=1)
    assert len(slack.requests) == 5  # five consecutive failures open the breaker; the other 15 leads are not tried
    assert store.sink_state(TENANT, "slack")["breaker_open"]
    store.advance(61)
    rig.worker.run_once(wait_horizon_s=1)
    assert len(slack.requests) == 6  # half open: exactly one probe, which failed, so it is open again
    slack.mode = "ok"
    store.advance(61)
    rig.settle(max_rounds=300)
    assert slack.effects == 20
    st = store.sink_state(TENANT, "slack")
    assert st["consecutive_failures"] == 0 and not st["breaker_open"]  # a success closes the breaker again


def test_the_minimum_interval_spaces_sends_in_database_time_without_sleeping(store, slack, make, record_sleeps):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    stamps: list[datetime] = []
    hooks = Hooks(before_send=lambda c: stamps.append(datetime.fromisoformat(store.db_now())))
    rig = make_rig(store, cfg, hooks=hooks)
    for i in range(4):
        ingest_one(store, cfg, payload(i))
    rig.settle()
    assert slack.effects == 4 and len(stamps) == 4
    gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
    assert all(g >= 1.0 for g in gaps), gaps  # Slack: one message per second per channel, measured on the database clock
    assert record_sleeps == []  # the spacing came from next_send_at, not from sleeping


def test_a_hanging_vendor_does_not_hold_back_another_vendors_leads(store, slack, make):
    cfg = make_cfg(slack.url, make.url, make_timeout=8.0)
    rig = make_rig(store, cfg)
    n = 6
    for i in range(n):
        ingest_one(store, cfg, payload(i))
    make.mode = "hang"
    t = threading.Thread(target=rig.worker.run_once, kwargs={"wait_horizon_s": 5.0})
    t.start()
    try:
        wait_for(lambda: len(make.requests) >= 1, what="the Make call to arrive and hang")
        wait_for(lambda: slack.effects == n, what="every Slack lead to be delivered while Make hangs")
        assert t.is_alive()  # Make is still hanging; Slack finished anyway
    finally:
        make.release.set()
        t.join(30)
    assert not t.is_alive()


# ---- sink disabled, bugs, reconcile, skipped ---------------------------------------------------------------------------------


def test_a_delivery_whose_sink_was_disabled_after_ingest_is_dead_not_an_exception(store, slack, make):
    full = make_cfg(slack.url, make.url)
    r = ingest_one(store, full)
    narrowed = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    rig = make_rig(store, narrowed)
    rig.settle()
    assert store.delivery(r.event_id, "make")["state"] == "dead" and store.delivery(r.event_id, "make")["last_error"] == "sink disabled"
    assert store.delivery(r.event_id, "slack")["state"] == "done" and make.requests == []


def test_a_delivery_for_a_tenant_that_left_the_config_is_dead(store, slack, make):
    cfg = make_cfg(slack.url, make.url)
    r = ingest_one(store, cfg)
    cfg.tenants.clear()
    rig = make_rig(store, cfg)
    rig.worker.run_once(wait_horizon_s=0)
    assert {d["state"] for d in store.deliveries(r.event_id)} == {"dead"}


def test_a_sink_that_raises_is_dead_on_the_first_occurrence_and_the_loop_continues(store, slack, make):
    cfg = make_cfg(slack.url, make.url)
    boom = ScriptSink("slack", outcomes=[KeyError("x")] * 5)
    registry = {"slack": boom, "make": MakeSink()}
    rig = make_rig(store, cfg, registry=registry)
    ids = [ingest_one(store, cfg, payload(i)).event_id for i in range(3)]
    rig.settle()
    for i in ids:
        d = store.delivery(i, "slack")
        assert d["state"] == "dead" and d["last_error"] == "sink bug: KeyError" and d["attempts"] == 1
        assert store.delivery(i, "make")["state"] == "done"
    assert len(boom.calls) == 3  # no retries of a bug
    assert make.effects == 3


def test_a_reconcile_that_raises_is_also_caught(store, slack, make):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    stub = ScriptSink("slack", outcomes=[Outcome.ambiguous(status=500)], can_reconcile=True, reconcile_result=ValueError("nope"))
    rig = make_rig(store, cfg, registry={"slack": stub, "make": MakeSink()})
    r = ingest_one(store, cfg)
    rig.settle()
    assert store.delivery(r.event_id, "slack")["last_error"] == "sink bug: ValueError"


@pytest.mark.parametrize("found,expect_state", [("found", "done"), ("absent", "pending"), ("unknown", "pending")])
def test_an_ambiguous_send_is_resolved_by_reconcile_when_the_sink_can_read_back(store, slack, make, found, expect_state):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    stub = ScriptSink("slack", outcomes=[Outcome.ambiguous(status=500)], can_reconcile=True, reconcile_result=found)
    rig = make_rig(store, cfg, registry={"slack": stub, "make": MakeSink()})
    r = ingest_one(store, cfg)
    rig.worker.run_once(wait_horizon_s=0)
    d = store.delivery(r.event_id, "slack")
    assert d["state"] == expect_state
    if found == "found":
        assert d["effect_ref"] == "reconciled"
    if found == "absent":
        assert d["retries"] == 1 and d["attempts"] == 0  # absent is a safe retry that spends no budget
    if found == "unknown":
        assert d["attempts"] == 1  # still ambiguous, still spends the budget


def test_ambiguous_without_a_read_back_is_not_reconciled(store, slack, make):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    stub = ScriptSink("slack", outcomes=[Outcome.ambiguous(status=500)], can_reconcile=False, reconcile_result="found")
    rig = make_rig(store, cfg, registry={"slack": stub, "make": MakeSink()})
    r = ingest_one(store, cfg)
    rig.worker.run_once(wait_horizon_s=0)
    assert store.delivery(r.event_id, "slack")["state"] == "pending"


def test_a_skipped_delivery_is_terminal_visible_and_quiet(store, slack, make):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    stub = ScriptSink("slack", outcomes=[Outcome.skipped("not a lead for this sink")])
    rig = make_rig(store, cfg, registry={"slack": stub, "make": MakeSink()})
    r = ingest_one(store, cfg)
    rig.settle()
    d = store.delivery(r.event_id, "slack")
    assert d["state"] == "skipped" and d["last_error"] == "not a lead for this sink"
    assert alert_kinds(store) == []
    assert len(stub.calls) == 1


def test_a_purged_payload_makes_the_delivery_dead_with_a_reason(store, slack, make):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    rig = make_rig(store, cfg)
    r = ingest_one(store, cfg)
    store.advance(40 * 86400)
    store.purge(30)
    with store.tx() as t:  # keep the row pending to isolate the purged-payload path from the age limit
        t.execute(f"UPDATE deliveries SET created_at = {store.now()}, next_at = {store.now()}")
    rig.settle()
    d = store.delivery(r.event_id, "slack")
    assert d["state"] == "dead" and d["last_error"] == "payload purged" and slack.requests == []


# ---- operator actions ----------------------------------------------------------------------------------------------------------


def test_replay_resends_a_dead_delivery_once_the_cause_is_fixed(store, slack, make):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    rig = make_rig(store, cfg)
    slack.mode = "invalid"
    r = ingest_one(store, cfg)
    rig.settle()
    assert store.delivery(r.event_id, "slack")["state"] == "dead"
    slack.mode = "ok"
    assert store.replay(r.event_id) == 1
    rig.settle()
    d = store.delivery(r.event_id, "slack")
    assert d["state"] == "done" and slack.effects == 1


def test_replay_of_a_done_delivery_needs_force_and_then_sends_again(store, slack, make):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    rig = make_rig(store, cfg)
    r = ingest_one(store, cfg)
    rig.settle()
    assert store.replay(r.event_id) == 0 and store.replay(r.event_id, "slack") == 0
    assert store.replay(r.event_id, "slack", force=True) == 1
    rig.settle()
    assert slack.effects == 2


# ---- alerts digest -------------------------------------------------------------------------------------------------------------


def test_dead_deliveries_produce_one_digest_per_sink_per_fifteen_minutes(store, slack, make):
    alert_hook = FakeSlack().start()
    try:
        cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
        cfg.alert_url = alert_hook.url
        rig = make_rig(store, cfg)
        slack.mode = "invalid"
        for i in range(3):
            ingest_one(store, cfg, payload(i))
        rig.settle()
        assert len(alert_hook.requests) == 1  # three dead leads, ONE message
        text = alert_hook.requests[0]["json"]["text"]
        assert "slack" in text and "3 dead" in text and TENANT in text
        for i in range(10, 13):
            ingest_one(store, cfg, payload(i))
        rig.settle()
        assert len(alert_hook.requests) == 1  # a second burst inside the 15 minutes waits
        store.advance(901)
        rig.worker.run_once(wait_horizon_s=0)
        assert len(alert_hook.requests) == 2 and "3 dead" in alert_hook.requests[1]["json"]["text"]
    finally:
        alert_hook.stop()


def test_a_failed_digest_is_retried_after_a_minute_not_lost(store, slack, make):
    alert_hook = FakeSlack().start()
    try:
        cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
        cfg.alert_url = alert_hook.url
        rig = make_rig(store, cfg)
        slack.mode = "invalid"
        ingest_one(store, cfg)
        alert_hook.mode = "server_error"
        rig.settle()
        assert alert_hook.effects == 0 and len(store.alerts_unsent("slack")) == 1
        alert_hook.mode = "ok"
        store.advance(61)
        rig.worker.run_once(wait_horizon_s=0)
        assert alert_hook.effects == 1 and store.alerts_unsent("slack") == []
    finally:
        alert_hook.stop()


def test_without_an_alert_webhook_the_digest_goes_to_the_log(store, slack, make, caplog):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    assert cfg.alert_url is None
    rig = make_rig(store, cfg)
    slack.mode = "invalid"
    ingest_one(store, cfg)
    with caplog.at_level("WARNING", logger="relay.alerts"):
        rig.settle()
    assert any("RELAY_ALERT" in r.getMessage() and "slack" in r.getMessage() for r in caplog.records)
    assert store.alerts_unsent("slack") == []


def test_class_every_backoff_survives_a_huge_retry_count():
    """An exponent past 1023 makes `float * 2 ** n` raise OverflowError, which would kill a delivery or a pull that retried long enough."""
    # CLASS-TABLE: every backoff function x a retry count at, past and far past the float limit
    import itertools

    from relay.pulls.common import backoff_s as pull_backoff_s

    cases = list(itertools.product((("fanout", backoff_s), ("ambiguous", ambiguous_backoff_s), ("pull", pull_backoff_s)), (1022, 1023, 1024, 5000, 10**6)))
    assert len(cases) == 15
    for (name, fn), n in cases:
        v = fn(n, random.Random(n))
        assert 0 < v <= 4500, (name, n, v)
