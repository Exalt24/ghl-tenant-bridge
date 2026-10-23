"""History import and the suppression list: the CLI (dry run, apply, all or nothing, idempotence, refusals) and the send time check
that refuses a suppressed or already contacted person and stores why.

Every send time test runs a positive control first: an unlisted contact IS delivered by the same rig, so a refusal below can
never be a rig that delivers nothing."""

from __future__ import annotations

import pytest

from relay.import_history import main, read_plan, run_import
from relay.reconcile import CsvError

from .helpers import env_for, ingest_one, make_cfg, make_rig, payload

PHONE_A = "(555) 010-1234"
PHONE_A_E164 = "+15550101234"


def write(tmp_path, text, name="in.csv", encoding="utf-8"):
    path = tmp_path / name
    path.write_bytes(text.encode(encoding) if isinstance(text, str) else text)
    return str(path)


def run(store, argv, capsys, tmp_path=None):
    env = env_for(None, None)
    code = main(argv, environ=env, store=store)
    out = capsys.readouterr()
    return code, out.out, out.err


def counts(store):
    with store.tx(write=False) as t:
        return (
            t.execute("SELECT COUNT(*) AS n FROM sent_history").fetchone()["n"],
            t.execute("SELECT COUNT(*) AS n FROM suppression_list").fetchone()["n"],
        )


HISTORY = "contact,kind\n(555) 010-1234,ContactCreate\npat@example.com,ContactCreate\n+63 917 123 4567,ContactCreate\n"


# ---- the CLI --------------------------------------------------------------------------------------------------------------------


def test_a_dry_run_is_the_default_reports_every_number_and_writes_nothing(store, tmp_path, capsys):
    code, out, err = run(store, [write(tmp_path, HISTORY)], capsys)
    assert code == 0 and err == ""
    assert "DRY RUN, nothing was written" in out
    for line in ("rows read: 3", "would insert: 3", "duplicates: 0", "invalid: 0", "suppressed: 0"):
        assert line in out
    assert counts(store) == (0, 0)


def test_apply_writes_the_rows_and_a_second_apply_adds_zero(store, tmp_path, capsys):
    path = write(tmp_path, HISTORY)
    code, out, _ = run(store, [path, "--apply"], capsys)
    assert code == 0 and "APPLIED" in out and "inserted: 3" in out
    assert counts(store) == (3, 0)
    code, out, _ = run(store, [path, "--apply"], capsys)
    assert code == 0 and "inserted: 0" in out and "duplicates: 3" in out
    assert counts(store) == (3, 0)  # the primary key, not a check then write: the count did not move
    code, out, _ = run(store, [path], capsys)  # and a dry run after the import says there is nothing left to do
    assert "would insert: 0" in out and "duplicates: 3" in out


def test_two_spellings_of_one_contact_are_one_row_and_the_second_is_a_duplicate(store, tmp_path, capsys):
    path = write(tmp_path, f"contact,kind\n{PHONE_A},ContactCreate\n+15550101234,ContactCreate\nPAT@Example.com,ContactCreate\npat@example.com ,ContactCreate\n")
    code, out, _ = run(store, [path, "--apply"], capsys)
    assert code == 0 and "rows read: 4" in out and "inserted: 2" in out and "duplicates: 2" in out
    with store.tx(write=False) as t:
        keys = sorted(r["contact_key"] for r in t.execute("SELECT contact_key FROM sent_history").fetchall())
    assert keys == ["email:pat@example.com", "phone:" + PHONE_A_E164]


def test_one_contact_with_two_message_kinds_is_two_rows(store, tmp_path, capsys):
    code, out, _ = run(store, [write(tmp_path, f"contact,kind\n{PHONE_A},ContactCreate\n{PHONE_A},AppointmentCreate\n"), "--apply"], capsys)
    assert "inserted: 2" in out and counts(store) == (2, 0)


