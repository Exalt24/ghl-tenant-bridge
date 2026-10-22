"""Generated tables for the classes of the first output pass that were fixed (a replay that appends a Sheets
row again, is a KNOWN_LIMIT pinned in test_output_corpus.py). Each table crosses every input route,
every omitted field, every sink and every recovery action it could meet, so a neighbour of the reproduced case cannot hide.

CLASS-TABLE: F1 an input a documented route may omit must never be skipped with an outcome the operator cannot see
CLASS-TABLE: F3 a bulk path must honour which source each sink accepts
CLASS-TABLE: F7 a row layout with an empty cell inside the row must never split the Sheets table
"""

from __future__ import annotations

import itertools
import json
import time

import pytest

from relay import ghl
from relay.app import Deps, create_app
from relay.config import load_config
from relay.sinks.capi import CapiSink
from relay.sinks.base import Event
from relay.sinks.sheets import SheetsSink
from relay.sinks.slack import SlackSink

from .fakes.fathom import WEBHOOK_SECRET
from .fakes.meta_graph import PIXEL_ID, TOKEN
from .fakes.sheets import SPREADSHEET_ID, service_account_json
from .helpers import ADMIN_TOKEN, INGEST_TOKEN, LOCATION, TENANT, WORKFLOW_SECRET, config_json, env_for, ingest_one, make_cfg, make_rig, payload
from .test_ghl_route import keypair, sign

# ---- F1: the documented workflow body, every route, every omitted field, every sink combination -----------------------------------

ROUTES = ("workflow", "ingest", "direct")
OMITTED = (None, "type", "email", "phone", "fbclid", "user_agent", "page_url")
COMBOS = (("capi",), ("capi", "slack"), ("slack",))
# What the CAPI sink does with the documented body minus one field (every other rule is the sink's own, unchanged).
CAPI_STATE = {None: "done", "type": "skipped", "email": "done", "phone": "done", "fbclid": "skipped", "user_agent": "done", "page_url": "done"}


def capi_sink_cfg() -> dict:
    return {
        "url_env": "ACME_META_URL", "apikey_env": "ACME_META_TOKEN", "env": {"pixel_id": "ACME_PIXEL_ID"},
        "options": {"test_event_code": "TEST1234"},
    }


def documented_body(n: int, omit: str | None) -> dict:
    """The workflow body docs/routes.md describes, plus the location the other two routes route by."""
    body = {
        "event_key": f"sub-{n}", "locationId": LOCATION, "type": "Lead", "email": "ana@example.com", "phone": "+14155550100",
        "fbclid": "IwAR2Ab_c-D", "user_agent": "Mozilla/5.0 (X11; Linux x86_64)", "page_url": "https://shop.example.com/lp?utm_source=facebook",
    }
    body.pop(omit, None)
    return body


def alert_rows(store) -> list[dict]:
    with store.tx(write=False) as t:
        return [dict(r) for r in t.execute("SELECT sink, tenant, kind, reason, event_id FROM alerts ORDER BY id").fetchall()]


