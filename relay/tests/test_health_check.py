"""The heartbeat the worker writes, `relay.health_check` (two separate findings, a read that fails is a finding) and the one alert
per cooldown that goes through the Slack sink."""

from __future__ import annotations

import json
import random
import threading

import pytest

from relay import health_check
from relay.health_check import Finding, alert_text, check, send_alert
from relay.http import Http, HttpConnectError, HttpResult, HttpTimeout
from relay.worker import Worker

from .helpers import env_for, config_json, ingest_one, make_cfg, make_rig, payload, wait_for

HB_LIMIT = 120.0
OLD_LIMIT = 3600.0


class StubStore:
    """Just the two reads the check makes, so the boundaries can be set to the exact second."""

    def __init__(self, heartbeat=5.0, oldest=None):
        self.heartbeat, self.oldest = heartbeat, oldest

    def heartbeat_age_s(self, name="worker"):
        if isinstance(self.heartbeat, Exception):
            raise self.heartbeat
        return self.heartbeat

    def oldest_unfinished_age_s(self):
        if isinstance(self.oldest, Exception):
            raise self.oldest
        return self.oldest


def codes(findings):
    return [f.code for f in findings]


# ---- the heartbeat the worker writes -------------------------------------------------------------------------------------------


def test_a_store_that_never_beat_has_no_heartbeat_and_a_beat_is_a_row_with_the_time(store):
    assert store.heartbeat_age_s("worker") is None
    store.beat("worker")
    first = store.heartbeat_age_s("worker")
    assert first is not None and -1 < first < 5  # the clock is read in milliseconds, so a fresh age can come out a hair under zero
    store.advance(100)
    assert store.heartbeat_age_s("worker") > 99  # the age runs on the database clock
    store.beat("worker")
    assert store.heartbeat_age_s("worker") < 5  # a second beat replaces the row, it does not stack one
    with store.tx(write=False) as t:
        assert t.execute("SELECT COUNT(*) AS n FROM heartbeat").fetchone()["n"] == 1
    assert store.heartbeat_age_s("another") is None


def test_every_worker_pass_writes_a_heartbeat(store, slack, make):
    cfg = make_cfg(slack.url, make.url)
    rig = make_rig(store, cfg)
    assert store.heartbeat_age_s("worker") is None
    rig.worker.run_once(wait_horizon_s=0)
    assert store.heartbeat_age_s("worker") < 5
    store.advance(500)
    assert store.heartbeat_age_s("worker") > 499
    rig.worker.run_once(wait_horizon_s=0)
    assert store.heartbeat_age_s("worker") < 5  # the next pass beat again


def test_the_loop_beats_on_every_cycle_while_it_runs(store, slack, make):
    cfg = make_cfg(slack.url, make.url)
    worker = Worker(cfg, store, Http())
    t = threading.Thread(target=worker.loop, args=(0.05,))
    t.start()
    try:
        wait_for(lambda: store.heartbeat_age_s("worker") is not None, what="the first heartbeat")
        store.advance(1000)  # make the row look old, then see the loop replace it by itself
        wait_for(lambda: store.heartbeat_age_s("worker") < 500, what="a later heartbeat")
    finally:
        worker.stop.set()
        t.join(30)
    assert not t.is_alive()


def test_a_failed_beat_is_logged_and_never_stops_the_pass_it_belongs_to(store, slack, make, monkeypatch, caplog):
    cfg = make_cfg(slack.url, make.url)
    rig = make_rig(store, cfg)
    r = ingest_one(store, cfg, payload(1))

    def broken(name="worker"):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "beat", broken)
    with caplog.at_level("ERROR"):
        rig.worker.run_once(wait_horizon_s=0)
    assert {d["state"] for d in store.deliveries(r.event_id)} == {"done"}  # delivery and the alert flush went on
    assert "heartbeat write failed: RuntimeError" in caplog.text
    assert store.heartbeat_age_s("worker") is None  # and the health check will see exactly that


