"""The contracts folders: every vendor has a SOURCE.md, every fixture is named in it and carries a provenance tag, and the
HighLevel fixtures behave as the code expects. Nothing is `observed`, because nothing here ran against a real vendor."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from relay import ghl
from relay.sinks import registry

CONTRACTS = Path(__file__).resolve().parents[1] / "contracts"
VENDORS = ["slack", "make", "meta", "google", "ghl", "windsor", "fathom"]
TAGS = (".doc.", ".assumed.", ".rfc7523.")


def raw(obj) -> bytes:
    return json.dumps(obj).encode()


def test_every_vendor_folder_has_a_source_file_and_nothing_else_is_unlisted():
    assert sorted(p.name for p in CONTRACTS.iterdir() if p.is_dir()) == sorted(VENDORS)
    for vendor in VENDORS:
        source = (CONTRACTS / vendor / "SOURCE.md").read_text(encoding="utf-8")
        assert "Retrieved 2026-10-22" in source and "no fixture is `observed`" in source, vendor
        fixtures = sorted(p.name for p in (CONTRACTS / vendor).glob("*.json"))
        assert fixtures, vendor
        for name in fixtures:
            assert f"`{name}`" in source, f"{vendor}/{name} is not named in SOURCE.md"
            assert any(tag in name for tag in TAGS), f"{vendor}/{name} carries no provenance tag in its name"
            assert ".observed." not in name


def test_every_fixture_is_valid_json_and_the_source_tables_use_only_the_three_provenance_words():
    for path in CONTRACTS.rglob("*.json"):
        json.loads(path.read_text(encoding="utf-8"))
    for vendor in VENDORS:
        text = (CONTRACTS / vendor / "SOURCE.md").read_text(encoding="utf-8")
        rows = [l for l in text.splitlines() if l.startswith("| `")]
        assert rows, vendor
        for row in rows:
            provenance = row.split("|")[2].strip()
            assert re.match(r"^(doc|ASSUMED)\b", provenance), (vendor, row[:60])


def test_the_assumed_fixtures_say_what_would_settle_them():
    for name in ("meta/response.assumed.json", "google/statuses.assumed.json"):
        doc = json.loads((CONTRACTS / name).read_text(encoding="utf-8"))
        assert doc["provenance"].startswith("ASSUMED") and ("note" in doc or "what_would_settle_it" in doc)


def test_every_registered_sink_has_a_contracts_folder():
    assert set(registry.names()) <= {"slack", "make", "capi", "sheets"}
    folders = {"slack": "slack", "make": "make", "capi": "meta", "sheets": "google"}
    for sink in registry.names():
        assert (CONTRACTS / folders[sink]).is_dir(), sink


# ---- HighLevel fixtures ------------------------------------------------------------------------------------------------------------


def fixture(name: str):
    return json.loads((CONTRACTS / "ghl" / name).read_text(encoding="utf-8"))


def test_the_contact_example_normalises_and_is_keyed_from_its_flat_fields():
    doc = fixture("contact_create.doc.json")
    lead = ghl.normalise_lead(doc)
    assert (lead["name"], lead["city"], lead["state"], lead["country"], lead["postal_code"], lead["contact_id"]) == ("John Deo", "ruDolomitebika", "AL", "DE", "452001", "nmFmQEsNgz6AVpgLVUJ0")
    assert lead["attribution"]["present"] is False  # the page's own contact example carries no attribution
    key, basis = ghl.derive_key("acme", doc, raw(doc))
    assert basis == "synthesised" and key == ghl.sha256_hex("acme", "ContactCreate", "nmFmQEsNgz6AVpgLVUJ0", "2021-11-26T12:41:03.193Z", ghl.sha256_hex(raw(doc)))


def test_the_appointment_example_reads_its_nested_fields():
    doc = fixture("appointment_create.doc.json")
    lead = ghl.normalise_lead(doc)
    assert lead["contact_id"] == "9NkT25Vor1v4aQatFsv2" and lead["name"] == "Appointment with CRM Dev team" and lead["status"] == "confirmed"
    assert lead["date_added"] == "2023-09-25T16:00:00+05:30" and "id" not in doc  # nested, so a flat read finds nothing
    assert ghl.derive_key("acme", doc, raw(doc))[1] == "synthesised"


def test_the_opportunity_example_is_flat_and_its_status_is_a_plain_string_with_no_currency():
    doc = fixture("opportunity_create.doc.json")
    lead = ghl.normalise_lead(doc)
    assert lead["status"] == "open" and lead["monetary_value"] == 40 and "currency" not in doc
    assert lead["contact_id"] == "cJAWDskpkJHbRbhAT7bs"


def test_the_signature_fixture_key_is_the_one_the_code_ships_and_names_the_legacy_header_as_announced_not_retired():
    doc = fixture("webhook_signature.doc.json")
    assert doc["ed25519_public_key_spki_base64"] == ghl.GHL_ED25519_SPKI_B64
    assert "announced for deprecation" in doc["headers"]["X-WH-Signature"].lower() and "retired" not in json.dumps(doc).lower()
    assert doc["signed_bytes"]["provenance"] == "ASSUMED"


def test_the_envelope_shape_is_read_like_the_flat_one_and_the_envelope_keys_win():
    envelope = fixture("webhook_signature.doc.json")["envelope_shown_on_the_integration_guide"]
    lead = ghl.normalise_lead(envelope)
    assert (lead["first_name"], lead["last_name"], lead["email"], lead["event_type"]) == ("John", "Doe", "john@example.com", "ContactCreate")
    clash = ghl.normalise_lead({"type": "ContactCreate", "email": "outer@example.com", "data": {"email": "inner@example.com", "city": "Austin"}})
    assert clash["email"] == "outer@example.com" and clash["city"] == "Austin"
    assert ghl.normalise_lead({"data": "not an object", "email": "a@b.co"})["email"] == "a@b.co"
    assert "data" not in ghl.normalise_lead(envelope)["answers"]


def test_the_attribution_fixture_nested_example_is_read_and_the_key_list_is_what_the_code_knows():
    doc = fixture("attribution.doc.json")
    lead = ghl.normalise_lead({"type": "ContactCreate", **doc["nested_example"]})
    attr = lead["attribution"]
    assert (attr["utm_source"], attr["utm_medium"], attr["utm_campaign"], attr["fbclid"], attr["ip"], attr["fb_event_id"]) == ("facebook", "cpc", "spring", "IwAR2Ab_c-D", "203.0.113.7", "evt-1")
    assert attr["page_url"].startswith("https://shop.example.com/lp")
    known = {k for keys in ghl._ATTR_FLAT.values() for k in keys}
    for key in doc["flat_workflow_keys"]:
        assert key in known, key
    assert "no signature is documented" in doc["workflow_custom_webhook"]["signature"]