@pytest.mark.parametrize("route,omit,combo", list(itertools.product(ROUTES, OMITTED, COMBOS)))
def test_class_f1_a_documented_input_a_route_may_omit_is_never_skipped_in_silence(store, meta, slack, route, omit, combo):
    # CLASS-TABLE: F1 routes x omitted field x tenant sink combination
    priv, pub = keypair()
    cfg = make_cfg(
        slack.url if "slack" in combo else None, None, extra_sinks={"capi": capi_sink_cfg()} if "capi" in combo else None,
        env_extra={"ACME_META_URL": meta.url, "ACME_META_TOKEN": TOKEN, "ACME_PIXEL_ID": PIXEL_ID, "RELAY_TEST_GHL_PUBLIC_KEY": pub},
    )
    client = create_app(cfg, Deps(store=store)).test_client()
    registry = {"capi": CapiSink(), "slack": SlackSink()}
    meta.clock = time.time
    raw = json.dumps(documented_body(1, omit)).encode()
    if route == "workflow":
        res = client.post(f"/hooks/{TENANT}/workflow", data=raw, headers={"X-Relay-Secret": WORKFLOW_SECRET, "Content-Type": "application/json"})
    elif route == "ingest":
        res = client.post("/ingest", data=raw, headers={"Authorization": f"Bearer {INGEST_TOKEN}", "Idempotency-Key": "k1", "Content-Type": "application/json"})
    else:
        res = client.post("/webhooks/ghl", data=raw, headers={"X-GHL-Signature": sign(priv, raw), "Content-Type": "application/json"})
    assert res.status_code == 200, (route, omit, combo, res.get_json())  # an accepted payload stays accepted
    make_rig(store, cfg, registry=registry).settle()
    states = {d["sink"]: d for d in store.deliveries()}
    assert set(states) == set(combo)
    if "slack" in combo:
        assert states["slack"]["state"] == "done"  # the missing field never costs the lead its Slack post
    alerts = alert_rows(store)
    if "capi" not in combo:
        assert alerts == []
        return
    assert states["capi"]["state"] == CAPI_STATE[omit], (route, omit, combo)
    type_alerts = [a for a in alerts if a["sink"] == "capi"]
    if omit == "type":
        assert meta.requests == [] and "type" in states["capi"]["last_error"]
        assert len(type_alerts) == 1 and type_alerts[0]["kind"] == "skipped" and "type" in type_alerts[0]["reason"]
        assert type_alerts[0]["event_id"] == states["capi"]["event_id"] and type_alerts[0]["tenant"] == TENANT
    else:
        assert type_alerts == []  # every other omission follows the sink's own, already stored, rules without an alert


# ---- F3: every sink, every source, the tenant's own fathom setting --------------------------------------------------------------

F3_SINKS = ("slack", "make", "capi", "sheets")
F3_SOURCES = ("ingest", "workflow", "ghl-direct", "fathom")
F3_FATHOM_SLACK = (True, False)


def f3_cfg(rsa_key, fathom_slack: bool):
    env = env_for("http://127.0.0.1:9/slack", "http://127.0.0.1:9/make", ACME_FATHOM_SECRET=WEBHOOK_SECRET)
    env.update(
        ACME_META_URL="http://127.0.0.1:9/graph", ACME_META_TOKEN=TOKEN, ACME_PIXEL_ID=PIXEL_ID, ACME_SHEETS_URL="http://127.0.0.1:9/sheets",
        ACME_SHEET_ID=SPREADSHEET_ID, ACME_SA_JSON=service_account_json(rsa_key, "http://127.0.0.1:9/token"),
    )
    doc = json.loads(config_json(extra_sinks={
        "capi": capi_sink_cfg(),
        "sheets": {"url_env": "ACME_SHEETS_URL", "env": {"spreadsheet_id": "ACME_SHEET_ID", "service_account": "ACME_SA_JSON"}, "options": {"sheet": "Sheet1"}},
    }))
    doc["tenants"][TENANT]["fathom"] = {"secret_env": "ACME_FATHOM_SECRET", "slack": fathom_slack}
    env["RELAY_CONFIG"] = json.dumps(doc)
    return load_config(env)


def admin_post(client, path: str, body: dict):
    return client.post(path, data=json.dumps(body), headers={"Authorization": f"Bearer {ADMIN_TOKEN}", "Content-Type": "application/json"})


