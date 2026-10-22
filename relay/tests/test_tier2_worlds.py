"""A seeded property test over random delivery worlds for the Meta and Sheets sinks and the signed direct route.

The dimensions that vary TOGETHER in every world: duplicate deliveries of one HighLevel webhook id (some re-sent byte for
byte, some re-formatted), deliveries in shuffled order, forged and tampered signatures, leads with and without a click id,
vendor failures on both sinks (throttles, 5xx, a 5xx after the vendor already acted, a request that lands late, a hang, a
revoked token, a forbidden sheet, a 400), worker crashes before and after a send, database clock jumps.

Invariants, checked after every world has been left to settle:
  1 no forged or tampered delivery created an event, and a duplicate never created a second one;
  2 no (event, sink) is lost, and every delivery ends terminal with a visible reason when it is not done;
  3 Meta: a done lead is at the fake exactly once, and the number of accepted requests for one lead never exceeds one plus the
    answers that were a 5xx AFTER the vendor acted plus the crashes after a send (a retry is safe, the fake keeps one copy);
  4 Sheets: a done lead has a row, and it has exactly one row unless an append landed LATE, in which case at most one more
    per late append (the fake-observed count never exceeds the ambiguous outcomes plus 1);
  5 a skipped lead (no click id) never reached Meta.
"""

from __future__ import annotations

import base64
import hashlib
import json
import random
import threading
import time
from collections import Counter

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from relay.app import Deps, create_app
from relay.fanout import Hooks
from relay.sinks.capi import CapiSink
from relay.sinks.sheets import SheetsSink

from .fakes.meta_graph import PIXEL_ID, TOKEN
from .fakes.sheets import SPREADSHEET_ID
from .helpers import SimulatedCrash, make_cfg, make_rig, payload

WORLD_STATS: list[dict] = []
META_MODES = ["ok", "ok", "ok", "throttle", "throttle_80004", "temporary", "server_error", "act_then_fail", "act_then_fail", "token_expired", "bad_param", "no_header"]
SHEETS_MODES = ["ok", "ok", "ok", "rate_limit", "server_error", "act_then_fail", "act_then_fail", "forbidden", "late", "hang", "bad_range"]
TOKEN_MODES = ["server_error", "invalid_grant"]


def iso(ts: float) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def world_cfg(meta, sheets, sa_json, pub_b64):
    capi = {"url_env": "ACME_META_URL", "apikey_env": "ACME_META_TOKEN", "env": {"pixel_id": "ACME_PIXEL_ID"}, "options": {"test_event_code": "TEST1"}, "timeout_s": 0.3}
    sht = {"url_env": "ACME_SHEETS_URL", "env": {"spreadsheet_id": "ACME_SHEET_ID", "service_account": "ACME_SA_JSON"}, "options": {"sheet": "Sheet1"}, "timeout_s": 0.3}
    return make_cfg(
        None, None, extra_sinks={"capi": capi, "sheets": sht},
        env_extra={"ACME_META_URL": meta.url, "ACME_META_TOKEN": TOKEN, "ACME_PIXEL_ID": PIXEL_ID, "ACME_SHEETS_URL": sheets.url,
                   "ACME_SHEET_ID": SPREADSHEET_ID, "ACME_SA_JSON": sa_json, "RELAY_TEST_GHL_PUBLIC_KEY": pub_b64},
    )


