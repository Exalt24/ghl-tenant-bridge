"""Reconciliation: the pure function, the CSV loader, and seeded property tests (shuffle, duplicate rows, split payments, rounding,
currency mismatch). Every class has a positive case before its negative one."""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from relay.reconcile import CsvError, ReconcileError, Row, canonical, load_csv, reconcile

RELAY = Path(__file__).resolve().parents[1]


def row(rid, amount, currency="USD", day="2026-10-01", bucket=None):
    return {"id": rid, "amount_minor": amount, "currency": currency, "date": day, "bucket": bucket}


def ids(entries):
    return [e["id"] for e in entries]


def classes(result):
    return {name: ids(getattr(result, name)) for name in ("matched", "missing_in_ledger", "missing_in_payments", "amount_mismatch", "wrong_bucket")}


# ---- the classes ---------------------------------------------------------------------------------------------------------------


def test_identical_sides_match_entirely():
    rows = [row("A", 1000), row("B", 250, bucket="sales")]
    r = reconcile(rows, [dict(x) for x in rows])
    assert ids(r.matched) == ["A", "B"] and r.counts() == {"matched": 2, "missing_in_ledger": 0, "missing_in_payments": 0, "amount_mismatch": 0, "duplicate": 0, "wrong_bucket": 0}
    assert r.matched[0] == {"id": "A", "amount_minor": 1000, "currency": "USD", "payments_rows": 1, "ledger_rows": 1}


def test_an_id_on_one_side_only_is_missing_from_the_other():
    r = reconcile([row("A", 100), row("P", 700)], [row("A", 100), row("L", 900, day="2026-10-05")])
    assert ids(r.matched) == ["A"]
    assert r.missing_in_ledger == [{"id": "P", "amount_minor": 700, "currency": "USD", "date": "2026-10-01"}]
    assert r.missing_in_payments == [{"id": "L", "amount_minor": 900, "currency": "USD", "date": "2026-10-05"}]


def test_a_different_total_is_an_amount_mismatch_with_the_signed_difference():
    r = reconcile([row("A", 1000), row("B", 1000)], [row("A", 1000), row("B", 900)])
    assert ids(r.matched) == ["A"] and ids(r.amount_mismatch) == ["B"]
    m = r.amount_mismatch[0]
    assert (m["reason"], m["payments_minor"], m["ledger_minor"], m["difference_minor"]) == ("amount", 1000, 900, 100)
    assert reconcile([row("B", 900)], [row("B", 1000)]).amount_mismatch[0]["difference_minor"] == -100


def test_the_tolerance_is_inclusive_and_one_more_is_a_mismatch():
    for tol in (0, 1, 5):
        assert ids(reconcile([row("A", 1000 + tol)], [row("A", 1000)], tolerance_minor=tol).matched) == ["A"]
        assert ids(reconcile([row("A", 1000 - tol)], [row("A", 1000)], tolerance_minor=tol).matched) == ["A"]
        r = reconcile([row("A", 1000 + tol + 1)], [row("A", 1000)], tolerance_minor=tol)
        assert ids(r.amount_mismatch) == ["A"] and r.matched == []
        assert ids(reconcile([row("A", 1000 - tol - 1)], [row("A", 1000)], tolerance_minor=tol).amount_mismatch) == ["A"]


def test_the_same_number_in_another_currency_is_never_a_match():
    assert ids(reconcile([row("A", 1000, "USD")], [row("A", 1000, "USD")]).matched) == ["A"]  # control
    r = reconcile([row("A", 1000, "USD")], [row("A", 1000, "EUR")])
    assert r.matched == [] and ids(r.amount_mismatch) == ["A"]
    m = r.amount_mismatch[0]
    assert (m["reason"], m["payments_currency"], m["ledger_currency"], m["difference_minor"]) == ("currency", ["USD"], ["EUR"], None)
    assert reconcile([row("A", 1000, "USD")], [row("A", 1000, "EUR")], tolerance_minor=10**9).matched == []  # no tolerance crosses currencies


def test_one_id_in_two_currencies_on_one_side_is_a_currency_mismatch():
    r = reconcile([row("A", 500, "USD"), row("A", 500, "EUR", day="2026-10-02")], [row("A", 1000, "USD")])
    assert r.matched == [] and r.amount_mismatch[0]["reason"] == "currency" and r.amount_mismatch[0]["payments_currency"] == ["EUR", "USD"]