# ---- the two findings ------------------------------------------------------------------------------------------------------------


def test_control_a_fresh_heartbeat_and_a_young_backlog_are_healthy(store, slack, make):
    cfg = make_cfg(slack.url, make.url)
    ingest_one(store, cfg, payload(1))  # a pending delivery, seconds old
    store.beat("worker")
    assert check(store, HB_LIMIT, OLD_LIMIT) == []


def test_a_stale_heartbeat_is_one_finding_about_the_worker_not_about_the_backlog(store):
    store.beat("worker")
    store.advance(HB_LIMIT + 30)
    found = check(store, HB_LIMIT, OLD_LIMIT)
    assert codes(found) == ["heartbeat"] and "heartbeat is 15" in found[0].message and "worker has stopped" in found[0].message


def test_a_worker_that_never_wrote_a_heartbeat_is_a_finding_not_a_pass(store):
    found = check(store, HB_LIMIT, OLD_LIMIT)
    assert codes(found) == ["heartbeat"] and "never written a heartbeat" in found[0].message


def test_an_old_unfinished_delivery_is_one_finding_about_stuck_work_while_the_worker_is_alive(store, slack, make):
    cfg = make_cfg(slack.url, make.url)
    ingest_one(store, cfg, payload(1))
    store.advance(OLD_LIMIT + 60)
    store.beat("worker")  # alive, just not finishing anything
    found = check(store, HB_LIMIT, OLD_LIMIT)
    assert codes(found) == ["backlog"] and "oldest unfinished delivery is 3" in found[0].message and "work is stuck" in found[0].message


def test_both_at_once_are_two_findings_with_their_own_messages(store, slack, make):
    cfg = make_cfg(slack.url, make.url)
    ingest_one(store, cfg, payload(1))
    store.beat("worker")
    store.advance(OLD_LIMIT + 60)
    found = check(store, HB_LIMIT, OLD_LIMIT)
    assert codes(found) == ["heartbeat", "backlog"] and found[0].message != found[1].message


def test_a_finished_delivery_is_not_an_unfinished_one_and_the_oldest_pending_row_decides(store, slack, make):
    cfg = make_cfg(slack.url, make.url)
    rig = make_rig(store, cfg)
    ingest_one(store, cfg, payload(1))
    rig.settle()
    store.advance(OLD_LIMIT * 3)
    store.beat("worker")
    assert store.oldest_unfinished_age_s() is None and check(store, HB_LIMIT, OLD_LIMIT) == []  # done rows never age into an alert
    ingest_one(store, cfg, payload(2))
    store.advance(OLD_LIMIT / 2)
    ingest_one(store, cfg, payload(3))
    store.beat("worker")
    age = store.oldest_unfinished_age_s()
    assert OLD_LIMIT / 2 - 1 < age < OLD_LIMIT / 2 + 30  # the older of the two pending leads, not the newer


@pytest.mark.parametrize("heartbeat,oldest,expected", [
    (HB_LIMIT, OLD_LIMIT, []),                          # exactly at both limits is still healthy
    (HB_LIMIT + 0.001, OLD_LIMIT, ["heartbeat"]),
    (HB_LIMIT, OLD_LIMIT + 0.001, ["backlog"]),
    (0.0, None, []),
    (None, None, ["heartbeat"]),
    (float("nan"), float("nan"), ["heartbeat", "backlog"]),  # an age that is not a number never reads as fresh
    (10 ** 9, 10 ** 9, ["heartbeat", "backlog"]),
])
def test_the_limits_are_exact_and_an_age_that_is_not_a_number_is_a_finding(heartbeat, oldest, expected):
    assert codes(check(StubStore(heartbeat, oldest), HB_LIMIT, OLD_LIMIT)) == expected