@pytest.mark.parametrize("seed", range(6))
def test_random_worlds_keep_every_invariant(store, meta, sheets, sa_json, seed):
    rng = random.Random(1000 + seed)
    priv = Ed25519PrivateKey.generate()
    pub = base64.b64encode(priv.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)).decode()
    attacker = Ed25519PrivateKey.generate()
    cfg = world_cfg(meta, sheets, sa_json, pub)
    client = create_app(cfg, Deps(store=store)).test_client()
    meta.clock = time.time

    crashes: Counter = Counter()
    armed = {"after": False, "before": False}
    lock = threading.Lock()

    def after_send(claim, outcome):
        with lock:
            if armed["after"]:
                armed["after"] = False
                crashes[(claim.event_id, claim.sink)] += 1
                raise SimulatedCrash("random crash after send")

    def before_send(claim):
        with lock:
            if armed["before"]:
                armed["before"] = False
                raise SimulatedCrash("random crash before send")

    rig = make_rig(store, cfg, seed=seed, registry={"capi": CapiSink(), "sheets": SheetsSink(rng=random.Random(seed))}, hooks=Hooks(before_send=before_send, after_send=after_send))

    leads: dict[int, dict] = {}  # index -> {"wid", "click", "raw"}
    sent_ok = 0
    duplicates = 0
    forged = 0
    forged_ids: set[str] = set()

    def new_lead() -> int:
        i = len(leads)
        now = time.time()
        click = rng.random() < 0.8
        extra = {"fbclid": f"Click{seed}x{i}", "userAgent": "Mozilla/5.0 (X11; Linux x86_64)", "page_url": f"https://shop.example.com/lp?utm_source=fb&email=p{i}%40x.co"} if click else {}
        wid = f"wh-{seed}-{i}"
        body = json.dumps(payload(seed * 100 + i, webhookId=wid, dateAdded=iso(now - 90), dateUpdated=iso(now - 60), **extra), indent=rng.choice([None, 1, 2])).encode()
        leads[i] = {"wid": wid, "click": click, "raw": body, "contact": f"contact{seed * 100 + i:05d}"}
        return i

    def deliver(i: int, how: str = "good"):
        nonlocal sent_ok, forged
        lead = leads[i]
        raw = lead["raw"]
        if how == "reformatted":  # the same webhook id, different bytes (a re-serialised retry)
            raw = json.dumps(json.loads(raw), indent=3).encode()
        key = attacker if how == "forged" else priv
        body = raw.replace(b"Lead", b"Evil") if how == "tampered" else raw
        res = client.post("/webhooks/ghl", data=body, headers={"X-GHL-Signature": base64.b64encode(key.sign(raw)).decode(), "Content-Type": "application/json"})
        if how in ("forged", "tampered"):
            forged += 1
            assert res.status_code == 401, how
        else:
            assert res.status_code == 200, (how, res.get_json())
            sent_ok += 1

    for _step in range(46):
        op = rng.random()
        if op < 0.30 or not leads:
            if leads and rng.random() < 0.35:  # a retry of an earlier delivery
                i = rng.choice(sorted(leads))
                deliver(i, rng.choice(["good", "reformatted"]))
            else:
                deliver(new_lead())
        elif op < 0.38:
            deliver(rng.choice(sorted(leads)), rng.choice(["forged", "tampered"]))
        elif op < 0.58:
            meta.queue(*[rng.choice(META_MODES) for _ in range(rng.randint(1, 3))])
            sheets.queue(*[rng.choice(SHEETS_MODES) for _ in range(rng.randint(1, 3))])
            if rng.random() < 0.2:
                sheets.queue_token(rng.choice(TOKEN_MODES))
        elif op < 0.86:
            if rng.random() < 0.2:
                armed[rng.choice(["after", "before"])] = True
            try:
                rig.worker.run_once(wait_horizon_s=rng.choice([0.0, 5.0]))
            except SimulatedCrash:
                pass
            if rng.random() < 0.3:  # the slow requests land, then the fake holds the next ones again
                sheets.release.set()
                sheets.release.clear()
        else:
            store.advance(rng.choice([1, 30, 70, 400, 4000]))

    # leave the world to settle: every vendor healthy, every held request released, the database clock moved on
    meta.script.clear()
    sheets.script.clear()
    sheets.token_script.clear()
    meta.mode, sheets.mode = "ok", "ok"
    armed["after"] = armed["before"] = False
    sheets.release.set()
    store.advance(400)
    rig.settle(max_rounds=800)

    # ---- 1: events ---------------------------------------------------------------------------------------------------
    events = store.events()
    assert sorted(e["event_id"] for e in events) == sorted(l["wid"] for l in leads.values()), "an event is missing or an extra one exists"
    assert not any("Evil" in (e["payload"] or "") for e in events)
    delivered_total = sent_ok
    assert store.health_counts()["suppressed"] == delivered_total - len(events)  # every duplicate was recorded, none made an event

    # ---- the fakes' observations ----------------------------------------------------------------------------------------
    meta_ids = Counter(r["json"]["data"][0]["event_id"] for r in meta.requests if r["accepted"])
    acted_failed = Counter(r["json"]["data"][0]["event_id"] for r in meta.requests if r["accepted"] and r["mode"] == "act_then_fail")
    rows = Counter(v for v in sheets.column_values(1) if v)
    late_rows = Counter()
    for r in sheets.requests:
        if r["mode"] == "late" and r["json"]:
            late_rows[r["json"]["values"][0][0]] += 1

    ndead = nskipped = ndone = 0
    for lead in leads.values():
        eid = lead["wid"]
        mid = sha(f"acme|{lead['contact']}|Lead")
        # ---- 2: nothing lost, everything terminal ---------------------------------------------------------------------
        capi_d, sheets_d = store.delivery(eid, "capi"), store.delivery(eid, "sheets")
        assert capi_d is not None and sheets_d is not None, f"lost: {eid}"
        for d in (capi_d, sheets_d):
            assert d["state"] in ("done", "dead", "skipped"), d
            if d["state"] in ("dead", "skipped"):
                assert d["last_error"], d
        ndead += sum(d["state"] == "dead" for d in (capi_d, sheets_d))
        nskipped += capi_d["state"] == "skipped"
        ndone += sum(d["state"] == "done" for d in (capi_d, sheets_d))
        # ---- 3: Meta ------------------------------------------------------------------------------------------------------
        allowed = 1 + acted_failed[mid] + crashes[(eid, "capi")]
        assert meta_ids[mid] <= allowed, (eid, meta_ids[mid], allowed)
        assert len([e for e in meta.events if e["event_id"] == mid]) <= 1  # the fake kept one copy
        if capi_d["state"] == "done":
            assert meta_ids[mid] >= 1
        if capi_d["state"] == "skipped":
            assert not lead["click"] and capi_d["last_error"].startswith("no click id") and meta_ids[mid] == 0  # ---- 5
        if not lead["click"]:
            assert capi_d["state"] == "skipped"
        # ---- 4: Sheets ----------------------------------------------------------------------------------------------------
        assert rows[eid] <= 1 + late_rows[eid], (eid, rows[eid], late_rows[eid])
        if sheets_d["state"] == "done":
            assert rows[eid] >= 1
        if late_rows[eid] == 0 and sheets_d["state"] == "done":
            assert rows[eid] == 1, (eid, rows[eid])

    WORLD_STATS.append({
        "seed": seed, "events": len(events), "dead": ndead, "skipped": nskipped, "done": ndone, "forged": forged,
        "duplicates": delivered_total - len(events), "crashes": sum(crashes.values()),
        "meta_acted_then_failed": sum(acted_failed.values()), "sheets_late": sum(late_rows.values()),
        "sheets_acted_then_failed": sum(1 for r in sheets.requests if r["mode"] == "act_then_fail" and r["accepted"]),
        "meta_requests": len(meta.requests), "paused_seen": sum(1 for r in meta.requests if r["mode"] == "token_expired"),
    })


def test_the_random_worlds_were_not_vacuous():
    """A property test that never meets a failure proves nothing: across the seeds it must have met every kind."""
    if len(WORLD_STATS) < 4:
        pytest.skip("run the whole file for this check: the random worlds did not all run in this selection")
    total = Counter()
    for w in WORLD_STATS:
        total.update({k: v for k, v in w.items() if k != "seed"})
    assert total["events"] > 10 and total["done"] > 20 and total["dead"] > 0 and total["skipped"] > 0, dict(total)
    assert total["forged"] > 0 and total["duplicates"] > 0 and total["crashes"] > 0, dict(total)
    assert total["meta_acted_then_failed"] > 0 and total["sheets_acted_then_failed"] > 0, dict(total)
    assert total["meta_requests"] > total["events"], dict(total)