def test_an_exact_repeat_is_a_duplicate_and_counts_once_in_the_totals():
    r = reconcile([row("A", 1000), row("A", 1000), row("A", 1000)], [row("A", 1000)])
    assert ids(r.matched) == ["A"] and r.amount_mismatch == []  # the repeats did not triple the amount
    assert r.duplicate == [{"id": "A", "side": "payments", "extra_copies": 2, "amount_minor": 1000, "currency": "USD", "date": "2026-10-01"}]
    r = reconcile([row("A", 1000)], [row("A", 1000), row("A", 1000)])
    assert ids(r.matched) == ["A"] and r.duplicate[0]["side"] == "ledger" and r.duplicate[0]["extra_copies"] == 1


def test_a_repeat_on_an_id_that_is_missing_elsewhere_is_both_missing_and_a_duplicate():
    r = reconcile([row("A", 1000), row("A", 1000)], [])
    assert ids(r.missing_in_ledger) == ["A"] and r.missing_in_ledger[0]["amount_minor"] == 1000 and ids(r.duplicate) == ["A"]


def test_split_payments_are_summed_against_the_one_ledger_line():
    parts = [row("A", 400, day="2026-10-01"), row("A", 600, day="2026-10-03")]
    r = reconcile(parts, [row("A", 1000)])
    assert ids(r.matched) == ["A"] and r.matched[0]["payments_rows"] == 2 and r.duplicate == []
    assert ids(reconcile(parts[:1], [row("A", 1000)]).amount_mismatch) == ["A"]  # a part is missing
    equal_parts = [row("A", 500, day="2026-10-01"), row("A", 500, day="2026-10-02")]  # same amount, different dates: two payments
    assert ids(reconcile(equal_parts, [row("A", 1000)]).matched) == ["A"]


def test_a_split_that_arrives_on_both_sides_still_matches():
    parts = [row("A", 400), row("A", 600, day="2026-10-04")]
    assert ids(reconcile(parts, [row("A", 600, day="2026-10-04"), row("A", 400)]).matched) == ["A"]


def test_the_bucket_is_judged_on_the_ledger_side_only_when_one_is_expected():
    pay = [row("A", 100), row("B", 100)]
    led = [row("A", 100, bucket="sales"), row("B", 100, bucket="refunds")]
    assert ids(reconcile(pay, led).matched) == ["A", "B"]  # nothing expected: nothing is wrong
    r = reconcile(pay, led, expected_bucket="sales")
    assert ids(r.matched) == ["A"] and ids(r.wrong_bucket) == ["B"]
    assert r.wrong_bucket[0] == {"id": "B", "amount_minor": 100, "currency": "USD", "buckets": ["refunds"], "expected_bucket": "sales"}
    assert ids(reconcile(pay, [row("A", 100), row("B", 100, bucket="sales")], expected_bucket="sales").wrong_bucket) == ["A"]  # no bucket is not "sales"
    assert ids(reconcile([row("A", 100, bucket="whatever")], [row("A", 100, bucket="sales")], expected_bucket="sales").matched) == ["A"]  # payments are not judged


def test_a_mismatch_is_reported_as_a_mismatch_and_not_also_as_a_wrong_bucket():
    r = reconcile([row("A", 100)], [row("A", 90, bucket="refunds")], expected_bucket="sales")
    assert ids(r.amount_mismatch) == ["A"] and r.wrong_bucket == [] and r.matched == []


def test_a_ledger_split_with_one_part_in_the_wrong_bucket_is_wrong():
    led = [row("A", 400, bucket="sales"), row("A", 600, bucket="refunds", day="2026-10-02")]
    r = reconcile([row("A", 1000)], led, expected_bucket="sales")
    assert ids(r.wrong_bucket) == ["A"] and r.wrong_bucket[0]["buckets"] == ["refunds", "sales"]


def test_ids_are_trimmed_and_rows_may_be_row_objects_and_any_iterable():
    r = reconcile(iter([row(" A1 ", 100)]), (x for x in [Row("A1", 100, "USD", "2026-10-01")]))
    assert ids(r.matched) == ["A1"]


def test_the_result_is_sorted_by_id_whatever_the_input_order():
    names = [f"id{n:02d}" for n in (9, 3, 7, 1, 8, 2, 6, 4, 5, 0)]
    assert ids(reconcile([row(n, 1) for n in names], []).missing_in_ledger) == sorted(names)
    assert ids(reconcile([], [row(n, 1) for n in names]).missing_in_payments) == sorted(names)


