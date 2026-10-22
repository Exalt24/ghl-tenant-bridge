"""POST /webhooks/ghl with real Ed25519 keys generated in the test.

The relay accepts a delivery whose X-GHL-Signature verifies over the exact bytes received OR over the compact
re-serialisation of the parsed body (HighLevel's own sample verifies JSON.stringify(req.body), and which one a live
delivery needs is not known). A request with only the legacy X-WH-Signature is refused. There is no timestamp window
unless a tenant enables one. Every refusal below runs after a positive control: the same request, properly signed.
"""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from relay import ghl
from relay.app import Deps, create_app

from .helpers import LOCATION, TENANT, ingest_one, make_cfg, make_rig, payload

NOW_S = 1_800_000_000.0


def keypair() -> tuple[Ed25519PrivateKey, str]:
    priv = Ed25519PrivateKey.generate()
    der = priv.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return priv, base64.b64encode(der).decode()


def sign(priv: Ed25519PrivateKey, message: bytes) -> str:
    return base64.b64encode(priv.sign(message)).decode()


def compact(raw: bytes) -> bytes:
    return json.dumps(json.loads(raw), separators=(",", ":"), ensure_ascii=False).encode()


@pytest.fixture
def world(store, slack, make):
    priv, pub = keypair()
    cfg = make_cfg(slack.url, make.url, env_extra={"RELAY_TEST_GHL_PUBLIC_KEY": pub})
    app = create_app(cfg, Deps(store=store, clock=lambda: NOW_S))
    return app.test_client(), cfg, store, priv, pub


def post(client, raw: bytes, sig: str | None, header: str = "X-GHL-Signature", extra: dict | None = None):
    headers = {"Content-Type": "application/json", **(extra or {})}
    if sig is not None:
        headers[header] = sig
    return client.post("/webhooks/ghl", data=raw, headers=headers)


def spaced(p: dict) -> bytes:
    """Bytes that are NOT the compact form: indented, so a re-serialisation differs from what was sent."""
    return json.dumps(p, indent=2).encode()


# ---- accepted: raw bytes and the re-serialised form --------------------------------------------------------------------------------


def test_a_delivery_signed_over_the_raw_bytes_is_accepted_and_stored_exactly_as_received(world):
    client, cfg, store, priv, _ = world
    raw = spaced(payload(webhookId="wh-raw-1"))
    assert compact(raw) != raw
    res = post(client, raw, sign(priv, raw))
    assert res.status_code == 200 and res.get_json() == {"ok": True, "duplicate": False, "event_id": "wh-raw-1"}
    assert store.get_event("wh-raw-1")["payload"].encode() == raw and store.get_event("wh-raw-1")["source"] == "ghl-direct"
    assert sorted(d["sink"] for d in store.deliveries("wh-raw-1")) == ["make", "slack"]


def test_a_delivery_signed_over_the_compact_reserialisation_is_accepted_too(world):
    client, _cfg, store, priv, _ = world
    raw = spaced(payload(webhookId="wh-compact-1"))
    res = post(client, raw, sign(priv, compact(raw)))
    assert res.status_code == 200 and store.get_event("wh-compact-1")["payload"].encode() == raw  # what is stored is what arrived


def test_each_verification_path_alone_is_enough_and_the_functions_say_which(world):
    _client, cfg, _store, priv, pub = world
    raw = spaced(payload())
    assert ghl.verify_signature(raw, sign(priv, raw), pub) == "raw"
    assert ghl.verify_signature(raw, sign(priv, compact(raw)), pub) == "reserialised"
    assert ghl.verify_signature(raw, sign(Ed25519PrivateKey.generate(), raw), pub) is None


# ---- refused ------------------------------------------------------------------------------------------------------------------------------