def test_a_refused_row_is_reported_with_its_line_and_reason_never_dropped_and_never_the_contact(store, tmp_path, capsys):
    text = "contact,kind\n(555) 010-1234,ContactCreate\n555-0101,ContactCreate\n\nnot an email@,ContactCreate\n+15550109999,ContactCreate,extra\npat@example.com,\n"
    code, out, err = run(store, [write(tmp_path, text), "--kind", "ContactCreate"], capsys)
    assert code == 1
    assert "rows read: 5" in out and "would insert: 2" in out and "invalid: 3" in out
    assert "  line 3: phone is not a valid US number: 7 digits" in out
    assert "  line 5: email is not a valid address" in out
    assert "  line 6: the row has 3 cells and the header has 2" in out
    assert "5550101" not in out and "not an email" not in out and "15550109999" not in out  # the report names lines, not people
    assert counts(store) == (0, 0)


def test_apply_with_a_refused_row_writes_nothing_unless_told_to_skip_it(store, tmp_path, capsys):
    path = write(tmp_path, "contact,kind\n(555) 010-1234,ContactCreate\n555-0101,ContactCreate\n")
    code, out, err = run(store, [path, "--apply"], capsys)
    assert code == 1 and out == "" and "nothing was written" in err and "line 3" in err
    assert counts(store) == (0, 0)  # the good row was not written either: a do-not-contact list with a hole is worse than none
    code, out, err = run(store, [path, "--apply", "--skip-invalid"], capsys)
    assert code == 1 and "inserted: 1" in out and "invalid: 1" in out and "  line 3:" in out  # still loud, still non-zero
    assert counts(store) == (1, 0)


def test_the_import_is_all_or_nothing_a_failure_part_way_rolls_back_every_row(store, tmp_path):
    plan = read_plan(write(tmp_path, HISTORY), "history", "US")
    calls = []

    def boom():
        calls.append(1)
        if len(calls) == 3:
            raise RuntimeError("disk full")

    store.faults["import_mid"] = boom
    with pytest.raises(RuntimeError):
        run_import(store, plan, apply=True)
    assert len(calls) == 3 and counts(store) == (0, 0)  # two rows had been inserted when it failed, none survived
    store.faults.clear()
    report = run_import(store, plan, apply=True)  # positive control: without the fault the same plan writes all three
    assert report.inserted == 3 and counts(store) == (3, 0)


@pytest.mark.parametrize("name,content,message", [
    ("empty file", "", "the file is empty"),
    ("header only", "contact,kind\n", "no rows"),
    ("header and blank lines only", "contact,kind\n\n  ,\n\n", "no rows"),
    ("no contact column", "phone,kind\n+15550101234,ContactCreate\n", "no column named contact"),
    ("repeated header", "contact,contact,kind\n+15550101234,x,ContactCreate\n", "repeats a column"),
])
def test_a_short_or_headless_file_is_refused_never_read_as_nothing_to_import(store, tmp_path, capsys, name, content, message):
    code, out, err = run(store, [write(tmp_path, content), "--apply"], capsys)
    assert code == 2 and out == "" and message in err, (name, err)
    assert counts(store) == (0, 0)
    good = write(tmp_path, "contact,kind\n+15550101234,ContactCreate\n", name="good.csv")  # positive control: a sound file passes
    assert run(store, [good, "--apply"], capsys)[0] == 0 and counts(store) == (1, 0)


def test_a_file_cut_short_is_caught_by_the_expected_row_count(store, tmp_path, capsys):
    path = write(tmp_path, HISTORY)
    code, out, err = run(store, [path, "--expect-rows", "5", "--apply"], capsys)
    assert code == 2 and "3 rows and --expect-rows said 5" in err and counts(store) == (0, 0)
    assert run(store, [path, "--expect-rows", "3", "--apply"], capsys)[0] == 0 and counts(store) == (3, 0)


def test_a_missing_file_a_utf16_file_and_a_bad_region_are_refused_with_a_message(store, tmp_path, capsys):
    code, _, err = run(store, [str(tmp_path / "nope.csv")], capsys)
    assert code == 2 and "cannot read it" in err
    code, _, err = run(store, [write(tmp_path, "contact,kind\n+15550101234,x\n", name="u16.csv", encoding="utf-16")], capsys)
    assert code == 2 and "UTF-16" in err
    code, _, err = run(store, [write(tmp_path, HISTORY), "--region", "ZZ"], capsys)
    assert code == 2 and "--region" in err
    assert counts(store) == (0, 0)


