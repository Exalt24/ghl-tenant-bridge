"""Payment reconciliation: a pure function over canonical rows, and a CSV loader with a column map.

There is no public API or documented export for the payment processor this was built for, so the loader takes ANY payments
or ledger CSV and a map from the canonical names to that file's headers. No provider's column names appear in this package.

Canonical row: {id, amount_minor, currency, date, bucket}
  id            the key both sides share (an order, an invoice or a charge reference)
  amount_minor  an integer number of minor units (cents). Never a float.
  currency      an ISO 4217 code from relay/money.py
  date          an ISO date, kept for the report, not used to match
  bucket        the ledger category, optional

`reconcile(payments, ledger, *, tolerance_minor=0, expected_bucket=None)` gives the classes below. Each id lands in exactly ONE of
matched, missing_in_ledger, missing_in_payments, amount_mismatch or wrong_bucket; `duplicate` is an extra note about repeated rows.

  rows with one id on one side    an exact repeat (same amount, currency, date and bucket) is a DUPLICATE and counted once in the
                                  totals; rows that differ are the PARTS of a split payment and are summed
  matched                         both sides have the id, the same currency, and the totals differ by at most `tolerance_minor`
  amount_mismatch                 both sides have the id and either the currency differs (reason "currency") or the totals differ
                                  by more than the tolerance (reason "amount")
  wrong_bucket                    amounts match, but with `expected_bucket` set a ledger row of that id carries another bucket
                                  or none. Only the ledger side is judged. An amount mismatch is reported as that and not here.
  missing_in_ledger / _payments   the id is on one side only

Two genuine payments with the same id, amount, currency, date and bucket cannot be told from a repeated row; they are reported
as a duplicate and the operator decides. The result does not depend on the order of the input rows.
"""

from __future__ import annotations

import codecs
import csv
import io
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

from .money import MoneyError, check_currency, to_minor_from_text
from .textsafe import clean_text

__all__ = ["CsvError", "ReconcileError", "Result", "Row", "canonical", "load_csv", "reconcile"]

MAX_ROWS = 1_000_000
CSV_KEYS = {"id", "amount", "currency", "date", "bucket"}


class ReconcileError(ValueError):
    """A readable reason that names the side and the row, never an amount."""


class CsvError(ValueError):
    """A readable reason that names the line and the column, never the cell."""


@dataclass(frozen=True)
class Row:
    id: str
    amount_minor: int
    currency: str
    date: str
    bucket: str | None = None


def canonical(row: Any, where: str = "row") -> Row:
    if isinstance(row, Row):
        row = {"id": row.id, "amount_minor": row.amount_minor, "currency": row.currency, "date": row.date, "bucket": row.bucket}
    if not isinstance(row, Mapping):
        raise ReconcileError(f"{where}: not an object")
    rid = row.get("id")
    if not isinstance(rid, str) or not rid.strip():
        raise ReconcileError(f"{where}: id must be a non empty string")
    amount = row.get("amount_minor")
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise ReconcileError(f"{where}: amount_minor must be an integer number of minor units, never a float")
    try:
        currency = check_currency(row.get("currency"))
    except MoneyError:
        raise ReconcileError(f"{where}: unknown currency") from None
    day = row.get("date")
    try:
        if not isinstance(day, str):
            raise ValueError
        date.fromisoformat(day)
    except ValueError:
        raise ReconcileError(f"{where}: date must be an ISO date") from None
    bucket = row.get("bucket")
    if bucket is not None and not isinstance(bucket, str):
        raise ReconcileError(f"{where}: bucket must be text or absent")
    # A lone half emoji becomes U+FFFD here, so no report built from a row can fail to encode.
    return Row(clean_text(rid).strip(), amount, currency, day, clean_text(bucket) if bucket else None)