def test_a_forged_tampered_missing_or_malformed_signature_is_refused_and_nothing_is_stored(world):
    client, _cfg, store, priv, _ = world
    raw = spaced(payload(webhookId="wh-1"))
    assert post(client, raw, sign(priv, raw)).status_code == 200  # positive control: the same request, properly signed
    attacker = Ed25519PrivateKey.generate()
    other = spaced(payload(2, webhookId="wh-2"))
    cases = {
        "forged": post(client, other, sign(attacker, other)),
        "tampered body": post(client, other.replace(b"Lead2", b"Evil2"), sign(priv, other)),
        "signature of another body": post(client, other, sign(priv, raw)),
        "missing": post(client, other, None),
        "empty": post(client, other, ""),
        "N/A literal": post(client, other, "N/A"),
        "not base64": post(client, other, "###not base64###"),
        "truncated": post(client, other, sign(priv, other)[:-6]),
    }
    for name, res in cases.items():
        assert res.status_code == 401 and res.get_json()["error"] == "unauthorised", name
    assert [e["event_id"] for e in store.events()] == ["wh-1"]


def test_a_request_with_only_the_legacy_signature_header_is_refused_even_if_it_would_verify(world):
    client, _cfg, store, priv, _ = world
    raw = spaced(payload(webhookId="wh-legacy"))
    assert post(client, raw, sign(priv, raw)).status_code == 200  # control
    other = spaced(payload(3, webhookId="wh-legacy-2"))
    res = post(client, other, sign(priv, other), header="X-WH-Signature")
    assert res.status_code == 401 and res.get_json()["error"] == "legacy-signature-refused"
    both = post(client, other, sign(priv, other), extra={"X-WH-Signature": "legacy"})
    assert both.status_code == 200  # the current header is present and valid, the legacy one is ignored
    assert store.get_event("wh-legacy-2") is not None


def test_a_valid_signature_over_a_body_that_is_not_json_or_not_an_object_is_a_400_and_the_same_bytes_were_checked(world):
    client, _cfg, store, priv, _ = world
    for raw, want in ((b"not json at all", "not-json"), (b"\xff\xfe\x00bad utf8", "not-json"), (b"[1, 2]", "not-an-object"), (b'"text"', "not-an-object")):
        res = post(client, raw, sign(priv, raw))
        assert res.status_code == 400 and res.get_json()["error"] == want, raw
    assert store.events() == []


def test_the_test_key_is_ignored_outside_relay_env_test_so_production_cannot_be_weakened(store, slack, make):
    priv, pub = keypair()
    cfg = make_cfg(slack.url, make.url, env_extra={"RELAY_TEST_GHL_PUBLIC_KEY": pub})
    raw = spaced(payload(webhookId="wh-1"))
    ok = create_app(cfg, Deps(store=store)).test_client()
    assert post(ok, raw, sign(priv, raw)).status_code == 200  # control: honoured in test
    cfg.ghl_public_key_b64 = ghl.GHL_ED25519_SPKI_B64  # what a RELAY_ENV other than test resolves to (tested in test_config)
    prod_like = create_app(cfg, Deps(store=store)).test_client()
    other = spaced(payload(2, webhookId="wh-2"))
    assert post(prod_like, other, sign(priv, other)).status_code == 401


# ---- keys ---------------------------------------------------------------------------------------------------------------------------------


def test_a_native_webhook_id_dedupes_a_retry_even_when_the_body_differs(world):
    client, _cfg, store, priv, _ = world
    first = spaced(payload(webhookId="wh-same"))
    retry = spaced(payload(webhookId="wh-same", dateUpdated="2026-10-21T00:00:00.000Z"))
    assert post(client, first, sign(priv, first)).get_json()["duplicate"] is False
    res = post(client, retry, sign(priv, retry))
    assert res.status_code == 200 and res.get_json()["duplicate"] is True and res.get_json()["event_id"] == "wh-same"
    assert len(store.events()) == 1 and len(store.deliveries()) == 2 and store.health_counts()["suppressed"] == 1
    assert store.events()[0]["dedupe_basis"] == "native"


def test_without_a_webhook_id_the_key_is_synthesised_and_a_byte_identical_redelivery_dedupes(world):
    client, _cfg, store, priv, _ = world
    raw = spaced(payload())
    first = post(client, raw, sign(priv, raw)).get_json()
    again = post(client, raw, sign(priv, raw)).get_json()
    assert first["duplicate"] is False and again["duplicate"] is True and first["event_id"] == again["event_id"]
    want = ghl.sha256_hex(TENANT, "ContactCreate", "contact00001", "2026-10-20T12:41:03.193Z", ghl.sha256_hex(raw))
    assert first["event_id"] == want and store.events()[0]["dedupe_basis"] == "synthesised"


