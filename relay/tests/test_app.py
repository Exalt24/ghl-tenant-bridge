"""The ingress routes through Flask's test client against a real store (SQLite and Postgres)."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

import pytest

from relay import ghl, redact
from relay.app import Deps, create_app
from relay.config import load_config
from relay.http import Http

from .helpers import ADMIN_TOKEN, INGEST_TOKEN, LOCATION, PREVIOUS_TOKEN, TENANT, WORKFLOW_SECRET, config_json, env_for, make_cfg, make_rig, payload, wait_for


def auth(token: str = INGEST_TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def post_ingest(client, p, key="wh-0001", token=INGEST_TOKEN, raw: bytes | None = None, headers: dict | None = None):
    h = {**auth(token), "Content-Type": "application/json", **(headers if headers is not None else {"Idempotency-Key": key})}
    return client.post("/ingest", data=raw if raw is not None else json.dumps(p), headers=h)


def post_wf(client, body, secret=WORKFLOW_SECRET, tenant=TENANT, raw: bytes | None = None):
    h = {"Content-Type": "application/json"}
    if secret is not None:
        h["X-Relay-Secret"] = secret
    return client.post(f"/hooks/{tenant}/workflow", data=raw if raw is not None else json.dumps(body), headers=h)


@pytest.fixture
def ctx(store, slack, make):
    cfg = make_cfg(slack.url, make.url)
    app = create_app(cfg, Deps(store=store))
    return app.test_client(), cfg, store


# ---- health -------------------------------------------------------------------------------------------------------------


def test_health_reports_the_database_and_counts_suppressed_duplicates(ctx):
    client, _cfg, store = ctx
    body = client.get("/health").get_json()
    assert body["ok"] is True and body["db"] == "ok" and body["suppressed"] == 0
    post_ingest(client, payload())
    post_ingest(client, payload())
    body = client.get("/health").get_json()
    assert body["suppressed"] == 1 and body["events"] == 1 and body["deliveries"] == {"pending": 2}


def test_health_is_503_when_the_database_is_down(ctx, monkeypatch):
    client, _cfg, store = ctx
    assert client.get("/health").status_code == 200  # control
    monkeypatch.setattr(store, "ping", lambda: (_ for _ in ()).throw(RuntimeError("db down")))
    assert client.get("/health").status_code == 503


def test_unknown_paths_and_methods_answer_json(ctx):
    client, *_ = ctx
    assert client.get("/nope").status_code == 404 and client.get("/nope").get_json()["error"] == "not-found"
    assert client.get("/ingest").status_code == 405


# ---- /ingest ------------------------------------------------------------------------------------------------------------


def test_ingest_accepts_a_valid_delivery_and_stores_it_once(ctx):
    client, _cfg, store = ctx
    res = post_ingest(client, payload(), key="wh-1")
    assert res.status_code == 200 and res.get_json()["duplicate"] is False and res.get_json()["event_id"] == "wh-1"
    assert [e["event_id"] for e in store.events()] == ["wh-1"]
    assert sorted(d["sink"] for d in store.deliveries("wh-1")) == ["make", "slack"]


def test_ingest_stores_the_exact_raw_bytes_it_was_sent(ctx):
    client, _cfg, store = ctx
    raw = b'{ "type":"ContactCreate",   "locationId":"LOC1", "name":"Pat  Spaced",\n "id":"x" }'
    assert post_ingest(client, None, key="wh-raw", raw=raw).status_code == 200
    assert store.get_event("wh-raw")["payload"].encode() == raw


def test_ingest_duplicate_returns_200_and_writes_nothing_new(ctx):
    client, _cfg, store = ctx
    assert post_ingest(client, payload(), key="dup").get_json()["duplicate"] is False
    for _ in range(4):
        r = post_ingest(client, payload(), key="dup")
        assert r.status_code == 200 and r.get_json()["duplicate"] is True
    assert len(store.events()) == 1 and len(store.deliveries()) == 2


def test_ingest_requires_the_bearer_token(ctx):
    client, *_ = ctx
    assert post_ingest(client, payload()).status_code == 200  # positive control
    assert client.post("/ingest", data=json.dumps(payload()), headers={"Idempotency-Key": "k"}).status_code == 401
    assert post_ingest(client, payload(), token="wrong-token-0123456789").status_code == 401
    assert post_ingest(client, payload(), token=INGEST_TOKEN[:-1]).status_code == 401
    assert client.post("/ingest", data=json.dumps(payload()), headers={"Authorization": f"Basic {INGEST_TOKEN}", "Idempotency-Key": "k"}).status_code == 401


def test_ingest_accepts_the_previous_token_during_rotation_and_only_then(store, slack, make):
    base = make_cfg(slack.url, make.url)
    assert post_ingest(create_app(base, Deps(store=store)).test_client(), payload(), key="a", token=PREVIOUS_TOKEN).status_code == 401  # not configured yet
    base.ingest_token_previous = PREVIOUS_TOKEN
    client = create_app(base, Deps(store=store)).test_client()
    assert post_ingest(client, payload(), key="b", token=PREVIOUS_TOKEN).status_code == 200
    assert post_ingest(client, payload(), key="c", token=INGEST_TOKEN).status_code == 200
    assert post_ingest(client, payload(), key="d", token="third-token-0123456789").status_code == 401


def test_ingest_needs_an_idempotency_key(ctx):
    client, _cfg, store = ctx
    assert post_ingest(client, payload(), key="ok-key").status_code == 200
    assert post_ingest(client, payload(), headers={}).status_code == 400
    assert post_ingest(client, payload(), headers={"Idempotency-Key": ""}).status_code == 400
    assert post_ingest(client, payload(), headers={"Idempotency-Key": "k" * 129}).status_code == 400
    assert len(store.events()) == 1


def test_a_webhook_id_in_the_body_must_equal_the_idempotency_key(ctx):
    client, _cfg, store = ctx
    assert post_ingest(client, payload(webhookId="same"), key="same").status_code == 200  # positive control
    bad = post_ingest(client, payload(webhookId="body-id"), key="header-id")
    assert bad.status_code == 400 and bad.get_json()["error"] == "webhook-id-mismatch"
    assert post_ingest(client, payload(webhookId=5), key="five").status_code == 400
    assert [e["event_id"] for e in store.events()] == ["same"]


def test_ingest_rejects_oversized_bodies_with_413(ctx):
    client, cfg, store = ctx
    cfg.max_body_bytes = 2000
    small = payload()
    assert post_ingest(client, small, key="fits").status_code == 200
    big = payload(note="x" * 3000)
    assert post_ingest(client, big, key="big").status_code == 413
    assert [e["event_id"] for e in store.events()] == ["fits"]


def test_ingest_rejects_bodies_that_are_not_json_objects(ctx):
    client, *_ = ctx
    assert post_ingest(client, None, key="a", raw=b"not json").status_code == 400
    assert post_ingest(client, None, key="b", raw=b"[1,2,3]").status_code == 400
    assert post_ingest(client, None, key="c", raw=b'"text"').status_code == 400
    assert post_ingest(client, None, key="d", raw=b"\xff\xfe").status_code == 400


def test_an_unmapped_location_is_503_and_alerts_and_stores_nothing(ctx):
    client, _cfg, store = ctx
    assert post_ingest(client, payload(), key="mapped").status_code == 200  # control
    for i, p in enumerate((payload(locationId="NOWHERE"), {"type": "ContactCreate"}, payload(locationId=["LOC1"]))):
        r = post_ingest(client, p, key=f"unmapped-{i}")
        assert r.status_code == 503 and r.get_json()["error"] == "unrouted"
    assert [e["event_id"] for e in store.events()] == ["mapped"]
    with store.tx(write=False) as t:
        rows = t.execute("SELECT sink, reason FROM alerts").fetchall()
    assert len(rows) == 3 and {r["sink"] for r in rows} == {"routing"} and {r["reason"] for r in rows} == {"unmapped location"}


def test_a_tenant_with_no_enabled_sink_is_503_and_alerts(store, slack, make):
    cfg = make_cfg(slack.url, make.url, slack_extra={"enabled": False}, make_extra={"enabled": False})
    client = create_app(cfg, Deps(store=store)).test_client()
    r = post_ingest(client, payload(), key="nosinks")
    assert r.status_code == 503 and store.events() == []
    with store.tx(write=False) as t:
        assert t.execute("SELECT reason FROM alerts").fetchone()["reason"] == "tenant has no enabled sink"


def test_a_store_failure_before_the_commit_is_503_never_200(ctx, monkeypatch):
    client, _cfg, store = ctx
    assert post_ingest(client, payload(), key="fine").status_code == 200  # control
    real = store.ingest

    def broken(**kw):
        raise RuntimeError("connection lost")

    monkeypatch.setattr(store, "ingest", broken)
    r = post_ingest(client, payload(), key="lost")
    assert r.status_code == 503 and r.get_json()["error"] == "store"
    monkeypatch.setattr(store, "ingest", real)
    assert store.get_event("lost") is None
    assert post_ingest(client, payload(), key="lost").status_code == 200  # the sender's retry then succeeds


def test_a_crash_inside_the_ingest_transaction_is_503_and_leaves_no_rows(ctx):
    client, _cfg, store = ctx

    def boom():
        raise RuntimeError("died after the event insert")

    store.faults["ingest_after_event"] = boom
    r = post_ingest(client, payload(), key="half")
    store.faults.clear()
    assert r.status_code == 503 and store.events() == [] and store.deliveries() == []


# ---- /hooks/<tenant>/workflow ----------------------------------------------------------------------------------------------


def wf(**extra):
    return {"contact_id": "c-1", "first_name": "Ana", "email": "ana@example.com", "event_key": "sub-1", **extra}


def test_workflow_needs_the_per_tenant_secret_header(ctx):
    client, _cfg, store = ctx
    assert post_wf(client, wf()).status_code == 200  # positive control
    assert post_wf(client, wf(event_key="s2"), secret=None).status_code == 401
    assert post_wf(client, wf(event_key="s3"), secret="wrong-secret-0123456789").status_code == 401
    assert post_wf(client, wf(event_key="s4"), tenant="nobody").status_code == 401  # an unknown tenant looks the same
    assert len(store.events()) == 1


def test_workflow_event_key_is_hashed_with_the_tenant_and_dedupes(ctx):
    client, _cfg, store = ctx
    a = post_wf(client, wf(event_key="sub-9"))
    assert a.get_json()["event_id"] == hashlib.sha256(f"{TENANT}|sub-9".encode()).hexdigest()
    assert store.get_event(a.get_json()["event_id"])["dedupe_basis"] == "workflow-key"
    b = post_wf(client, wf(event_key="sub-9", first_name="Changed Later"))
    assert b.status_code == 200 and b.get_json()["duplicate"] is True and len(store.events()) == 1


def test_workflow_without_an_event_key_is_400_naming_event_key(ctx):
    client, _cfg, store = ctx
    body = {"contact_id": "c-1", "email": "a@b.test"}
    r = post_wf(client, body)
    assert r.status_code == 400 and "event_key" in r.get_json()["error"] and store.events() == []
    assert post_wf(client, {**body, "event_key": ""}).status_code == 400
    assert post_wf(client, {**body, "event_key": 12}).status_code == 400
    assert post_wf(client, {**body, "event_key": "k" * 300}).status_code == 400


def test_workflow_body_hash_mode_dedupes_identical_bodies_but_never_distinct_ones(store, slack, make):
    cfg = make_cfg(slack.url, make.url, tenant_extra={"dedupe_mode": "body_hash"})
    client = create_app(cfg, Deps(store=store)).test_client()
    one = {"contact_id": "c-1", "email": "a@b.test", "answer": "first"}
    two = {"contact_id": "c-1", "email": "a@b.test", "answer": "second"}  # same contact, a different submission
    assert post_wf(client, one).get_json()["duplicate"] is False
    assert post_wf(client, one).get_json()["duplicate"] is True
    assert post_wf(client, two).get_json()["duplicate"] is False
    events = store.events()
    assert len(events) == 2 and {e["dedupe_basis"] for e in events} == {"body-hash"}


def test_workflow_same_body_ten_minutes_later_is_suppressed_and_two_hours_later_is_new(ctx):
    client, _cfg, store = ctx
    first = post_wf(client, wf(event_key="repeat"))
    store.advance(600)
    assert post_wf(client, wf(event_key="repeat")).get_json()["duplicate"] is True
    store.advance(2 * 3600)
    later = post_wf(client, wf(event_key="repeat"))
    assert later.get_json()["duplicate"] is False and later.get_json()["event_id"] == first.get_json()["event_id"] + "|1"
    assert store.health_counts()["suppressed"] == 1


def test_workflow_is_503_never_200_when_anything_fails_before_the_commit(ctx, monkeypatch):
    client, _cfg, store = ctx
    assert post_wf(client, wf(event_key="control")).status_code == 200
    monkeypatch.setattr(store, "ingest", lambda **kw: (_ for _ in ()).throw(RuntimeError("gone")))
    r = post_wf(client, wf(event_key="fails"))
    assert r.status_code == 503
    monkeypatch.undo()
    assert store.get_event(hashlib.sha256(f"{TENANT}|fails".encode()).hexdigest()) is None


def test_workflow_respects_a_per_tenant_body_cap(store, slack, make):
    cfg = make_cfg(slack.url, make.url, tenant_extra={"max_body_bytes": 600})
    client = create_app(cfg, Deps(store=store)).test_client()
    assert post_wf(client, wf(event_key="small")).status_code == 200
    assert post_wf(client, wf(event_key="huge", note="x" * 2000)).status_code == 413


def test_workflow_rejects_non_json(ctx):
    client, *_ = ctx
    assert post_wf(client, None, raw=b"nope").status_code == 400
    assert post_wf(client, None, raw=b"[]").status_code == 400


# ---- admin ----------------------------------------------------------------------------------------------------------------------


def admin(client, path, body, token=ADMIN_TOKEN):
    return client.post(path, data=json.dumps(body), headers={**auth(token), "Content-Type": "application/json"})


def test_admin_uses_its_own_token_and_refuses_the_ingest_token(ctx):
    client, *_ = ctx
    assert admin(client, "/admin/replay", {"event_id": "nope"}).status_code == 404  # control: the admin token is accepted (event unknown)
    assert admin(client, "/admin/replay", {"event_id": "x"}, token=INGEST_TOKEN).status_code == 401
    assert client.post("/admin/replay", data="{}").status_code == 401
    assert admin(client, "/admin/backfill", {}, token=INGEST_TOKEN).status_code == 401


def test_admin_is_off_when_no_admin_token_is_configured(store, slack, make):
    cfg = make_cfg(slack.url, make.url)
    cfg.admin_token = None
    client = create_app(cfg, Deps(store=store)).test_client()
    assert client.post("/admin/replay", data="{}", headers=auth(ADMIN_TOKEN)).status_code == 404


def test_replay_resets_only_dead_rows_by_default_and_done_needs_sink_and_force(ctx):
    client, _cfg, store = ctx
    post_ingest(client, payload(), key="r1")
    a, b = store.claim("slack", 300), store.claim("make", 300)
    from relay.store.base import FinishPlan

    store.finish(a, FinishPlan(state="dead", attempts_inc=1, error="http 400"))
    store.finish(b, FinishPlan(state="done", attempts_inc=1, ok=True))
    assert admin(client, "/admin/replay", {"event_id": "r1"}).get_json()["reset"] == 1
    assert store.delivery("r1", "slack")["state"] == "pending" and store.delivery("r1", "make")["state"] == "done"
    assert admin(client, "/admin/replay", {"event_id": "r1", "sink": "make"}).get_json()["reset"] == 0
    assert admin(client, "/admin/replay", {"event_id": "r1", "force": True}).status_code == 400  # force needs a sink
    assert admin(client, "/admin/replay", {"event_id": "r1", "sink": "make", "force": True}).get_json()["reset"] == 1
    assert store.delivery("r1", "make")["state"] == "pending"


def test_replay_validates_its_body(ctx):
    client, *_ = ctx
    for body in ({}, {"event_id": 5}, {"event_id": "x", "sink": 5}, {"event_id": "x", "force": "yes"}):
        assert admin(client, "/admin/replay", body).status_code == 400, body
    assert admin(client, "/admin/replay", {"event_id": "unknown"}).status_code == 404


def test_backfill_inserts_the_missing_delivery_rows_for_a_newly_enabled_sink(store, slack, make):
    only_slack = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    old = create_app(only_slack, Deps(store=store)).test_client()
    post_ingest(old, payload(1), key="b1")
    post_ingest(old, payload(2), key="b2")
    assert [d["sink"] for d in store.deliveries()] == ["slack", "slack"]
    client = create_app(make_cfg(slack.url, make.url), Deps(store=store)).test_client()  # Make enabled now
    r = admin(client, "/admin/backfill", {"tenant": TENANT, "sink": "make", "since": "2000-01-01T00:00:00Z"})
    assert r.status_code == 200 and r.get_json()["inserted"] == 2
    assert sorted((d["event_id"], d["sink"]) for d in store.deliveries()) == [("b1", "make"), ("b1", "slack"), ("b2", "make"), ("b2", "slack")]
    assert admin(client, "/admin/backfill", {"tenant": TENANT, "sink": "make", "since": "2000-01-01T00:00:00Z"}).get_json()["inserted"] == 0


def test_backfill_validates_tenant_sink_and_time(ctx):
    client, *_ = ctx
    good = {"tenant": TENANT, "sink": "make", "since": "2026-01-01T00:00:00Z"}
    assert admin(client, "/admin/backfill", good).status_code == 200  # control
    for patch in ({"tenant": "nobody"}, {"sink": "carrier-pigeon"}, {"since": "yesterday"}, {"since": 5}, {"tenant": None}):
        assert admin(client, "/admin/backfill", {**good, **patch}).status_code == 400, patch


def test_backfill_for_a_disabled_sink_is_refused(store, slack, make):
    cfg = make_cfg(slack.url, make.url, make_extra={"enabled": False})
    client = create_app(cfg, Deps(store=store)).test_client()
    assert admin(client, "/admin/backfill", {"tenant": TENANT, "sink": "make", "since": "2026-01-01T00:00:00Z"}).status_code == 400


# ---- secrets are compared in constant time ---------------------------------------------------------------------------------------


def test_every_secret_is_compared_with_hmac_compare_digest(ctx, monkeypatch):
    client, *_ = ctx
    calls: list[int] = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: (calls.append(1), real(a, b))[1])
    post_ingest(client, payload(), key="cd1")
    n = len(calls)
    assert n >= 1
    post_wf(client, wf(event_key="cd2"))
    assert len(calls) > n
    n = len(calls)
    admin(client, "/admin/replay", {"event_id": "cd1"})
    assert len(calls) > n


def test_both_ingest_tokens_are_always_compared_so_timing_does_not_reveal_which_matched(store, slack, make, monkeypatch):
    cfg = make_cfg(slack.url, make.url)
    cfg.ingest_token_previous = PREVIOUS_TOKEN
    client = create_app(cfg, Deps(store=store)).test_client()
    calls: list[int] = []
    real = hmac.compare_digest
    monkeypatch.setattr(hmac, "compare_digest", lambda a, b: (calls.append(1), real(a, b))[1])
    post_ingest(client, payload(), key="t1", token=INGEST_TOKEN)  # matches the first, still compares the second
    assert len(calls) == 2


# ---- wiring ------------------------------------------------------------------------------------------------------------------------


def test_creating_the_app_installs_the_log_mask_and_registers_the_secrets(store, slack, make, caplog):
    redact.uninstall()
    logging.setLogRecordFactory(logging.LogRecord)  # a clean factory, as before any relay code ran
    cfg = make_cfg(slack.url, make.url)
    create_app(cfg, Deps(store=store))
    with caplog.at_level(logging.INFO):
        logging.getLogger("anything").warning("posting to %s with token %s", slack.url, INGEST_TOKEN)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert slack.url not in text and INGEST_TOKEN not in text and "***" in text


def test_the_inline_worker_delivers_without_a_separate_process(store, slack, make):
    env = env_for(slack.url, make.url, RELAY_INLINE_WORKER="1", RELAY_WORKER_INTERVAL_S="0.05")
    env["RELAY_CONFIG"] = config_json(make=False)
    cfg = load_config(env)
    assert cfg.inline_worker and cfg.worker_interval_s == 0.05
    app = create_app(cfg, Deps(store=store, http=Http()))
    worker = app.config["RELAY_INLINE_WORKER"]
    try:
        assert post_ingest(app.test_client(), payload(), key="inline-1").status_code == 200
        wait_for(lambda: slack.effects == 1, what="the inline worker to deliver the lead")
        wait_for(lambda: store.delivery("inline-1", "slack")["state"] == "done", what="the delivery to be committed")
    finally:
        worker.stop.set()


def test_a_web_only_app_does_not_start_a_worker(ctx):
    client, cfg, _store = ctx
    assert "RELAY_INLINE_WORKER" not in client.application.config
