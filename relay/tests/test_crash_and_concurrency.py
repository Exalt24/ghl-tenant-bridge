"""Crash windows, leases and stale writes, overlapping workers, and a seeded property test over random worlds.

The vendor's EFFECT count (what the fake actually accepted) is what every assertion here is about, because "the vendor
acted" is the thing that must neither be lost nor repeated without cause.
"""

from __future__ import annotations

import json
import random
import re
import threading
from collections import Counter

import pytest

from relay.fanout import MAX_ATTEMPTS, Hooks
from relay.sinks.base import AMBIGUOUS

from .helpers import SimulatedCrash, ingest_one, make_cfg, make_rig, payload, wait_for


def slack_event_ids(slack) -> list[str]:
    """First 8 characters of the event id from the footer of each ACCEPTED message."""
    out = []
    for r in slack.requests:
        if r["accepted"]:
            footer = r["json"]["blocks"][-1]["elements"][0]["text"]
            out.append(re.search(r"event `([0-9a-f]{8})`", footer).group(1))
    return out


def make_event_ids(make) -> list[str]:
    return [r["json"]["event_id"][:8] for r in make.requests if r["accepted"]]


def crash_once_per_sink(where: str):
    fired: set[str] = set()
    lock = threading.Lock()

    def hook(claim, *_rest):
        with lock:
            if claim.sink in fired:
                return
            fired.add(claim.sink)
        raise SimulatedCrash(f"{where} {claim.sink}")

    return hook


# ---- crash windows ------------------------------------------------------------------------------------------------------------


def test_a_crash_after_the_vendor_acted_and_before_the_commit_sends_at_most_twice_never_zero(store, slack, make):
    cfg = make_cfg(slack.url, make.url)
    rig = make_rig(store, cfg, hooks=Hooks(after_send=crash_once_per_sink("after send")))
    r = ingest_one(store, cfg)
    with pytest.raises(SimulatedCrash):
        rig.worker.run_once(wait_horizon_s=0)
    assert (slack.effects, make.effects) == (1, 1)  # the vendors acted
    assert {d["state"] for d in store.deliveries(r.event_id)} == {"pending"}  # and nothing was committed
    assert store.attempts_for(r.event_id, "slack") == []
    # the process restarts: after the lease ends the rows are claimed again
    restarted = make_rig(store, cfg)
    assert restarted.worker.run_once(wait_horizon_s=0) == 0  # inside the lease nobody may take them
    store.advance(301)
    restarted.settle()
    assert slack.effects == 2 and make.effects == 2  # at most 2, and never 0
    assert {d["state"] for d in store.deliveries(r.event_id)} == {"done"}
    assert all(d["attempts"] == 1 for d in store.deliveries(r.event_id))


def test_a_crash_before_the_send_loses_nothing_and_sends_exactly_once(store, slack, make):
    cfg = make_cfg(slack.url, make.url)
    rig = make_rig(store, cfg, hooks=Hooks(before_send=crash_once_per_sink("before send")))
    r = ingest_one(store, cfg)
    with pytest.raises(SimulatedCrash):
        rig.worker.run_once(wait_horizon_s=0)
    assert (slack.effects, make.effects) == (0, 0)
    store.advance(301)
    make_rig(store, cfg).settle()
    assert (slack.effects, make.effects) == (1, 1)
    assert {d["state"] for d in store.deliveries(r.event_id)} == {"done"}


def test_a_crash_between_the_state_change_and_the_log_commits_neither(store, slack, make):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    rig = make_rig(store, cfg)
    r = ingest_one(store, cfg)

    def die():
        raise SimulatedCrash("between state change and attempts row")

    store.faults["finish_mid"] = die
    with pytest.raises(SimulatedCrash):
        rig.worker.run_once(wait_horizon_s=0)
    store.faults.clear()
    d = store.delivery(r.event_id, "slack")
    assert d["state"] == "pending" and store.attempts_for(r.event_id, "slack") == []
    store.advance(301)
    rig.settle()
    assert store.delivery(r.event_id, "slack")["state"] == "done" and len(store.attempts_for(r.event_id, "slack")) == 1