@pytest.mark.parametrize("fathom_slack", F3_FATHOM_SLACK)
def test_class_f3_a_backfill_gives_each_sink_only_the_sources_it_accepts(store, rsa_key, fathom_slack):
    # CLASS-TABLE: F3 sinks x sources (every lead route, and the meeting) x the tenant's fathom slack setting
    client = create_app(f3_cfg(rsa_key, fathom_slack), Deps(store=store)).test_client()
    for source in F3_SOURCES:  # events that arrived before any sink was enabled for them: no delivery rows yet
        doc = {"recording_id": "5001", "title": "Weekly sync", "summary": "notes"} if source == "fathom" else payload(1)
        store.ingest(key=f"ev-{source}", tenant=TENANT, source=source, basis="native", payload=json.dumps(doc), sinks=[])
    assert store.deliveries() == []
    got: set[tuple[str, str]] = set()
    for sink in F3_SINKS:
        res = admin_post(client, "/admin/backfill", {"tenant": TENANT, "sink": sink, "since": "2000-01-01T00:00:00Z"})
        assert res.status_code == 200, (sink, res.get_json())
        got |= {(d["event_id"], d["sink"]) for d in store.deliveries() if d["sink"] == sink}
    expected = {
        (f"ev-{source}", sink)
        for sink, source in itertools.product(F3_SINKS, F3_SOURCES)
        if source != "fathom" or (sink == "slack" and fathom_slack)  # a meeting reaches Slack only, and only when the tenant opted in
    }
    assert got == expected


# ---- F7: a row layout with an empty cell inside the row must never split the Sheets table --------------------------------------------

F7_OPTIONAL = ("email", "phone", "city", "state", "source", "tags", "utm_source", "utm_medium", "utm_campaign", "utm_content", "utm_term", "fbclid", "gclid")
F7_LAYOUTS = {
    "default": None,  # the shipped default columns
    "optional-in-the-middle": ["event_id", "name", "utm_term", "fbclid", "gclid", "email"],
    "optional-first-then-gaps": ["event_id", "tags", "utm_content", "utm_campaign", "utm_source", "phone", "city"],
}
F7_LEADS = 3


@pytest.mark.parametrize("layout,omitted", list(itertools.product(F7_LAYOUTS, F7_OPTIONAL)))
def test_class_f7_a_column_that_is_empty_in_every_row_never_splits_the_sheets_table(store, sheets, sa_json, layout, omitted):
    # CLASS-TABLE: F7 row layouts (default and custom) x every optional field left out of every lead
    options = {"sheet": "Sheet1", **({"columns": F7_LAYOUTS[layout]} if F7_LAYOUTS[layout] else {})}
    sink_cfg = {"url_env": "ACME_SHEETS_URL", "env": {"spreadsheet_id": "ACME_SHEET_ID", "service_account": "ACME_SA_JSON"}, "options": options, "timeout_s": 5.0}
    cfg = make_cfg(None, None, extra_sinks={"sheets": sink_cfg}, env_extra={"ACME_SHEETS_URL": sheets.url, "ACME_SHEET_ID": SPREADSHEET_ID, "ACME_SA_JSON": sa_json})
    rig = make_rig(store, cfg, registry={"sheets": SheetsSink()})
    fields = {"fbclid": "IwAR2Ab_c-D", "gclid": "Cj0KCQ", "utm_source": "facebook", "utm_medium": "cpc", "utm_campaign": "spring", "utm_content": "ad1", "utm_term": "roof"}
    ids = []
    for i in range(1, F7_LEADS + 1):
        lead = {**fields, omitted: None}  # the omitted field is missing from EVERY lead, so its column is empty in every row
        ids.append(ingest_one(store, cfg, payload(i, **lead)).event_id)
    rig.settle()
    assert [d["state"] for d in store.deliveries()] == ["done"] * F7_LEADS, (layout, omitted)
    assert alert_rows(store) == []  # no dead row, no pause
    columns = cfg.tenants[TENANT].sinks["sheets"].extra["columns"]
    assert len(sheets.column_values(1)) == F7_LEADS and set(sheets.column_values(1)) == set(ids)  # one row per lead, all in column A
    assert max(col for (_row, col) in sheets.cells) <= len(columns)  # nothing past the last configured column
    for row in range(1, F7_LEADS + 1):
        stored = store.get_event(sheets.cells[(row, 1)])
        lead = ghl.normalise_lead(json.loads(stored["payload"]))
        event = Event(event_id=stored["event_id"], tenant=TENANT, source=stored["source"], payload={}, lead=lead, received_epoch=stored["received_epoch"])
        expected = SheetsSink.row_for(event, columns)
        while expected and expected[-1] == "":
            expected.pop()
        assert sheets.row_values(row) == expected, (layout, omitted, row)  # every value in its own column
