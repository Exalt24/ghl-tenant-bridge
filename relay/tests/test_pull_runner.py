"""`python -m relay.pulls`: exit codes, alerts, tenant selection and the window override, against the three fakes."""

from __future__ import annotations

import json
from datetime import timedelta

import pytest

from relay.config import load_config
from relay.http import Http
from relay.pulls.__main__ import main
from relay.pulls.runner import EXIT_ABORTED, EXIT_CONFIG, EXIT_ERROR, EXIT_OK, EXIT_THROTTLED, run_pull

from .fakes.ads import ACCOUNT, TOKEN, WINDSOR_KEY, day_rows, windsor_rows
from .fakes.fathom import API_KEY, make_meeting
from .helpers import TENANT, config_json, env_for
from .pull_helpers import FIXED_TODAY, StubHttp, pull_env, reply

START = FIXED_TODAY - timedelta(days=27)


def pulls_env(insights, windsor, fathom, slack_url, **extra):
    env = env_for(slack_url, None, **extra)
    env.update(
        {
            "ACME_INSIGHTS_BASE": insights.url, "ACME_INSIGHTS_TOKEN": TOKEN, "ACME_INSIGHTS_ACCOUNT": ACCOUNT,
            "ACME_WINDSOR_BASE": windsor.url, "ACME_WINDSOR_KEY": WINDSOR_KEY, "ACME_FATHOM_BASE": fathom.url, "ACME_FATHOM_KEY": API_KEY,
            "ALERT_WEBHOOK": slack_url, "DATABASE_URL": "",
        }
    )
    pulls = {
        "meta_insights": {"base_url_env": "ACME_INSIGHTS_BASE", "token_env": "ACME_INSIGHTS_TOKEN", "account_env": "ACME_INSIGHTS_ACCOUNT", "timeout_s": 5},
        "windsor": {"base_url_env": "ACME_WINDSOR_BASE", "api_key_env": "ACME_WINDSOR_KEY", "label": "main", "timeout_s": 5},
        "fathom": {"base_url_env": "ACME_FATHOM_BASE", "api_key_env": "ACME_FATHOM_KEY", "timeout_s": 5},
    }
    env["RELAY_CONFIG"] = config_json(make=False, tenant_extra={"pulls": pulls})
    return env


@pytest.fixture
def world(store, insights, windsor, fathom, slack):
    insights.rows = day_rows({"c1": "One"}, START, 28)
    windsor.rows = windsor_rows({"c1": "One"}, START, 28)
    fathom.meetings = [make_meeting(i, created="2026-10-20T10:00:00Z") for i in range(1, 4)]
    return pulls_env(insights, windsor, fathom, slack.url)


def run(argv, world, store, http=None, env=None):
    penv, clock = pull_env(http or Http())
    fathom_clock = clock
    return main(argv, environ=world, store=store, env=env or penv), clock, fathom_clock


def test_each_pull_runs_and_exits_zero_with_its_rows_stored(world, store, fathom):
    code, clock, _ = run(["meta_insights"], world, store)
    assert code == EXIT_OK and len(store.spend_rows("meta")) == 28
    assert run(["windsor"], world, store)[0] == EXIT_OK and len(store.spend_rows("windsor")) == 28
    fathom.clock = clock.monotonic
    assert run(["fathom", "--tenant", TENANT], world, store, env=pull_env(Http(), clock)[0])[0] == EXIT_OK and len(store.meetings()) == 3


def test_a_refused_token_exits_4_and_posts_one_alert_digest_to_the_alert_webhook(world, store, insights, slack):
    insights.queue("token_expired", "token_expired")
    code, clock, _ = run(["meta_insights"], world, store)
    assert code == EXIT_ABORTED and store.spend_rows() == [] and clock.sleeps == []
    assert slack.effects == 1
    text = json.dumps(slack.requests[0]["json"])
    assert "meta_insights" in text and "credentials refused" in text and "190" in text
    code, *_ = run(["meta_insights"], world, store)  # the next run is refused again: one digest per sink per 15 minutes
    assert code == EXIT_ABORTED and slack.effects == 1


def test_a_refused_windsor_key_and_a_refused_fathom_key_alert_under_their_own_names(world, store, windsor, fathom, slack):
    windsor.queue("unauthorized")
    assert run(["windsor"], world, store)[0] == EXIT_ABORTED
    fathom.queue("unauthorized")
    assert run(["fathom"], world, store)[0] == EXIT_ABORTED
    store.advance(16 * 60)
    from relay import alerts

    http = Http()
    cfg = load_config(world, role="worker_once")
    alerts.flush(store, http, cfg)  # the second sink's digest was held by the 15 minute rule per sink, not per run
    text = json.dumps([r["json"] for r in slack.requests])
    assert "windsor" in text and "fathom" in text


def test_a_throttle_longer_than_the_run_will_wait_exits_3_and_raises_no_alert(world, store, insights, slack):
    insights.regain_minutes = 30
    insights.queue("throttle")
    code, clock, _ = run(["meta_insights"], world, store)
    assert code == EXIT_THROTTLED and clock.sleeps == [] and store.spend_rows() == [] and slack.effects == 0