def test_a_stale_worker_finishing_late_cannot_overwrite_a_newer_result(store, slack, make, caplog):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    r = ingest_one(store, cfg)
    reached, go = threading.Event(), threading.Event()

    def hold(claim, outcome):
        reached.set()
        assert go.wait(30)

    slow = make_rig(store, cfg, hooks=Hooks(after_send=hold))
    t = threading.Thread(target=slow.worker.run_once, kwargs={"wait_horizon_s": 0})
    t.start()
    try:
        assert reached.wait(30)  # the slow worker holds the row, has sent, and has not committed
        store.advance(301)  # its lease ran out
        fast = make_rig(store, cfg)
        assert fast.worker.run_once(wait_horizon_s=0) == 1
        assert store.delivery(r.event_id, "slack")["state"] == "done"
        slack.mode = "invalid"  # if the slow worker's late verdict were applied it would change the row
    finally:
        go.set()
        t.join(30)
    d = store.delivery(r.event_id, "slack")
    assert d["state"] == "done" and d["last_error"] is None
    assert len(store.attempts_for(r.event_id, "slack")) == 1
    assert slack.effects == 2  # the double send is the documented at-least-once cost


# ---- overlapping workers --------------------------------------------------------------------------------------------------------


def test_overlapping_workers_never_send_the_same_delivery_twice(store, slack, make):
    cfg = make_cfg(slack.url, make.url)
    n = 24
    ids = [ingest_one(store, cfg, payload(i)).event_id for i in range(n)]
    a, b = make_rig(store, cfg, seed=1), make_rig(store, cfg, seed=2)
    barrier = threading.Barrier(2)
    errors: list[str] = []

    def run(rig):
        barrier.wait()
        try:
            rig.worker.run_once(wait_horizon_s=5)
        except Exception as exc:
            errors.append(type(exc).__name__)

    threads = [threading.Thread(target=run, args=(r,)) for r in (a, b)]
    [t.start() for t in threads]
    [t.join(120) for t in threads]
    assert errors == []
    a.settle()
    assert {d["state"] for d in store.deliveries()} == {"done"}
    assert Counter(slack_event_ids(slack)) == Counter({i[:8]: 1 for i in ids})
    assert Counter(make_event_ids(make)) == Counter({i[:8]: 1 for i in ids})


# ---- seeded property test ---------------------------------------------------------------------------------------------------------

WORLD_STATS: list[dict] = []
SLACK_MODES = ["ok", "ok", "rate_limit", "server_error", "act_then_fail", "invalid", "forbidden"]
MAKE_MODES = ["accepted", "accepted", "queue_full", "rate_limit", "server_error", "act_then_fail", "bad_request", "gone"]


