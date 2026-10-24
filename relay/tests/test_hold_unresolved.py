"""`hold_unresolved`: a sink with no idempotency key and no lookup (Slack, Make) can be told to HOLD a send that may have
landed instead of retrying it. The assertions are about the vendor's EFFECT count (what the fake really accepted), the
same yardstick as test_crash_and_concurrency, and every held world has an unheld twin that shows the duplicate it prevents.
"""

from __future__ import annotations

import random
import threading
from collections import Counter

import pytest

from relay.config import ConfigError
from relay.fanout import Hooks
from relay.sinks.base import Outcome
from relay.sinks.make import MakeSink

from .helpers import ScriptSink, SimulatedCrash, ingest_one, make_cfg, make_rig, payload
from .test_crash_and_concurrency import crash_once_per_sink, make_event_ids, slack_event_ids
from .test_fanout import alert_kinds

HOLD = {"hold_unresolved": True}


def run_until_quiet(rig, store, rounds: int = 12, jump_s: float = 3600) -> None:
    for _ in range(rounds):
        rig.worker.run_once(wait_horizon_s=0)
        store.advance(jump_s)


# ---- config ------------------------------------------------------------------------------------------------------------------


def test_the_option_defaults_to_off_and_must_be_a_real_boolean(slack, make):
    assert make_cfg(slack.url, make.url).tenants["acme"].sinks["slack"].hold_unresolved is False
    assert make_cfg(slack.url, make.url, slack_extra=HOLD).tenants["acme"].sinks["slack"].hold_unresolved is True
    for bad in ("yes", 1, None, "true"):
        with pytest.raises(ConfigError, match="must be true or false"):
            make_cfg(slack.url, make.url, slack_extra={"hold_unresolved": bad})


# ---- an ambiguous answer ---------------------------------------------------------------------------------------------------


def test_an_accepted_message_with_a_lost_answer_is_held_and_never_sent_again(store, slack, make):
    cfg = make_cfg(slack.url, make.url, slack_extra=HOLD, make_extra={"enabled": False})
    rig = make_rig(store, cfg)
    r = ingest_one(store, cfg)
    slack.mode = "act_then_fail"  # Slack posts it, then answers 500
    run_until_quiet(rig, store)
    assert slack.effects == 1  # one message, ever
    d = store.delivery(r.event_id, "slack")
    assert d["state"] == "dead" and d["last_error"].startswith("held:") and d["effect_ref"] == "held"
    assert alert_kinds(store) == ["dead"]
    assert len(store.attempts_for(r.event_id, "slack")) == 1


def test_the_same_world_without_the_option_sends_it_again(store, slack, make):
    """The twin of the test above: the duplicate the option prevents. Without it this is the documented at-least-once cost."""
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    rig = make_rig(store, cfg)
    ingest_one(store, cfg)
    slack.queue("act_then_fail")  # the first try is posted and answered 500, then the fake behaves
    run_until_quiet(rig, store)
    assert slack.effects == 2


def test_make_is_held_too(store, slack, make):
    cfg = make_cfg(slack.url, make.url, slack_extra={"enabled": False}, make_extra=HOLD)
    rig = make_rig(store, cfg)
    r = ingest_one(store, cfg)
    make.mode = "act_then_fail"
    run_until_quiet(rig, store)
    assert make.effects == 1
    d = store.delivery(r.event_id, "make")
    assert d["state"] == "dead" and d["last_error"].startswith("held:")


# ---- a worker that died ------------------------------------------------------------------------------------------------------


def test_a_worker_that_died_after_the_send_does_not_send_again_after_the_lease_ends(store, slack, make):
    cfg = make_cfg(slack.url, make.url, slack_extra=HOLD, make_extra={"enabled": False})
    rig = make_rig(store, cfg, hooks=Hooks(after_send=crash_once_per_sink("after send")))
    r = ingest_one(store, cfg)
    with pytest.raises(SimulatedCrash):
        rig.worker.run_once(wait_horizon_s=0)
    assert slack.effects == 1 and store.delivery(r.event_id, "slack")["state"] == "pending"
    store.advance(301)  # the lease ran out; the unheld twin (test_crash_and_concurrency) sends a second message here
    make_rig(store, cfg).settle()
    assert slack.effects == 1
    d = store.delivery(r.event_id, "slack")
    assert d["state"] == "dead" and d["last_error"].startswith("held:")


