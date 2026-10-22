"""POST /webhooks/fathom/<tenant> through Flask's test client against a real store (SQLite and Postgres), with signatures built by
code that does not import the relay, and the Slack fan-out through a real worker against the Slack fake."""

from __future__ import annotations

import base64
import json
import logging
import random

import pytest

from relay.app import Deps, create_app
from relay.config import load_config

from .fakes.fathom import WEBHOOK_SECRET, make_meeting, sign_webhook, webhook_headers
from .helpers import LOCATION, TENANT, config_json, env_for, make_rig

NOW = 1_760_000_000
OTHER_SECRET = "whsec_" + base64.b64encode(b"beta-tenant-secret-of-32-bytes!!").decode()
SECOND = "beta"


def build_cfg(slack_url, make_url, *, fathom_extra=None, acme_fathom=True):
    env = env_for(slack_url, make_url, ACME_FATHOM_SECRET=WEBHOOK_SECRET, BETA_FATHOM_SECRET=OTHER_SECRET)
    base = json.loads(config_json())
    if acme_fathom:
        base["tenants"][TENANT]["fathom"] = {"secret_env": "ACME_FATHOM_SECRET", "slack": True, **(fathom_extra or {})}
    base["tenants"][SECOND] = {"fathom": {"secret_env": "BETA_FATHOM_SECRET"}}
    env["RELAY_CONFIG"] = json.dumps(base)
    return load_config(env)


@pytest.fixture
def ctx(store, slack, make):
    cfg = build_cfg(slack.url, make.url)
    app = create_app(cfg, Deps(store=store, clock=lambda: float(NOW)))
    return app.test_client(), cfg, store


def body_of(i: int = 1, **over) -> bytes:
    return json.dumps({**make_meeting(i), **over}).encode()


def post(client, body, *, tenant=TENANT, mid="msg_1", ts=NOW, secret=WEBHOOK_SECRET, headers=None):
    h = headers if headers is not None else webhook_headers(secret, mid, ts, body)
    return client.post(f"/webhooks/fathom/{tenant}", data=body, headers=h)


def test_a_signed_delivery_is_stored_with_its_meeting_in_one_commit_and_acknowledged(ctx):
    client, _cfg, store = ctx
    res = post(client, body_of(1))
    assert res.status_code == 200 and res.get_json() == {"ok": True, "duplicate": False, "event_id": "msg_1"}
    events = store.events()
    assert [(e["event_id"], e["tenant"], e["source"], e["dedupe_basis"]) for e in events] == [("msg_1", TENANT, "fathom", "native")]
    [m] = store.meetings()
    assert (m["recording_id"], m["tenant"], m["title"]) == ("5001", TENANT, "Weekly sync 1") and m["summary"].startswith("## Summary")
    assert json.loads(m["action_items"])[0]["description"] == "Send recap 1"


def test_only_the_slack_sink_gets_a_delivery_never_make_or_the_others(ctx):
    client, _cfg, store = ctx
    post(client, body_of(1))
    assert [d["sink"] for d in store.deliveries("msg_1")] == ["slack"]  # the tenant has make enabled too


def test_the_stored_event_is_the_reduced_record_not_the_delivery(ctx):
    client, _cfg, store = ctx
    full = body_of(1, transcript=[{"speaker": {"display_name": "J"}, "text": "launch code hunter2", "timestamp": "00:00:01"}], calendar_invitees=[{"email": "private.person@example.test"}])
    assert post(client, full).status_code == 200
    stored = json.loads(store.get_event("msg_1")["payload"])
    assert set(stored) == {"recording_id", "title", "summary", "action_items", "url", "created_at"}
    text = "\n".join(store.dump_all_text())
    assert "hunter2" not in text and "private.person" not in text


def test_a_retry_with_the_same_webhook_id_is_a_suppressed_duplicate_and_stores_nothing_new(ctx):
    client, _cfg, store = ctx
    assert post(client, body_of(1)).get_json()["duplicate"] is False  # control
    for _ in range(3):
        res = post(client, body_of(1))
        assert res.status_code == 200 and res.get_json()["duplicate"] is True and res.get_json()["event_id"] == "msg_1"
    assert len(store.events()) == 1 and len(store.meetings()) == 1 and len(store.deliveries()) == 1
    assert client.get("/health").get_json()["suppressed"] == 3


