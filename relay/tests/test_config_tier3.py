"""Config for the Fathom receiver and the pulls: names only, the base URL guard, the secret shape, and what is refused."""

from __future__ import annotations

import json

import pytest

from relay.config import FATHOM_MAX_BODY_BYTES, ConfigError, load_config

from .fakes.fathom import WEBHOOK_SECRET
from .helpers import TENANT, config_json, env_for

SLACK = "http://127.0.0.1:9/services/T0/B0/XX"
INSIGHTS_BASE = "http://127.0.0.1:9/v26.0"
SECRET_VALUES = {
    "ACME_FATHOM_SECRET": WEBHOOK_SECRET, "ACME_INSIGHTS_BASE": INSIGHTS_BASE, "ACME_INSIGHTS_TOKEN": "tok-insights-0123456789",
    "ACME_INSIGHTS_ACCOUNT": "act_fake01", "ACME_WINDSOR_BASE": "http://127.0.0.1:9", "ACME_WINDSOR_KEY": "key-windsor-0123456789",
    "ACME_FATHOM_BASE": "http://127.0.0.1:9/external/v1", "ACME_FATHOM_KEY": "key-fathom-0123456789",
}
FATHOM = {"secret_env": "ACME_FATHOM_SECRET", "slack": True}
PULLS = {
    "meta_insights": {"base_url_env": "ACME_INSIGHTS_BASE", "token_env": "ACME_INSIGHTS_TOKEN", "account_env": "ACME_INSIGHTS_ACCOUNT"},
    "windsor": {"base_url_env": "ACME_WINDSOR_BASE", "api_key_env": "ACME_WINDSOR_KEY", "label": "main"},
    "fathom": {"base_url_env": "ACME_FATHOM_BASE", "api_key_env": "ACME_FATHOM_KEY"},
}


def env_with(fathom=FATHOM, pulls=PULLS, extra_env=None, tenant_extra=None, slack=True, **top):
    e = env_for(SLACK, None, **SECRET_VALUES)
    e.update(extra_env or {})
    te = {}
    if fathom is not None:
        te["fathom"] = fathom
    if pulls is not None:
        te["pulls"] = pulls
    te.update(tenant_extra or {})
    e["RELAY_CONFIG"] = config_json(make=False, slack=slack, tenant_extra=te)
    return e


def test_a_config_with_fathom_and_all_three_pulls_loads():
    cfg = load_config(env_with())
    t = cfg.tenants[TENANT]
    assert t.fathom.secret == WEBHOOK_SECRET and t.fathom.slack is True
    assert t.fathom.tolerance_s == 300.0 and t.fathom.max_body_bytes == 2 * 1024 * 1024 == FATHOM_MAX_BODY_BYTES
    assert sorted(t.pulls) == ["fathom", "meta_insights", "windsor"]
    m, w, f = t.pulls["meta_insights"], t.pulls["windsor"], t.pulls["fathom"]
    assert (m.base_url, m.key, m.account, m.days, m.extra["limit"]) == (INSIGHTS_BASE, "tok-insights-0123456789", "act_fake01", 28, 500)
    assert (w.key, w.account, w.days) == ("key-windsor-0123456789", "main", 28)
    assert (f.key, f.extra["include_summary"], f.timeout_s) == ("key-fathom-0123456789", False, 30.0)


def test_a_config_without_them_still_loads_with_nothing_set():
    cfg = load_config(env_with(fathom=None, pulls=None))
    assert cfg.tenants[TENANT].fathom is None and cfg.tenants[TENANT].pulls == {}


def test_overrides_are_read():
    fathom = {**FATHOM, "tolerance_s": 120, "max_body_bytes": 4096}
    pulls = {"meta_insights": {**PULLS["meta_insights"], "days": 14, "limit": 100, "timeout_s": 9}, "fathom": {**PULLS["fathom"], "include_summary": True, "days": 3}}
    t = load_config(env_with(fathom=fathom, pulls=pulls)).tenants[TENANT]
    assert (t.fathom.tolerance_s, t.fathom.max_body_bytes) == (120.0, 4096)
    assert (t.pulls["meta_insights"].days, t.pulls["meta_insights"].extra["limit"], t.pulls["meta_insights"].timeout_s) == (14, 100, 9.0)
    assert (t.pulls["fathom"].extra["include_summary"], t.pulls["fathom"].days) == (True, 3)


@pytest.mark.parametrize(
    "secret",
    ["not-a-whsec-value-0123456789", "whsec_!!!notbase64!!!", "whsec_c2hvcnQ=", "whsec_" + "A" * 3],
)
def test_a_fathom_secret_that_is_not_whsec_plus_enough_base64_is_refused_without_echoing_it(secret):
    load_config(env_with())  # control
    with pytest.raises(ConfigError) as err:
        load_config(env_with(extra_env={"ACME_FATHOM_SECRET": secret}))
    assert "whsec_" in str(err.value) and secret not in str(err.value)


