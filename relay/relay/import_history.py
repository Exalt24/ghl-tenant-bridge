"""Import a contact list from a CSV: prior outbound contacts (never message them the same thing again) or a suppression list
(never contact them at all).

    python -m relay.import_history history.csv --kind ContactCreate            dry run: a report, nothing written
    python -m relay.import_history history.csv --kind ContactCreate --apply    write it, all or nothing
    python -m relay.import_history dnc.csv --list suppression --apply

The file needs a `contact` column (a phone or an email). A history file also needs a message kind, from a `kind` column or
`--kind`; a suppression file may have a `reason` column. Other columns are ignored. Contacts are normalised by relay/contacts.py
(E.164 phone, lower case email) so `(555) 010-1234` and `+15550101234` are one contact, and the primary key is
(contact, message kind) for history and the contact for suppression, so a second import adds zero rows.

Exit codes: 0 clean, 1 rows were refused (a dry run says so, and --apply then writes nothing unless --skip-invalid), 2 the file
or the setup was refused (no rows, no contact column, unreadable, wrong row count, bad configuration).

A refused row is reported with its line number and reason, never the contact. A file with no data rows, or without the contact
column, is refused: a short read must never look like "nothing to import".
"""

from __future__ import annotations

import argparse
import csv
import io
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from .config import ConfigError, load_config
from .contacts import REGIONS, ContactError, contact_key
from .reconcile import CsvError, _read_text
from .store import open_store
from .store.base import SqlStore

log = logging.getLogger("relay.import_history")

MAX_ROWS = 1_000_000
MAX_KIND_CHARS = 100
MAX_REASON_CHARS = 200
DEFAULT_REASON = "do-not-contact"
LISTS = ("history", "suppression")


@dataclass
class Plan:
    which: str
    rows_read: int = 0
    rows: list[tuple[str, str]] = field(default_factory=list)  # distinct (contact_key, kind or reason), file order
    file_duplicates: int = 0
    invalid: list[tuple[int, str]] = field(default_factory=list)  # (line number, reason)


@dataclass
class Report:
    which: str
    applied: bool
    rows_read: int
    inserted: int  # rows written, or rows that WOULD be written on a dry run
    duplicates: int  # repeats inside the file plus rows the database already holds
    invalid: list[tuple[int, str]]
    suppressed: int  # history rows whose contact is on the suppression list (still imported: it is a fact about the past)

    def lines(self) -> list[str]:
        verb = "inserted" if self.applied else "would insert"
        out = [
            f"{'APPLIED' if self.applied else 'DRY RUN, nothing was written'}: {self.which} list",
            f"rows read: {self.rows_read}",
            f"{verb}: {self.inserted}",
            f"duplicates: {self.duplicates}",
            f"invalid: {len(self.invalid)}",
            f"suppressed: {self.suppressed}",
        ]
        out.extend(f"  line {n}: {why}" for n, why in self.invalid)
        return out


def _clean(cell: str) -> str:
    return cell.strip()


def read_plan(path: str | Path, which: str, region: str, kind: str | None = None, expect_rows: int | None = None) -> Plan:
    """Read and judge every row. Raises CsvError when the file itself must be refused."""
    if which not in LISTS:
        raise CsvError("list must be history or suppression")
    with io.StringIO(_read_text(path), newline="") as fh:
        reader = csv.reader(fh)
        try:
            header = [h.strip().lower() for h in next(reader)]
        except StopIteration:
            raise CsvError("the file is empty") from None
        if len(set(header)) != len(header):
            raise CsvError("the header row repeats a column name")
        if "contact" not in header:
            raise CsvError("the file has no column named contact")
        at = {name: header.index(name) for name in ("contact", "kind", "reason") if name in header}
        if which == "history" and "kind" not in at and not kind:
            raise CsvError("a history file needs a message kind: add a kind column or pass --kind")
        plan = Plan(which)
        seen: set[tuple[str, str]] = set()
        for cells in reader:
            line_no = reader.line_num  # the physical line, so a quoted cell that spans lines does not shift the numbers after it
            if not cells or all(not c.strip() for c in cells):
                continue
            if plan.rows_read >= MAX_ROWS:
                raise CsvError("the file has more rows than the importer reads")
            plan.rows_read += 1
            if len(cells) != len(header):
                plan.invalid.append((line_no, f"the row has {len(cells)} cells and the header has {len(header)}"))
                continue
            try:
                key = contact_key(_clean(cells[at["contact"]]), region)
            except ContactError as exc:
                plan.invalid.append((line_no, str(exc)))
                continue
            if which == "history":
                value = _clean(cells[at["kind"]]) if "kind" in at else ""
                value = value or (kind or "")
                if not value:
                    plan.invalid.append((line_no, "the message kind is empty"))
                    continue
                if len(value) > MAX_KIND_CHARS or not value.isprintable():
                    plan.invalid.append((line_no, "the message kind is too long or has control characters"))
                    continue
            else:
                value = (_clean(cells[at["reason"]]) if "reason" in at else "") or DEFAULT_REASON
                if len(value) > MAX_REASON_CHARS or not value.isprintable():
                    plan.invalid.append((line_no, "the reason is too long or has control characters"))
                    continue
            # A suppression row repeats on its contact alone: the first reason is kept, which is also what the database does.
            ident = (key, value) if which == "history" else (key, "")
            if ident in seen:
                plan.file_duplicates += 1
                continue
            seen.add(ident)
            plan.rows.append((key, value))
    if plan.rows_read == 0:
        raise CsvError("the file has a header and no rows: nothing was read, so nothing is imported")
    if expect_rows is not None and plan.rows_read != expect_rows:
        raise CsvError(f"the file has {plan.rows_read} rows and --expect-rows said {expect_rows}: the file may be cut short")
    return plan