@dataclass
class Result:
    matched: list[dict[str, Any]] = field(default_factory=list)
    missing_in_ledger: list[dict[str, Any]] = field(default_factory=list)
    missing_in_payments: list[dict[str, Any]] = field(default_factory=list)
    amount_mismatch: list[dict[str, Any]] = field(default_factory=list)
    duplicate: list[dict[str, Any]] = field(default_factory=list)
    wrong_bucket: list[dict[str, Any]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {name: len(getattr(self, name)) for name in ("matched", "missing_in_ledger", "missing_in_payments", "amount_mismatch", "duplicate", "wrong_bucket")}


@dataclass
class _Side:
    parts: list[Row]  # the distinct rows of one id: the parts of a split payment
    repeats: int  # how many exact copies were dropped

    @property
    def total(self) -> int:
        return sum(r.amount_minor for r in self.parts)

    @property
    def currencies(self) -> set[str]:
        return {r.currency for r in self.parts}


def _group(rows: Iterable[Any], side: str) -> dict[str, _Side]:
    seen: dict[str, dict[Row, int]] = {}
    for n, raw in enumerate(rows, 1):
        r = canonical(raw, f"{side} row {n}")
        seen.setdefault(r.id, {})
        seen[r.id][r] = seen[r.id].get(r, 0) + 1
    return {rid: _Side(parts=sorted(copies, key=lambda r: (r.date, r.amount_minor, r.currency, r.bucket or "")), repeats=sum(c - 1 for c in copies.values())) for rid, copies in seen.items()}


def reconcile(payments: Iterable[Any], ledger: Iterable[Any], *, tolerance_minor: int = 0, expected_bucket: str | None = None) -> Result:
    if isinstance(tolerance_minor, bool) or not isinstance(tolerance_minor, int) or tolerance_minor < 0:
        raise ReconcileError("tolerance_minor must be a non negative integer")
    pay, led = _group(payments, "payments"), _group(ledger, "ledger")
    out = Result()
    for side, groups in (("payments", pay), ("ledger", led)):
        for rid, g in groups.items():
            if g.repeats:
                first = g.parts[0]
                out.duplicate.append({"id": rid, "side": side, "extra_copies": g.repeats, "amount_minor": first.amount_minor, "currency": first.currency, "date": first.date})
    for rid in sorted(set(pay) | set(led)):
        p, l = pay.get(rid), led.get(rid)
        if l is None:
            assert p is not None
            out.missing_in_ledger.append({"id": rid, "amount_minor": p.total, "currency": sorted(p.currencies)[0], "date": p.parts[0].date})
            continue
        if p is None:
            out.missing_in_payments.append({"id": rid, "amount_minor": l.total, "currency": sorted(l.currencies)[0], "date": l.parts[0].date})
            continue
        if len(p.currencies) != 1 or p.currencies != l.currencies:
            out.amount_mismatch.append({"id": rid, "reason": "currency", "payments_minor": p.total, "ledger_minor": l.total, "payments_currency": sorted(p.currencies), "ledger_currency": sorted(l.currencies), "difference_minor": None})
            continue
        currency = next(iter(p.currencies))
        difference = p.total - l.total
        if abs(difference) > tolerance_minor:
            out.amount_mismatch.append({"id": rid, "reason": "amount", "payments_minor": p.total, "ledger_minor": l.total, "payments_currency": [currency], "ledger_currency": [currency], "difference_minor": difference})
            continue
        if expected_bucket is not None and any(r.bucket != expected_bucket for r in l.parts):
            out.wrong_bucket.append({"id": rid, "amount_minor": l.total, "currency": currency, "buckets": sorted({r.bucket or "" for r in l.parts}), "expected_bucket": expected_bucket})
            continue
        out.matched.append({"id": rid, "amount_minor": l.total, "currency": currency, "payments_rows": len(p.parts), "ledger_rows": len(l.parts)})
    out.duplicate.sort(key=lambda d: (d["id"], d["side"]))
    return out


# ---- the CSV loader --------------------------------------------------------------------------------------------------------------


def _read_text(path: str | Path) -> str:
    """The file as text. Excel saves "CSV (Comma delimited)" as Windows-1252 and "CSV UTF-8" as UTF-8 with a byte order mark,
    and other tools write UTF-8 without one, so the three are read: UTF-8 first (with or without the mark), then Windows-1252.
    A file that is neither (or UTF-16, which Excel's "Unicode Text" writes) is a CsvError that names the file and the offset
    of the first byte each reading refused, never a raw UnicodeDecodeError."""
    where = Path(path)
    data = where.read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise CsvError(f"{where.name}: the file is UTF-16 (byte order mark at offset 0): save it as CSV UTF-8")
    mark = len(codecs.BOM_UTF8) if data.startswith(codecs.BOM_UTF8) else 0  # offsets below are in the file's own bytes
    try:
        return data[mark:].decode("utf-8")
    except UnicodeDecodeError as utf8:
        try:
            return data.decode("cp1252")
        except UnicodeDecodeError as legacy:
            raise CsvError(
                f"{where.name}: not UTF-8 (first bad byte at offset {utf8.start + mark}) and not Windows-1252 "
                f"(first bad byte at offset {legacy.start}): save it as CSV UTF-8"
            ) from None


def load_csv(
    path: str | Path,
    column_map: Mapping[str, str],
    *,
    currency: str | None = None,
    date_format: str = "%Y-%m-%d",
    delimiter: str = ",",
) -> list[dict[str, Any]]:
    """Read any CSV into canonical rows. `column_map` maps the canonical names (`id`, `amount`, `date`, and optionally `currency`
    and `bucket`) to the file's header names. Without a `currency` column, pass the one currency of the whole file as `currency`.

    An amount is a plain decimal string turned into minor units with `Decimal` and the currency's exponent: it is refused
    when it has more decimals than the currency allows, when it uses a thousands separator, an exponent, a currency symbol or
    brackets, and a currency that is not in the ISO 4217 list is refused. An error names the line and the column, never the cell."""
    unknown = set(column_map) - CSV_KEYS
    if unknown:
        raise CsvError(f"column_map has unknown keys: {', '.join(sorted(unknown))}")
    for needed in ("id", "amount", "date"):
        if needed not in column_map:
            raise CsvError(f"column_map must name the {needed} column")
    if "currency" not in column_map and currency is None:
        raise CsvError("name a currency column in column_map or pass currency=")
    if "currency" in column_map and currency is not None:
        raise CsvError("give either a currency column or currency=, not both")
    fixed = None
    if currency is not None:
        try:
            fixed = check_currency(currency)
        except MoneyError:
            raise CsvError("currency= is not a known ISO 4217 code") from None

    with io.StringIO(_read_text(path), newline="") as fh:
        reader = csv.reader(fh, delimiter=delimiter)
        try:
            header = [h.strip() for h in next(reader)]
        except StopIteration:
            raise CsvError("the file is empty") from None
        if len(set(header)) != len(header):
            raise CsvError("the header row repeats a column name")
        index: dict[str, int] = {}
        for canon, name in column_map.items():
            if name not in header:
                raise CsvError(f"the file has no column named for {canon}")
            index[canon] = header.index(name)
        rows: list[dict[str, Any]] = []
        for line_no, cells in enumerate(reader, 2):
            if not cells or all(not c.strip() for c in cells):
                continue
            if len(rows) >= MAX_ROWS:
                raise CsvError("the file has more rows than the loader reads")
            if len(cells) != len(header):
                raise CsvError(f"line {line_no}: the row has {len(cells)} cells and the header has {len(header)}")

            def cell(key: str, _cells: list[str] = cells) -> str:
                return _cells[index[key]].strip()

            code = fixed if fixed is not None else cell("currency").upper()
            try:
                check_currency(code)
            except MoneyError:
                raise CsvError(f"line {line_no}: unknown currency") from None
            try:
                minor = to_minor_from_text(cell("amount"), code, exact=True, allow_negative=True)
            except MoneyError as exc:
                raise CsvError(f"line {line_no}: amount refused: {exc}") from None
            try:
                day = datetime.strptime(cell("date"), date_format).date().isoformat()
            except ValueError:
                raise CsvError(f"line {line_no}: date does not match {date_format}") from None
            rid = cell("id")
            if not rid:
                raise CsvError(f"line {line_no}: id is empty")
            bucket = cell("bucket") if "bucket" in index else ""
            rows.append({"id": rid, "amount_minor": minor, "currency": code, "date": day, "bucket": bucket or None})
    return rows