def test_the_synthesised_key_includes_the_raw_body_hash_so_two_different_events_never_collide(world):
    client, _cfg, store, priv, _ = world
    a = spaced(payload(email="a@example.com"))
    b = spaced(payload(email="b@example.com"))  # same id, same type, same stamps, different content
    ra, rb = post(client, a, sign(priv, a)).get_json(), post(client, b, sign(priv, b)).get_json()
    assert ra["event_id"] != rb["event_id"] and ra["duplicate"] is False and rb["duplicate"] is False
    assert len(store.events()) == 2


def test_an_appointment_event_is_keyed_from_its_nested_fields(world):
    client, _cfg, _store, priv, _ = world
    appt = {"type": "AppointmentCreate", "locationId": LOCATION, "appointment": {"id": "apt-1", "contactId": "c-1", "title": "Roof check", "dateAdded": "2026-10-20T10:00:00Z", "dateUpdated": "2026-10-20T10:30:00Z"}}
    raw = spaced(appt)
    res = post(client, raw, sign(priv, raw)).get_json()
    assert res["event_id"] == ghl.sha256_hex(TENANT, "AppointmentCreate", "apt-1", "2026-10-20T10:30:00Z", ghl.sha256_hex(raw))


def test_a_non_string_or_oversized_webhook_id_is_not_used_as_the_key(world):
    client, _cfg, store, priv, _ = world
    for i, wid in enumerate((12345, ["a"], "", "x" * 500)):
        raw = spaced(payload(i + 10, webhookId=wid))
        res = post(client, raw, sign(priv, raw)).get_json()
        assert res["event_id"] != wid and store.get_event(res["event_id"])["dedupe_basis"] == "synthesised"


# ---- the timestamp window ------------------------------------------------------------------------------------------------------------------


def iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_there_is_no_timestamp_window_by_default_because_a_retry_may_carry_an_old_or_odd_stamp(world):
    client, _cfg, store, priv, _ = world
    for i, ts in enumerate(("2019-01-01T00:00:00.000Z", iso(NOW_S + 86400), "garbage", None)):
        raw = spaced(payload(i + 20, webhookId=f"wh-ts-{i}", timestamp=ts))
        assert post(client, raw, sign(priv, raw)).status_code == 200, ts
    assert len(store.events()) == 4


def test_a_tenant_can_enable_the_window_and_then_old_future_and_missing_stamps_are_refused(store, slack, make):
    priv, pub = keypair()
    cfg = make_cfg(slack.url, make.url, env_extra={"RELAY_TEST_GHL_PUBLIC_KEY": pub}, tenant_extra={"ghl_timestamp_window": True})
    client = create_app(cfg, Deps(store=store, clock=lambda: NOW_S)).test_client()
    assert cfg.tenants[TENANT].ghl_timestamp_window is True
    ok = spaced(payload(webhookId="wh-in", timestamp=iso(NOW_S - 3600)))
    assert post(client, ok, sign(priv, ok)).status_code == 200  # control: inside the window
    edge_old = spaced(payload(2, webhookId="wh-edge-old", timestamp=iso(NOW_S - 30 * 86400 + 5)))
    edge_future = spaced(payload(3, webhookId="wh-edge-future", timestamp=iso(NOW_S + 299)))
    assert post(client, edge_old, sign(priv, edge_old)).status_code == 200 and post(client, edge_future, sign(priv, edge_future)).status_code == 200
    for i, ts in enumerate((iso(NOW_S - 31 * 86400), iso(NOW_S + 400), "garbage", None, 12345)):
        raw = spaced(payload(i + 30, webhookId=f"wh-out-{i}", timestamp=ts))
        res = post(client, raw, sign(priv, raw))
        assert res.status_code == 401 and res.get_json()["error"] == "timestamp-outside-window", ts
    assert sorted(e["event_id"] for e in store.events()) == ["wh-edge-future", "wh-edge-old", "wh-in"]