def test_a_new_webhook_id_for_the_same_recording_refreshes_the_meeting_and_is_a_new_event(ctx):
    client, _cfg, store = ctx
    post(client, body_of(1), mid="msg_1")
    res = post(client, body_of(1, title="Renamed"), mid="msg_2")
    assert res.get_json()["duplicate"] is False
    assert len(store.events()) == 2 and [m["title"] for m in store.meetings()] == ["Renamed"]


def test_a_forged_signature_is_refused_and_stores_nothing(ctx):
    client, _cfg, store = ctx
    assert post(client, body_of(1), mid="msg_ok").status_code == 200  # control: the same request, correctly signed
    store_before = (len(store.events()), len(store.meetings()))
    forged = post(client, body_of(2), mid="msg_bad", secret=OTHER_SECRET)
    assert forged.status_code == 401 and forged.get_json() == {"ok": False, "error": "bad-signature"}
    tampered = body_of(3)
    h = webhook_headers(WEBHOOK_SECRET, "msg_t", NOW, tampered)
    assert post(client, tampered + b" ", mid="msg_t", headers=h).status_code == 401
    assert (len(store.events()), len(store.meetings())) == store_before


def test_a_signature_over_a_reserialised_body_is_refused(ctx):
    client, _cfg, store = ctx
    spaced = b'{ "recording_id" : 7, "title" : "t" }'
    assert post(client, spaced, mid="msg_a").status_code == 200  # control: signed over the bytes sent
    h = webhook_headers(WEBHOOK_SECRET, "msg_b", NOW, json.dumps(json.loads(spaced)).encode())
    assert post(client, spaced, mid="msg_b", headers=h).status_code == 401
    assert [e["event_id"] for e in store.events()] == ["msg_a"]


def test_a_stale_or_future_timestamp_is_refused(ctx):
    client, _cfg, store = ctx
    assert post(client, body_of(1), ts=NOW - 300).status_code == 200  # control: on the edge of the window
    for i, ts in enumerate((NOW - 301, NOW + 301, 0)):
        res = post(client, body_of(2), mid=f"msg_old{i}", ts=ts)
        assert res.status_code == 401 and res.get_json()["error"] == "timestamp-outside-window"
    assert len(store.events()) == 1


def test_missing_headers_are_refused(ctx):
    client, _cfg, store = ctx
    body = body_of(1)
    assert post(client, body).status_code == 200  # control
    for drop in ("webhook-id", "webhook-timestamp", "webhook-signature"):
        h = webhook_headers(WEBHOOK_SECRET, "msg_x", NOW, body)
        del h[drop]
        res = post(client, body, headers=h)
        assert res.status_code == 401 and res.get_json()["error"] == "missing-header", drop
    assert len(store.events()) == 1


def test_an_unknown_tenant_and_a_tenant_without_fathom_answer_like_a_bad_signature(store, slack, make):
    cfg = build_cfg(slack.url, make.url, acme_fathom=False)
    client = create_app(cfg, Deps(store=store, clock=lambda: float(NOW))).test_client()
    body = body_of(1)
    assert post(client, body, tenant=SECOND, secret=OTHER_SECRET).status_code == 200  # control: beta has Fathom configured
    for tenant in ("nobody", TENANT):
        res = post(client, body, tenant=tenant, mid=f"msg_{tenant}")
        assert res.status_code == 401 and res.get_json() == {"ok": False, "error": "unauthorised"}
    assert len(store.events()) == 1


def test_a_delivery_signed_for_one_tenant_is_refused_by_another(ctx):
    client, _cfg, store = ctx
    body = body_of(1)
    assert post(client, body, tenant=SECOND, secret=OTHER_SECRET, mid="msg_b").status_code == 200  # control
    assert post(client, body, tenant=SECOND, secret=WEBHOOK_SECRET, mid="msg_c").status_code == 401
    assert post(client, body, tenant=TENANT, secret=OTHER_SECRET, mid="msg_d").status_code == 401
    assert [(e["event_id"], e["tenant"]) for e in store.events()] == [("msg_b", SECOND)]


