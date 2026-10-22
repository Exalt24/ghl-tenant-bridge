"""The mutation harness itself: how it reads the failing test out of pytest's summary.

A pytest parameter id can hold spaces, and the harness reruns the failing test by that id on an unmutated copy. Cut at the
first space the id names no test, the rerun fails, and a mutant that WAS caught is reported BROKEN.
"""

from __future__ import annotations

import re

import pytest

from . import mutants

OLD_PARSING = re.compile(r"^FAILED (\S+)", re.MULTILINE)  # the pattern the harness used before


def summary(*lines: str) -> str:
    return "\n".join(["=== short test summary info ===", *lines, "1 failed, 3 passed in 0.5s"])


@pytest.mark.parametrize(
    "line,node",
    [
        ("FAILED tests/test_a.py::test_plain - AssertionError: boom", "tests/test_a.py::test_plain"),
        ("FAILED tests/test_a.py::test_plain", "tests/test_a.py::test_plain"),
        ("FAILED tests/test_a.py::test_phone[(650) 555-1212-None-None] - assert '6505551212' is None", "tests/test_a.py::test_phone[(650) 555-1212-None-None]"),
        ("FAILED tests/test_a.py::test_x[opts0-unknown option] - ValueError", "tests/test_a.py::test_x[opts0-unknown option]"),
        ("FAILED tests/test_a.py::test_x[sqlite]", "tests/test_a.py::test_x[sqlite]"),
        ("FAILED tests/test_a.py::test_x[a b - c d] - AssertionError", "tests/test_a.py::test_x[a b - c d]"),
    ],
)
def test_the_failing_test_id_is_read_whole_including_spaces_in_a_parameter_id(line, node):
    found = mutants.FAILED_LINE.search(summary(line))
    assert found is not None and found.group(1) == node


def test_the_old_parsing_cut_such_an_id_at_the_first_space_which_is_the_bug_this_guards():
    line = "FAILED tests/test_a.py::test_phone[(650) 555-1212-None-None] - assert 1"
    assert OLD_PARSING.search(summary(line)).group(1) == "tests/test_a.py::test_phone[(650)"  # not a test id at all
    assert mutants.FAILED_LINE.search(summary(line)).group(1) == "tests/test_a.py::test_phone[(650) 555-1212-None-None]"


def test_the_first_failure_is_the_one_named_and_a_summary_without_one_names_nothing():
    two = summary("FAILED tests/a.py::t1[x y] - E", "FAILED tests/a.py::t2 - E")
    assert mutants.FAILED_LINE.search(two).group(1) == "tests/a.py::t1[x y]"
    assert mutants.FAILED_LINE.search("5 passed in 0.1s") is None
