"""The first output pass's corpus, kept in the suite.

`tests/corpus/relay_corpus_1.jsonl` holds 60 cases an independent adversary wrote against the relay: each has an input, an
expectation (`pass` is an ordinary input the relay must handle, `fail` an input it must refuse or flag), a `why` that states
the harm, a `source` (`real` was measured against a vendor page, `invented` was made up) and, for an invented case, the
`promise` it quotes from the spec. Eight cases disagreed with the code when the corpus was written: 16, 25, 26, 44 and 60
were harm and are fixed (their tests assert the fixed behaviour), 59 was README text and is fixed, and 28 and 55 are
KNOWN_LIMIT (their tests pin the current behaviour so a change is noticed and decided, not silent). The 52 other cases agreed
with the code and are covered by the suites named in each module; this file replays the eight.

A case added to the file carries `source` of `real` or `invented` only and a `why` of 10 or more characters.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from relay.app import Deps, create_app
from relay.reconcile import load_csv
from relay.sinks.capi import CapiSink
from relay.sinks.make import MakeSink
from relay.sinks.sheets import SheetsSink
from relay.sinks.slack import SlackSink

from .fakes.fathom import WEBHOOK_SECRET, make_meeting, webhook_headers
from .fakes.meta_graph import PIXEL_ID, TOKEN
from .fakes.sheets import SPREADSHEET_ID
from .helpers import ADMIN_TOKEN, TENANT, WORKFLOW_SECRET, ingest_one, make_cfg, make_rig, payload

RELAY = Path(__file__).resolve().parents[1]
CORPUS = Path(__file__).resolve().parent / "corpus" / "relay_corpus_1.jsonl"
NOW = 1_760_000_000
KINDS = ("pass", "fail")
SOURCES = ("real", "invented")


def load_cases() -> list[dict]:
    return [json.loads(line) for line in CORPUS.read_text(encoding="utf-8").splitlines() if line.strip()]


def case(n: int) -> dict:
    return load_cases()[n - 1]  # cases are numbered from 1 in the findings and the triage file


def test_the_corpus_has_enough_cases_of_both_kinds_each_with_a_why_a_source_and_a_promise():
    cases = load_cases()
    assert len(cases) >= 30
    for kind in KINDS:
        assert sum(1 for c in cases if c["expect"] == kind) >= 10, kind
    for n, c in enumerate(cases, 1):
        assert c["expect"] in KINDS and c["source"] in SOURCES, n
        assert isinstance(c["input"], str) and len(c["input"]) >= 10, n
        assert isinstance(c["why"], str) and len(c["why"]) >= 10, n
        if c["source"] == "invented":
            assert isinstance(c.get("promise"), str) and len(c["promise"].strip()) >= 10, n  # a quote from the spec


@pytest.mark.parametrize("n,snippet", [
    (16, "no type field"), (25, "force-replays a DONE Sheets delivery"), (26, "replays the dead row"), (28, "lone UTF-16 surrogate"),
    (44, "/admin/backfill for Sheets"), (55, "Windows-1252"), (59, "PowerShell"), (60, "pixel_id"),
])
def test_the_eight_replayed_cases_are_the_ones_the_findings_name(n, snippet):
    assert snippet in case(n)["input"]


# ---- case 16 and 17: the workflow body the docs describe, with and without `type` -------------------------------------------------


def capi_world(store, meta, slack=None):
    sink_cfg = {"url_env": "ACME_META_URL", "apikey_env": "ACME_META_TOKEN", "env": {"pixel_id": "ACME_PIXEL_ID"}, "options": {"test_event_code": "TEST1234"}}
    cfg = make_cfg(slack.url if slack else None, None, extra_sinks={"capi": sink_cfg}, env_extra={"ACME_META_URL": meta.url, "ACME_META_TOKEN": TOKEN, "ACME_PIXEL_ID": PIXEL_ID})
    meta.clock = time.time
    registry = {"capi": CapiSink(), "slack": SlackSink()}
    return cfg, create_app(cfg, Deps(store=store)).test_client(), make_rig(store, cfg, registry=registry)


def workflow_post(client, **body):
    base = {"event_key": "sub-16", "email": "ana@example.com", "phone": "+14155550100", "fbclid": "IwAR2Ab_c-D", "user_agent": "Mozilla/5.0", "page_url": "https://shop.example.com/lp"}
    return client.post(f"/hooks/{TENANT}/workflow", data=json.dumps({**base, **body}), headers={"X-Relay-Secret": WORKFLOW_SECRET, "Content-Type": "application/json"})


def alerts_of(store) -> list[dict]:
    with store.tx(write=False) as t:
        return [dict(r) for r in t.execute("SELECT sink, kind, reason FROM alerts ORDER BY id").fetchall()]


def test_case_16_a_workflow_body_with_no_type_is_accepted_and_the_skip_is_loud(store, meta):
    _cfg, client, rig = capi_world(store, meta)
    assert workflow_post(client).status_code == 200  # still accepted: the documented body is not refused
    rig.settle()
    [d] = store.deliveries()
    assert d["sink"] == "capi" and d["state"] == "skipped" and meta.requests == []
    assert "type" in d["last_error"] and "not mapped" not in d["last_error"]  # says what to add, not a vocabulary line
    assert [(a["sink"], a["kind"]) for a in alerts_of(store)] == [("capi", "skipped")] and "type" in alerts_of(store)[0]["reason"]


def test_case_17_the_same_body_with_type_lead_is_delivered_and_raises_no_alert(store, meta):
    _cfg, client, rig = capi_world(store, meta)
    assert workflow_post(client, type="Lead").status_code == 200
    rig.settle()
    [d] = store.deliveries()
    assert d["state"] == "done" and meta.effects == 1 and alerts_of(store) == []
    assert meta.requests[0]["json"]["data"][0]["action_source"] == "website"


# ---- case 25 and 26: a replay of a Sheets row that already landed must not add it again -------------------------------------------


def sheets_world(store, sheets, sa_json):
    sink_cfg = {
        "url_env": "ACME_SHEETS_URL", "env": {"spreadsheet_id": "ACME_SHEET_ID", "service_account": "ACME_SA_JSON"},
        "options": {"sheet": "Sheet1"}, "timeout_s": 5.0,
    }
    cfg = make_cfg(None, None, extra_sinks={"sheets": sink_cfg}, env_extra={"ACME_SHEETS_URL": sheets.url, "ACME_SHEET_ID": SPREADSHEET_ID, "ACME_SA_JSON": sa_json})
    return cfg, create_app(cfg, Deps(store=store)).test_client(), make_rig(store, cfg, registry={"sheets": SheetsSink()})


def admin(client, path, body):
    return client.post(path, data=json.dumps(body), headers={"Authorization": f"Bearer {ADMIN_TOKEN}", "Content-Type": "application/json"})


def test_case_25_a_force_replay_of_a_done_sheets_delivery_does_not_add_the_row_again(store, sheets, sa_json):
    cfg, client, rig = sheets_world(store, sheets, sa_json)
    r = ingest_one(store, cfg, payload(1))
    rig.settle()
    assert sheets.column_values(1) == [r.event_id]  # positive control: it landed once
    res = admin(client, "/admin/replay", {"event_id": r.event_id, "sink": "sheets", "force": True})
    assert res.get_json() == {"ok": True, "reset": 1}
    rig.settle()
    assert store.delivery(r.event_id, "sheets")["state"] == "done" and sheets.column_values(1) == [r.event_id]


def test_case_26_a_dead_sheets_row_that_had_landed_is_not_duplicated_by_the_documented_replay(store, sheets, sa_json):
    cfg, client, rig = sheets_world(store, sheets, sa_json)
    r = ingest_one(store, cfg, payload(1))
    rig.settle()
    with store.tx() as t:  # Sheets was down 73 hours after the row landed, so the delivery expired
        t.execute("UPDATE deliveries SET state = 'dead', last_error = 'expired: older than 72 hours' WHERE event_id = ?", (r.event_id,))
    assert admin(client, "/admin/replay", {"event_id": r.event_id}).get_json()["reset"] == 1
    rig.settle()
    assert store.delivery(r.event_id, "sheets")["state"] == "done" and sheets.column_values(1) == [r.event_id]


# ---- case 44: backfill never sends a stored meeting to a sink that does not take meetings ------------------------------------------


def test_case_44_backfill_for_sheets_and_make_skips_a_stored_fathom_meeting(store, slack, make, sheets, sa_json):
    import json as _json

    from relay.config import load_config

    from .fakes.fathom import WEBHOOK_SECRET as SECRET
    from .helpers import config_json, env_for

    env = env_for(slack.url, make.url, ACME_FATHOM_SECRET=SECRET, ACME_SHEETS_URL=sheets.url, ACME_SHEET_ID=SPREADSHEET_ID, ACME_SA_JSON=sa_json)
    doc = _json.loads(config_json(extra_sinks={"sheets": {"url_env": "ACME_SHEETS_URL", "env": {"spreadsheet_id": "ACME_SHEET_ID", "service_account": "ACME_SA_JSON"}, "options": {"sheet": "Sheet1"}}}))
    doc["tenants"][TENANT]["fathom"] = {"secret_env": "ACME_FATHOM_SECRET", "slack": True}
    env["RELAY_CONFIG"] = _json.dumps(doc)
    client = create_app(load_config(env), Deps(store=store, clock=lambda: float(NOW))).test_client()
    body = _json.dumps(make_meeting(1)).encode()
    assert client.post(f"/webhooks/fathom/{TENANT}", data=body, headers=webhook_headers(WEBHOOK_SECRET, "msg_1", NOW, body)).status_code == 200
    assert [d["sink"] for d in store.deliveries()] == ["slack"]  # the meeting went to Slack only
    for sink in ("sheets", "make"):
        res = admin(client, "/admin/backfill", {"tenant": TENANT, "sink": sink, "since": "2000-01-01T00:00:00Z"})
        assert res.get_json() == {"ok": True, "inserted": 0}, sink
    assert [d["sink"] for d in store.deliveries()] == ["slack"]


# ---- case 28 and 55: KNOWN_LIMIT, pinned so a change is noticed ----------------------------------------------------------------------


def test_case_28_known_limit_a_lone_surrogate_in_a_name_kills_that_one_lead_and_no_other(store, slack, make):
    # KNOWN_LIMIT: one lead in a million carries half an emoji (a form cut a character in two). Measured at this commit, the
    # catch-all records `sink bug: UnicodeEncodeError` on BOTH Slack and Make for that lead (the corpus text says Slack
    # posted it) and raises an alert, so the person is re-entered by hand. No other lead is touched, and replay fails the same way.
    cfg = make_cfg(slack.url, make.url)
    rig = make_rig(store, cfg)
    r = ingest_one(store, cfg, payload(1, name="Pat \ud83d"))
    ok = ingest_one(store, cfg, payload(2))
    rig.settle()
    states = {(d["event_id"], d["sink"]): (d["state"], d["last_error"]) for d in store.deliveries()}
    assert states[(r.event_id, "make")] == ("dead", "sink bug: UnicodeEncodeError")
    assert states[(r.event_id, "slack")] == ("dead", "sink bug: UnicodeEncodeError")
    assert states[(ok.event_id, "make")][0] == "done" and states[(ok.event_id, "slack")][0] == "done"
    assert any(a["kind"] == "dead" for a in alerts_of(store))


def test_case_55_known_limit_a_windows_1252_csv_raises_a_decode_error_the_bookkeeper_can_resave_as_utf8(tmp_path):
    # KNOWN_LIMIT: an Excel "CSV (Comma delimited)" file is Windows-1252. The loader reads UTF-8 and says so in the error.
    path = tmp_path / "ledger.csv"
    path.write_bytes("id,amount,date,bucket\nl1,10.00,2026-10-01,Café\n".encode("cp1252"))
    with pytest.raises(UnicodeDecodeError):
        load_csv(path, {"id": "id", "amount": "amount", "date": "date", "bucket": "bucket"}, currency="USD")
    path.write_bytes("id,amount,date,bucket\nl1,10.00,2026-10-01,Café\n".encode("utf-8"))
    assert load_csv(path, {"id": "id", "amount": "amount", "date": "date", "bucket": "bucket"}, currency="USD")[0]["bucket"] == "Café"


# ---- case 59 and 60: what a reader finds in the docs ------------------------------------------------------------------------------------


def test_case_59_the_readme_run_it_block_has_a_powershell_form_with_the_venv_python():
    text = (RELAY / "README.md").read_text(encoding="utf-8")
    assert '$env:DATABASE_URL_TEST = "postgresql://' in text and ".venv\\Scripts\\python.exe -m pytest" in text
    assert ".venv\\Scripts\\python.exe tests\\mutants.py" in text


def test_case_60_the_sink_options_and_the_type_a_workflow_body_needs_are_documented_where_a_reader_looks():
    configuration = (RELAY / "docs" / "configuration.md").read_text(encoding="utf-8")
    routes = (RELAY / "docs" / "routes.md").read_text(encoding="utf-8")
    readme = (RELAY / "README.md").read_text(encoding="utf-8")
    for word in ("pixel_id", "spreadsheet_id", "service_account", "capi_require_click_id", "test_event_code", "click_id_sources", "default_country_code", "event_map", "sheet", "columns"):
        assert word in configuration, word
    assert "`type`" in routes and "Lead" in routes and "skipped" in routes
    row = next(line for line in readme.splitlines() if line.startswith("| Meta Conversions API"))
    assert "`type`" in row and "click id" in row