def test_a_failed_read_of_either_is_its_own_finding_never_a_pass():
    only_hb = check(StubStore(RuntimeError("boom"), None), HB_LIMIT, OLD_LIMIT)
    assert codes(only_hb) == ["heartbeat"] and "could not be read (RuntimeError)" in only_hb[0].message
    only_backlog = check(StubStore(1.0, OSError("boom")), HB_LIMIT, OLD_LIMIT)
    assert codes(only_backlog) == ["backlog"] and "could not be read (OSError)" in only_backlog[0].message
    both = check(StubStore(RuntimeError("a"), OSError("b")), HB_LIMIT, OLD_LIMIT)
    assert codes(both) == ["heartbeat", "backlog"]
    assert "boom" not in only_hb[0].message  # the class name, never the exception text


def test_the_heartbeat_table_missing_is_a_failed_read_and_so_a_finding(tmp_path):
    from relay.store.sqlite import SqliteStore

    s = SqliteStore(str(tmp_path / "bare.db"))  # no init_schema: no table at all
    found = check(s, HB_LIMIT, OLD_LIMIT)
    s.close()
    assert codes(found) == ["heartbeat", "backlog"] and all("could not be read" in f.message for f in found)


# ---- the command ---------------------------------------------------------------------------------------------------------------


UNUSED_SLACK = "http://127.0.0.1:9/slack"  # the config names both sinks; the health check never sends to either
UNUSED_MAKE = "http://127.0.0.1:9/make"


def run_main(store, capsys, argv=(), http=None, **env_extra):
    env = env_for(UNUSED_SLACK, UNUSED_MAKE, **env_extra)
    env["RELAY_CONFIG"] = config_json()
    code = health_check.main(list(argv), environ=env, http=http, store=store)
    out = capsys.readouterr()
    return code, out.out, out.err


def test_the_command_exits_zero_when_healthy_and_one_when_not_and_names_each_finding(store, capsys):
    store.beat("worker")
    code, out, _ = run_main(store, capsys)
    assert code == 0 and out.startswith("OK:")
    store.advance(500)
    code, out, _ = run_main(store, capsys)
    assert code == 1 and out.startswith("FINDING heartbeat:") and "OK" not in out
    code, out, _ = run_main(store, capsys, ["--max-heartbeat-age-s", "1000"])  # the limit is configurable
    assert code == 0


def test_the_limits_can_come_from_the_environment_and_a_bad_one_is_a_setup_error(store, capsys):
    store.beat("worker")
    store.advance(50)
    assert run_main(store, capsys, RELAY_HEALTH_MAX_HEARTBEAT_AGE_S="10")[0] == 1
    assert run_main(store, capsys, RELAY_HEALTH_MAX_HEARTBEAT_AGE_S="100")[0] == 0
    for bad in ("0", "-5", "abc", "inf"):
        code, out, err = run_main(store, capsys, RELAY_HEALTH_MAX_HEARTBEAT_AGE_S=bad)
        assert code == 2 and out == "" and "RELAY_HEALTH_MAX_HEARTBEAT_AGE_S" in err, bad
    assert run_main(store, capsys, ["--max-oldest-pending-s", "0"])[0] == 2


def test_a_database_that_cannot_be_opened_is_a_finding_and_a_non_zero_exit(capsys, monkeypatch):
    def refuse(*a, **k):
        raise RuntimeError("no route to host")

    monkeypatch.setattr(health_check, "open_store", refuse)
    env = env_for(UNUSED_SLACK, UNUSED_MAKE)
    env["RELAY_CONFIG"] = config_json()
    assert health_check.main([], environ=env) == 1
    out = capsys.readouterr().out
    assert out.startswith("FINDING database:") and "RuntimeError" in out and "no route" not in out


# ---- the alert -------------------------------------------------------------------------------------------------------------------

FINDINGS = [Finding("heartbeat", "the worker heartbeat is 500s old"), Finding("backlog", "the oldest unfinished delivery is 4000s old")]