def run_import(store: SqlStore, plan: Plan, apply: bool, skip_invalid: bool = False) -> Report | None:
    """Judge the plan against the database and, when `apply`, write it in one transaction. None means --apply was refused
    because rows were invalid (and nothing was written)."""
    if apply and plan.invalid and not skip_invalid:
        return None
    inserted, existing = store.import_contacts(plan.which, plan.rows, apply)
    suppressed = 0
    if plan.which == "history":
        suppressed = len(store.suppressed_among([k for k, _ in plan.rows]))
    return Report(
        which=plan.which, applied=apply, rows_read=plan.rows_read, inserted=inserted, duplicates=plan.file_duplicates + existing,
        invalid=plan.invalid, suppressed=suppressed,
    )


def main(argv: list[str] | None = None, environ: Mapping[str, str] | None = None, store: SqlStore | None = None) -> int:
    parser = argparse.ArgumentParser(prog="relay.import_history", description=__doc__.split("\n")[0])
    parser.add_argument("file", help="the CSV to import")
    parser.add_argument("--list", dest="which", choices=LISTS, default="history", help="history (default) or suppression")
    parser.add_argument("--kind", default=None, help="the message kind of every history row without a kind column, such as ContactCreate")
    parser.add_argument("--region", default=None, help="how a phone with no country is read (default RELAY_DEFAULT_REGION, else US)")
    parser.add_argument("--expect-rows", type=int, default=None, help="refuse the file unless it holds exactly this many rows")
    parser.add_argument("--apply", action="store_true", help="write the rows (the default is a dry run)")
    parser.add_argument("--skip-invalid", action="store_true", help="with --apply, write the valid rows even when some are refused")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    try:
        cfg = load_config(environ, role="worker")
    except ConfigError as exc:
        print(f"relay.import_history: configuration refused: {exc}", file=sys.stderr)
        return 2
    region = (args.region or cfg.default_region).upper()
    if region not in REGIONS:
        print(f"relay.import_history: --region must be one of {', '.join(sorted(REGIONS))}", file=sys.stderr)
        return 2
    try:
        plan = read_plan(args.file, args.which, region, args.kind, args.expect_rows)
    except CsvError as exc:
        print(f"relay.import_history: file refused: {exc}", file=sys.stderr)
        return 2
    except csv.Error:
        print("relay.import_history: file refused: it is not valid CSV", file=sys.stderr)
        return 2
    except OSError:
        print("relay.import_history: file refused: cannot read it", file=sys.stderr)
        return 2
    store = store or open_store(cfg.database_url, cfg.db_path)
    report = run_import(store, plan, args.apply, args.skip_invalid)
    if report is None:
        print(f"relay.import_history: {len(plan.invalid)} row(s) refused, nothing was written. Fix them or pass --skip-invalid.", file=sys.stderr)
        for n, why in plan.invalid:
            print(f"  line {n}: {why}", file=sys.stderr)
        return 1
    print("\n".join(report.lines()))
    return 1 if report.invalid else 0


if __name__ == "__main__":
    sys.exit(main())