def test_a_history_row_needs_a_message_kind_from_the_file_or_the_flag(store, tmp_path, capsys):
    path = write(tmp_path, "contact\n(555) 010-1234\n")
    code, _, err = run(store, [path, "--apply"], capsys)
    assert code == 2 and "needs a message kind" in err and counts(store) == (0, 0)
    code, out, _ = run(store, [path, "--kind", "ContactCreate", "--apply"], capsys)
    assert code == 0 and "inserted: 1" in out
    mixed = write(tmp_path, "contact,kind\n+15550101111,\n+15550102222,AppointmentCreate\n", name="mixed.csv")
    code, out, _ = run(store, [mixed, "--kind", "ContactCreate", "--apply"], capsys)  # an empty cell falls back to the flag
    assert code == 0 and "inserted: 2" in out
    with store.tx(write=False) as t:
        kinds = {(r["contact_key"], r["message_kind"]) for r in t.execute("SELECT contact_key, message_kind FROM sent_history").fetchall()}
    assert ("phone:+15550101111", "ContactCreate") in kinds and ("phone:+15550102222", "AppointmentCreate") in kinds


def test_a_suppression_list_has_a_reason_a_default_and_keeps_the_first_reason(store, tmp_path, capsys):
    path = write(tmp_path, "contact,reason\n(555) 010-1234,asked to stop\n+15550101234,duplicate of the above\npat@example.com,\n")
    code, out, _ = run(store, [path, "--list", "suppression", "--apply"], capsys)
    assert code == 0 and "inserted: 2" in out and "duplicates: 1" in out and counts(store) == (0, 2)
    with store.tx(write=False) as t:
        reasons = {r["contact_key"]: r["reason"] for r in t.execute("SELECT contact_key, reason FROM suppression_list").fetchall()}
    assert reasons == {"phone:" + PHONE_A_E164: "asked to stop", "email:pat@example.com": "do-not-contact"}
    code, out, _ = run(store, [path, "--list", "suppression", "--apply"], capsys)
    assert "inserted: 0" in out and counts(store) == (0, 2)


def test_a_suppression_list_needs_no_message_kind(store, tmp_path, capsys):
    code, out, _ = run(store, [write(tmp_path, "contact\n(555) 010-1234\n"), "--list", "suppression", "--apply"], capsys)
    assert code == 0 and counts(store) == (0, 1)


def test_history_rows_of_a_suppressed_contact_are_counted_as_suppressed_and_still_imported(store, tmp_path, capsys):
    run(store, [write(tmp_path, "contact\n+15550101234\n", name="dnc.csv"), "--list", "suppression", "--apply"], capsys)
    code, out, _ = run(store, [write(tmp_path, HISTORY), "--apply"], capsys)
    assert code == 0 and "suppressed: 1" in out and "inserted: 3" in out and counts(store) == (3, 1)


def test_excel_files_are_read_utf8_with_a_mark_and_windows_1252(store, tmp_path, capsys):
    bom = write(tmp_path, b"\xef\xbb\xbfcontact,kind\n(555) 010-1234,ContactCreate\n", name="bom.csv")
    assert run(store, [bom, "--apply"], capsys)[0] == 0
    legacy = write(tmp_path, "contact,kind\nrené@example.com,ContactCreate\n", name="legacy.csv", encoding="cp1252")
    code, out, _ = run(store, [legacy], capsys)
    assert code == 0 and "would insert: 1" in out  # an accented local part is a valid address and the Windows-1252 byte was read as one


def test_a_line_number_is_the_physical_line_even_after_a_quoted_cell_that_spans_lines(tmp_path):
    plan = read_plan(write(tmp_path, 'contact,kind\n"(555) 010-1234",ContactCreate\n"line one\nline two",ContactCreate\n555-0101,ContactCreate\n'), "history", "US")
    assert [n for n, _ in plan.invalid] == [4, 5]