def test_a_signed_body_that_is_not_json_or_has_no_recording_id_is_a_400(ctx):
    client, _cfg, store = ctx
    assert post(client, body_of(1), mid="msg_ok").status_code == 200  # control
    assert post(client, b"not json at all", mid="msg_1").get_json()["error"] == "not-json"
    assert post(client, b"\xff\xfe", mid="msg_2").get_json()["error"] == "not-json"
    assert post(client, b'{"title": "no id"}', mid="msg_3").get_json()["error"] == "recording_id-required"
    assert post(client, b"[1]", mid="msg_4").get_json()["error"] == "recording_id-required"
    assert len(store.events()) == 1


def test_the_default_size_cap_is_two_megabytes_and_a_larger_body_is_a_413(ctx):
    client, _cfg, store = ctx
    under = body_of(1, pad="p" * (1_900_000))
    assert len(under) < 2 * 1024 * 1024 and post(client, under).status_code == 200  # a transcript sized body is accepted
    over = body_of(2, pad="p" * (2 * 1024 * 1024))
    assert post(client, over, mid="msg_big").status_code == 413
    assert len(store.events()) == 1


def test_the_size_cap_is_per_tenant(store, slack, make):
    cfg = build_cfg(slack.url, make.url, fathom_extra={"max_body_bytes": 2000})
    client = create_app(cfg, Deps(store=store, clock=lambda: float(NOW))).test_client()
    assert post(client, body_of(1)).status_code == 200  # a normal body fits under 2000 bytes (control)
    assert post(client, body_of(2, pad="p" * 5000), mid="msg_2").status_code == 413
    assert post(client, body_of(3, pad="p" * 5000), tenant=SECOND, secret=OTHER_SECRET, mid="msg_3").status_code == 200  # the other tenant keeps 2 MB


def test_a_failure_before_the_commit_is_a_503_and_leaves_nothing_behind(ctx, monkeypatch):
    client, _cfg, store = ctx
    real = store.save_meeting
    calls = {"n": 0}

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            real(*a, **kw)
            raise RuntimeError("the database went away after the meeting insert")
        return real(*a, **kw)

    monkeypatch.setattr(store, "save_meeting", flaky)
    res = post(client, body_of(1))
    assert res.status_code == 503 and res.get_json()["error"] == "store"
    assert store.events() == [] and store.meetings() == [] and store.deliveries() == []  # the event and the meeting rolled back together
    retry = post(client, body_of(1))  # Fathom retries a non-2xx: the same webhook-id now lands
    assert retry.status_code == 200 and retry.get_json()["duplicate"] is False
    assert len(store.events()) == 1 and len(store.meetings()) == 1


def test_a_database_that_is_down_is_a_503_never_a_200(ctx, monkeypatch):
    client, _cfg, _store = ctx
    monkeypatch.setattr(_store, "ingest", lambda **kw: (_ for _ in ()).throw(RuntimeError("down")))
    assert post(client, body_of(1)).status_code == 503


def test_the_get_method_and_other_paths_do_not_reach_the_receiver(ctx):
    client, *_ = ctx
    assert client.get("/webhooks/fathom/acme").status_code == 405
    assert client.post("/webhooks/fathom/").status_code == 404


# ---- Slack fan-out -------------------------------------------------------------------------------------------------------------