def alert_cfg(url):
    return make_cfg(UNUSED_SLACK, UNUSED_MAKE, env_extra={"ALERT_WEBHOOK": url} if url else None)


def test_one_alert_per_cooldown_through_the_slack_sink_and_again_after_it(store, slack):
    cfg = alert_cfg(slack.url)
    http = Http()
    assert send_alert(store, http, cfg, FINDINGS, 3600) == "sent"
    assert slack.effects == 1
    text = slack.requests[0]["json"]["text"]
    assert text.startswith("relay health alert:") and "heartbeat:" in text and "backlog:" in text  # one message, both findings
    for _ in range(5):  # the monitor ticks every minute through one outage
        store.advance(60)
        assert send_alert(store, http, cfg, FINDINGS, 3600) == "cooldown"
    assert slack.effects == 1
    store.advance(3600)
    assert send_alert(store, http, cfg, FINDINGS, 3600) == "sent" and slack.effects == 2  # a long outage reminds once per cooldown
    http.close()


def test_a_post_that_fails_is_retried_after_a_minute_not_after_a_whole_cooldown(store, slack):
    cfg = alert_cfg(slack.url)
    http = Http()
    slack.queue("server_error")
    assert send_alert(store, http, cfg, FINDINGS, 3600) == "failed" and slack.effects == 0
    store.advance(30)
    assert send_alert(store, http, cfg, FINDINGS, 3600) == "cooldown"  # not on every tick
    store.advance(40)
    assert send_alert(store, http, cfg, FINDINGS, 3600) == "sent" and slack.effects == 1
    http.close()


@pytest.mark.parametrize("mode", ["forbidden", "rate_limit", "invalid", "gone", "odd_ok"])
def test_every_kind_of_refusal_from_slack_is_a_failed_alert_and_reopens_the_gate(store, slack, mode):
    cfg = alert_cfg(slack.url)
    http = Http()
    slack.queue(mode)
    assert send_alert(store, http, cfg, FINDINGS, 3600) == "failed"
    store.advance(61)
    assert send_alert(store, http, cfg, FINDINGS, 3600) == "sent"  # the control: the same call succeeds once Slack does
    http.close()


def test_a_post_that_raises_is_a_failed_alert_not_a_crash(store):
    class Down:
        def request(self, *a, **k):
            raise HttpConnectError("ConnectError")

    assert send_alert(store, Down(), alert_cfg("http://127.0.0.1:9/hook"), FINDINGS, 3600) == "failed"


def test_without_an_alert_webhook_it_logs_and_does_not_use_up_the_cooldown(store, caplog):
    with caplog.at_level("WARNING"):
        assert send_alert(store, Http(), alert_cfg(None), FINDINGS, 3600) == "no_webhook"
    assert "RELAY_ALERT relay health alert" in caplog.text
    with store.tx(write=False) as t:
        assert t.execute("SELECT COUNT(*) AS n FROM alert_state WHERE sink = 'health'").fetchone()["n"] == 0


def test_when_the_cooldown_cannot_be_read_nothing_is_posted_and_it_is_said(store, slack, monkeypatch, caplog):
    cfg = alert_cfg(slack.url)

    def broken(sink, interval):
        raise RuntimeError("locked")

    monkeypatch.setattr(store, "alert_gate", broken)
    with caplog.at_level("ERROR"):
        assert send_alert(store, Http(), cfg, FINDINGS, 3600) == "gate_unreadable"
    assert slack.requests == [] and "cooldown could not be read" in caplog.text


def test_the_alert_text_is_escaped_for_slack_and_the_fake_accepts_it(slack, store):
    nasty = [Finding("heartbeat", "a < b & c > d")]
    assert "&lt;" in alert_text(nasty) and "&amp;" in alert_text(nasty) and "<" not in alert_text(nasty)
    http = Http()
    assert send_alert(store, http, alert_cfg(slack.url), nasty, 3600) == "sent"  # the strict fake refuses a raw < or a bare &
    http.close()


