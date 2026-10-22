"""The worker's entry points: --once, --purge, the loop, and what the config refuses."""

from __future__ import annotations

import threading

from relay.config import load_config
from relay.http import Http
from relay.worker import Worker, main

from .helpers import config_json, env_for, ingest_one, make_cfg, make_rig, payload, wait_for


def worker_env(slack_url, make_url, **extra):
    env = env_for(slack_url, make_url, **extra)
    env["RELAY_CONFIG"] = config_json()
    return env


def test_repeated_passes_do_not_leave_a_database_connection_behind_per_thread(store, slack, make):
    cfg = make_cfg(slack.url, make.url)
    worker = Worker(cfg, store, Http(), sleeper=store.advance)
    worker.run_once(wait_horizon_s=0)
    baseline = len(store._conns)
    for _ in range(12):
        worker.run_once(wait_horizon_s=0)
    assert len(store._conns) == baseline <= 2  # this thread's own, and nothing from the pass threads


def test_a_released_connection_is_reopened_on_the_next_use(store):
    assert store.ping() is True
    store.release_connection()
    store.release_connection()  # nothing to release the second time
    assert store.ping() is True


def test_once_drains_what_is_due_and_exits(store, slack, make):
    env = worker_env(slack.url, make.url)
    cfg = load_config(env, role="worker_once")
    r = ingest_one(store, cfg)
    assert main(["--once"], environ=env, http=Http(), store=store) == 0
    assert {d["state"] for d in store.deliveries(r.event_id)} == {"done"}
    assert (slack.effects, make.effects) == (1, 1)


def test_once_leaves_work_that_is_not_due_yet_for_the_next_run(store, slack, make):
    env = worker_env(slack.url, make.url)
    cfg = load_config(env, role="worker_once")
    r = ingest_one(store, cfg)
    slack.queue("rate_limit")  # Slack asks for 7 seconds, inside the horizon, but the worker budget below is 0
    worker = Worker(cfg, store, Http(), sleeper=store.advance)
    worker.run_once(wait_horizon_s=0)
    d = store.delivery(r.event_id, "slack")
    assert d["state"] == "pending" and d["retries"] == 1  # waiting for its Retry-After, not spinning on it
    assert store.delivery(r.event_id, "make")["state"] == "done"
    store.advance(8)
    worker.run_once(wait_horizon_s=0)
    assert store.delivery(r.event_id, "slack")["state"] == "done" and slack.effects == 1


def test_the_wait_budget_stops_a_run_that_a_vendor_keeps_rate_limiting(store, slack, make):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    rig = make_rig(store, cfg)
    ingest_one(store, cfg)
    slack.retry_after = 1
    slack.queue(*["rate_limit"] * 60)  # a long streak, then the vendor recovers (so a broken budget ends, and fails below)
    rig.worker.run_once(wait_horizon_s=60, max_total_wait_s=10)
    sends = len(slack.requests)
    assert 2 <= sends <= 12, sends  # it tried for about 10 simulated seconds, then gave the run back


def test_purge_nulls_old_payloads_and_exits(store, slack, make):
    env = worker_env(slack.url, make.url)
    cfg = load_config(env, role="worker_once")
    r = ingest_one(store, cfg)
    assert main(["--purge"], environ=env, http=Http(), store=store) == 0
    assert store.get_event(r.event_id)["payload"] is not None  # too young
    store.advance(31 * 86400)
    assert main(["--purge"], environ=env, http=Http(), store=store) == 0
    assert store.get_event(r.event_id)["payload"] is None


def test_main_refuses_an_unsafe_config_without_a_traceback(capsys, tmp_path, slack, make):
    env = worker_env(slack.url, make.url, RELAY_ENV="prod")
    env["RELAY_DB_PATH"] = str(tmp_path / "x.db")
    env["RELAY_INLINE_WORKER"] = "1"
    assert main(["--once"], environ=env) == 2  # a one-shot service with SQLite in prod is refused
    assert "separate worker service needs Postgres" in capsys.readouterr().err


def test_the_loop_keeps_delivering_until_stopped(store, slack, make):
    cfg = make_cfg(slack.url, make.url)
    worker = Worker(cfg, store, Http())
    t = threading.Thread(target=worker.loop, args=(0.05,))
    t.start()
    try:
        ingest_one(store, cfg, payload(1))
        wait_for(lambda: slack.effects == 1 and make.effects == 1, what="the first lead")
        ingest_one(store, cfg, payload(2))
        wait_for(lambda: slack.effects == 2 and make.effects == 2, what="a lead that arrived while the loop was running")
    finally:
        worker.stop.set()
        t.join(30)
    assert not t.is_alive()
    assert {d["state"] for d in store.deliveries()} == {"done"}


def test_a_worker_with_one_registered_sink_still_runs(store, slack, make):
    from relay.sinks.slack import SlackSink

    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    rig = make_rig(store, cfg, registry={"slack": SlackSink()})
    r = ingest_one(store, cfg)
    rig.settle()
    assert store.delivery(r.event_id, "slack")["state"] == "done"
