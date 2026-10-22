"""Event keys, lead normalisation, and the Ed25519 verify function (its route is built in tier 2)."""

from __future__ import annotations

import base64
import hashlib
import json
import random
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from relay import ghl


def sha(*parts: str) -> str:
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def raw(obj) -> bytes:
    return json.dumps(obj).encode()


CONTACT = {
    "type": "ContactCreate", "locationId": "LOC1", "id": "nmFmQEsNgz6AVpgLVUJ0", "name": "John Deo", "email": "JohnDeo@gmail.comm",
    "dateAdded": "2021-11-26T12:41:02.193Z", "dateUpdated": "2021-11-26T12:41:03.193Z", "phone": "+919509597501",
}
APPOINTMENT = {
    "type": "AppointmentCreate", "locationId": "LOC1",
    "appointment": {"id": "0007BWpSzSwfiuSl0tR2", "title": "Appointment with CRM Dev team", "contactId": "9NkT25Vor1v4aQatFsv2",
                    "appointmentStatus": "confirmed", "source": "booking_widget", "dateAdded": "2023-09-25T16:00:00+05:30",
                    "dateUpdated": "2023-09-25T16:30:00+05:30"},
}
OPPORTUNITY = {
    "type": "OpportunityCreate", "locationId": "LOC1", "id": "wWhVuzqpRuOA1ZVWi4FC", "contactId": "cJAWDskpkJHbRbhAT7bs",
    "monetaryValue": 40, "name": "Loram ipsu", "status": "open", "dateAdded": "2021-11-26T12:41:02.193Z",
}


# ---- keys -----------------------------------------------------------------------------------------------------------


def test_a_native_id_is_the_key_as_is():
    assert ghl.derive_key("acme", CONTACT, raw(CONTACT), native_id="wh-123") == ("wh-123", "native")


def test_a_workflow_event_key_is_hashed_with_the_tenant():
    assert ghl.derive_key("acme", CONTACT, raw(CONTACT), event_key="sub-42") == (sha("acme", "sub-42"), "workflow-key")
    assert ghl.derive_key("other", CONTACT, raw(CONTACT), event_key="sub-42")[0] != sha("acme", "sub-42")  # tenants never collide


def test_a_body_without_a_key_or_a_known_type_is_keyed_on_the_body_hash():
    body = {"hello": "world"}
    assert ghl.derive_key("acme", body, raw(body)) == (hashlib.sha256(b"acme|" + raw(body)).hexdigest(), "body-hash")
    other = {"hello": "mars"}
    assert ghl.derive_key("acme", other, raw(other))[0] != ghl.derive_key("acme", body, raw(body))[0]


def test_a_contact_event_is_keyed_on_type_id_stamp_and_the_body_hash():
    key, basis = ghl.derive_key("acme", CONTACT, raw(CONTACT))
    assert basis == "synthesised"
    assert key == sha("acme", "ContactCreate", "nmFmQEsNgz6AVpgLVUJ0", "2021-11-26T12:41:03.193Z", hashlib.sha256(raw(CONTACT)).hexdigest())


def test_two_different_events_for_one_contact_never_share_a_key():
    update = {**CONTACT, "type": "ContactUpdate", "dateUpdated": "2021-12-01T00:00:00.000Z", "email": "new@example.com"}
    same_stamp_other_body = {**CONTACT, "email": "changed@example.com"}
    keys = {ghl.derive_key("acme", p, raw(p))[0] for p in (CONTACT, update, same_stamp_other_body)}
    assert len(keys) == 3


def test_the_key_uses_the_updated_stamp_before_the_added_stamp():
    no_updated = {k: v for k, v in CONTACT.items() if k != "dateUpdated"}
    assert ghl.derive_key("acme", no_updated, raw(no_updated))[0] == sha("acme", "ContactCreate", CONTACT["id"], CONTACT["dateAdded"], hashlib.sha256(raw(no_updated)).hexdigest())