def test_the_meeting_reaches_slack_through_the_worker_once_even_when_fathom_retries(ctx, slack):
    client, cfg, store = ctx
    meeting = body_of(1, title="Sync <script>alert(1)</script> & co")
    post(client, meeting, mid="msg_abcdef123")
    rig = make_rig(store, cfg)
    rig.settle()
    assert slack.effects == 1
    sent = slack.requests[0]["json"]
    texts = json.dumps(sent)
    assert sent["text"] == "Meeting: Sync &lt;script&gt;alert(1)&lt;/script&gt; &amp; co"  # the top level text is parsed as mrkdwn, so it is escaped
    mrkdwn = [b["text"]["text"] for b in sent["blocks"] if b["type"] == "section"] + [e["text"] for b in sent["blocks"] if b["type"] == "context" for e in b["elements"]]
    assert mrkdwn and all("<script>" not in t and t.count("<") == 0 for t in mrkdwn)
    assert sent["blocks"][0]["text"]["type"] == "plain_text"  # the header block is plain text, which Slack never parses, as in the lead message
    assert "## Summary" in texts and "Send recap 1" in texts and "msg_abcd" in texts  # the footer carries 8 characters of the event id
    post(client, meeting, mid="msg_abcdef123")  # Fathom retries after the message was already delivered
    rig.settle()
    assert slack.effects == 1 and store.delivery("msg_abcdef123", "slack")["state"] == "done"


def test_without_slack_fan_out_no_delivery_is_made(store, slack, make):
    cfg = build_cfg(slack.url, make.url, fathom_extra={"slack": False})
    client = create_app(cfg, Deps(store=store, clock=lambda: float(NOW))).test_client()
    assert post(client, body_of(1)).status_code == 200
    assert store.deliveries() == [] and len(store.meetings()) == 1
    make_rig(store, cfg).settle()
    assert slack.effects == 0


def test_a_meeting_with_nothing_but_a_title_still_posts_a_readable_message(ctx, slack):
    client, cfg, store = ctx
    post(client, json.dumps({"recording_id": 99, "title": "Quick call"}).encode(), mid="msg_q")
    make_rig(store, cfg).settle()
    assert slack.effects == 1 and "no summary or action items" in json.dumps(slack.requests[0]["json"])


# ---- secrets ---------------------------------------------------------------------------------------------------------------------


def test_the_signing_secret_reaches_neither_a_log_line_nor_a_database_column(ctx, caplog):
    client, cfg, store = ctx
    key_part = WEBHOOK_SECRET.removeprefix("whsec_")
    with caplog.at_level(logging.DEBUG):
        post(client, body_of(1))
        post(client, body_of(2), mid="msg_f", secret=OTHER_SECRET)
        make_rig(store, cfg).settle()
    text = "\n".join(f"{r.name} {r.getMessage()} {r.exc_text or ''}" for r in caplog.records if not r.name.startswith("werkzeug"))
    text += "\n".join(store.dump_all_text())
    assert WEBHOOK_SECRET not in text and key_part not in text and OTHER_SECRET not in text
    assert "msg_1" in text  # control: the sweep did read the data that was written