def test_the_command_alerts_only_when_asked_and_only_when_something_is_wrong(store, slack, capsys):
    http = Http()
    url = {"ALERT_WEBHOOK": slack.url}
    store.advance(10)
    assert run_main(store, capsys, http=http, **url)[0] == 1 and slack.effects == 0  # findings, no --alert: no post
    code, out, _ = run_main(store, capsys, ["--alert"], http=http, **url)
    assert code == 1 and "alert: sent" in out and slack.effects == 1
    code, out, _ = run_main(store, capsys, ["--alert"], http=http, **url)
    assert code == 1 and "alert: cooldown" in out and slack.effects == 1  # the exit code stays non-zero, the channel stays quiet
    store.beat("worker")
    store.advance(7200)
    store.beat("worker")
    code, out, _ = run_main(store, capsys, ["--alert", "--cooldown-s", "1"], http=http, **url)
    assert code == 0 and out.startswith("OK:") and slack.effects == 1  # healthy: never an alert, whatever the cooldown
    http.close()


# ---- a seeded property test over random worlds -----------------------------------------------------------------------------------


class ScriptHttp:
    """A Slack stand in whose answers come from the world's random generator, and that records every post."""

    def __init__(self, rng):
        self.rng, self.posts = rng, []

    def request(self, method, url, *, headers=None, body=None, timeout):
        self.posts.append((url, json.loads(body)["text"]))
        roll = self.rng.random()
        if roll < 0.55:
            return HttpResult(200, {}, "ok")
        if roll < 0.75:
            return HttpResult(500, {}, "internal_error")
        if roll < 0.9:
            raise HttpTimeout("ReadTimeout")
        raise HttpConnectError("ConnectError")


CODES = ("heartbeat", "backlog")
MESSAGES = {"heartbeat": "the worker has stopped", "backlog": "work is stuck"}


@pytest.mark.parametrize("seed", range(12))
def test_property_one_outage_never_alerts_twice_inside_a_cooldown_and_an_undelivered_alert_comes_back(store, seed):
    """Dimensions varied together: time (idle gaps from zero to hours), which findings are present, the post's fate (delivered,
    5xx, timeout, refused connection), and the cooldown. Rules: a post happens exactly when the model's gate is open (no repeat
    inside a cooldown after a delivery, a retry one minute after a failure, nothing silenced after that), and an alert is about
    exactly the findings it was given."""
    rng = random.Random(seed)
    cooldown = rng.choice([300.0, 3600.0, 7200.0])
    cfg = alert_cfg("http://127.0.0.1:9/hook")
    http = ScriptHttp(rng)
    clock = 0.0
    next_open = 0.0
    last_delivery = None
    for step in range(120):
        gap = rng.choice([0, 1, 7, 45, 61, 600, 3500, 3700, 8000])
        store.advance(gap)
        clock += gap
        present = [c for c in CODES if rng.random() < 0.6] or [rng.choice(CODES)]
        findings = [Finding(c, MESSAGES[c]) for c in present]
        posts_before = len(http.posts)
        result = send_alert(store, http, cfg, findings, cooldown)
        posted = len(http.posts) - posts_before
        if clock + 1e-9 < next_open:
            assert (result, posted) == ("cooldown", 0), (seed, step, result)  # nothing is sent while the gate is shut
            continue
        assert posted == 1 and result in ("sent", "failed"), (seed, step, result)  # nothing is silenced once it is open
        text = http.posts[-1][1]
        for c in CODES:
            assert (f"{c}:" in text) == (c in present), (seed, step, text)  # a message about X is about X, and about nothing else
        if result == "sent":
            if last_delivery is not None:
                assert clock - last_delivery >= cooldown - 1e-9, (seed, step)  # never two delivered alerts inside a cooldown
            last_delivery = clock
            next_open = clock + cooldown
        else:
            next_open = clock + health_check.RETRY_AFTER_FAILURE_S