# ---- size, routing, durability ------------------------------------------------------------------------------------------------------------


def test_the_direct_route_has_its_own_size_cap_separate_from_the_other_routes(store, slack, make):
    priv, pub = keypair()
    cfg = make_cfg(slack.url, make.url, env_extra={"RELAY_TEST_GHL_PUBLIC_KEY": pub})
    cfg.ghl_webhook_max_body_bytes = 2000
    client = create_app(cfg, Deps(store=store)).test_client()
    small = spaced(payload(webhookId="wh-small"))
    assert len(small) < 2000 and post(client, small, sign(priv, small)).status_code == 200  # control
    big = spaced(payload(2, webhookId="wh-big", question="x" * 3000))
    res = post(client, big, sign(priv, big))
    assert res.status_code == 413 and res.get_json()["error"] == "body-too-large"
    assert store.get_event("wh-big") is None
    from .test_app import auth  # the ingest route keeps its own, larger cap

    ing = client.post("/ingest", data=json.dumps(payload(3, question="x" * 3000)), headers={**auth(), "Idempotency-Key": "k-ing", "Content-Type": "application/json"})
    assert ing.status_code == 200


def test_the_cap_is_configurable_in_the_json_config(store, slack, make):
    import json as _json

    from relay.config import load_config

    from .helpers import config_json, env_for

    env = env_for(slack.url, make.url)
    env["RELAY_CONFIG"] = _json.dumps({**_json.loads(config_json()), "ghl_webhook_max_body_bytes": 1234})
    assert load_config(env).ghl_webhook_max_body_bytes == 1234
    assert load_config(env_for(slack.url, make.url) | {"RELAY_CONFIG": config_json()}).ghl_webhook_max_body_bytes == 256 * 1024


def test_an_unmapped_location_is_a_503_with_an_alert_and_nothing_is_acknowledged(world):
    client, _cfg, store, priv, _ = world
    raw = spaced(payload(webhookId="wh-ok"))
    assert post(client, raw, sign(priv, raw)).status_code == 200  # control
    lost = spaced(payload(2, webhookId="wh-lost", locationId="UNKNOWN"))
    res = post(client, lost, sign(priv, lost))
    assert res.status_code == 503 and res.get_json()["error"] == "unrouted"
    assert store.get_event("wh-lost") is None and "routing" in store.alert_sinks_waiting()


def test_a_store_failure_before_the_commit_is_a_503_and_the_retry_lands_once(world):
    client, _cfg, store, priv, _ = world
    raw = spaced(payload(webhookId="wh-fail"))

    def die():
        raise RuntimeError("database went away")

    store.faults["ingest_mid_deliveries"] = die
    res = post(client, raw, sign(priv, raw))
    assert res.status_code == 503 and store.get_event("wh-fail") is None  # the event row rolled back with the deliveries
    store.faults.clear()
    again = post(client, raw, sign(priv, raw))  # HighLevel retries
    assert again.status_code == 200 and again.get_json()["duplicate"] is False and len(store.deliveries("wh-fail")) == 2


def test_a_signed_delivery_reaches_slack_and_make_once_end_to_end(store, slack, make):
    priv, pub = keypair()
    cfg = make_cfg(slack.url, make.url, env_extra={"RELAY_TEST_GHL_PUBLIC_KEY": pub})
    client = create_app(cfg, Deps(store=store)).test_client()
    raw = spaced(payload(webhookId="wh-e2e"))
    for _ in range(3):
        assert post(client, raw, sign(priv, raw)).status_code == 200  # HighLevel retries a delivery it thinks failed
    make_rig(store, cfg).settle()
    assert slack.effects == 1 and make.effects == 1
    assert {d["state"] for d in store.deliveries("wh-e2e")} == {"done"}


def test_the_shipped_signature_header_helper_reads_only_the_current_header():
    assert ghl.signature_header({"X-GHL-Signature": "abc"}) == "abc"
    assert ghl.signature_header({"X-WH-Signature": "abc"}) is None
    assert ghl.signature_header({}) is None
    assert hashlib.sha256(b"x").hexdigest() == ghl.sha256_hex(b"x")