def test_a_worker_that_died_before_the_send_is_also_held_because_nothing_can_tell_the_two_apart(store, slack, make):
    """The price of the option, stated: a dead worker is held even when it never sent. The operator looks, then replays."""
    cfg = make_cfg(slack.url, make.url, slack_extra=HOLD, make_extra={"enabled": False})
    rig = make_rig(store, cfg, hooks=Hooks(before_send=crash_once_per_sink("before send")))
    r = ingest_one(store, cfg)
    with pytest.raises(SimulatedCrash):
        rig.worker.run_once(wait_horizon_s=0)
    store.advance(301)
    make_rig(store, cfg).settle()
    assert slack.effects == 0
    assert store.delivery(r.event_id, "slack")["state"] == "dead"


# ---- the operator ------------------------------------------------------------------------------------------------------------


def test_a_replay_is_the_operators_decision_and_sends_exactly_once_more(store, slack, make):
    cfg = make_cfg(slack.url, make.url, slack_extra=HOLD, make_extra={"enabled": False})
    rig = make_rig(store, cfg)
    r = ingest_one(store, cfg)
    slack.queue("act_then_fail")
    run_until_quiet(rig, store)
    assert slack.effects == 1 and store.delivery(r.event_id, "slack")["state"] == "dead"
    assert store.replay(r.event_id, "slack") == 1  # the operator looked at the channel and chose to send it again
    run_until_quiet(rig, store)
    assert slack.effects == 2
    assert store.delivery(r.event_id, "slack")["state"] == "done"


def test_every_sink_that_cannot_read_back_is_a_sink_the_hold_was_written_for():
    """The class guard. The hold applies to exactly the sinks with `can_reconcile = False`. A new sink that cannot look a
    send up lands in this set and fails here until someone decides whether its ambiguous outcomes need a hold test."""
    from relay.sinks import registry

    cannot = {name for name, cls in registry.SINK_CLASSES.items() if not cls.can_reconcile}
    assert cannot == {"slack", "make", "capi"}, cannot
    assert {name for name, cls in registry.SINK_CLASSES.items() if cls.can_reconcile} == {"sheets"}


# ---- what the option must not touch ----------------------------------------------------------------------------------------


def test_a_sink_that_can_read_back_is_still_reconciled_not_held(store, slack, make):
    cfg = make_cfg(slack.url, make.url, slack_extra=HOLD, make_extra={"enabled": False})
    stub = ScriptSink("slack", outcomes=[Outcome.ambiguous(status=500)], can_reconcile=True, reconcile_result="found")
    rig = make_rig(store, cfg, registry={"slack": stub, "make": MakeSink()})
    r = ingest_one(store, cfg)
    rig.worker.run_once(wait_horizon_s=0)
    d = store.delivery(r.event_id, "slack")
    assert d["state"] == "done" and d["effect_ref"] == "reconciled"


def test_a_sink_that_can_read_back_is_looked_up_again_on_the_next_try_not_held(store, slack, make):
    """The next try of a row that may have acted is where the hold could wrongly catch a sink that can look it up."""
    cfg = make_cfg(slack.url, make.url, slack_extra=HOLD, make_extra={"enabled": False})
    stub = ScriptSink("slack", outcomes=[Outcome.ambiguous(status=500), Outcome.done()], can_reconcile=True, reconcile_result="absent")
    rig = make_rig(store, cfg, registry={"slack": stub, "make": MakeSink()})
    r = ingest_one(store, cfg)
    rig.settle()
    assert store.delivery(r.event_id, "slack")["state"] == "done"
    assert len(stub.calls) == 2  # the second try really sent: the read back said it was absent


def test_a_vendor_that_certainly_did_not_act_is_still_retried_when_held_is_on(store, slack, make):
    cfg = make_cfg(slack.url, make.url, slack_extra=HOLD, make_extra={"enabled": False})
    rig = make_rig(store, cfg)
    r = ingest_one(store, cfg)
    slack.queue("rate_limit", "rate_limit")  # a 429 means Slack did not post it
    run_until_quiet(rig, store)
    assert slack.effects == 1
    assert store.delivery(r.event_id, "slack")["state"] == "done"


# ---- seeded property test ---------------------------------------------------------------------------------------------------