def test_untrustworthy_data_exits_1_with_an_alert_and_nothing_written(world, store, insights, slack):
    insights.spend_type = "number"
    code, *_ = run(["meta_insights"], world, store)
    assert code == EXIT_ERROR and store.spend_rows() == [] and slack.effects == 1
    assert "pull failed" in json.dumps(slack.requests[0]["json"])


def test_the_alert_goes_to_the_log_when_there_is_no_alert_webhook(world, store, insights, caplog):
    import logging

    world = {k: v for k, v in world.items() if k != "ALERT_WEBHOOK"}
    insights.queue("token_expired")
    with caplog.at_level(logging.WARNING):
        assert run(["meta_insights"], world, store)[0] == EXIT_ABORTED
    assert any("RELAY_ALERT" in r.getMessage() and "meta_insights" in r.getMessage() for r in caplog.records)


def test_days_overrides_the_window_for_one_run(world, store, insights):
    assert run(["meta_insights", "--days", "3"], world, store)[0] == EXIT_OK
    assert json.loads(insights.queries[0]["time_range"])["since"] == (FIXED_TODAY - timedelta(days=2)).isoformat()
    assert {r["day"] for r in store.spend_rows("meta")} == {(FIXED_TODAY - timedelta(days=i)).isoformat() for i in range(3)}


@pytest.mark.parametrize("days", ["0", "366", "-1"])
def test_a_days_value_outside_1_to_365_is_a_config_exit(world, store, days, capsys):
    assert run(["meta_insights", "--days", days], world, store)[0] == EXIT_CONFIG
    assert "--days must be from 1 to 365" in capsys.readouterr().err and store.spend_rows() == []


def test_the_tenant_is_picked_when_only_one_has_the_pull_and_must_be_named_when_it_is_ambiguous_or_wrong(world, store, capsys):
    assert run(["windsor"], world, store)[0] == EXIT_OK  # one tenant configures it: no --tenant needed
    assert run(["windsor", "--tenant", "nobody"], world, store)[0] == EXIT_CONFIG
    assert "no tenant to run windsor for (configured: acme)" in capsys.readouterr().err
    two = json.loads(world["RELAY_CONFIG"])
    two["tenants"]["acme2"] = {"pulls": two["tenants"]["acme"]["pulls"]}
    ambiguous = {**world, "RELAY_CONFIG": json.dumps(two)}
    assert run(["windsor"], ambiguous, store)[0] == EXIT_CONFIG
    assert "configured: acme, acme2" in capsys.readouterr().err
    assert run(["windsor", "--tenant", "acme2"], ambiguous, store)[0] == EXIT_OK


def test_a_pull_that_nobody_configured_is_a_config_exit(store, slack, capsys):
    env = env_for(slack.url, None)
    env["RELAY_CONFIG"] = config_json(make=False)
    penv, _ = pull_env(Http())
    assert main(["fathom"], environ=env, store=store, env=penv) == EXIT_CONFIG
    assert "configured: none" in capsys.readouterr().err


def test_an_unsafe_config_is_refused_without_a_traceback(world, capsys, tmp_path):
    prod = {**world, "RELAY_ENV": "prod", "RELAY_DB_PATH": str(tmp_path / "x.db"), "RELAY_INLINE_WORKER": "1", "DATABASE_URL": ""}
    assert main(["windsor"], environ=prod) == EXIT_CONFIG  # a one shot service in prod needs Postgres
    assert "configuration refused" in capsys.readouterr().err


def test_the_cli_prints_no_key_and_logs_no_key(world, store, capsys, caplog):
    import logging

    with caplog.at_level(logging.DEBUG):
        run(["windsor"], world, store)
        run(["meta_insights"], world, store)
    out = capsys.readouterr()
    text = out.out + out.err + "\n".join(f"{r.name} {r.getMessage()}" for r in caplog.records if not r.name.startswith("werkzeug"))
    assert WINDSOR_KEY not in text and TOKEN not in text and API_KEY not in text
    assert any(r.name == "relay.pulls" for r in caplog.records)  # control: the run did log


def test_an_unknown_pull_name_is_refused_by_the_parser(world, store):
    with pytest.raises(SystemExit):
        run(["google_ads"], world, store)


def test_run_pull_turns_an_unexpected_pull_name_into_an_error(store, slack):
    env = env_for(slack.url, None)
    env["RELAY_CONFIG"] = config_json(make=False)
    cfg = load_config(env, role="worker_once")
    from relay.config import PullCfg

    cfg.tenants[TENANT].pulls["bogus"] = PullCfg(name="bogus", base_url="http://127.0.0.1:9", key="k" * 10, account="a", days=1, timeout_s=1.0)
    penv, _ = pull_env(StubHttp(reply(200, "{}")))
    assert run_pull("bogus", store, cfg, TENANT, penv) == EXIT_ERROR