# ---- what is refused -------------------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "change,match",
    [
        ({"amount_minor": 12.5}, "never a float"),
        ({"amount_minor": 12.0}, "never a float"),
        ({"amount_minor": "12"}, "integer"),
        ({"amount_minor": True}, "integer"),
        ({"amount_minor": None}, "integer"),
        ({"id": ""}, "id"),
        ({"id": "  "}, "id"),
        ({"id": 5}, "id"),
        ({"currency": "ZZZ"}, "unknown currency"),
        ({"currency": "usd"}, "unknown currency"),
        ({"currency": None}, "unknown currency"),
        ({"date": "10/01/2026"}, "ISO date"),
        ({"date": None}, "ISO date"),
        ({"bucket": 5}, "bucket"),
    ],
)
def test_a_malformed_row_is_refused_with_its_side_and_position(change, match):
    good = row("A", 100)
    assert reconcile([good], [good]).matched  # control
    with pytest.raises(ReconcileError, match=match) as err:
        reconcile([good, {**good, **change}], [good])
    assert "payments row 2" in str(err.value)
    with pytest.raises(ReconcileError, match="ledger row 1"):
        reconcile([good], [{**good, **change}])


def test_a_row_that_is_not_an_object_is_refused():
    with pytest.raises(ReconcileError, match="not an object"):
        canonical(["A", 100])


@pytest.mark.parametrize("tol", [1.0, -1, True, "5", None])
def test_the_tolerance_must_be_a_non_negative_integer(tol):
    assert reconcile([row("A", 1)], [row("A", 1)], tolerance_minor=0).matched
    with pytest.raises(ReconcileError, match="tolerance"):
        reconcile([row("A", 1)], [row("A", 1)], tolerance_minor=tol)


def test_the_inputs_are_not_changed():
    pay, led = [row("A", 100), row("A", 100)], [row("A", 100)]
    before = ([dict(x) for x in pay], [dict(x) for x in led])
    reconcile(pay, led)
    assert (pay, led) == before


def test_no_provider_is_named_anywhere_in_the_package_the_tests_the_fixtures_or_the_docs():
    needle = ("fan" + "basis", "com" + "mas")
    hits = []
    for p in RELAY.rglob("*"):
        if p.is_file() and p.suffix in {".py", ".md", ".json", ".sql", ".txt", ".yml"} and not (set(p.parts) & {".venv", "__pycache__"}) and p != Path(__file__):
            text = p.read_text(encoding="utf-8", errors="ignore").lower()
            hits += [f"{p.relative_to(RELAY)}: {n}" for n in needle if n in text]
    assert hits == []


# ---- the CSV loader ---------------------------------------------------------------------------------------------------------------

MAP = {"id": "Reference", "amount": "Gross", "currency": "Cur", "date": "Paid on", "bucket": "Account"}


def write(tmp_path, text, name="payments.csv", bom=False):
    p = tmp_path / name
    p.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
    return p


def test_a_csv_is_read_through_the_column_map_into_exact_integer_rows(tmp_path):
    p = write(tmp_path, "Reference,Gross,Cur,Paid on,Account,Note\nA1,12.50,usd,2026-10-01,sales,hello\nA2,1500,JPY,2026-10-02,,\"a, b\"\nA3,-45.10,EUR,2026-10-03,refunds,\n\n  , , , ,\n", bom=True)
    rows = load_csv(p, MAP)
    assert rows == [
        {"id": "A1", "amount_minor": 1250, "currency": "USD", "date": "2026-10-01", "bucket": "sales"},
        {"id": "A2", "amount_minor": 1500, "currency": "JPY", "date": "2026-10-02", "bucket": None},
        {"id": "A3", "amount_minor": -4510, "currency": "EUR", "date": "2026-10-03", "bucket": "refunds"},
    ]
    assert all(type(r["amount_minor"]) is int for r in rows)
    assert reconcile(rows, rows).counts()["matched"] == 3  # the loader's rows go straight into reconcile


