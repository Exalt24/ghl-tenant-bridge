"""Config guards. Each guard has a test here and a mutant in tests/mutants.py."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from relay import redact
from relay.config import ConfigError, check_base_url, load_config
from relay.ghl import GHL_ED25519_SPKI_B64

from .helpers import INGEST_TOKEN, TENANT, config_json, env_for

LOCAL_SLACK = "http://127.0.0.1:9/services/T0/B0/XX"
LOCAL_MAKE = "http://localhost:9/hook/abc"
REPO = Path(__file__).resolve().parents[2]


def env(**extra: str) -> dict[str, str]:
    e = env_for(LOCAL_SLACK, LOCAL_MAKE, **extra)
    e["RELAY_CONFIG"] = config_json()
    return e


def test_a_valid_config_loads_with_tenants_sinks_and_locations():
    cfg = load_config(env())
    assert cfg.env == "test" and cfg.locations == {"LOC1": TENANT}
    tenant = cfg.tenants[TENANT]
    assert tenant.enabled_sinks() == ["make", "slack"]
    assert tenant.sinks["slack"].url == LOCAL_SLACK and tenant.sinks["slack"].timeout_s == 5.0
    assert tenant.sinks["slack"].min_interval_s == 1.0 and tenant.sinks["make"].min_interval_s == 0.1
    assert tenant.dedupe_window_s == 3600.0 and tenant.max_body_bytes == 256 * 1024


def test_config_can_be_given_as_a_file_path(tmp_path):
    f = tmp_path / "relay.json"
    f.write_text(config_json(), encoding="utf-8")
    e = env()
    e["RELAY_CONFIG"] = str(f)
    assert load_config(e).tenants[TENANT].enabled_sinks() == ["make", "slack"]


@pytest.mark.parametrize("key", ["url", "token", "secret", "apikey", "webhook_url", "password"])
def test_a_literal_secret_key_in_the_json_is_refused_by_name(key):
    bad = json.loads(config_json())
    bad["tenants"][TENANT]["sinks"]["slack"][key] = "https://hooks.slack.com/services/T0/B0/real"
    e = env()
    e["RELAY_CONFIG"] = json.dumps(bad)
    with pytest.raises(ConfigError) as err:
        load_config(e)
    assert f"`{key}`" in str(err.value) and "_env" in str(err.value)
    assert "hooks.slack.com" not in str(err.value)


def test_a_literal_workflow_secret_is_refused():
    bad = json.loads(config_json())
    bad["tenants"][TENANT]["workflow_secret"] = "hunter2hunter2hunter2"
    e = env()
    e["RELAY_CONFIG"] = json.dumps(bad)
    with pytest.raises(ConfigError, match="workflow_secret"):
        load_config(e)


def test_unknown_keys_and_unknown_sinks_are_refused():
    for mutate, text in (
        (lambda c: c.update(surprise=1), "unknown key"),
        (lambda c: c["tenants"][TENANT]["sinks"].update(carrier_pigeon={"url_env": "X"}), "unknown sink"),
        (lambda c: c["tenants"][TENANT]["sinks"]["slack"].update(colour="red"), "unknown key"),
    ):
        bad = json.loads(config_json())
        mutate(bad)
        e = env()
        e["RELAY_CONFIG"] = json.dumps(bad)
        with pytest.raises(ConfigError, match=text):
            load_config(e)


def test_a_missing_environment_variable_is_named_but_no_value_leaks():
    e = env()
    del e["ACME_SLACK_URL"]
    with pytest.raises(ConfigError, match="ACME_SLACK_URL"):
        load_config(e)


def test_a_location_must_map_to_a_known_tenant():
    bad = json.loads(config_json())
    bad["locations"]["LOC2"] = "nobody"
    e = env()
    e["RELAY_CONFIG"] = json.dumps(bad)
    with pytest.raises(ConfigError, match="unknown tenant"):
        load_config(e)


def test_tenant_for_location_resolves_and_unmapped_is_none():
    cfg = load_config(env())
    assert cfg.tenant_for_location("LOC1") == TENANT
    assert cfg.tenant_for_location("LOC9") is None and cfg.tenant_for_location(None) is None and cfg.tenant_for_location(["LOC1"]) is None


# ---- RELAY_LIVE: the fake base URL guard ------------------------------------------------------------------------------


def test_a_real_vendor_url_is_refused_unless_relay_live_is_1():
    e = env(ACME_SLACK_URL="https://hooks.slack.com/services/T0/B0/XYZ")
    with pytest.raises(ConfigError, match="RELAY_LIVE"):
        load_config(e)
    e["RELAY_LIVE"] = "1"
    assert load_config(e).tenants[TENANT].sinks["slack"].url.startswith("https://hooks.slack.com/")  # positive control


def test_relay_live_still_requires_https_for_a_non_local_host():
    e = env(ACME_SLACK_URL="http://hooks.example.test/services/x", RELAY_LIVE="1")
    with pytest.raises(ConfigError, match="https"):
        load_config(e)


def test_only_the_literal_value_1_turns_live_on():
    for v in ("true", "yes", "0", "", "11"):
        e = env(ACME_SLACK_URL="https://hooks.slack.com/services/T0/B0/XYZ", RELAY_LIVE=v)
        with pytest.raises(ConfigError):
            load_config(e)


def test_the_error_message_never_contains_the_url():
    secret_url = "https://hooks.slack.com/services/T0SECRET/B0SECRET/TOKENSECRET"
    try:
        load_config(env(ACME_SLACK_URL=secret_url))
    except ConfigError as exc:
        assert "TOKENSECRET" not in str(exc) and "hooks.slack.com" not in str(exc)
    else:
        pytest.fail("the guard did not fire")


def test_local_hosts_pass_and_odd_schemes_do_not():
    for ok in ("http://127.0.0.1:1/x", "http://localhost/x", "http://[::1]:5/x"):
        check_base_url(ok, False, "u")
    for bad in ("ftp://127.0.0.1/x", "file:///etc/passwd", "127.0.0.1:5", "http:///nohost"):
        with pytest.raises(ConfigError):
            check_base_url(bad, True, "u")


def test_the_alert_webhook_obeys_the_same_guard():
    with pytest.raises(ConfigError, match="alert webhook"):
        load_config(env(ALERT_WEBHOOK="https://hooks.slack.com/services/T0/B0/ALERT"))
    assert load_config(env(ALERT_WEBHOOK="http://127.0.0.1:9/alert")).alert_url == "http://127.0.0.1:9/alert"


# ---- RELAY_ENV and storage ---------------------------------------------------------------------------------------------


def test_relay_env_must_be_one_of_three_and_unset_means_prod():
    with pytest.raises(ConfigError, match="RELAY_ENV"):
        load_config(env(RELAY_ENV="staging"))
    e = env()
    del e["RELAY_ENV"]
    # prod without Postgres and without the SQLite conditions is refused, which proves unset resolved to prod
    with pytest.raises(ConfigError, match="SQLite is refused in prod"):
        load_config(e)


def test_sqlite_is_refused_in_prod_without_the_single_container_conditions(tmp_path):
    prod = env(RELAY_ENV="prod")
    with pytest.raises(ConfigError, match="SQLite is refused in prod"):
        load_config(prod)
    with pytest.raises(ConfigError, match="SQLite is refused in prod"):
        load_config({**prod, "RELAY_DB_PATH": str(tmp_path / "x.db")})  # path but no inline worker
    with pytest.raises(ConfigError, match="SQLite is refused in prod"):
        load_config({**prod, "RELAY_INLINE_WORKER": "1"})  # inline worker but no path
    with pytest.raises(ConfigError, match="SQLite is refused in prod"):
        load_config({**prod, "RELAY_DB_PATH": "relative/x.db", "RELAY_INLINE_WORKER": "1"})  # not an absolute volume path
    ok = load_config({**prod, "RELAY_DB_PATH": str(tmp_path / "x.db"), "RELAY_INLINE_WORKER": "1"})  # positive control
    assert ok.env == "prod" and ok.inline_worker


def test_postgres_is_always_allowed_in_prod():
    cfg = load_config({**env(RELAY_ENV="prod"), "DATABASE_URL": "postgresql://u:p@db.internal/relay"})
    assert cfg.database_url and cfg.db_path is None


def test_a_separate_once_worker_requires_postgres_even_with_a_volume(tmp_path):
    prod = {**env(RELAY_ENV="prod"), "RELAY_DB_PATH": str(tmp_path / "x.db"), "RELAY_INLINE_WORKER": "1"}
    assert load_config(prod, role="web")  # positive control: the same environment is fine for the web role
    with pytest.raises(ConfigError, match="separate worker service needs Postgres"):
        load_config(prod, role="worker_once")
    assert load_config({**prod, "DATABASE_URL": "postgresql://u:p@h/db"}, role="worker_once")


def test_dev_and_test_may_use_sqlite_freely():
    for name in ("dev", "test"):
        assert load_config(env(RELAY_ENV=name)).database_url is None


# ---- tokens and the test key override -----------------------------------------------------------------------------------


def test_the_web_role_needs_an_ingest_token_and_short_tokens_are_refused():
    e = env()
    del e["RELAY_INGEST_TOKEN"]
    with pytest.raises(ConfigError, match="RELAY_INGEST_TOKEN"):
        load_config(e)
    assert load_config(e, role="worker")  # a worker has no ingress
    with pytest.raises(ConfigError, match="at least"):
        load_config(env(RELAY_INGEST_TOKEN="short"))
    with pytest.raises(ConfigError, match="at least"):
        load_config(env(RELAY_ADMIN_TOKEN="short"))


def test_the_admin_token_must_differ_from_the_ingest_tokens():
    with pytest.raises(ConfigError, match="differ"):
        load_config(env(RELAY_ADMIN_TOKEN=INGEST_TOKEN))
    with pytest.raises(ConfigError, match="differ"):
        load_config(env(RELAY_INGEST_TOKEN_PREVIOUS="x" * 20, RELAY_ADMIN_TOKEN="x" * 20))


def test_the_test_signature_key_is_honoured_only_when_relay_env_is_test():
    other = "MCowBQYDK2VwAyEAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="
    assert load_config(env(RELAY_ENV="test", RELAY_TEST_GHL_PUBLIC_KEY=other)).ghl_public_key_b64 == other
    assert load_config(env(RELAY_ENV="dev", RELAY_TEST_GHL_PUBLIC_KEY=other)).ghl_public_key_b64 == GHL_ED25519_SPKI_B64
    prod = {**env(RELAY_ENV="prod"), "DATABASE_URL": "postgresql://u:p@h/db", "RELAY_TEST_GHL_PUBLIC_KEY": other}
    assert load_config(prod).ghl_public_key_b64 == GHL_ED25519_SPKI_B64
    assert load_config(env()).ghl_public_key_b64 == GHL_ED25519_SPKI_B64  # no override given, published key


def test_nothing_shipped_sets_the_test_signature_key_or_relay_live():
    shipped = [REPO / "relay" / "Dockerfile", REPO / ".github" / "workflows" / "ci.yml", *(REPO / "relay").glob("*.example*"), *(REPO / "relay").glob("*.json")]
    for f in shipped:
        if f.exists():
            text = f.read_text(encoding="utf-8")
            assert "RELAY_TEST_GHL_PUBLIC_KEY" not in text, f
            assert not re.search(r"RELAY_LIVE\s*[=:]\s*[\"']?1", text), f


def test_loading_a_config_registers_every_secret_for_redaction():
    load_config(env(ALERT_WEBHOOK="http://127.0.0.1:9/alert-secret-path"))
    for secret in (LOCAL_SLACK, LOCAL_MAKE, INGEST_TOKEN, "http://127.0.0.1:9/alert-secret-path"):
        assert secret not in redact.mask(f"error talking to {secret} now")


def test_per_tenant_overrides_are_validated():
    bad = json.loads(config_json(tenant_extra={"dedupe_mode": "sometimes"}))
    e = env()
    e["RELAY_CONFIG"] = json.dumps(bad)
    with pytest.raises(ConfigError, match="dedupe_mode"):
        load_config(e)
    for key, val in (("timeout_s", -1), ("timeout_s", "fast"), ("min_interval_s", True), ("pause_s", 0)):
        bad = json.loads(config_json())
        bad["tenants"][TENANT]["sinks"]["slack"][key] = val
        e["RELAY_CONFIG"] = json.dumps(bad)
        with pytest.raises(ConfigError, match=key):
            load_config(e)