def test_an_appointment_is_keyed_from_its_nested_fields():
    key, basis = ghl.derive_key("acme", APPOINTMENT, raw(APPOINTMENT))
    assert basis == "synthesised"
    assert key == sha("acme", "AppointmentCreate", "0007BWpSzSwfiuSl0tR2", "2023-09-25T16:30:00+05:30", hashlib.sha256(raw(APPOINTMENT)).hexdigest())
    assert "id" not in APPOINTMENT  # the nested shape has no top level id, so a flat read would find nothing


def test_an_appointment_with_a_missing_nested_object_falls_back_to_the_body_hash():
    broken = {"type": "AppointmentCreate", "locationId": "LOC1", "appointment": "oops"}
    assert ghl.derive_key("acme", broken, raw(broken))[1] == "body-hash"


def test_an_opportunity_is_keyed_like_a_flat_event():
    key, basis = ghl.derive_key("acme", OPPORTUNITY, raw(OPPORTUNITY))
    assert basis == "synthesised"
    assert key == sha("acme", "OpportunityCreate", "wWhVuzqpRuOA1ZVWi4FC", "2021-11-26T12:41:02.193Z", hashlib.sha256(raw(OPPORTUNITY)).hexdigest())


def test_an_unknown_type_or_missing_identity_falls_back_to_the_body_hash():
    for p in ({"type": "InvoicePaid", "id": "i1", "dateAdded": "x"}, {"type": "ContactCreate"}, {"type": "ContactCreate", "id": "c1"}, {"type": 5, "id": "c"}):
        assert ghl.derive_key("acme", p, raw(p))[1] == "body-hash", p


def test_the_key_is_not_the_contact_id_alone():
    key, _ = ghl.derive_key("acme", CONTACT, raw(CONTACT))
    assert key != CONTACT["id"] and key != sha("acme", CONTACT["id"]) and "nmFm" not in key


# ---- normalise_lead -----------------------------------------------------------------------------------------------------


def test_a_contact_event_is_normalised():
    lead = ghl.normalise_lead({**CONTACT, "firstName": "John", "lastName": "Deo", "city": "Austin", "state": "TX", "source": "xyz form", "tags": ["a", "b"]})
    assert (lead["name"], lead["email"], lead["phone"], lead["city"], lead["state"], lead["source"]) == ("John Deo", "JohnDeo@gmail.comm", "+919509597501", "Austin", "TX", "xyz form")
    assert lead["tags"] == ["a", "b"] and lead["contact_id"] == CONTACT["id"] and lead["event_type"] == "ContactCreate"
    assert lead["attribution"]["present"] is False


def test_missing_attribution_is_recorded_as_missing_never_guessed():
    attr = ghl.normalise_lead(CONTACT)["attribution"]
    assert attr["present"] is False and all(v is None for k, v in attr.items() if k != "present")


def test_attribution_is_read_from_the_nested_object_and_flat_workflow_keys():
    nested = ghl.normalise_lead({"type": "ContactCreate", "attributionSource": {"url": "https://x.test/lp?utm_source=fb", "utmSource": "facebook", "utmMedium": "cpc", "campaign": "roof-hail", "fbclid": "AbC123xyz", "fbp": "fb.1.1.2", "userAgent": "UA/1", "ip": "203.0.113.9"}})
    a = nested["attribution"]
    assert (a["utm_source"], a["utm_medium"], a["utm_campaign"], a["fbclid"], a["fbp"], a["user_agent"], a["ip"], a["page_url"]) == ("facebook", "cpc", "roof-hail", "AbC123xyz", "fb.1.1.2", "UA/1", "203.0.113.9", "https://x.test/lp?utm_source=fb")
    assert a["present"] is True
    flat = ghl.normalise_lead({"type": "ContactCreate", "utm_source": "google", "utm_term": "roofer", "fbclid": "ZZ9", "event_id": "pix-1", "page_url": "https://x.test/", "user_agent": "UA/2"})
    f = flat["attribution"]
    assert (f["utm_source"], f["utm_term"], f["fbclid"], f["fb_event_id"], f["page_url"], f["user_agent"]) == ("google", "roofer", "ZZ9", "pix-1", "https://x.test/", "UA/2")