def test_a_default_region_of_the_deployment_reads_a_national_number_there(store, tmp_path, capsys):
    env = env_for(None, None, RELAY_DEFAULT_REGION="PH")
    code = main([write(tmp_path, "contact,kind\n0917 123 4567,ContactCreate\n"), "--apply"], environ=env, store=store)
    assert code == 0
    with store.tx(write=False) as t:
        assert t.execute("SELECT contact_key FROM sent_history").fetchone()["contact_key"] == "phone:+639171234567"
    assert main([write(tmp_path, "contact,kind\n0917 123 4567,ContactCreate\n", name="b.csv"), "--region", "US"], environ=env, store=store) == 1  # the flag wins, and a PH number is not a US one


def test_a_bad_setup_is_a_message_not_a_traceback(tmp_path, capsys):
    env = env_for(None, None, RELAY_DEFAULT_REGION="ZZ")
    assert main([write(tmp_path, HISTORY)], environ=env) == 2
    assert "RELAY_DEFAULT_REGION" in capsys.readouterr().err


# ---- the send time check -------------------------------------------------------------------------------------------------------


def seed_lists(store, tmp_path, capsys, history=None, suppression=None):
    if history:
        assert run(store, [write(tmp_path, history, name="h.csv"), "--apply"], capsys)[0] == 0
    if suppression:
        assert run(store, [write(tmp_path, suppression, name="s.csv"), "--list", "suppression", "--apply"], capsys)[0] == 0


def delivered(store, event_id, sink):
    return store.delivery(event_id, sink)


def test_control_an_unlisted_contact_is_delivered_by_the_same_rig(store, slack, make):
    cfg = make_cfg(slack.url, make.url)
    rig = make_rig(store, cfg)
    r = ingest_one(store, cfg, payload(1))
    rig.settle()
    assert (slack.effects, make.effects) == (1, 1) and rig.states() == {"done": 2}


def test_a_suppressed_contact_is_refused_at_send_time_and_the_reason_is_stored_never_a_silent_skip(store, slack, make, tmp_path, capsys):
    cfg = make_cfg(slack.url, make.url)
    rig = make_rig(store, cfg)
    ok = ingest_one(store, cfg, payload(1))  # positive control inside the test: a neighbour is sent in the same pass
    seed_lists(store, tmp_path, capsys, suppression="contact,reason\n+14155550199,asked to stop\n")
    blocked = ingest_one(store, cfg, payload(2, phone="(415) 555-0199", email="other@example.com"))  # another spelling of the listed number
    rig.settle()
    assert (slack.effects, make.effects) == (1, 1)  # only the neighbour reached either vendor
    assert len(slack.requests) == 1 and len(make.requests) == 1  # and the refused lead never even made a request
    assert {d["state"] for d in store.deliveries(ok.event_id)} == {"done"}
    for sink in ("slack", "make"):
        d = delivered(store, blocked.event_id, sink)
        assert d["state"] == "skipped" and d["effect_ref"] == "suppressed"
        assert d["last_error"] == "suppressed: asked to stop" and d["attempts"] == 0 and d["finished_at"] is not None
        (attempt,) = store.attempts_for(blocked.event_id, sink)
        assert attempt["note"] == "suppressed: asked to stop" and attempt["ok"] == 0
    assert "4155550199" not in " ".join(store.dump_all_text()).replace("+14155550199", "")  # the contact is in the list, not in the delivery's reason


def test_a_contact_is_suppressed_by_its_email_alone(store, slack, make, tmp_path, capsys):
    cfg = make_cfg(slack.url, make.url)
    rig = make_rig(store, cfg)
    seed_lists(store, tmp_path, capsys, suppression="contact\nPat9@Example.com\n")
    r = ingest_one(store, cfg, payload(9, phone="+14155550123"))  # payload(9) carries pat9@example.com
    rig.settle()
    assert slack.effects == 0 and make.effects == 0
    assert delivered(store, r.event_id, "slack")["last_error"] == "suppressed: do-not-contact"


