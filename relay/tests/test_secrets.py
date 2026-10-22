"""Secrets never reach a log line or a database column, in the happy path and through every failure path.

The seeded secrets are the Slack and Make hook URLs, the Make api key, every token, and a Meta style access token (the
Meta sink is built in a later tier, its token is registered here so the mask is proven for it too).
"""

from __future__ import annotations

import logging
import re

import httpx
import pytest

from relay import redact
from relay.app import Deps, create_app
from relay.config import load_config
from relay.sinks.base import Outcome
from relay.sinks.make import MakeSink

from .fakes.make import FakeMake
from .helpers import ADMIN_TOKEN, INGEST_TOKEN, WORKFLOW_SECRET, ScriptSink, config_json, env_for, ingest_one, make_cfg, make_rig, payload

META_TOKEN = "EAAGm0PX4ZCpsBAKtestMetaTokenValue1234567890"
MAKE_APIKEY = "make-api-key-0123456789abcdef"


def fragments(*urls: str) -> list[str]:
    """The secret parts of a URL, not just the whole: a leak of the path alone is still a leak."""
    out = []
    for u in urls:
        out.append(u)
        out.extend(seg for seg in re.split(r"[/:?&=]", u) if len(seg) >= 24)
    return out


def test_the_log_mask_masks_registered_urls_and_tokens_by_value_and_query_params_by_name(caplog):
    redact.install()
    redact.register("https://hooks.example.test/services/T0AAAAAAA/B0BBBBBBB/SECRETSECRETSECRETSECRET", META_TOKEN)
    log = logging.getLogger("relay.test")
    with caplog.at_level(logging.INFO):
        log.warning("slack said no for https://hooks.example.test/services/T0AAAAAAA/B0BBBBBBB/SECRETSECRETSECRETSECRET")
        log.warning("GET https://graph.facebook.com/v26.0/123/events?access_token=%s&x=1", META_TOKEN)
        log.warning("GET https://connectors.windsor.ai/facebook?api_key=abcd1234efgh&date_from=2026-01-01")
        log.warning("auth header Authorization: Bearer abc.def-ghi_jkl123")
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRETSECRET" not in text and META_TOKEN not in text and "abcd1234efgh" not in text and "abc.def-ghi_jkl123" not in text
    assert "date_from=2026-01-01" in text and "x=1" in text  # the rest of the line survives


def test_an_exception_traceback_is_masked_too(caplog):
    redact.install()
    redact.register(MAKE_APIKEY)
    with caplog.at_level(logging.ERROR):
        try:
            raise RuntimeError(f"upstream refused key {MAKE_APIKEY}")
        except RuntimeError:
            logging.getLogger("relay.test").error("it broke", exc_info=True)
    full = "\n".join(r.getMessage() + (r.exc_text or "") for r in caplog.records)
    assert MAKE_APIKEY not in full and "RuntimeError" in full


def test_the_http_libraries_are_quiet_by_default():
    redact.install()
    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


def test_no_secret_reaches_any_log_line_or_database_column_through_success_and_every_failure(store, slack, make, caplog):
    redact.uninstall()
    secret_env = env_for(slack.url, make.url, ACME_MAKE_KEY=MAKE_APIKEY, ALERT_WEBHOOK=slack.url + "-alerts")
    secret_env["RELAY_CONFIG"] = config_json(make_extra={"apikey_env": "ACME_MAKE_KEY"})
    cfg = load_config(secret_env)
    create_app(cfg, Deps(store=store))  # installs the mask, as a real start does
    redact.register(META_TOKEN)

    leaky = ScriptSink("slack", outcomes=[RuntimeError(f"cannot post to {slack.url} with {META_TOKEN}")] * 3)
    rig = make_rig(store, cfg, registry={"slack": leaky, "make": MakeSink()})
    make_fake_with_key = FakeMake(apikey=MAKE_APIKEY)  # an api key protected Make, to put the key on the wire
    make_fake_with_key.start()
    try:
        cfg.tenants["acme"].sinks["make"].url = make_fake_with_key.url
        redact.register(make_fake_with_key.url)
        with caplog.at_level(logging.DEBUG):
            ids = []
            for i, mode in enumerate(["accepted", "server_error", "queue_full", "rate_limit", "gone", "bad_request", "act_then_fail"]):
                make_fake_with_key.queue(mode)
                ids.append(ingest_one(store, cfg, payload(i)).event_id)
            rig.settle(max_rounds=200, jump_s=1000)
            with httpx.Client() as raw_client:  # a direct httpx call: its own logger must stay quiet
                raw_client.post(slack.url, json={"text": "x"})
    finally:
        make_fake_with_key.stop()

    secrets = fragments(slack.url, make.url, make_fake_with_key.url, cfg.alert_url or "") + [MAKE_APIKEY, META_TOKEN, INGEST_TOKEN, ADMIN_TOKEN, WORKFLOW_SECRET]
    secrets = [s for s in secrets if len(s) >= 8]
    log_text = "\n".join(
        f"{r.name} {r.getMessage()} {r.exc_text or ''}" for r in caplog.records if not r.name.startswith("werkzeug")
    )
    db_text = "\n".join(store.dump_all_text())
    # positive controls: the sweeps would have found something real, and the run really produced both kinds of output
    assert "sink bug: RuntimeError" in db_text and ids[0] in db_text
    assert any(r.name == "relay.fanout" for r in caplog.records)
    for s in secrets:
        assert s not in log_text, f"secret reached a log line: {s[:12]}..."
        assert s not in db_text, f"secret reached the database: {s[:12]}..."


def test_the_stored_error_is_a_status_or_a_class_name_never_str_of_the_exception(store, slack, make):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    secret_message = f"boom with {slack.url}"
    rig = make_rig(store, cfg, registry={"slack": ScriptSink("slack", outcomes=[ValueError(secret_message)]), "make": MakeSink()})
    r = ingest_one(store, cfg)
    rig.settle()
    d = store.delivery(r.event_id, "slack")
    assert d["last_error"] == "sink bug: ValueError"
    assert all(a["note"] in (None, "sink bug: ValueError") for a in store.attempts_for(r.event_id, "slack"))
    assert slack.url not in "\n".join(store.dump_all_text())


def test_a_sink_reason_with_a_url_in_it_is_a_sink_bug_the_fan_out_cannot_prevent_but_the_mask_still_covers(store, slack, make, caplog):
    redact.uninstall()
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    create_app(cfg, Deps(store=store))
    with caplog.at_level(logging.INFO):
        logging.getLogger("relay.fanout").info("sink said %s", Outcome.dead(f"bad hook {slack.url}").reason)
    assert slack.url not in "\n".join(r.getMessage() for r in caplog.records)