def test_a_fixed_currency_and_no_bucket_column_work(tmp_path):
    p = write(tmp_path, "ref;amt;when\nX;10.5;03/10/2026\n")
    rows = load_csv(p, {"id": "ref", "amount": "amt", "date": "when"}, currency="PHP", date_format="%d/%m/%Y", delimiter=";")
    assert rows == [{"id": "X", "amount_minor": 1050, "currency": "PHP", "date": "2026-10-03", "bucket": None}]


@pytest.mark.parametrize("amount", ["10.505", "1,234.50", "1e3", "1E3", "$5", "(5.00)", "", "abc", "NaN", "inf", "5.", ".5", "+5", "1 234", "12.5.1"])
def test_an_amount_that_is_not_a_plain_exact_decimal_is_refused_naming_the_line_and_not_the_cell(tmp_path, amount):
    good = write(tmp_path, "Reference,Gross,Cur,Paid on,Account\nA1,10.50,USD,2026-10-01,\n")
    assert load_csv(good, MAP)[0]["amount_minor"] == 1050  # control
    p = write(tmp_path, f"Reference,Gross,Cur,Paid on,Account\nA1,10.50,USD,2026-10-01,\nA2,\"{amount}\",USD,2026-10-02,\n", name="bad.csv")
    with pytest.raises(CsvError) as err:
        load_csv(p, MAP)
    assert "line 3" in str(err.value) and "amount refused" in str(err.value)
    assert amount not in str(err.value) or amount == ""


def test_trailing_zeros_are_not_extra_precision_but_a_fraction_of_a_minor_unit_is(tmp_path):
    p = write(tmp_path, "Reference,Gross,Cur,Paid on,Account\nA,10.500,USD,2026-10-01,\nB,1500.0,JPY,2026-10-01,\n")
    assert [r["amount_minor"] for r in load_csv(p, MAP)] == [1050, 1500]
    with pytest.raises(CsvError, match="more decimals"):
        load_csv(write(tmp_path, "Reference,Gross,Cur,Paid on,Account\nA,1500.5,JPY,2026-10-01,\n", name="y.csv"), MAP)


@pytest.mark.parametrize("cur", ["XXX", "ZZZ", "", "US", "US$", "dollars", "123"])
def test_an_unknown_currency_is_refused(tmp_path, cur):
    p = write(tmp_path, f"Reference,Gross,Cur,Paid on,Account\nA,1.00,{cur},2026-10-01,\n")
    with pytest.raises(CsvError, match="line 2: unknown currency"):
        load_csv(p, MAP)


def test_the_currency_choices_are_checked(tmp_path):
    p = write(tmp_path, "Reference,Gross,Cur,Paid on,Account\nA,1.00,USD,2026-10-01,\n")
    without_col = {k: v for k, v in MAP.items() if k != "currency"}
    assert load_csv(p, without_col, currency="USD")[0]["currency"] == "USD"  # control
    with pytest.raises(CsvError, match="either a currency column or currency="):
        load_csv(p, MAP, currency="USD")
    with pytest.raises(CsvError, match="name a currency column"):
        load_csv(p, without_col)
    with pytest.raises(CsvError, match="not a known ISO 4217"):
        load_csv(p, without_col, currency="ZZZ")


def test_the_map_and_the_header_must_agree(tmp_path):
    p = write(tmp_path, "Reference,Gross,Cur,Paid on,Account\nA,1.00,USD,2026-10-01,\n")
    assert load_csv(p, MAP)  # control
    with pytest.raises(CsvError, match="unknown keys: fee"):
        load_csv(p, {**MAP, "fee": "Fee"})
    for missing in ("id", "amount", "date"):
        with pytest.raises(CsvError, match=f"name the {missing} column"):
            load_csv(p, {k: v for k, v in MAP.items() if k != missing})
    with pytest.raises(CsvError, match="no column named for amount"):
        load_csv(p, {**MAP, "amount": "Net"})
    with pytest.raises(CsvError, match="repeats a column name"):
        load_csv(write(tmp_path, "Reference,Gross,Gross,Cur,Paid on,Account\n", name="d.csv"), MAP)
    with pytest.raises(CsvError, match="empty"):
        load_csv(write(tmp_path, "", name="e.csv"), MAP)


