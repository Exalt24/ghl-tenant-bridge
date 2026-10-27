# LIFESPAN: permanent (relay tooling that CI runs next to check_readme_claims.py)
"""Fails when a static count in relay/README.md no longer matches the files it counts.

    python tools/check_readme_counts.py

The README says how many mutants `tests/mutants.py` holds, how many test files there are and how many lines in them start with `assert`. Those three are counts of
files, not results of a run, so they can be checked mechanically. What the README says about a run (how many tests passed, which date) cannot be, and stays a
dated claim that a person re-reads: a README line that was stronger than its dated run is how this check came to exist (2026-10-24).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

RELAY = Path(__file__).resolve().parent.parent


def counts() -> dict[str, int]:
    mutants = sum(1 for line in (RELAY / "tests" / "mutants.py").read_text(encoding="utf-8").splitlines() if line.startswith("    M("))
    files = sorted((RELAY / "tests").glob("test_*.py"))
    asserts = sum(1 for f in files for line in f.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip().startswith("assert"))
    return {"mutants": mutants, "test files": len(files), "assert lines": asserts}


def claimed(text: str) -> dict[str, int]:
    out: dict[str, int] = {}
    m = re.search(r"holds (\d+) mutants", text)
    if m:
        out["mutants"] = int(m.group(1))
    m = re.search(r"The (\d+) test files hold (\d+) lines", text)
    if m:
        out["test files"], out["assert lines"] = int(m.group(1)), int(m.group(2))
    return out


EDGE = RELAY.parent / "edge"


def edge_counts() -> int | None:
    """Mutants in edge/test/mutants.mjs (one array row each, starting `  ["`), or None when this checkout has no edge package."""
    path = EDGE / "test" / "mutants.mjs"
    if not path.exists():
        return None
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.startswith('  ["'))


def edge_claimed(text: str | None = None) -> int | None:
    """The `N mutants that must all be caught` in edge/README.md, or None when it is not stated."""
    if text is None:
        readme = EDGE / "README.md"
        if not readme.exists():
            return None
        text = readme.read_text(encoding="utf-8")
    m = re.search(r"(?<!\d)(\d{1,6}) mutants that must all be caught", text)
    return int(m.group(1)) if m else None


def selftest() -> int:
    """CLASS-TABLE: a README count that disagrees with the files it counts. Vocabulary: each of the three counted things x a README off by -1, 0 or +1."""
    import itertools

    actual = counts()
    bad = 0
    for key, delta in itertools.product(("mutants", "test files", "assert lines"), (-1, 0, 1)):
        said = dict(actual)
        said[key] += delta
        text = "`python tests/mutants.py` holds %d mutants. The %d test files hold %d lines" % (said["mutants"], said["test files"], said["assert lines"])
        read = claimed(text)
        detected = any(read[k] != actual[k] for k in actual)
        if detected != (delta != 0):
            bad += 1
            print("FAIL %s off by %+d: detected=%s" % (key, delta, detected))
    edge_actual = edge_counts()
    if edge_actual is not None:
        for delta in (-1, 0, 1):
            read = edge_claimed("holds %d mutants that must all be caught" % (edge_actual + delta))
            if (read != edge_actual) != (delta != 0):
                bad += 1
                print("FAIL edge mutants off by %+d: detected=%s" % (delta, read != edge_actual))
    print("readme counts selftest " + ("FAIL" if bad else "ok: %d cases" % (9 if edge_actual is None else 12)))
    return 1 if bad else 0


def main() -> int:
    if "--selftest" in sys.argv[1:]:
        return selftest()
    actual = counts()
    said = claimed((RELAY / "README.md").read_text(encoding="utf-8"))
    problems = []
    for key in ("mutants", "test files", "assert lines"):
        if key not in said:
            problems.append("README no longer states the number of %s" % key)
        elif said[key] != actual[key]:
            problems.append("README says %d %s, the files hold %d" % (said[key], key, actual[key]))
    edge_actual, edge_said = edge_counts(), edge_claimed()
    if edge_actual is not None:
        if edge_said is None:
            problems.append("edge/README.md no longer states its number of mutants")
        elif edge_said != edge_actual:
            problems.append("edge/README.md says %d mutants, edge/test/mutants.mjs holds %d" % (edge_said, edge_actual))
    for p in problems:
        print("FAIL " + p)
    shown = ", ".join("%d %s" % (actual[k], k) for k in ("mutants", "test files", "assert lines"))
    print("readme counts " + ("FAIL" if problems else "ok: %s%s" % (shown, "" if edge_actual is None else ", %d edge mutants" % edge_actual)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