def test_a_contact_already_sent_this_message_kind_is_not_sent_again_but_another_kind_or_person_is(store, slack, make, tmp_path, capsys):
    cfg = make_cfg(slack.url, make.url)
    rig = make_rig(store, cfg)
    seed_lists(store, tmp_path, capsys, history="contact,kind\n(415) 555-0100,ContactCreate\n")
    again = ingest_one(store, cfg, payload(1))  # payload() defaults to phone +14155550100 and type ContactCreate
    other_kind = ingest_one(store, cfg, payload(2, type="ContactUpdate"))
    other_person = ingest_one(store, cfg, payload(3, phone="+14155550777", email="else@example.com"))
    rig.settle()
    for sink in ("slack", "make"):
        d = delivered(store, again.event_id, sink)
        assert d["state"] == "skipped" and d["effect_ref"] == "already_sent" and d["last_error"] == "already sent before the relay"
        assert delivered(store, other_kind.event_id, sink)["state"] == "done"
        assert delivered(store, other_person.event_id, sink)["state"] == "done"
    assert (slack.effects, make.effects) == (2, 2)


def test_suppression_wins_over_history(store, slack, make, tmp_path, capsys):
    cfg = make_cfg(slack.url, make.url)
    rig = make_rig(store, cfg)
    seed_lists(store, tmp_path, capsys, history="contact,kind\n+14155550100,ContactCreate\n", suppression="contact,reason\n+14155550100,legal hold\n")
    r = ingest_one(store, cfg, payload(1))
    rig.settle()
    assert delivered(store, r.event_id, "slack")["effect_ref"] == "suppressed" and delivered(store, r.event_id, "slack")["last_error"] == "suppressed: legal hold"


def test_a_lead_with_no_contact_fields_is_not_blocked_by_either_list(store, slack, make, tmp_path, capsys):
    cfg = make_cfg(slack.url, make.url)
    rig = make_rig(store, cfg)
    seed_lists(store, tmp_path, capsys, history="contact,kind\n+14155550100,ContactCreate\n", suppression="contact\n+14155550100\n")
    r = ingest_one(store, cfg, payload(1, phone=None, email=None))
    rig.settle()
    assert rig.states() == {"done": 2} and slack.effects == 1


def test_a_list_that_cannot_be_read_never_reads_as_an_empty_one_the_row_waits_and_nothing_is_sent(store, slack, make, monkeypatch):
    cfg = make_cfg(slack.url, make.url)
    rig = make_rig(store, cfg)
    r = ingest_one(store, cfg, payload(1))

    def broken(keys, kind):
        raise RuntimeError("connection reset")

    real = store.contact_block
    monkeypatch.setattr(store, "contact_block", broken)
    rig.worker.run_once(wait_horizon_s=0)
    assert (slack.requests, make.requests) == ([], [])
    d = delivered(store, r.event_id, "slack")
    assert d["state"] == "pending" and d["retries"] == 1 and d["last_error"] == "contact check failed: RuntimeError"
    monkeypatch.setattr(store, "contact_block", real)  # the database is back: the same row is now sent, once
    rig.settle()
    assert rig.states() == {"done": 2} and (slack.effects, make.effects) == (1, 1)


def test_the_store_answers_for_any_of_a_leads_keys(store, tmp_path, capsys):
    seed_lists(store, tmp_path, capsys, history="contact,kind\n+14155550100,ContactCreate\n", suppression="contact\nx@example.com\n")
    assert store.contact_block(["phone:+19999999999"], "ContactCreate") is None
    assert store.contact_block([], "ContactCreate") is None
    assert store.contact_block(["email:x@example.com", "phone:+19999999999"], "ContactCreate") == ("suppressed", "do-not-contact")
    assert store.contact_block(["email:y@example.com", "phone:+14155550100"], "ContactCreate") == ("already_sent", "already sent before the relay")
    assert store.contact_block(["phone:+14155550100"], "ContactUpdate") is None
    assert store.suppressed_among(["email:x@example.com", "email:z@example.com"]) == {"email:x@example.com"}
    assert store.suppressed_among([]) == set()
    with pytest.raises(ValueError):
        store.import_contacts("nope", [], True)