def test_a_missing_secret_variable_is_named_not_guessed():
    e = env_with()
    del e["ACME_FATHOM_SECRET"]
    with pytest.raises(ConfigError, match="ACME_FATHOM_SECRET is not set"):
        load_config(e)


@pytest.mark.parametrize("where", ["fathom", "pull"])
def test_a_literal_secret_in_the_json_is_refused_by_name(where):
    load_config(env_with())  # control
    if where == "fathom":
        bad = env_with(fathom={**FATHOM, "secret": "whsec_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"})
    else:
        bad = env_with(pulls={**PULLS, "windsor": {**PULLS["windsor"], "api_key": "literal-key-value-0123"}})
    with pytest.raises(ConfigError) as err:
        load_config(bad)
    assert "_env" in str(err.value) and "literal-key" not in str(err.value) and "aaaaaaaa" not in str(err.value)


def test_unknown_keys_and_unknown_pulls_are_refused():
    with pytest.raises(ConfigError, match="unknown key `bogus`"):
        load_config(env_with(fathom={**FATHOM, "bogus": 1}))
    with pytest.raises(ConfigError, match="unknown key `bogus`"):
        load_config(env_with(pulls={**PULLS, "fathom": {**PULLS["fathom"], "bogus": 1}}))
    with pytest.raises(ConfigError, match="unknown pull `google_ads`"):
        load_config(env_with(pulls={**PULLS, "google_ads": {}}))
    with pytest.raises(ConfigError, match="must be an object"):
        load_config(env_with(pulls={**PULLS, "windsor": "yes"}))
    with pytest.raises(ConfigError, match="pulls must be an object"):
        load_config(env_with(pulls=[]))
    with pytest.raises(ConfigError, match="fathom must be an object"):
        load_config(env_with(fathom=[]))


def test_a_required_environment_name_for_a_pull_must_be_given():
    bad = {**PULLS, "meta_insights": {"base_url_env": "ACME_INSIGHTS_BASE", "token_env": "ACME_INSIGHTS_TOKEN"}}
    with pytest.raises(ConfigError, match="account_env"):
        load_config(env_with(pulls=bad))


def test_slack_fan_out_needs_an_enabled_slack_sink():
    assert load_config(env_with()).tenants[TENANT].fathom.slack is True  # control
    with pytest.raises(ConfigError, match="enabled slack sink"):
        load_config(env_with(slack=False))
    with pytest.raises(ConfigError, match="true or false"):
        load_config(env_with(fathom={**FATHOM, "slack": "yes"}))
    assert load_config(env_with(slack=False, fathom={"secret_env": "ACME_FATHOM_SECRET"})).tenants[TENANT].fathom.slack is False


def test_a_pull_base_url_that_is_not_local_is_refused_unless_relay_live_and_https():
    real = {"ACME_INSIGHTS_BASE": "https://graph.facebook.com/v26.0"}
    with pytest.raises(ConfigError, match="RELAY_LIVE is not 1") as err:
        load_config(env_with(extra_env=real))
    assert "graph.facebook.com" not in str(err.value)
    assert load_config(env_with(extra_env={**real, "RELAY_LIVE": "1"})).tenants[TENANT].pulls["meta_insights"].base_url.startswith("https://graph")
    with pytest.raises(ConfigError, match="https"):
        load_config(env_with(extra_env={"ACME_WINDSOR_BASE": "http://connectors.windsor.ai", "RELAY_LIVE": "1"}))


def test_the_window_is_bounded_and_the_windsor_label_is_plain():
    with pytest.raises(ConfigError, match="at most 365"):
        load_config(env_with(pulls={**PULLS, "meta_insights": {**PULLS["meta_insights"], "days": 400}}))
    with pytest.raises(ConfigError, match="positive"):
        load_config(env_with(pulls={**PULLS, "meta_insights": {**PULLS["meta_insights"], "days": 0}}))
    for label in ("has space", "x" * 65, "a/b", ""):
        with pytest.raises(ConfigError, match="label"):
            load_config(env_with(pulls={**PULLS, "windsor": {**PULLS["windsor"], "label": label}}))
    default = {**PULLS, "windsor": {k: v for k, v in PULLS["windsor"].items() if k != "label"}}
    assert load_config(env_with(pulls=default)).tenants[TENANT].pulls["windsor"].account == "default"


def test_the_fathom_secret_and_every_pull_key_are_in_the_set_the_log_mask_hides():
    cfg = load_config(env_with())
    hidden = set(cfg.secret_values())
    assert {WEBHOOK_SECRET, "tok-insights-0123456789", "key-windsor-0123456789", "key-fathom-0123456789"} <= hidden


def test_the_example_config_names_the_new_blocks_and_holds_no_value():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "config.example.json").read_text(encoding="utf-8")
    doc = json.loads(text)
    t = doc["tenants"]["acme"]
    assert "fathom" in t and "pulls" in t and set(t["pulls"]) == {"meta_insights", "windsor", "fathom"}
    assert "whsec_" not in text