def test_the_nested_object_wins_over_a_flat_key_for_the_same_field():
    lead = ghl.normalise_lead({"attributionSource": {"utmSource": "nested"}, "utm_source": "flat"})
    assert lead["attribution"]["utm_source"] == "nested"


def test_an_appointment_event_is_normalised_from_its_nested_object():
    lead = ghl.normalise_lead(APPOINTMENT)
    assert lead["contact_id"] == "9NkT25Vor1v4aQatFsv2" and lead["name"] == "Appointment with CRM Dev team"
    assert lead["status"] == "confirmed" and lead["source"] == "booking_widget" and lead["date_added"] == "2023-09-25T16:00:00+05:30"


def test_an_opportunity_event_carries_its_value_and_status():
    lead = ghl.normalise_lead(OPPORTUNITY)
    assert lead["monetary_value"] == 40 and lead["status"] == "open" and lead["contact_id"] == "cJAWDskpkJHbRbhAT7bs"


def test_workflow_snake_case_fields_are_understood_and_extra_answers_are_kept():
    lead = ghl.normalise_lead({"first_name": "Ana", "last_name": "Cruz", "email": "a@b.test", "contact_id": "c9", "roof_age": "20 years", "event_key": "k1"})
    assert lead["name"] == "Ana Cruz" and lead["contact_id"] == "c9"
    assert lead["answers"] == {"roof_age": "20 years"}  # known keys are not repeated as answers, event_key is plumbing


@pytest.mark.parametrize("bad", [None, [], "text", 7, [1, 2]])
def test_a_payload_that_is_not_an_object_becomes_an_empty_lead(bad):
    lead = ghl.normalise_lead(bad)
    assert lead["name"] is None and lead["tags"] == [] and lead["attribution"]["present"] is False


def test_odd_field_types_never_raise():
    nasty = {"name": ["x"], "email": {"a": 1}, "tags": "not a list", "phone": True, "monetaryValue": "40", "city": None, "answers": 1,
             "attributionSource": "string", "appointment": 5, "q": {"nested": 1}, "n": 7}
    lead = ghl.normalise_lead(nasty)
    assert lead["name"] is None and lead["email"] is None and lead["tags"] == [] and lead["phone"] is None
    assert lead["monetary_value"] is None and lead["answers"] == {"answers": "1", "n": "7"}


def test_a_seeded_fuzz_of_nested_garbage_never_raises():
    rng = random.Random(7)

    def junk(depth=0):
        kinds = ["s", "n", "none", "list", "dict", "bool"]
        k = rng.choice(kinds if depth < 3 else ["s", "n", "none", "bool"])
        return {"s": lambda: "x" * rng.randint(0, 50), "n": lambda: rng.random() * 1e6, "none": lambda: None, "bool": lambda: rng.random() < 0.5,
                "list": lambda: [junk(depth + 1) for _ in range(rng.randint(0, 4))],
                "dict": lambda: {rng.choice(["name", "email", "tags", "appointment", "attributionSource", "type", "id", "q" * 70, "k"]): junk(depth + 1) for _ in range(rng.randint(0, 6))}}[k]()

    for _ in range(300):
        lead = ghl.normalise_lead(junk())
        assert isinstance(lead["answers"], dict) and isinstance(lead["tags"], list)
        json.dumps(lead)  # the lead is always serialisable, the Make sink depends on it


def test_answers_are_capped():
    lead = ghl.normalise_lead({f"q{i}": "v" for i in range(200)})
    assert len(lead["answers"]) == 40


# ---- Ed25519 -------------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def keypair():
    priv = Ed25519PrivateKey.generate()
    spki = priv.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return priv, base64.b64encode(spki).decode()