# ---- a seeded property test over random worlds ----------------------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(12))
def test_property_only_valid_first_deliveries_are_stored_and_every_retry_is_a_suppressed_duplicate(store, slack, make, seed):
    rng = random.Random(seed)
    cfg = build_cfg(slack.url, make.url)
    client = create_app(cfg, Deps(store=store, clock=lambda: float(NOW))).test_client()
    secrets = {TENANT: WEBHOOK_SECRET, SECOND: OTHER_SECRET}
    accepted: dict[str, tuple[str, str]] = {}  # webhook-id -> (tenant, recording id)
    duplicates = 0
    for step in range(rng.randint(15, 40)):
        tenant = rng.choice([TENANT, SECOND])
        action = rng.choice(["new", "new", "retry", "retry", "forged", "stale", "cross", "bad_json", "no_id"])
        mid = f"msg_{seed}_{step}"
        rec = rng.randint(1, 8)
        body = body_of(rec, title=f"meeting {rec}")
        if action == "retry" and accepted:
            old = rng.choice(sorted(accepted))
            tenant, rec_old = accepted[old]
            res = post(client, body_of(int(rec_old) - 5000), tenant=tenant, mid=old, secret=secrets[tenant])
            assert res.status_code == 200 and res.get_json()["duplicate"] is True
            duplicates += 1
            continue
        if action == "forged":
            assert post(client, body, tenant=tenant, mid=mid, secret=secrets[SECOND if tenant == TENANT else TENANT] if rng.random() < 0.5 else "whsec_" + base64.b64encode(b"x" * 24).decode()).status_code == 401
        elif action == "stale":
            assert post(client, body, tenant=tenant, mid=mid, ts=NOW - rng.choice([301, 4000])).status_code == 401
        elif action == "cross":
            other = SECOND if tenant == TENANT else TENANT
            assert post(client, body, tenant=other, mid=mid, secret=secrets[tenant]).status_code == 401
        elif action == "bad_json":
            assert post(client, b"{nope", tenant=tenant, mid=mid, secret=secrets[tenant]).status_code == 400
        elif action == "no_id":
            assert post(client, b'{"title": "x"}', tenant=tenant, mid=mid, secret=secrets[tenant]).status_code == 400
        else:
            res = post(client, body, tenant=tenant, mid=mid, secret=secrets[tenant])
            assert res.status_code == 200 and res.get_json()["duplicate"] is False
            accepted[mid] = (tenant, str(5000 + rec))
    assert sorted(e["event_id"] for e in store.events()) == sorted(accepted)
    assert client.get("/health").get_json()["suppressed"] == duplicates
    stored = {m["recording_id"]: m["tenant"] for m in store.meetings()}
    last_tenant_for_recording: dict[str, str] = {}
    for mid in sorted(accepted, key=lambda k: int(k.rsplit("_", 1)[1])):
        tenant, rid = accepted[mid]
        if rid not in last_tenant_for_recording:
            last_tenant_for_recording[rid] = tenant  # a recording belongs to the first tenant that stored it
    assert stored == last_tenant_for_recording
    assert all(d["sink"] == "slack" for d in store.deliveries())
    assert {d["event_id"] for d in store.deliveries()} == {m for m, (t, _r) in accepted.items() if t == TENANT}


def test_a_fathom_event_purged_after_30_days_still_dedupes_a_late_retry_and_keeps_its_meeting(ctx):
    client, _cfg, store = ctx
    assert post(client, body_of(1), mid="msg_old").get_json()["duplicate"] is False
    store.advance(31 * 86400)
    assert store.purge(30) == 1 and store.get_event("msg_old")["payload"] is None
    late = post(client, body_of(1), mid="msg_old")  # the signature is still inside the injected clock's window
    assert late.status_code == 200 and late.get_json()["duplicate"] is True
    assert len(store.meetings()) == 1 and len(store.events()) == 1


def test_the_tolerance_is_per_tenant(store, slack, make):
    cfg = build_cfg(slack.url, make.url, fathom_extra={"tolerance_s": 60})
    client = create_app(cfg, Deps(store=store, clock=lambda: float(NOW))).test_client()
    assert post(client, body_of(1), ts=NOW - 50).status_code == 200  # inside 60 seconds (control)
    assert post(client, body_of(2), mid="msg_2", ts=NOW - 100).status_code == 401  # outside this tenant's 60, inside the default 300
    assert post(client, body_of(3), mid="msg_3", ts=NOW - 100, tenant=SECOND, secret=OTHER_SECRET).status_code == 200


def test_the_slack_message_escapes_the_summary_caps_the_header_and_counts_the_action_items_it_does_not_show(ctx, slack):
    client, cfg, store = ctx
    items = [{"description": f"item <{i}> & more", "completed": i % 2 == 0, "assignee": {"name": "Jane <J>"}} for i in range(30)]
    body = body_of(1, title="T" * 400, default_summary={"template_name": "general", "markdown_formatted": "## Notes <b>x</b> & y"}, action_items=items)
    assert post(client, body, mid="msg_long").status_code == 200
    make_rig(store, cfg).settle()
    assert slack.effects == 1  # the strict fake refuses a header over 150 characters, an unescaped < and a section over 3000
    sent = slack.requests[0]["json"]
    header = sent["blocks"][0]["text"]["text"]
    assert len(header) <= 150 and header.endswith("\u2026")
    section = sent["blocks"][1]["text"]["text"]
    assert "## Notes &lt;b&gt;x&lt;/b&gt; &amp; y" in section and "_and 20 more_" in section
    assert "[x] item &lt;0&gt; &amp; more (Jane &lt;J&gt;)" in section and "item &lt;10&gt;" not in section  # ten are shown