SLACK_MODES = ["ok", "ok", "rate_limit", "server_error", "act_then_fail", "invalid", "forbidden"]
MAKE_MODES = ["accepted", "accepted", "queue_full", "rate_limit", "server_error", "act_then_fail", "bad_request", "gone"]
HELD_STATS: list[dict] = []


@pytest.mark.parametrize("seed", range(8))
def test_with_the_hold_on_a_vendor_never_acts_twice_unless_an_operator_replays(store, slack, make, seed):
    """Random duplicates, vendor failures including 5xx after the vendor acted, worker crashes before and after the send,
    clock jumps and operator replays. Invariants: the vendor's effect count for an (event, sink) never exceeds 1 plus the
    replays at that pair; every delivery ends terminal; a dead delivery has a visible reason; nothing is lost."""
    rng = random.Random(1000 + seed)
    cfg = make_cfg(slack.url, make.url, slack_extra=HOLD, make_extra=HOLD)
    replays: Counter = Counter()
    armed = {"after": False, "before": False}
    lock = threading.Lock()

    def after_send(claim, outcome):
        with lock:
            if armed["after"]:
                armed["after"] = False
                raise SimulatedCrash("random crash after send")

    def before_send(claim):
        with lock:
            if armed["before"]:
                armed["before"] = False
                raise SimulatedCrash("random crash before send")

    rig = make_rig(store, cfg, seed=seed, hooks=Hooks(before_send=before_send, after_send=after_send))
    keys: dict[str, str] = {}
    for _ in range(80):
        op = rng.random()
        if op < 0.30 or not keys:
            i = f"{len(keys):03d}"
            keys[i] = ingest_one(store, cfg, payload(int(i)), key=f"{seed:02d}{i}" + "0" * 4 + "ffff").event_id
        elif op < 0.50:
            slack.queue(*[rng.choice(SLACK_MODES) for _ in range(rng.randint(1, 3))])
            make.queue(*[rng.choice(MAKE_MODES) for _ in range(rng.randint(1, 3))])
        elif op < 0.78:
            if rng.random() < 0.25:
                armed[rng.choice(["after", "before"])] = True
            try:
                rig.worker.run_once(wait_horizon_s=rng.choice([0.0, 5.0]))
            except SimulatedCrash:
                pass
        elif op < 0.90:
            store.advance(rng.choice([1, 30, 400, 4000]))
        else:
            dead = [d for d in store.deliveries() if d["state"] == "dead"]
            if dead:
                pick = rng.choice(dead)
                if store.replay(pick["event_id"], pick["sink"]):
                    replays[(pick["event_id"][:8], pick["sink"])] += 1

    slack.script.clear()
    make.script.clear()
    slack.mode, make.mode = "ok", "accepted"
    armed["after"] = armed["before"] = False
    store.advance(400)
    rig.settle(max_rounds=600)

    slack_seen, make_seen = Counter(slack_event_ids(slack)), Counter(make_event_ids(make))
    for e in store.events():
        for sink, seen in (("slack", slack_seen), ("make", make_seen)):
            d = store.delivery(e["event_id"], sink)
            assert d is not None, f"lost: {e['event_id']} {sink}"
            assert d["state"] in ("done", "dead", "skipped"), d
            ev8 = e["event_id"][:8]
            assert seen[ev8] <= 1 + replays[(ev8, sink)], (sink, ev8, seen[ev8], replays[(ev8, sink)])
            if d["state"] == "done":
                assert seen[ev8] >= 1
            if d["state"] == "dead":
                assert d["last_error"], d
                assert d["last_error"].startswith(("held:", "http 4")) or "attempt budget" in d["last_error"], d["last_error"]
    HELD_STATS.append({
        "held": sum(1 for d in store.deliveries() if d["state"] == "dead" and (d["last_error"] or "").startswith("held:")),
        "replays": sum(replays.values()), "done": sum(1 for d in store.deliveries() if d["state"] == "done"),
    })


def test_the_held_worlds_were_not_vacuous():
    if len(HELD_STATS) < 4:
        pytest.skip("run the whole file for this check: the random worlds did not all run in this selection")
    total = Counter()
    for w in HELD_STATS:
        total.update(w)
    assert total["held"] > 0 and total["replays"] > 0 and total["done"] > 20, dict(total)