@pytest.mark.parametrize("seed", range(8))
def test_random_worlds_keep_every_invariant(store, slack, make, seed):
    """Random duplicates, reorders, vendor failures including 5xx after the vendor acted, worker crashes and clock jumps.

    Invariants: no (event, sink) is lost; a duplicate delivery never makes a second event; every delivery ends terminal;
    the vendor's effect count for an (event, sink) never exceeds 1 + the ambiguous outcomes + the crashes at that pair;
    a delivery that is done had an effect; a dead delivery has a visible reason.
    """
    rng = random.Random(seed)
    cfg = make_cfg(slack.url, make.url)
    ambiguous: Counter = Counter()
    crashes: Counter = Counter()
    armed = {"after": False, "before": False}
    lock = threading.Lock()

    def after_send(claim, outcome):
        with lock:
            if outcome.kind == AMBIGUOUS:
                ambiguous[(claim.event_id[:8], claim.sink)] += 1
            if armed["after"]:
                armed["after"] = False
                crashes[(claim.event_id[:8], claim.sink)] += 1
                raise SimulatedCrash("random crash after send")

    def before_send(claim):
        with lock:
            if armed["before"]:
                armed["before"] = False
                raise SimulatedCrash("random crash before send")

    rig = make_rig(store, cfg, seed=seed, hooks=Hooks(before_send=before_send, after_send=after_send))
    keys: dict[str, str] = {}
    total_ingests = 0
    for step in range(70):
        op = rng.random()
        if op < 0.35 or not keys:
            if keys and rng.random() < 0.4:  # a duplicate delivery of an earlier event (a retry or a replay)
                i = rng.choice(sorted(keys))
            else:
                i = f"{len(keys):03d}"
            p = payload(int(i))
            res = ingest_one(store, cfg, p, key=f"{seed:02d}{i}" + "0" * 4 + "ffff")
            keys.setdefault(i, res.event_id)
            total_ingests += 1
        elif op < 0.55:
            slack.queue(*[rng.choice(SLACK_MODES) for _ in range(rng.randint(1, 3))])
            make.queue(*[rng.choice(MAKE_MODES) for _ in range(rng.randint(1, 3))])
        elif op < 0.85:
            if rng.random() < 0.2:
                armed[rng.choice(["after", "before"])] = True
            try:
                rig.worker.run_once(wait_horizon_s=rng.choice([0.0, 5.0]))
            except SimulatedCrash:
                pass
        else:
            store.advance(rng.choice([1, 30, 400, 4000]))

    slack.script.clear()
    make.script.clear()
    slack.mode, make.mode = "ok", "accepted"
    armed["after"] = armed["before"] = False
    store.advance(400)
    rig.settle(max_rounds=600)

    events = store.events()
    assert len(events) == len(keys)  # a duplicate delivery never made a second event
    assert store.health_counts()["suppressed"] == total_ingests - len(keys)
    slack_seen, make_seen = Counter(slack_event_ids(slack)), Counter(make_event_ids(make))
    for e in events:
        for sink, seen in (("slack", slack_seen), ("make", make_seen)):
            d = store.delivery(e["event_id"], sink)
            assert d is not None, f"lost: {e['event_id']} {sink}"  # no (event, sink) lost
            assert d["state"] in ("done", "dead", "skipped"), d  # everything ends terminal
            ev8 = e["event_id"][:8]
            allowed = 1 + ambiguous[(ev8, sink)] + crashes[(ev8, sink)]
            assert seen[ev8] <= allowed, (sink, ev8, seen[ev8], allowed)  # no effect repeated without cause
            if d["state"] == "done":
                assert seen[ev8] >= 1
            if d["state"] == "dead":
                assert d["last_error"], d
                assert d["last_error"].startswith("http 4") or "attempt budget" in d["last_error"], d["last_error"]
                if "attempt budget" in d["last_error"]:
                    assert d["attempts"] >= MAX_ATTEMPTS
    WORLD_STATS.append({
        "seed": seed, "events": len(events), "ambiguous": sum(ambiguous.values()), "crashes": sum(crashes.values()),
        "dead": sum(1 for d in store.deliveries() if d["state"] == "dead"), "done": sum(1 for d in store.deliveries() if d["state"] == "done"),
        "duplicates": total_ingests - len(keys),
    })


def test_the_random_worlds_were_not_vacuous():
    """A property test that never meets a failure proves nothing. Across the seeds it must have met every kind."""
    if len(WORLD_STATS) < 4:
        pytest.skip("run the whole file for this check: the random worlds did not all run in this selection")
    total = Counter()
    for w in WORLD_STATS:
        total.update({k: v for k, v in w.items() if k != "seed"})
    assert total["ambiguous"] > 0 and total["crashes"] > 0 and total["dead"] > 0 and total["done"] > 20 and total["duplicates"] > 0, dict(total)
