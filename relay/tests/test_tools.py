"""The two scripts in relay/tools: the README claim checker (a doc fixture can never be called verified or live) and the helpers of
the container smoke test (the docker part itself runs in CI and by hand, the pieces around it are proven here without docker)."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

RELAY = Path(__file__).resolve().parents[1]


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"relay_tools_{name}", RELAY / "tools" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


claims = load("check_readme_claims")
smoke = load("container_smoke")
NONE: set[str] = set()


def bad(text: str, observed: set[str] = NONE) -> list[str]:
    return claims.findings(text, observed)


# ---- the claim checker -----------------------------------------------------------------------------------------------------------


def test_the_real_readme_makes_no_claim_stronger_than_its_fixtures():
    text = (RELAY / "README.md").read_text(encoding="utf-8")
    assert claims.findings(text, claims.observed_vendors(RELAY / "contracts")) == []
    assert claims.main([]) == 0


def test_nothing_in_this_repo_is_observed():
    assert claims.observed_vendors(RELAY / "contracts") == set()


@pytest.mark.parametrize(
    "text",
    [
        "Slack is verified.",
        "The Slack sink is verified against a real workspace.",
        "Meta Insights runs in production.",
        "Windsor.ai is live.",
        "Fathom was tested live.",
        "Google Sheets: verified end to end.",
        "Slack works. It is verified.",
        "| Make | The sink is verified | nothing | doc |",
        "| Fathom | Receives webhooks | production ready and verified | doc |",
        "Reconciliation is verified on real exports.",
        "Docker is live in production.",
        "Railway is live.",
        "CAPI events are verified.",
        "HighLevel deliveries are live.",
        "SLACK is fine, and slack is LIVE.",
    ],
)
def test_a_claim_of_verified_live_or_production_for_a_doc_integration_is_found(text):
    assert bad("Slack is documented, nothing more.") == []  # control: a plain sentence passes
    found = bad(text) or bad(text.replace("SLACK", "Slack").replace("slack", "Slack"))
    assert found, text


@pytest.mark.parametrize(
    "text",
    [
        "No real Slack workspace was used, so nothing is verified.",
        "Nothing here is verified live.",
        "The sink was never run against a live Slack account.",
        "Make was not verified.",
        "Fathom is not live and is not in production.",
        "| Slack | A fake | no real Slack workspace; nothing is live | doc |",
        "Without a live account, Windsor.ai stays unverified.",
        "Neither verified nor live: Meta Insights.",
        "It wasn't verified against Fathom.",
        "Set RELAY_LIVE=1 to allow a real Slack URL.",
        "The database is live and verified.",
        "A real delivery would settle it, and the relay is not deployed to Railway.",
        "```\nSlack is verified\n```",
    ],
)
def test_a_negated_claim_a_setting_or_a_sentence_naming_no_integration_passes(text):
    assert bad("Slack is verified.") != []  # control: the same words without the negation are found
    assert bad(text) == [], text


def test_the_negation_must_come_before_the_word_in_the_same_sentence():
    assert bad("Slack is verified, not a guess.") != []  # the "not" comes after
    assert bad("Not a guess. Slack is verified.") != []  # the "not" is in another sentence
    assert bad("Slack is not a guess, it is verified.") == []  # one sentence, negation first (a known looseness, stated in the docstring)


def test_an_integration_with_an_observed_fixture_may_be_called_verified_but_the_others_may_not():
    assert bad("Slack is verified.", {"slack"}) == []
    assert bad("Slack is verified.", {"make"}) != []
    assert bad("Slack and Make are verified.", {"slack"}) != []  # one unproven name in the sentence is enough
    assert bad("Reconciliation is verified.", {"slack", "make", "meta", "google", "windsor", "fathom"}) != []  # no vendor folder, never eligible
    assert bad("Meta Insights is verified.", {"meta"}) == []  # the Insights pull and CAPI share one folder


def test_observed_vendors_reads_the_provenance_column_of_each_source_file(tmp_path):
    for vendor, prov in (("slack", "doc"), ("make", "observed"), ("fathom", "Observed: captured on 2026-11-01"), ("google", "ASSUMED")):
        d = tmp_path / vendor
        d.mkdir()
        (d / "SOURCE.md").write_text(f"| Fixture | Provenance |\n|---|---|\n| `x.json` | {prov} | url | what |\n", encoding="utf-8")
    assert claims.observed_vendors(tmp_path) == {"make", "fathom"}


def test_the_command_line_exits_1_with_the_findings_and_0_when_clean(tmp_path, capsys):
    readme = tmp_path / "README.md"
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    readme.write_text("Fathom is verified.\n", encoding="utf-8")
    assert claims.main([str(readme), str(contracts)]) == 1
    out = capsys.readouterr().out
    assert "line 1" in out and "Fathom" in out and "1 claim(s)" in out
    readme.write_text("Fathom has fixtures from documentation only.\n", encoding="utf-8")
    assert claims.main([str(readme), str(contracts)]) == 0
    done = subprocess.run([sys.executable, str(RELAY / "tools" / "check_readme_claims.py")], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0 and "readme claims ok" in done.stdout


def test_a_very_long_line_is_cut_before_it_is_scanned():
    assert claims.MAX_LINE_CHARS == 5000
    assert bad("Slack is verified." + "x" * 20000) != []  # the claim sits inside the first 5000 characters
    assert bad("x" * 20000 + " Slack is verified.") == []  # one beyond the cut is not read: the cost of bounding the work


# ---- the container smoke helpers -----------------------------------------------------------------------------------------------------


def env_of(args: list[str]) -> dict[str, str]:
    out = {}
    for flag, value in zip(args, args[1:]):
        if flag == "-e":
            k, _, v = value.partition("=")
            out[k] = v
    return out


def test_the_docker_command_mounts_the_relay_folder_read_only_publishes_the_port_and_names_the_image_last():
    args = smoke.docker_run_args("relay-smoke-1", 8123, "relay-x")
    assert args[:3] == ["docker", "run", "-d"] and args[-1] == "relay-x" and "relay-smoke-1" in args
    assert "8123:8000" in args and any(a.endswith(":/smoke:ro") for a in args)
    for forbidden in ("--privileged", "--network", "host"):
        assert forbidden not in args
    env = env_of(args)
    assert env["RELAY_ENV"] == "dev" and env["RELAY_INLINE_WORKER"] == "1" and env["WEB_CONCURRENCY"] == "1"


def test_the_container_config_and_lead_work_through_the_real_app_and_worker_with_no_docker(store, slack):
    """The smoke test's own assumptions, run in process: the config the container gets loads, the lead routes to its tenant,
    and a lead posted to /ingest reaches a Slack fake once."""
    from relay.app import Deps, create_app
    from relay.config import load_config

    from .helpers import make_rig

    env = env_of(smoke.docker_run_args("n", 8000))
    env["SMOKE_SLACK_URL"] = slack.url
    cfg = load_config(env)
    client = create_app(cfg, Deps(store=store)).test_client()
    headers = {"Authorization": f"Bearer {smoke.INGEST_TOKEN}", "Idempotency-Key": "smoke-1", "Content-Type": "application/json"}
    body = json.dumps(smoke.lead_payload())
    assert client.post("/ingest", data=body, headers=headers).get_json()["duplicate"] is False
    make_rig(store, cfg).settle()
    assert slack.effects == 1 and smoke.LEAD_NAME in json.dumps(slack.requests[0]["json"])
    assert client.post("/ingest", data=body, headers=headers).get_json()["duplicate"] is True


def test_the_fake_inside_the_container_uses_the_port_and_hook_path_the_smoke_test_configures():
    from .fakes.slack import HOOK_PATH

    fake = load("smoke_fake")
    assert fake.PORT == smoke.FAKE_PORT and smoke.HOOK_PATH == HOOK_PATH
    assert smoke.docker_run_args("n", 8000).count(f"SMOKE_SLACK_URL=http://127.0.0.1:{fake.PORT}{HOOK_PATH}") == 1
    assert fake.LOG == "/tmp/slack-requests.jsonl"


def test_the_fake_inside_the_container_logs_what_it_accepts_and_only_that(tmp_path, monkeypatch):
    import http.client

    from werkzeug.serving import make_server

    from .fakes.slack import HOOK_PATH, FakeSlack

    log = tmp_path / "log.jsonl"
    fake_mod = load("smoke_fake")
    monkeypatch.setattr(fake_mod, "LOG", str(log))
    fake = FakeSlack()

    def app(environ, start_response):
        before = fake.effects
        response = fake(environ, start_response)
        if fake.effects > before:
            log.open("a", encoding="utf-8").write(json.dumps(fake.requests[-1]["json"]) + "\n")
        return response

    # the wrapper in smoke_fake.main is the same code; this runs it against a local port without the blocking serve loop
    server = make_server("127.0.0.1", 0, app, threaded=True)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        good = json.dumps({"text": "hi"})
        conn.request("POST", HOOK_PATH, good, {"Content-Type": "application/json"})
        assert conn.getresponse().read() == b"ok"
        conn.request("POST", HOOK_PATH, json.dumps({"text": "a < b"}), {"Content-Type": "application/json"})
        conn.getresponse().read()
    finally:
        server.shutdown()
    assert [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines()] == [{"text": "hi"}]  # the refused message was not logged


def test_wait_for_returns_the_value_as_soon_as_the_condition_holds_and_times_out_otherwise():
    state = {"n": 0}

    def cond():
        state["n"] += 1
        return state["n"] >= 3 and "ready"

    assert smoke.wait_for(cond, "x", timeout_s=5.0) == "ready" and state["n"] == 3
    with pytest.raises(smoke.SmokeFailure, match="timed out after 1s waiting for the moon"):
        smoke.wait_for(lambda: False, "the moon", timeout_s=0.6)


def test_http_helper_returns_status_zero_for_a_refused_connection_and_the_status_for_an_error_reply(slack):
    assert smoke.http("GET", "http://127.0.0.1:9/health")[0] == 0
    status, body = smoke.http("POST", slack.url, b"{}", {"Content-Type": "application/json"})
    assert status == 400 and body is None  # the fake answers 400 no_text, which is not JSON: the helper reports the status
    assert smoke.health(9) is None


def test_a_negation_in_one_table_cell_does_not_excuse_a_claim_in_another():
    assert bad("| Slack | not tested | nothing live |") == []  # control: the negation is in the same cell as the word
    assert bad("| Slack | not tested | verified |") != []