def test_a_ragged_row_a_bad_date_and_an_empty_id_are_refused_with_their_line(tmp_path):
    head = "Reference,Gross,Cur,Paid on,Account\n"
    with pytest.raises(CsvError, match="line 2: the row has 3 cells and the header has 5"):
        load_csv(write(tmp_path, head + "A,1.00,USD\n", name="r.csv"), MAP)
    with pytest.raises(CsvError, match=r"line 3: date does not match %Y-%m-%d"):
        load_csv(write(tmp_path, head + "A,1.00,USD,2026-10-01,\nB,1.00,USD,10/02/2026,\n", name="t.csv"), MAP)
    with pytest.raises(CsvError, match="line 2: id is empty"):
        load_csv(write(tmp_path, head + " ,1.00,USD,2026-10-01,\n", name="i.csv"), MAP)


def test_two_files_with_different_headers_reconcile_end_to_end(tmp_path):
    pay = write(tmp_path, "Reference,Gross,Cur,Paid on,Account\nA,100.00,USD,2026-10-01,\nB,50.00,USD,2026-10-02,\nC,10.00,USD,2026-10-03,\nA,100.00,USD,2026-10-01,\n", name="pay.csv")
    led = write(tmp_path, "inv,total,day,cat\nA,100.00,2026-10-01,sales\nB,49.00,2026-10-02,sales\nD,5.00,2026-10-04,sales\n", name="led.csv")
    payments = load_csv(pay, MAP)
    ledger = load_csv(led, {"id": "inv", "amount": "total", "date": "day", "bucket": "cat"}, currency="USD")
    r = reconcile(payments, ledger, expected_bucket="sales")
    assert classes(r) == {"matched": ["A"], "missing_in_ledger": ["C"], "missing_in_payments": ["D"], "amount_mismatch": ["B"], "wrong_bucket": []}
    assert ids(r.duplicate) == ["A"]


# ---- seeded property tests ----------------------------------------------------------------------------------------------------------

KINDS = ["match", "split", "tolerated", "mismatch", "currency", "only_payments", "only_ledger", "bucket"]


def build_world(rng: random.Random):
    """A random world with a known truth: each id gets a kind, rows are built from it, exact repeats are sprinkled in."""
    tolerance = rng.choice([0, 0, 1, 3])
    pay, led, truth = [], [], {}
    for n in range(rng.randint(5, 40)):
        rid = f"id{n:03d}"
        kind = rng.choice(KINDS)
        amount = rng.randint(1, 10**7)
        currency = rng.choice(["USD", "EUR", "JPY", "PHP"])
        day = f"2026-10-{rng.randint(1, 28):02d}"
        bucket = "sales"
        if kind == "match":
            pay.append(row(rid, amount, currency, day)), led.append(row(rid, amount, currency, day, bucket))
            truth[rid] = "matched"
        elif kind == "split":
            k = rng.randint(2, 4)
            cuts = sorted(rng.sample(range(1, max(amount, k + 1)), k - 1)) if amount > k else []
            sizes = [b - a for a, b in zip([0] + cuts, cuts + [amount])] if cuts else [amount]
            for j, s in enumerate(sizes):
                pay.append(row(rid, s, currency, f"2026-09-{j + 1:02d}"))
            led.append(row(rid, amount, currency, day, bucket))
            truth[rid] = "matched"
        elif kind == "tolerated":
            noise = rng.randint(-tolerance, tolerance)
            pay.append(row(rid, amount + noise, currency, day)), led.append(row(rid, amount, currency, day, bucket))
            truth[rid] = "matched"
        elif kind == "mismatch":
            off = tolerance + rng.randint(1, 500)
            pay.append(row(rid, amount + rng.choice([off, -off]), currency, day)), led.append(row(rid, amount, currency, day, bucket))
            truth[rid] = "amount_mismatch"
        elif kind == "currency":
            other = rng.choice([c for c in ("USD", "EUR", "JPY", "PHP") if c != currency])
            pay.append(row(rid, amount, currency, day)), led.append(row(rid, amount, other, day, bucket))
            truth[rid] = "amount_mismatch"
        elif kind == "only_payments":
            pay.append(row(rid, amount, currency, day))
            truth[rid] = "missing_in_ledger"
        elif kind == "only_ledger":
            led.append(row(rid, amount, currency, day, bucket))
            truth[rid] = "missing_in_payments"
        else:
            pay.append(row(rid, amount, currency, day)), led.append(row(rid, amount, currency, day, rng.choice(["refunds", None])))
            truth[rid] = "wrong_bucket"
    repeats = {"payments": 0, "ledger": 0}
    for side, rows in (("payments", pay), ("ledger", led)):
        for r in rng.sample(rows, min(len(rows), rng.randint(0, 4))):
            rows.append(dict(r))
            repeats[side] += 1
    return pay, led, truth, tolerance, repeats


