"""The store queries tier 3 added, run on SQLite and on Postgres with the same expectations: the spend upsert a 28 day
re-pull relies on, the meeting upsert, and the hook that stores a meeting in the same transaction as its event."""

from __future__ import annotations

import pytest




def spend(day="2026-10-20", campaign="c1", minor=1234, **kw):
    row = {"source": "meta", "account": "act_1", "campaign_id": campaign, "day": day, "spend_minor": minor, "currency": "USD", "impressions": 10, "clicks": 2}
    row.update(kw)
    return row


# ---- spend -------------------------------------------------------------------------------------------------------------------


def test_a_repull_replaces_a_day_on_its_primary_key_and_never_stacks_a_second_copy(store):
    assert store.upsert_spend([spend(minor=1000), spend(day="2026-10-21", minor=500)]) == 2
    assert [r["spend_minor"] for r in store.spend_rows()] == [1000, 500]
    first_pulled = store.spend_rows()[0]["pulled_at"]
    store.advance(3600)
    store.upsert_spend([spend(minor=1500, impressions=99, clicks=7)])  # the vendor restated the day
    rows = store.spend_rows()
    assert len(rows) == 2
    restated = rows[0]
    assert (restated["spend_minor"], restated["impressions"], restated["clicks"]) == (1500, 99, 7)
    assert str(restated["pulled_at"]) != str(first_pulled)  # the pull time moved with the restatement
    assert rows[1]["spend_minor"] == 500  # the other day was left alone


def test_the_primary_key_is_source_account_campaign_and_day(store):
    store.upsert_spend([spend(), spend(source="windsor"), spend(account="act_2"), spend(campaign="c2"), spend(day="2026-10-21")])
    assert len(store.spend_rows()) == 5
    assert len(store.spend_rows("windsor")) == 1 and len(store.spend_rows("meta")) == 4
    store.upsert_spend([spend()])
    assert len(store.spend_rows()) == 5


def test_a_spend_batch_is_one_transaction(store):
    store.upsert_spend([spend(minor=1)])
    bad = [spend(minor=2), {"source": "meta"}]  # the second row has no other keys: the batch must roll back whole
    with pytest.raises(KeyError):
        store.upsert_spend(bad)
    assert [r["spend_minor"] for r in store.spend_rows()] == [1]


def test_missing_impressions_and_clicks_are_stored_as_null(store):
    row = spend()
    del row["impressions"], row["clicks"]
    store.upsert_spend([row])
    got = store.spend_rows()[0]
    assert got["impressions"] is None and got["clicks"] is None


# ---- meetings ----------------------------------------------------------------------------------------------------------------


def meeting(**kw):
    return {"recording_id": "123", "title": "QBR", "summary": "## Summary\nwent well", "action_items": '[{"description": "email"}]', **kw}


def test_a_meeting_refresh_keeps_a_summary_the_new_copy_does_not_carry(store):
    store.upsert_meetings("acme", [meeting()])
    store.upsert_meetings("acme", [meeting(title="QBR renamed", summary=None, action_items=None)])  # the puller asked for less
    row = store.meetings()[0]
    assert (row["title"], row["summary"], row["action_items"]) == ("QBR renamed", "## Summary\nwent well", '[{"description": "email"}]')
    store.upsert_meetings("acme", [meeting(summary="newer summary")])
    assert store.meetings()[0]["summary"] == "newer summary"


def test_a_meeting_of_another_tenant_is_never_overwritten(store):
    store.upsert_meetings("acme", [meeting()])
    store.upsert_meetings("other", [meeting(title="hijacked", summary="x")])
    row = store.meetings()[0]
    assert (row["tenant"], row["title"], row["summary"]) == ("acme", "QBR", "## Summary\nwent well")


# ---- the receiver's transaction ----------------------------------------------------------------------------------------------


def test_the_on_new_hook_runs_inside_the_ingest_transaction_for_a_new_event_only(store):
    calls = []

    def hook(t):
        calls.append(1)
        store.save_meeting(t, tenant="acme", recording_id="9", title="t", summary=None, action_items=None)

    first = store.ingest(key="wh-1", tenant="acme", source="fathom", basis="native", payload="{}", sinks=[], on_new=hook)
    assert first.duplicate is False and calls == [1] and len(store.meetings()) == 1
    again = store.ingest(key="wh-1", tenant="acme", source="fathom", basis="native", payload="{}", sinks=[], on_new=hook)
    assert again.duplicate is True and calls == [1]  # a duplicate delivery does not run it again


def test_a_failing_on_new_hook_rolls_back_the_event_and_its_deliveries(store):
    def boom(t):
        store.save_meeting(t, tenant="acme", recording_id="9", title="t", summary=None, action_items=None)
        raise RuntimeError("after the meeting insert")

    with pytest.raises(RuntimeError):
        store.ingest(key="wh-2", tenant="acme", source="fathom", basis="native", payload="{}", sinks=["slack"], on_new=boom)
    assert store.events() == [] and store.deliveries() == [] and store.meetings() == []
    ok = store.ingest(key="wh-2", tenant="acme", source="fathom", basis="native", payload="{}", sinks=["slack"])  # nothing was left behind
    assert ok.duplicate is False
