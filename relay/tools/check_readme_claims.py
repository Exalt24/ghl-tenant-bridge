# LIFESPAN: permanent (relay tooling that the README lists as a command)
"""Fails when relay/README.md says "verified", "live" or "production" for an integration whose fixtures are all `doc`.

    python tools/check_readme_claims.py            check relay/README.md against relay/contracts/*/SOURCE.md

An integration may be called verified only when its vendor folder has a fixture tagged `observed` (a real captured response).
Only the Google folder has `observed` fixtures, and those were captured with a user OAuth token, not a service account, so the
README keeps to the words of what was observed (tests/test_tools.py checks the Google row). Every other integration is held to
the rule, and an integration that has no vendor folder at all (reconciliation, Docker, Railway) can never be called verified.

The rule is per sentence: a forbidden word is allowed when a negation ("no", "not", "never", "nothing", "without", "none",
"neither", "nor", or a word ending in n't) comes earlier in the same sentence, as in "no real Slack account was used, so nothing
is verified". A sentence that names no integration is not looked at, and `RELAY_LIVE` (the setting) is not the word "live".
A table row is one scope, so a claim in the "what ran" cell is judged with the integration named in the first cell.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

RELAY = Path(__file__).resolve().parent.parent
FORBIDDEN = re.compile(r"\b(verified|live|production)\b", re.IGNORECASE)
NEGATION = re.compile(r"\b(no|not|never|nothing|none|without|neither|nor)\b|n't\b", re.IGNORECASE)
SENTENCE_END = re.compile(r"(?<=[.!?])\s+")
MAX_LINE_CHARS = 5000
SETTING = re.compile(r"RELAY_LIVE")

# The names a README uses for an integration, and the contracts folder that holds its fixtures (None: no folder at all).
INTEGRATIONS: dict[str, str | None] = {
    "Slack": "slack",
    "Make": "make",
    "Meta Conversions API": "meta",
    "Meta CAPI": "meta",
    "CAPI": "meta",
    "Google Sheets": "google",
    "Sheets": "google",
    "Meta Insights": "meta",
    "Insights": "meta",
    "Windsor.ai": "windsor",
    "Windsor": "windsor",
    "Fathom": "fathom",
    "Reconciliation": None,
    "Docker": None,
    "Railway": None,
    "HighLevel": "ghl",
}
NAME_RE = re.compile(r"(?<![A-Za-z])(" + "|".join(re.escape(n) for n in sorted(INTEGRATIONS, key=len, reverse=True)) + r")(?![A-Za-z])")


def observed_vendors(contracts: Path) -> set[str]:
    """Vendor folders whose SOURCE.md has a row with provenance `observed`."""
    out: set[str] = set()
    for source in contracts.glob("*/SOURCE.md"):
        for line in source.read_text(encoding="utf-8").splitlines():
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if line.startswith("| `") and len(cells) >= 2 and cells[1].lower().startswith("observed"):
                out.add(source.parent.name)
    return out


def findings(text: str, observed: set[str]) -> list[str]:
    out: list[str] = []
    in_fence = False
    for n, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        line = line[:MAX_LINE_CHARS]
        names = set(NAME_RE.findall(line))
        unproven = sorted(i for i in names if INTEGRATIONS[i] is None or INTEGRATIONS[i] not in observed)
        if not unproven:
            continue
        cleaned = SETTING.sub("", line)
        for cell in cleaned.split("|"):  # a table cell ends a sentence, whatever its punctuation
            for sentence in SENTENCE_END.split(cell):
                for m in FORBIDDEN.finditer(sentence):
                    if not NEGATION.search(sentence[: m.start()]):
                        out.append(f"README line {n}: says '{m.group(0)}' where {', '.join(unproven)} has no observed fixture: {sentence.strip()[:100]}")
    return out


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    readme = Path(args[0]) if args else RELAY / "README.md"
    contracts = Path(args[1]) if len(args) > 1 else RELAY / "contracts"
    bad = findings(readme.read_text(encoding="utf-8"), observed_vendors(contracts))
    for line in bad:
        print(line)
    if bad:
        print(f"{len(bad)} claim(s) stronger than the fixtures allow")
        return 1
    print("readme claims ok: no integration is called verified, live or production without an observed fixture")
    return 0


if __name__ == "__main__":
    sys.exit(main())
