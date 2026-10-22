"""The repository itself: no real secrets anywhere, the files the build promises exist, and the README register is whole.

Every scan has a positive control, so a scanner that matches nothing cannot pass for a clean repo.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from relay.config import load_config

RELAY = Path(__file__).resolve().parents[1]
REPO = RELAY.parent
SKIP_PARTS = {".venv", "__pycache__", ".pytest_cache", ".git", "node_modules"}
SKIP_SUFFIX = {".pyc", ".db", ".png", ".gif"}

# Placeholder values copied from vendor documentation, never real ones.
ALLOWED = {
    "hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX",
    "EAAGm0PX4ZCpsBAKtestMetaTokenValue1234567890",  # a made up token the secrets test seeds, named in that test
}
PATTERNS = {
    "slack hook url": re.compile(r"hooks\.slack\.com/services/T[A-Z0-9]{8,}/B[A-Z0-9]{8,}/[A-Za-z0-9]{16,}"),
    "make hook url": re.compile(r"hook\.[a-z0-9.]*make\.com/[a-z0-9]{20,}"),
    "slack token": re.compile(r"xox[abprs]-[A-Za-z0-9-]{10,}"),
    "meta access token": re.compile(r"EAA[A-Za-z0-9]{30,}"),
    "private key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----"),
    "github token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "cloud access key id": re.compile(r"AKIA[0-9A-Z]{16}"),
    "pixel id": re.compile(r"(?i)pixel[_ -]?id\W{0,6}\d{14,16}"),
    "google service account key": re.compile(r'"private_key_id"\s*:\s*"[0-9a-f]{20,}"'),
}


def scan(text: str) -> list[str]:
    found = []
    for name, rx in PATTERNS.items():
        for m in rx.finditer(text):
            if len(set(m.group(0))) < 8:
                continue  # a run of one repeated character is a placeholder, not a credential
            if not any(a in m.group(0) or m.group(0) in a for a in ALLOWED):
                found.append(f"{name}: {m.group(0)[:30]}...")
    return found


def relay_files():
    for p in RELAY.rglob("*"):
        if p.is_file() and not (set(p.relative_to(RELAY).parts) & SKIP_PARTS) and p.suffix not in SKIP_SUFFIX:
            yield p


def scanned_files():
    # This file holds the planted examples the scanner is proven against, so it cannot scan itself.
    return [p for p in relay_files() if p.name != Path(__file__).name]


def test_the_scanner_finds_each_kind_of_secret_it_is_looking_for():
    # Each example is assembled from pieces at run time, so this file never holds a literal that a secret scanner
    # (GitHub push protection included) would take for a credential.
    planted = {
        "slack hook url": "https://hooks." + "slack.com/services/" + "T01ABCDEFGH/" + "B02ABCDEFGH/" + "abcdefghijklmnopqrstuvwx",
        "make hook url": "https://hook.eu2." + "make.com/" + "abcdefghijklmnopqrstuvwxyz012345",
        "slack token": "xox" + "b-1234567890-abcdefghij",
        "meta access token": "EAA" + "Bzq9Lk2mVw8PnXr4TjYb7HcDf3GsAe5QuIo1N",
        "private key block": "-----BEGIN " + "PRIVATE KEY-----",
        "github token": "gh" + "p_q8Wz3LmN5vBx7RtY2uKd9HsPc4JfAe6GoTi1",
        "cloud access key id": "AK" + "IAQ7XMZ4PLV2NB9HDR",
        "pixel id": "pixel_id: 123456789012345",
        "google service account key": '"private_key' + '_id": "' + "a1b2c3d4e5" * 4 + '"',
    }
    for name, text in planted.items():
        assert any(f.startswith(name) for f in scan(text)), name
    assert scan("hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX") == []  # the documented placeholder


def test_no_relay_file_holds_a_real_looking_token_hook_url_pixel_id_or_key():
    files = scanned_files()
    assert len(files) > 30  # the walk found the package
    hits = {str(p.relative_to(RELAY)): scan(p.read_text(encoding="utf-8", errors="ignore")) for p in files}
    assert {k: v for k, v in hits.items() if v} == {}


def test_the_other_files_this_change_touches_are_clean_too():
    for rel in (".github/workflows/ci.yml", ".github/dependabot.yml", "CLAUDE.md", "README.md"):
        p = REPO / rel
        assert p.exists(), rel
        assert scan(p.read_text(encoding="utf-8", errors="ignore")) == [], rel


def test_no_railway_config_file_exists_because_config_as_code_is_deprecated():
    for name in ("railway.toml", "railway.json", "Procfile", "nixpacks.toml"):
        assert not (RELAY / name).exists() and not (REPO / name).exists(), name


def test_the_dockerfile_runs_as_a_non_root_user_through_a_shell_so_port_expands():
    text = (RELAY / "Dockerfile").read_text(encoding="utf-8")
    assert text.startswith("FROM python:3.13-slim")
    assert re.search(r"^USER relay$", text, re.M) and "useradd" in text
    cmd = re.search(r"^CMD (\[.*\])$", text, re.M)
    assert cmd and json.loads(cmd.group(1))[:2] == ["/bin/sh", "-c"] and "${PORT:-8000}" in json.loads(cmd.group(1))[2]


def test_requirements_are_exact_pins():
    for name in ("requirements.txt", "requirements-dev.txt"):
        for line in (RELAY / name).read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("-r"):
                assert re.fullmatch(r"[A-Za-z0-9_.\[\]-]+==\d+(\.\d+)*", line.strip()), (name, line)


def test_ci_has_a_relay_job_with_postgres_and_every_action_pinned_by_sha():
    ci = (REPO / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = ci[ci.index("\n  relay:"):]
    assert "working-directory: relay" in job and "image: postgres:17" in job and "timeout-minutes: 10" in job
    assert "python tests/mutants.py" in job and "python -m pytest" in job and 'RELAY_REQUIRE_POSTGRES: "1"' in job
    uses = re.findall(r"uses:\s*(\S+)", ci)
    assert uses and all(re.fullmatch(r"[\w./-]+@[0-9a-f]{40}", u) for u in uses), uses
    assert any(u.startswith("actions/setup-python@") for u in uses)


def test_dependabot_covers_relay_for_pip_and_docker():
    text = (REPO / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    for eco in ("pip", "docker"):
        assert re.search(rf'package-ecosystem: {eco}\s+directory: "/relay"', text), eco


def test_claude_md_and_the_root_readme_know_about_relay():
    claude = (REPO / "CLAUDE.md").read_text(encoding="utf-8")
    assert "Three packages" in claude and "`relay/`" in claude and "python tests/mutants.py" in claude
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "[relay/README.md](relay/README.md)" in readme


def test_the_example_config_loads_with_local_urls_and_holds_no_value_only_names():
    example = json.loads((RELAY / "config.example.json").read_text(encoding="utf-8"))
    env = {
        "RELAY_ENV": "test", "RELAY_INGEST_TOKEN": "i" * 20, "ACME_WORKFLOW_SECRET": "w" * 20,
        "ACME_SLACK_URL": "http://127.0.0.1:9/slack", "ACME_MAKE_URL": "http://127.0.0.1:9/make", "ACME_MAKE_KEY": "k" * 20,
        "RELAY_CONFIG": json.dumps(example),
    }
    cfg = load_config(env)
    assert cfg.tenants["acme"].enabled_sinks() == ["make", "slack"]
    assert all(k.endswith("_env") or k in ("sinks", "dedupe_window_s") for k in example["tenants"]["acme"])


# ---- the README register -----------------------------------------------------------------------------------------------------

INTEGRATIONS = ["Slack", "Make", "Meta Conversions API", "Google Sheets", "Meta Insights", "Windsor.ai", "Fathom", "Reconciliation", "Docker", "Railway"]


def readme() -> str:
    return (RELAY / "README.md").read_text(encoding="utf-8")


def register_rows() -> dict[str, list[str]]:
    rows = {}
    for line in readme().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if line.startswith("|") and len(cells) == 4 and cells[0] in INTEGRATIONS:
            rows[cells[0]] = cells
    return rows


def test_the_register_has_a_row_with_four_cells_for_every_integration():
    rows = register_rows()
    assert sorted(rows) == sorted(INTEGRATIONS)
    assert all(all(cells) for cells in rows.values())
    header = [l for l in readme().splitlines() if l.startswith("| integration")]
    assert header and [c.strip() for c in header[0].strip("|").split("|")] == ["integration", "what ran", "what did not", "provenance"]


def test_rows_for_integrations_not_built_yet_say_so_and_built_ones_do_not():
    rows = register_rows()
    for name in ("Meta Conversions API", "Google Sheets", "Meta Insights", "Windsor.ai", "Fathom", "Reconciliation"):
        assert "NOT BUILT YET" in rows[name][1], name
    for name in ("Slack", "Make"):
        assert "NOT BUILT YET" not in " ".join(rows[name]), name
        assert "doc" in rows[name][3] and "observed" not in rows[name][3].lower().replace("nothing is observed", ""), name


def test_the_readme_says_no_real_vendor_account_was_used_and_never_calls_a_doc_integration_verified_or_live():
    text = readme()
    assert "no real vendor account" in text.lower()
    assert "at least once" in text and "NOT exactly once" in text
    for m in re.finditer(r"(?i)\b(verified against|tested against a real|production ready|in production)\b", text):
        pytest.fail(f"overclaim in the README: {m.group(0)!r}")


def test_the_readme_states_the_relay_counts_apart_from_the_existing_263_with_a_date():
    text = readme()
    assert re.search(r"\d+ tests.*\d{4}-\d{2}-\d{2}", text) and "263" in text


def prose_lines(text: str):
    fence = False
    for line in text.splitlines():
        if line.strip().startswith("```"):
            fence = not fence
            continue
        if not fence and not line.lstrip().startswith("|") and not line.lstrip().startswith("    "):
            yield line


def test_the_readme_uses_no_dashes_as_connectors_and_no_emoji():
    text = readme()
    assert "—" not in text and "–" not in text
    for line in prose_lines(text):
        assert " - " not in line, line
    assert not re.search("[\U0001F300-\U0001FAFF☀-➿]", text)


def test_python_comments_use_no_dashes_as_connectors():
    bad = []
    for p in scanned_files():
        if p.suffix != ".py":
            continue
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") and (" - " in stripped or "—" in stripped or "–" in stripped):
                bad.append(f"{p.relative_to(RELAY)}:{n}")
            if "—" in line or "–" in line:
                bad.append(f"{p.relative_to(RELAY)}:{n}")
    assert bad == []