@pytest.mark.parametrize("seed", range(80))
def test_property_every_id_lands_in_the_class_the_world_was_built_with_whatever_the_order(seed):
    rng = random.Random(seed)
    pay, led, truth, tolerance, repeats = build_world(rng)
    results = []
    for shuffle in range(3):
        p, l = list(pay), list(led)
        random.Random(seed * 10 + shuffle).shuffle(p)
        random.Random(seed * 10 + shuffle + 5).shuffle(l)
        results.append(reconcile(p, l, tolerance_minor=tolerance, expected_bucket="sales"))
    assert results[0] == results[1] == results[2]  # the order of the rows changes nothing
    r = results[0]
    got = {}
    for name in ("matched", "missing_in_ledger", "missing_in_payments", "amount_mismatch", "wrong_bucket"):
        for rid in ids(getattr(r, name)):
            assert rid not in got, f"{rid} is in two classes"
            got[rid] = name
    assert got == truth  # every id exactly once, in the class it was built for
    assert sum(d["extra_copies"] for d in r.duplicate if d["side"] == "payments") == repeats["payments"]
    assert sum(d["extra_copies"] for d in r.duplicate if d["side"] == "ledger") == repeats["ledger"]


@pytest.mark.parametrize("seed", range(30))
def test_property_adding_exact_repeats_never_changes_a_classification(seed):
    rng = random.Random(1000 + seed)
    pay, led, truth, tolerance, _ = build_world(rng)
    base = classes(reconcile(pay, led, tolerance_minor=tolerance, expected_bucket="sales"))
    doubled_payments = pay + [dict(x) for x in rng.sample(pay, min(len(pay), 5))]
    doubled_ledger = led + [dict(x) for x in rng.sample(led, min(len(led), 5))]
    assert classes(reconcile(doubled_payments, led, tolerance_minor=tolerance, expected_bucket="sales")) == base
    assert classes(reconcile(pay, doubled_ledger, tolerance_minor=tolerance, expected_bucket="sales")) == base


@pytest.mark.parametrize("seed", range(30))
def test_property_a_split_matches_only_when_every_part_is_there(seed):
    rng = random.Random(2000 + seed)
    total = rng.randint(10, 10**6)
    k = rng.randint(2, 6)
    cuts = sorted(rng.sample(range(1, total), k - 1))
    parts = [b - a for a, b in zip([0] + cuts, cuts + [total])]
    pay = [row("S", p, day=f"2026-09-{i + 1:02d}") for i, p in enumerate(parts)]
    led = [row("S", total)]
    assert ids(reconcile(pay, led).matched) == ["S"]
    dropped = rng.randrange(k)
    assert ids(reconcile(pay[:dropped] + pay[dropped + 1:], led).amount_mismatch) == ["S"]  # every part is positive, so any missing one shows
    assert ids(reconcile(pay + [row("S", 1, day="2026-09-30")], led).amount_mismatch) == ["S"]  # an extra part shows too


@pytest.mark.parametrize("seed", range(30))
def test_property_rounding_noise_inside_the_tolerance_matches_and_one_cent_more_does_not(seed):
    rng = random.Random(3000 + seed)
    tol = rng.randint(0, 25)
    amount = rng.randint(100, 10**6)
    for noise in range(-tol, tol + 1):
        assert reconcile([row("R", amount + noise)], [row("R", amount)], tolerance_minor=tol).matched
    for noise in (tol + 1, -(tol + 1)):
        assert reconcile([row("R", amount + noise)], [row("R", amount)], tolerance_minor=tol).amount_mismatch


@pytest.mark.parametrize("seed", range(20))
def test_property_a_currency_difference_is_never_a_match_for_any_amount_or_tolerance(seed):
    rng = random.Random(4000 + seed)
    a, b = rng.sample(["USD", "EUR", "GBP", "JPY", "PHP", "AUD"], 2)
    amount = rng.randint(0, 10**9)
    r = reconcile([row("C", amount, a)], [row("C", amount, b)], tolerance_minor=rng.choice([0, 10, 10**12]))
    assert r.matched == [] and r.amount_mismatch[0]["reason"] == "currency"