def sign(priv, data: bytes) -> str:
    return base64.b64encode(priv.sign(data)).decode()


def test_the_published_ghl_key_is_a_valid_ed25519_key():
    ghl._load_key(ghl.GHL_ED25519_SPKI_B64)


def test_a_signature_over_the_raw_bytes_verifies_as_raw(keypair):
    priv, pub = keypair
    body = b'{\n  "type": "ContactCreate",\n  "id": "c1"\n}'  # pretty printed: raw and compact forms differ
    assert ghl.verify_signature(body, sign(priv, body), pub) == "raw"


def test_a_signature_over_the_compact_reserialisation_verifies_as_reserialised(keypair):
    priv, pub = keypair
    body = b'{\n  "type": "ContactCreate",\n  "id": "c1"\n}'
    compact = json.dumps(json.loads(body), separators=(",", ":"), ensure_ascii=False).encode()
    assert compact != body
    assert ghl.verify_signature(body, sign(priv, compact), pub) == "reserialised"


def test_each_path_alone_is_accepted_and_neither_accepts_the_other_ones_signature(keypair):
    priv, pub = keypair
    body = b'{"a": 1,  "b": "x"}'
    compact = b'{"a":1,"b":"x"}'
    assert ghl.verify_signature(body, sign(priv, body), pub) == "raw"
    assert ghl.verify_signature(body, sign(priv, compact), pub) == "reserialised"
    assert ghl.verify_signature(compact, sign(priv, compact), pub) == "raw"


def test_a_forged_or_wrong_key_signature_is_refused(keypair):
    priv, pub = keypair
    body = b'{"a":1}'
    attacker = Ed25519PrivateKey.generate()
    assert ghl.verify_signature(body, sign(attacker, body), pub) is None
    assert ghl.verify_signature(body, sign(priv, body), pub) == "raw"  # positive control


def test_a_tampered_body_is_refused(keypair):
    priv, pub = keypair
    sig = sign(priv, b'{"amount":10}')
    assert ghl.verify_signature(b'{"amount":10}', sig, pub) == "raw"
    assert ghl.verify_signature(b'{"amount":11}', sig, pub) is None


def test_missing_na_and_garbage_signatures_are_refused(keypair):
    _priv, pub = keypair
    for sig in (None, "", "N/A", "!!!notbase64!!!", "AAAA", base64.b64encode(b"x" * 64).decode()):
        assert ghl.verify_signature(b'{"a":1}', sig, pub) is None, sig


def test_a_non_json_body_with_a_valid_raw_signature_still_verifies_raw(keypair):
    priv, pub = keypair
    body = b"not json at all"
    assert ghl.verify_signature(body, sign(priv, body), pub) == "raw"
    assert ghl.verify_signature(body, sign(Ed25519PrivateKey.generate(), body), pub) is None


def test_the_legacy_header_alone_yields_no_signature():
    assert ghl.signature_header({"X-WH-Signature": "abc"}) is None
    assert ghl.signature_header({"X-GHL-Signature": "abc"}) == "abc"
    assert ghl.signature_header({"X-WH-Signature": "legacy", "X-GHL-Signature": "current"}) == "current"


def test_the_timestamp_window_is_30_days_back_and_5_minutes_ahead():
    now = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc)
    now_ms = now.timestamp() * 1000

    def iso(delta):
        return (now + delta).isoformat().replace("+00:00", "Z")

    assert ghl.timestamp_ok(iso(timedelta(0)), now_ms)
    assert ghl.timestamp_ok(iso(-timedelta(days=29, hours=23)), now_ms)
    assert not ghl.timestamp_ok(iso(-timedelta(days=30, minutes=1)), now_ms)
    assert ghl.timestamp_ok(iso(timedelta(minutes=4)), now_ms)
    assert not ghl.timestamp_ok(iso(timedelta(minutes=6)), now_ms)
    for bad in (None, 5, "yesterday", ""):
        assert not ghl.timestamp_ok(bad, now_ms)
