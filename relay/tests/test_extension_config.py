"""The settings and seams the history import, the Windows service and the health check added: the lease floor, the default region,
the Slack sink's plain text alert and the outcome of a refused contact."""

from __future__ import annotations

import pytest

from relay.config import ConfigError, load_config
from relay.fanout import LEASE_MIN_S, lease_for
from relay.http import Http
from relay.sinks.base import SKIPPED, Outcome, SinkContext
from relay.sinks.slack import SAFE_TEXT_CHARS, SlackSink, build_alert_message

from .helpers import config_json, env_for, make_cfg


def cfg_with(**env_extra):
    env = env_for("http://127.0.0.1:9/s", "http://127.0.0.1:9/m", **env_extra)
    env["RELAY_CONFIG"] = config_json()
    return load_config(env, role="worker")


def test_the_lease_floor_is_the_default_unless_the_environment_lowers_it():
    assert cfg_with().lease_min_s is None
    cfg = make_cfg("http://127.0.0.1:9/s", None, slack_timeout=10.0)
    assert lease_for(cfg, "slack", 10.0) == LEASE_MIN_S == 300.0  # control: no setting, the old behaviour
    low = cfg_with(RELAY_LEASE_MIN_S="1")
    assert low.lease_min_s == 1.0


@pytest.mark.parametrize("floor,timeout,expected", [(1.0, 2.0, 6.0), (1.0, 0.1, 1.0), (50.0, 5.0, 50.0), (1.0, 30.0, 90.0), (400.0, 30.0, 400.0)])
def test_a_lease_is_never_shorter_than_three_sink_timeouts_whatever_the_floor(floor, timeout, expected):
    from dataclasses import replace

    cfg = replace(make_cfg("http://127.0.0.1:9/s", None, slack_timeout=timeout), lease_min_s=floor)
    assert lease_for(cfg, "slack", timeout) == expected


@pytest.mark.parametrize("bad", ["0", "0.5", "-3", "abc", "nan", "inf"])
def test_a_lease_floor_under_a_second_or_not_a_number_is_refused(bad):
    with pytest.raises(ConfigError, match="RELAY_LEASE_MIN_S"):
        cfg_with(RELAY_LEASE_MIN_S=bad)


def test_the_default_region_is_us_unless_set_and_a_bad_one_is_refused_at_load():
    assert cfg_with().default_region == "US"
    assert cfg_with(RELAY_DEFAULT_REGION="ph").default_region == "PH"
    with pytest.raises(ConfigError, match="RELAY_DEFAULT_REGION"):
        cfg_with(RELAY_DEFAULT_REGION="ZZ")


def test_a_refused_contact_is_a_skipped_outcome_with_the_reason_kept_as_its_effect_ref():
    sup = Outcome.suppressed("suppressed: asked to stop")
    seen = Outcome.suppressed("already sent before the relay")
    assert (sup.kind, sup.effect_ref, sup.reason) == (SKIPPED, "suppressed", "suppressed: asked to stop")
    assert (seen.kind, seen.effect_ref) == (SKIPPED, "already_sent")
    assert sup.alert is False  # working as designed: stored and visible, not a page


def test_the_slack_sink_sends_a_plain_text_alert_through_the_same_request_as_a_lead(slack):
    ctx = SinkContext(tenant="relay", url=slack.url, timeout_s=5.0)
    http = Http()
    out = SlackSink().send_alert("relay health alert: heartbeat: stopped", http, ctx)
    assert out.kind == "DONE" and slack.effects == 1
    assert slack.requests[0]["json"] == {"text": "relay health alert: heartbeat: stopped"}
    slack.queue("rate_limit")
    assert SlackSink().send_alert("again", http, ctx).kind == "RETRY"  # the same status handling as a lead: 429 is a retry
    slack.queue("forbidden")
    assert SlackSink().send_alert("again", http, ctx).kind == "PAUSE"
    http.close()


def test_an_alert_text_is_escaped_then_cut_and_never_empty():
    assert build_alert_message("a < b & c")["text"] == "a &lt; b &amp; c"
    long = build_alert_message("x" * 5000)["text"]
    assert len(long) <= SAFE_TEXT_CHARS + 80 and long.endswith("(truncated, the full payload is stored by the relay)")
    assert build_alert_message("")["text"] == "relay alert"
