"""The cutover demo (tools/demo_cutover.py) is a test too: it asserts every step of the story, so a guard that stops working turns it red.
It runs as its own process because the story starts and kills real worker processes."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

RELAY = Path(__file__).resolve().parent.parent


def test_the_cutover_demo_passes_end_to_end():
    res = subprocess.run(
        [sys.executable, str(RELAY / "tools" / "demo_cutover.py")], cwd=str(RELAY), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=240,
    )
    out = res.stdout + res.stderr
    assert res.returncode == 0, out[-3000:]
    assert "PASS: every expectation held" in out and "[FAIL]" not in out
