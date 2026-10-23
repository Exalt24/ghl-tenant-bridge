"""Generated tables for the classes the second (blind) output pass found in the relay. Each table crosses the inputs a neighbour
of the reproduced case could use, so a variant cannot hide next to the one the reviewer wrote.

The blind corpus cases that disagreed with the code: 13 and 14 (an ambiguous outcome spends its budget inside half an hour),
37 (a Purchase counted twice when the delivery wraps its fields in `data`), 51 (a Windows-1252 payments CSV), and the lone
half emoji that killed every sink and not only Slack and Make.

CLASS-TABLE: F8 and F9 an ambiguous outcome must outlast an ordinary vendor incident, sinks x outage lengths x outcome kinds
CLASS-TABLE: F10 one event id per opportunity however the delivery wraps its fields, shapes x event kinds x repeats
CLASS-TABLE: F11 a payments CSV in any encoding Excel writes, encodings x byte order marks x column x valid or invalid byte
CLASS-TABLE: surrogate a lone half emoji in any text field reaches no sink as an error, fields x sinks
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import itertools
import json
import random
import subprocess
import sys
import time
from pathlib import Path

import pytest

from relay import ghl
from relay.app import Deps, create_app
from relay.fanout import AMBIGUOUS_BACKOFF_BASE_S, MAX_ATTEMPTS
from relay.http import Http, HttpFailure, HttpResult, HttpTimeout
from relay.reconcile import CsvError, canonical, load_csv, reconcile
from relay.sinks.base import Event
from relay.sinks.capi import CapiSink, make_event_id
from relay.sinks.make import MakeSink
from relay.sinks.sheets import SheetsSink
from relay.sinks.slack import SlackSink
from relay.worker import Worker

from .fakes.meta_graph import PIXEL_ID, TOKEN
from .fakes.sheets import SPREADSHEET_ID
from .helpers import INGEST_TOKEN, LOCATION, TENANT, Rig, ingest_one, make_cfg, payload

RELAY = Path(__file__).resolve().parents[1]
FOUR_HOURS_S = 4 * 3600

# ---- F8 and F9: sinks x outage lengths x outcome kinds, on the simulated database clock ---------------------------------------------


class OutageHttp:
    """The real Http (so a delivery after the outage reaches the real fake) wrapped so that, while the database clock is
    inside the outage, a call to the vendor fails the way the kind says and never reaches the fake. The token call of the
    Sheets sink is never failed. It counts every call that is a delivery attempt."""

    def __init__(self, inner: Http, store, outage_s: float, kind: str, sink: str, pass_url: str | None = None) -> None:
        self.inner, self.store, self.outage_s, self.kind, self.sink, self.pass_url = inner, store, outage_s, kind, sink, pass_url
        self.sends = 0

    def request(self, method, url, **kw):
        if self.sink != "sheets" or (method == "POST" and ":append" in url):
            self.sends += 1
        if url != self.pass_url and self.store.offset_s < self.outage_s:
            if self.kind == "5xx":
                return HttpResult(503, {}, "service unavailable")
            if self.kind == "timeout":
                raise HttpTimeout("ReadTimeout")
            if self.kind == "reset":
                raise HttpFailure("RemoteProtocolError")
            return HttpResult(429, {"retry-after": "300"}, "rate limited")
        return self.inner.request(method, url, **kw)


F8_SINKS = ("slack", "make", "sheets")
F8_OUTAGES_S = (60, 1500, 7200, 14399, 28800, 43200)  # one minute, the 25 minute incident of the case, 2 h, just under 4 h, 8 h, 12 h
F8_KINDS = ("5xx", "timeout", "reset", "429")
# CLASS-TABLE: F8 and F9 sinks x outage lengths x outcome kinds (the generated table of the class)
F8_TABLE = list(itertools.product(F8_SINKS, F8_OUTAGES_S, F8_KINDS))


def outage_world(request, store, sink: str, outage_s: float, kind: str):
    if sink == "slack":
        fake = request.getfixturevalue("slack")
        cfg, registry, pass_url = make_cfg(fake.url, None), {"slack": SlackSink()}, None
    elif sink == "make":
        fake = request.getfixturevalue("make")
        cfg, registry, pass_url = make_cfg(None, fake.url), {"make": MakeSink()}, None
    else:
        fake = request.getfixturevalue("sheets")
        sa_json = request.getfixturevalue("sa_json")
        sink_cfg = {"url_env": "ACME_SHEETS_URL", "env": {"spreadsheet_id": "ACME_SHEET_ID", "service_account": "ACME_SA_JSON"}, "options": {"sheet": "Sheet1"}, "timeout_s": 5.0}
        cfg = make_cfg(None, None, extra_sinks={"sheets": sink_cfg}, env_extra={"ACME_SHEETS_URL": fake.url, "ACME_SHEET_ID": SPREADSHEET_ID, "ACME_SA_JSON": sa_json})
        registry, pass_url = {"sheets": SheetsSink()}, fake.token_url
    http = OutageHttp(Http(), store, outage_s, kind, sink, pass_url)
    worker = Worker(cfg, store, http, rng=random.Random(1), sleeper=store.advance, registry=registry)
    return fake, cfg, Rig(store=store, cfg=cfg, worker=worker, http=http), http


def landed(sink: str, fake) -> int:
    return len(fake.column_values(1)) if sink == "sheets" else fake.effects


@pytest.mark.parametrize("sink,outage_s,kind", F8_TABLE)
def test_class_f8_an_ambiguous_outcome_outlasts_an_ordinary_vendor_incident(request, store, sink, outage_s, kind):
    fake, cfg, rig, http = outage_world(request, store, sink, outage_s, kind)
    r = ingest_one(store, cfg)
    rig.settle(max_rounds=2000)
    d = store.delivery(r.event_id, sink)
    where = (sink, outage_s, kind)
    if kind == "429":  # the vendor certainly did not act: a RETRY spends no budget and a lead dies of age, never of an outage
        assert d["state"] == "done" and d["attempts"] == 1 and d["retries"] >= 1, where
        assert landed(sink, fake) == 1, where
        return
    assert http.sends <= MAX_ATTEMPTS + (1 if sink == "sheets" else 0), where  # the duplicate cap holds: at most 6 sends
    if sink == "sheets":
        # Its later tries read the sheet back first (a failed read is a plain RETRY), so the budget is not what ends it.
        assert d["state"] == "done" and landed(sink, fake) == 1, where
        return
    if outage_s <= FOUR_HOURS_S:
        assert d["state"] == "done" and landed(sink, fake) == 1, where  # an incident under 4 hours ends in a delivered alert
    else:
        assert d["state"] == "dead" and d["attempts"] == MAX_ATTEMPTS and "attempt budget" in d["last_error"], where
        assert http.sends == MAX_ATTEMPTS and landed(sink, fake) == 0, where  # dead only after the whole budget of 6, no 7th send


def test_the_six_ambiguous_tries_span_at_least_four_hours_on_the_simulated_clock(slack, store):
    """The case itself: Slack answers 503 for two hours. Every try is made at a database time at least this far apart."""
    cfg = make_cfg(slack.url, None)
    http = OutageHttp(Http(), store, 10 * 3600, "5xx", "slack")
    stamps: list[float] = []
    real = http.request

    def note(method, url, **kw):
        stamps.append(store.offset_s)
        return real(method, url, **kw)

    http.request = note  # type: ignore[method-assign]
    worker = Worker(cfg, store, http, rng=random.Random(2), sleeper=store.advance, registry={"slack": SlackSink()})
    ingest_one(store, cfg)
    Rig(store=store, cfg=cfg, worker=worker, http=http).settle(max_rounds=200)
    assert len(stamps) == MAX_ATTEMPTS
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert gaps[0] >= AMBIGUOUS_BACKOFF_BASE_S and all(g >= AMBIGUOUS_BACKOFF_BASE_S for g in gaps)
    assert stamps[-1] - stamps[0] >= FOUR_HOURS_S


# ---- F10: one event id per opportunity however the delivery wraps its fields ----------------------------------------------------------------

SHAPES = ("flat", "envelope", "type-in-data", "all-in-data")
KINDS = ("won", "appointment", "lead")
REPEATS = (1, 2, 3)
EXPECTED_NAME = {"won": "Purchase", "appointment": "Schedule", "lead": "Lead"}
TYPE_OF = {"won": "OpportunityStatusUpdate", "appointment": "AppointmentCreate", "lead": "ContactCreate"}


def stamp(offset_s: int = 0) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=300 - offset_s)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def entity_fields(kind: str, entity: str, n: int = 0) -> dict:
    if kind == "won":
        return {"id": entity, "status": "won", "monetaryValue": 500, "email": "a@b.com", "dateUpdated": stamp(n)}
    if kind == "appointment":
        return {"contactId": "cX", "email": "a@b.com", "appointment": {"id": entity, "status": "confirmed", "dateAdded": stamp(n), "dateUpdated": stamp(n)}}
    return {"id": entity, "email": "a@b.com", "firstName": "Pat", "dateAdded": stamp(n)}


def shaped(shape: str, kind: str, entity: str, n: int = 0) -> dict:
    fields, type_ = entity_fields(kind, entity, n), TYPE_OF[kind]
    if shape == "flat":
        return {"type": type_, "locationId": LOCATION, **fields}
    if shape == "envelope":
        return {"type": type_, "locationId": LOCATION, "timestamp": stamp(n), "data": fields}
    if shape == "type-in-data":
        return {"locationId": LOCATION, "data": {"type": type_, **fields}}
    return {"data": {"type": type_, "locationId": LOCATION, **fields}}


def expected_id(kind: str, entity: str) -> str:
    # Written here from the documented rule (sha256 of tenant|entity id|event name), not with the relay's own function.
    return hashlib.sha256(f"{TENANT}|{entity}|{EXPECTED_NAME[kind]}".encode()).hexdigest()


def event_for(raw: dict) -> Event:
    return Event(event_id="evt-1", tenant=TENANT, source="ingest", payload=copy.deepcopy(raw), lead=ghl.normalise_lead(raw), received_epoch=None)


def capi_cfg():
    sink_cfg = {
        "url_env": "ACME_META_URL", "apikey_env": "ACME_META_TOKEN", "env": {"pixel_id": "ACME_PIXEL_ID"},
        "options": {"test_event_code": "TEST1234", "currency": "USD", "capi_require_click_id": False},
    }
    return sink_cfg


# CLASS-TABLE: F10 payload shapes x event kinds x repeat counts (each repeat arrives in the shape, then in the flat shape, alternating)
F10_TABLE = list(itertools.product(SHAPES, KINDS, REPEATS))


@pytest.mark.parametrize("shape,kind,repeats", F10_TABLE)
def test_class_f10_one_event_id_per_opportunity_however_the_delivery_wraps_its_fields(store, meta, shape, kind, repeats):
    from relay.sinks.capi import CapiSink as Sink

    cfg = make_cfg(None, None, extra_sinks={"capi": capi_cfg()}, env_extra={"ACME_META_URL": meta.url, "ACME_META_TOKEN": TOKEN, "ACME_PIXEL_ID": PIXEL_ID})
    meta.clock = time.time
    from .helpers import make_rig

    rig = make_rig(store, cfg, registry={"capi": Sink()})
    entity = "ent-0001"
    for i in range(repeats):  # the same thing delivered again (won, reopened, won again), the repeats alternating shapes
        raw = shaped(shape if i % 2 == 0 else "flat", kind, entity, i)
        ingest_one(store, cfg, raw, key=f"k{i}")
    rig.settle()
    sent = [req["json"]["data"][0] for req in meta.requests]
    assert [d["state"] for d in store.deliveries()] == ["done"] * repeats, (shape, kind, repeats)
    assert len(sent) == repeats and {e["event_id"] for e in sent} == {expected_id(kind, entity)}, (shape, kind, repeats)
    assert {e["event_name"] for e in sent} == {EXPECTED_NAME[kind]}
    assert meta.effects == 1, (shape, kind, repeats)  # Meta keeps one copy, so the revenue counts once


# CLASS-TABLE: F10 shape of the first delivery x shape of the second x event kinds, pure: the same thing shares an id, two things never do
F10_PAIRS = list(itertools.product(SHAPES, SHAPES, KINDS))


@pytest.mark.parametrize("first,second,kind", F10_PAIRS)
def test_class_f10_the_same_entity_shares_one_id_across_shapes_and_two_entities_never_share(first, second, kind):
    name = EXPECTED_NAME[kind]
    a = make_event_id(event_for(shaped(first, kind, "ent-A", 0)), name)
    b = make_event_id(event_for(shaped(second, kind, "ent-A", 5)), name)
    other = make_event_id(event_for(shaped(second, kind, "ent-B", 0)), name)
    assert a == b == expected_id(kind, "ent-A"), (first, second, kind)
    assert other == expected_id(kind, "ent-B") and other != a, (first, second, kind)


# CLASS-TABLE: F10 every reader of the raw payload x payload shapes: they all see the same ids, email and location
READERS = ("opportunity id", "appointment id", "contact id", "email", "location", "identity")
F10_READERS = list(itertools.product(SHAPES, READERS))


@pytest.mark.parametrize("shape,reader", F10_READERS)
def test_class_f10_every_reader_of_the_raw_payload_agrees_with_the_flat_shape(shape, reader):
    def read(p):
        lead = ghl.normalise_lead(p)
        return {
            "opportunity id": ghl.opportunity_id(p), "appointment id": ghl.appointment_id(p), "contact id": lead["contact_id"],
            "email": lead["email"], "location": ghl.location_id(p), "identity": ghl.identity_for(p),
        }[reader]

    kind = {"opportunity id": "won", "appointment id": "appointment", "contact id": "lead", "email": "won", "location": "won", "identity": "won"}[reader]
    flat = shaped("flat", kind, "ent-A", 0)
    wrapped = shaped(shape, kind, "ent-A", 0)
    assert read(wrapped) == read(flat) and read(flat) not in (None, (None, None)), (shape, reader)


@pytest.mark.parametrize("route,shape", list(itertools.product(("ingest",), SHAPES)))
def test_class_f10_a_delivery_is_routed_to_its_tenant_whichever_shape_carries_the_location(store, slack, route, shape):
    cfg = make_cfg(slack.url, None)
    client = create_app(cfg, Deps(store=store)).test_client()
    raw = json.dumps(shaped(shape, "won", "ent-A", 0))
    res = client.post("/ingest", data=raw, headers={"Authorization": f"Bearer {INGEST_TOKEN}", "Idempotency-Key": "k1", "Content-Type": "application/json"})
    assert res.status_code == 200, (shape, res.get_json())
    assert [d["sink"] for d in store.deliveries()] == ["slack"]


# ---- F11: encodings x byte order marks x the column of the non-ASCII byte x a valid or an invalid byte --------------------------------

MAP = {"id": "Reference", "amount": "Gross", "currency": "Cur", "date": "Paid on"}
HEADER = "Reference,Gross,Cur,Paid on,Memo,Note"
ENCODINGS = ("utf-8", "cp1252")
BOMS = ("none", "utf-8 bom")
COLUMNS = ("id (first column)", "memo (middle)", "note (last, before the line end)", "second data row")
BYTES = ("valid", "invalid")
UTF8_BOM = b"\xef\xbb\xbf"


def csv_bytes(encoding: str, bom: str, column: str, kind: str) -> tuple[bytes, int | None, str]:
    """(file bytes, index of the bad byte or None, the text the valid character should read back as)."""
    char = "\u00e9"
    cells = [["A1", "100.00", "USD", "2026-10-01", "m1", "n1"], ["A2", "50.00", "USD", "2026-10-02", "m2", "n2"]]
    where = {"id (first column)": (0, 0), "memo (middle)": (0, 4), "note (last, before the line end)": (0, 5), "second data row": (1, 4)}[column]
    marker = "\x00"
    cells[where[0]][where[1]] = "Jos" + marker + "x"
    text = HEADER + "\r\n" + "\r\n".join(",".join(row) for row in cells) + "\r\n"
    head, tail = text.split(marker)
    piece = char.encode(encoding) if kind == "valid" else b"\x81"  # 0x81 is undefined in Windows-1252 and a stray byte in UTF-8
    body = head.encode("ascii") + piece + tail.encode("ascii")
    prefix = UTF8_BOM if bom == "utf-8 bom" else b""
    return prefix + body, (len(prefix) + len(head) if kind == "invalid" else None), "Jos" + char + "x"


# CLASS-TABLE: F11 encodings x byte order marks x the column holding the non-ASCII byte x a valid or an invalid byte
F11_TABLE = [row for row in itertools.product(ENCODINGS, BOMS, COLUMNS, BYTES) if not (row[0] == "cp1252" and row[1] != "none")]


@pytest.mark.parametrize("encoding,bom,column,kind", F11_TABLE)
def test_class_f11_a_payments_csv_in_any_encoding_excel_writes_loads_and_a_bad_byte_is_a_readable_csv_error(tmp_path, encoding, bom, column, kind):
    data, bad_at, text = csv_bytes(encoding, bom, column, kind)
    path = tmp_path / "payments export.csv"
    path.write_bytes(data)
    where = (encoding, bom, column, kind)
    if kind == "invalid":
        with pytest.raises(CsvError) as err:
            load_csv(path, MAP)
        message = str(err.value)
        assert "payments export.csv" in message and message.count(f"offset {bad_at}") == 2, (where, message)  # the file and the first bad byte
        return
    rows = load_csv(path, MAP)
    assert [r["amount_minor"] for r in rows] == [10000, 5000], where
    assert rows[0]["id"] == (text if column.startswith("id") else "A1"), where  # the character came through as itself
    assert rows[0]["currency"] == "USD" and rows[0]["date"] == "2026-10-01", where


def test_a_utf_16_file_is_a_csv_error_naming_the_file(tmp_path):
    path = tmp_path / "unicode text.csv"
    path.write_bytes(("\ufeff" + HEADER + "\r\nA1,100.00,USD,2026-10-01,m,n\r\n").encode("utf-16"))
    with pytest.raises(CsvError, match=r"unicode text\.csv.*UTF-16"):
        load_csv(path, MAP)


# CLASS-TABLE: F11 the script an operator runs (the runbook example) x a UTF-8 file, a Windows-1252 file and a file that is neither
CLI_CASES = [("utf-8", True), ("cp1252", True), ("neither", False)]
CLI_SCRIPT = """
import sys
from relay.reconcile import load_csv
rows = load_csv(sys.argv[1], {"id": "Reference", "amount": "Gross", "currency": "Cur", "date": "Paid on"})
print(len(rows), rows[0]["id"])
"""


@pytest.mark.parametrize("which,loads", CLI_CASES)
def test_class_f11_the_script_that_calls_load_csv_prints_a_result_or_a_readable_error_never_a_decode_traceback(tmp_path, which, loads):
    csv_path = tmp_path / "export.csv"
    if which == "neither":
        csv_path.write_bytes(HEADER.encode() + b"\r\nA1,100.00,USD,2026-10-01,\x81,n\r\n")
    else:
        csv_path.write_bytes((HEADER + "\r\nJos\u00e9,100.00,USD,2026-10-01,m,n\r\n").encode(which))
    script = tmp_path / "run_reconcile.py"
    script.write_text(CLI_SCRIPT, encoding="utf-8")
    res = subprocess.run([sys.executable, str(script), str(csv_path)], cwd=tmp_path, env={"PYTHONPATH": str(RELAY), "PYTHONIOENCODING": "utf-8", "SYSTEMROOT": "C:\\Windows"}, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert "UnicodeDecodeError" not in res.stderr, res.stderr
    if loads:
        assert res.returncode == 0 and res.stdout.split() == ["1", "Jos\u00e9"], (which, res.stdout, res.stderr)
    else:
        assert res.returncode != 0 and "CsvError" in res.stderr and "export.csv" in res.stderr and "offset" in res.stderr, res.stderr


# ---- the lone half emoji: every text field x every sink ----------------------------------------------------------------------------------------

LONE = "\ud83d"  # a half of an emoji, which no UTF-8 encoder accepts
SINKS = ("slack", "make", "capi", "sheets")
# (label, path into the payload). A path ending in a list index reaches a tag.
FIELDS = [
    ("name", ("name",)), ("first name", ("firstName",)), ("last name", ("lastName",)), ("email", ("email",)), ("phone", ("phone",)),
    ("city", ("city",)), ("state", ("state",)), ("country", ("country",)), ("postal code", ("postalCode",)), ("source", ("source",)),
    ("tag", ("tags", 0)), ("utm_source", ("utm_source",)), ("utm_campaign", ("utm_campaign",)), ("fbclid", ("fbclid",)),
    ("page url", ("page_url",)), ("user agent", ("user_agent",)), ("nested attribution", ("attributionSource", "utmMedium")),
    ("custom answer value", ("Roof type",)), ("custom answer key", ("__key__",)),
]


def lead_with(path: tuple, wrapped: bool = False) -> dict:
    p = payload(1, dateAdded=stamp(), dateUpdated=stamp(), fbclid="IwAR2Ab_c-D", user_agent="Mozilla/5.0 (X11)", page_url="https://shop.example.com/lp",
                utm_source="facebook", utm_campaign="spring", country="US", postalCode="78701", attributionSource={"utmMedium": "cpc"}, **{"Roof type": "tile"})
    p["type"] = "ContactCreate"
    if path == ("__key__",):
        p["Roof " + LONE + " type"] = "tile"
    else:
        holder = p
        for step in path[:-1]:
            holder = holder[step]
        last = path[-1]
        current = holder[last]
        holder[last] = (current if isinstance(current, str) else "x") + LONE
    return {"locationId": LOCATION, "data": {k: v for k, v in p.items() if k != "locationId"}} if wrapped else p


def has_surrogate(value) -> bool:
    if isinstance(value, str):
        return any(0xD800 <= ord(c) <= 0xDFFF for c in value)
    if isinstance(value, dict):
        return any(has_surrogate(k) or has_surrogate(v) for k, v in value.items())
    if isinstance(value, list):
        return any(has_surrogate(v) for v in value)
    return False


# CLASS-TABLE: surrogate text fields x sinks (each delivered end to end with the half emoji in that one field)
SURROGATE_TABLE = list(itertools.product(range(len(FIELDS)), SINKS))


@pytest.mark.parametrize("index,sink", SURROGATE_TABLE, ids=[f"{FIELDS[i][0]}-{s}" for i, s in SURROGATE_TABLE])
def test_class_surrogate_a_lone_half_emoji_in_any_field_reaches_no_sink_as_an_error(request, store, index, sink):
    label, path = FIELDS[index]
    meta_fake = None
    if sink == "slack":
        fake = request.getfixturevalue("slack")
        cfg, registry = make_cfg(fake.url, None), {"slack": SlackSink()}
    elif sink == "make":
        fake = request.getfixturevalue("make")
        cfg, registry = make_cfg(None, fake.url), {"make": MakeSink()}
    elif sink == "capi":
        fake = meta_fake = request.getfixturevalue("meta")
        fake.clock = time.time
        cfg = make_cfg(None, None, extra_sinks={"capi": capi_cfg()}, env_extra={"ACME_META_URL": fake.url, "ACME_META_TOKEN": TOKEN, "ACME_PIXEL_ID": PIXEL_ID})
        registry = {"capi": CapiSink()}
    else:
        fake = request.getfixturevalue("sheets")
        sa_json = request.getfixturevalue("sa_json")
        sink_cfg = {"url_env": "ACME_SHEETS_URL", "env": {"spreadsheet_id": "ACME_SHEET_ID", "service_account": "ACME_SA_JSON"}, "options": {"sheet": "Sheet1"}, "timeout_s": 5.0}
        cfg = make_cfg(None, None, extra_sinks={"sheets": sink_cfg}, env_extra={"ACME_SHEETS_URL": fake.url, "ACME_SHEET_ID": SPREADSHEET_ID, "ACME_SA_JSON": sa_json})
        registry = {"sheets": SheetsSink()}
    from .helpers import make_rig

    rig = make_rig(store, cfg, registry=registry)
    ingest_one(store, cfg, lead_with(path))
    rig.settle()
    [d] = store.deliveries()
    assert d["state"] == "done" and d["last_error"] is None, (label, sink, d["state"], d["last_error"])
    with store.tx(write=False) as t:
        assert t.execute("SELECT COUNT(*) AS n FROM alerts").fetchone()["n"] == 0, (label, sink)


# CLASS-TABLE: surrogate the canonical lead record x text fields x flat or wrapped: no string in it can be a lone half
@pytest.mark.parametrize("index,wrapped", list(itertools.product(range(len(FIELDS)), (False, True))), ids=lambda v: FIELDS[v][0] if isinstance(v, int) else ("data" if v else "flat"))
def test_class_surrogate_the_canonical_lead_holds_no_lone_half(index, wrapped):
    label, path = FIELDS[index]
    raw = lead_with(path, wrapped)
    assert has_surrogate(raw), label  # the control: the input really carries one
    assert not has_surrogate(ghl.normalise_lead(raw)), (label, wrapped)


def test_a_half_emoji_becomes_the_replacement_character_and_nothing_else_changes():
    lead = ghl.normalise_lead(payload(1, name="Pat " + LONE + " Smith", city="Z\u00fcrich \U0001F600"))
    assert lead["name"] == "Pat \ufffd Smith" and lead["city"] == "Z\u00fcrich \U0001F600"  # a whole emoji and an accent are untouched


# CLASS-TABLE: surrogate the canonical payment row x fields: an id or a bucket with a half emoji reaches the report encodable
@pytest.mark.parametrize("field", ("id", "bucket"))
def test_class_surrogate_a_payment_row_with_a_half_emoji_reconciles_and_encodes(field):
    row = {"id": "INV-1", "amount_minor": 100, "currency": "USD", "date": "2026-10-01", "bucket": "sales"}
    row[field] = row[field] + LONE
    assert has_surrogate(row)
    result = reconcile([row], [dict(row)], expected_bucket=None)
    json.dumps(result.counts()).encode("utf-8")
    assert not has_surrogate(canonical(row).id) and not has_surrogate(canonical(row).bucket)
    json.dumps(result.matched, ensure_ascii=False).encode("utf-8")


# CLASS-TABLE: surrogate the Fathom meeting text x fields: a half emoji in a title, summary or action item reaches Slack
@pytest.mark.parametrize("field", ("title", "summary", "action item"))
def test_class_surrogate_a_fathom_meeting_with_a_half_emoji_reaches_slack(store, slack, field):
    cfg = make_cfg(slack.url, None)
    meeting = {"title": "Weekly sync", "summary": "notes", "action_items": [{"description": "send the deck", "completed": False}], "url": "https://fathom.video/x"}
    if field == "action item":
        meeting["action_items"][0]["description"] += LONE
    else:
        meeting[field] += LONE
    store.ingest(key="m1", tenant=TENANT, source="fathom", basis="native", payload=json.dumps(meeting), sinks=["slack"])
    from .helpers import make_rig

    make_rig(store, cfg, registry={"slack": SlackSink()}).settle()
    [d] = store.deliveries()
    assert d["state"] == "done" and slack.effects == 1, (field, d["state"], d["last_error"])
