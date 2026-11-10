"""The bridge over real HTTP, between a stand-in gateway and a stand-in HighLevel."""

from __future__ import annotations

import json
import logging

import pytest
import requests

from sms_bridge import config as config_mod
from sms_bridge.store import Store

from .conftest import BRIDGE_TOKEN, sms, wait_for


# --- positive controls: the stand-ins refuse what they should, so a refusal below is not an accident -------------

def test_control_gateway_demands_basic_auth(rig):
    assert requests.post(rig.gw.url + "/message", json={"message": "x", "phoneNumbers": ["+1"]}, timeout=2).status_code == 401
    ok = requests.post(rig.gw.url + "/message", json={"message": "x", "phoneNumbers": ["+1"]}, auth=(rig.gw.user, rig.gw.password), timeout=2)
    assert ok.status_code == 202 and ok.json()["state"] == "Pending"


def test_control_highlevel_demands_token_and_version(rig):
    url = rig.hl.url + "/conversations/messages/abc/status"
    good = {"Authorization": "Bearer " + rig.hl.token, "Version": rig.hl.version}
    assert requests.put(url, json={"status": "read"}, headers=good, timeout=2).status_code == 200
    assert requests.put(url, json={"status": "read"}, headers={**good, "Authorization": "Bearer x"}, timeout=2).status_code == 401
    assert requests.put(url, json={"status": "read"}, headers={"Authorization": good["Authorization"]}, timeout=2).status_code == 400
    assert requests.post(url, json={"status": "read"}, headers=good, timeout=2).status_code == 404
    assert requests.put(url, json={"status": "sent"}, headers=good, timeout=2).status_code == 422
    assert requests.put(url, json={"status": "failed", "error": {"code": 1}}, headers=good, timeout=2).status_code == 422


# --- happy path -----------------------------------------------------------------------------------------------

def test_happy_path_sends_polls_and_reports_delivered(rig):
    r = rig.post(sms())
    assert r.status_code == 200 and r.json()["status"] == "queued" and r.json()["gatewayId"] == "gw1"
    rig.final()
    assert rig.gw.posts == [{"message": "Hi Dana, your quote is ready", "phoneNumbers": ["+15864603685"], "simNumber": 2}]
    assert rig.gw.gets == 4  # Pending, Processed, Sent, Delivered, and then it stops asking
    assert rig.hl.calls == [{"messageId": "m1", "body": {"status": "delivered"}}]
    rec = rig.bridge.store.get("m1")
    assert rec["status"] == "delivered" and rec["gatewayState"] == "Delivered" and rec["reported"] is True


def test_token_may_come_in_a_header(rig):
    assert rig.post(sms(), header=True).status_code == 200
    rig.final()


def test_sim_number_is_left_out_when_not_set(rig):
    rig.bridge.gateway.sim_number = None
    assert rig.post(sms()).status_code == 200
    rig.final()
    assert "simNumber" not in rig.gw.posts[0]


def test_attachments_are_counted_as_ignored(rig):
    r = rig.post(sms(attachments=["https://example.com/a.png"]))
    assert r.json()["attachmentsIgnored"] == 1
    assert rig.gw.posts[0]["message"] == "Hi Dana, your quote is ready"
    rig.final()


def test_message_id_with_odd_characters_is_encoded_in_the_callback_path(rig):
    assert rig.post(sms(messageId="a b/c")).status_code == 200
    rig.final()
    assert rig.hl.calls[0]["messageId"] == "a%20b%2Fc"


# --- replay ---------------------------------------------------------------------------------------------------

def test_replay_is_not_sent_twice(rig):
    assert rig.post(sms()).status_code == 200
    rig.final()
    again = rig.post(sms())
    assert again.status_code == 200 and again.json()["duplicate"] is True and again.json()["status"] == "delivered"
    assert len(rig.gw.posts) == 1 and len(rig.hl.calls) == 1


def test_replay_while_in_flight_is_not_sent_twice(rig):
    rig.gw.script = ["Pending"] * 5 + ["Delivered"]
    assert rig.post(sms()).status_code == 200
    again = rig.post(sms())
    assert again.json()["duplicate"] is True
    rig.final()
    assert len(rig.gw.posts) == 1


def test_a_different_message_id_is_a_new_text(rig):
    rig.post(sms(messageId="m1"))
    rig.post(sms(messageId="m2"))
    rig.final(2)
    assert len(rig.gw.posts) == 2


def test_replay_survives_a_restart(rig, tmp_path):
    rig.post(sms())
    rig.final()
    claimed, rec = Store(rig.cfg.store_path).claim("m1")
    assert claimed is False and rec["status"] == "delivered"


def test_store_file_is_valid_json_with_no_secret_in_it(rig):
    rig.post(sms())
    rig.final()
    text = open(rig.cfg.store_path, encoding="utf-8").read()
    data = json.loads(text)
    assert data["m1"]["status"] == "delivered"
    for secret in (BRIDGE_TOKEN, rig.gw.password, rig.hl.token):
        assert secret not in text


# --- refusals -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("token,header", [(None, False), ("wrong-token-0123456789", False), ("wrong-token-0123456789", True), ("", False)])
def test_bad_or_missing_secret_is_401_and_nothing_is_sent(rig, token, header):
    assert rig.post(sms(), token=token, header=header).status_code == 401
    assert rig.gw.posts == [] and rig.hl.calls == []


def test_secret_is_checked_before_the_body_is_read(rig):
    assert rig.post(None, token="nope", raw=b"not json").status_code == 401


def test_secret_in_the_wrong_place_for_the_query_is_not_enough(rig):
    r = requests.post(rig.url + "/outbound?other=" + BRIDGE_TOKEN, json=sms(), timeout=5)
    assert r.status_code == 401


@pytest.mark.parametrize("raw", [b"not json", b"[1, 2]", b"\xff\xfe", b"", b'"text"'])
def test_unreadable_body_is_400(rig, raw):
    assert rig.post(None, raw=raw).status_code == 400


@pytest.mark.parametrize("over", [
    {"messageId": ""}, {"messageId": 7}, {"messageId": None}, {"type": None}, {"type": ""},
    {"phone": "0965 935 1300"}, {"phone": "+1"}, {"phone": 15864603685}, {"phone": "+15864603685; DROP"},
    {"message": ""}, {"message": "   "}, {"message": 5}, {"message": "x" * 1601}, {"attachments": "a.png"},
])
def test_bad_fields_are_400(rig, over):
    r = rig.post(sms(**over))
    assert r.status_code == 400, r.text
    assert rig.gw.posts == []


@pytest.mark.parametrize("missing", ["messageId", "type", "phone", "message"])
def test_missing_required_field_is_400(rig, missing):
    body = sms()
    del body[missing]
    assert rig.post(body).status_code == 400
    assert rig.gw.posts == []


def test_email_type_is_422_and_not_sent(rig):
    email = {"contactId": "c1", "locationId": "l1", "messageId": "e1", "type": "Email", "emailTo": ["a@example.com"],
             "emailFrom": "A <b@example.com>", "html": "<p>x</p>", "subject": "s"}
    r = rig.post(email)
    assert r.status_code == 422 and "SMS" in r.json()["error"]
    assert rig.gw.posts == []
    assert rig.bridge.store.get("e1") is None


def test_lowercase_sms_is_422_because_the_docs_say_SMS(rig):
    assert rig.post(sms(type="sms")).status_code == 422


def test_body_over_the_limit_is_413(rig):
    r = rig.post(None, raw=b'{"messageId": "m1", "pad": "' + b"x" * 70000 + b'"}')
    assert r.status_code == 413 and rig.gw.posts == []


def test_unknown_route_is_404_and_health_is_open(rig):
    assert rig.post(sms(), path="/other").status_code == 404
    assert requests.get(rig.url + "/health", timeout=2).json() == {"ok": True}
    assert requests.get(rig.url + "/outbound", timeout=2).status_code == 404


# --- gateway trouble ------------------------------------------------------------------------------------------

def test_gateway_401_is_a_502_and_highlevel_is_told_failed(rig):
    rig.gw.password = "changed-on-the-phone"
    r = rig.post(sms())
    assert r.status_code == 502
    call = rig.hl.calls[0]["body"]
    assert call["status"] == "failed" and call["error"]["type"] == "sms_gateway" and "401" in call["error"]["message"]
    assert rig.gw.password not in json.dumps(call) and BRIDGE_TOKEN not in json.dumps(call)


def test_a_refused_text_can_be_tried_again_with_the_same_message_id(rig):
    rig.gw.post_status = 401
    assert rig.post(sms()).status_code == 502
    rig.gw.post_status = 202
    assert rig.post(sms()).status_code == 200
    rig.final(2)
    assert len(rig.gw.posts) == 2  # the first was refused before anything was queued
    assert rig.hl.calls[-1]["body"] == {"status": "delivered"}
    assert rig.bridge.store.get("m1")["attempts"] == 2


def test_an_unanswered_post_is_never_sent_again(rig):
    rig.gw.hang_post = True
    assert rig.post(sms()).status_code == 502
    rig.gw.hang_post = False
    again = rig.post(sms())
    assert again.json()["duplicate"] is True
    assert rig.gw.posts == []  # the hung request never reached the stand-in's list, and the retry was refused
    assert rig.hl.calls[0]["body"]["status"] == "failed"


def test_gateway_failed_state_reports_failed_with_the_modem_error(rig):
    rig.gw.script = ["Pending", "Processed", "Failed"]
    rig.gw.recipient_error = "RESULT_MODEM_ERROR"
    assert rig.post(sms()).status_code == 200
    rig.final()
    body = rig.hl.calls[0]["body"]
    assert body["status"] == "failed"
    assert body["error"]["code"] == "RESULT_MODEM_ERROR"
    assert "RESULT_MODEM_ERROR" in body["error"]["message"]
    assert rig.bridge.store.get("m1")["status"] == "failed"
    assert rig.gw.gets == 3  # Failed is final, so the bridge does not keep asking until the deadline


def test_failed_state_without_an_error_text_still_reports_failed(rig):
    rig.gw.script = ["Failed"]
    rig.post(sms())
    rig.final()
    assert rig.hl.calls[0]["body"]["status"] == "failed" and rig.hl.calls[0]["body"]["error"]["code"] == "unknown"


def test_gateway_that_never_finishes_times_out_as_failed(rig):
    rig.gw.script = ["Pending", "Processed", "Sent"]
    assert rig.post(sms()).status_code == 200
    rig.final()
    body = rig.hl.calls[0]["body"]
    assert body["status"] == "failed" and body["error"]["code"] == "timeout" and "Sent" in body["error"]["message"]
    assert rig.bridge.store.get("m1")["gatewayState"] == "Sent"


def test_delivered_after_a_poll_that_failed_in_between(rig):
    rig.gw.script = ["Pending", "Delivered"]
    orig = rig.bridge.gateway.status
    calls = {"n": 0}

    def flaky(gid):
        calls["n"] += 1
        if calls["n"] == 1:
            raise requests.ConnectionError("blip")
        return orig(gid)

    rig.bridge.gateway.status = flaky
    rig.post(sms())
    rig.final()
    assert rig.hl.calls[0]["body"] == {"status": "delivered"}


# --- the status callback --------------------------------------------------------------------------------------

def test_callback_refused_by_highlevel_is_stored_not_lost(rig):
    rig.hl.answer = 401
    rig.post(sms())
    assert wait_for(lambda: (rig.bridge.store.get("m1") or {}).get("reported") is False)
    rec = rig.bridge.store.get("m1")
    assert rec["status"] == "delivered" and "401" in rec["reportError"]


def test_callback_with_the_wrong_token_is_refused_by_the_stand_in(rig):
    rig.bridge.reporter.update_status  # the real client
    rig.bridge.reporter._token = "not-the-token"
    rig.post(sms())
    assert wait_for(lambda: (rig.bridge.store.get("m1") or {}).get("reported") is False)
    assert rig.hl.calls == []


def test_without_a_highlevel_token_the_text_still_goes(tmp_path):
    from .conftest import Rig
    from sms_bridge.highlevel import NoReporter
    r = Rig(tmp_path)
    try:
        r.bridge.reporter = NoReporter()
        assert r.post(sms()).status_code == 200
        assert wait_for(lambda: (r.bridge.store.get("m1") or {}).get("status") == "delivered")
        assert wait_for(lambda: (r.bridge.store.get("m1") or {}).get("reported") is False)
        assert len(r.gw.posts) == 1
    finally:
        r.close()


# --- secrets and config ---------------------------------------------------------------------------------------

def test_the_token_never_reaches_a_log_line(rig, caplog):
    with caplog.at_level(logging.DEBUG):
        rig.post(sms())
        rig.post(sms(), token="wrong-token-0123456789")
        rig.final()
    # the test's own HTTP client logs the URLs it calls, so only the bridge's lines are judged
    ours = "\n".join(r.getMessage() for r in caplog.records if r.name == "sms_bridge")
    assert "POST /outbound" in ours  # positive control: the bridge did log the request
    assert BRIDGE_TOKEN not in ours and "wrong-token" not in ours and rig.gw.password not in ours


def test_config_repr_hides_secrets(rig):
    shown = repr(rig.cfg)
    for secret in (BRIDGE_TOKEN, rig.gw.password, rig.hl.token):
        assert secret not in shown


ENV = {"BRIDGE_TOKEN": BRIDGE_TOKEN, "GATEWAY_URL": "http://127.0.0.1:18080/", "GATEWAY_USER": "u", "GATEWAY_PASSWORD": "p"}


def test_config_reads_the_documented_names():
    cfg = config_mod.from_env({**ENV, "SIM_NUMBER": "2", "HIGHLEVEL_ACCESS_TOKEN": "t", "BRIDGE_PORT": "9000",
                               "POLL_DEADLINE": "30", "POLL_INTERVAL": "0.5", "STORE_PATH": "s.json"})
    assert (cfg.gateway_url, cfg.sim_number, cfg.highlevel_token, cfg.port, cfg.poll_deadline, cfg.poll_interval, cfg.store_path) == \
        ("http://127.0.0.1:18080", 2, "t", 9000, 30.0, 0.5, "s.json")
    d = config_mod.from_env(ENV)
    assert (d.sim_number, d.highlevel_token, d.port, d.poll_deadline, d.host) == (None, None, 8787, 60.0, "127.0.0.1")


@pytest.mark.parametrize("drop", ["BRIDGE_TOKEN", "GATEWAY_URL", "GATEWAY_USER", "GATEWAY_PASSWORD"])
def test_config_refuses_a_missing_required_variable(drop):
    env = dict(ENV)
    del env[drop]
    with pytest.raises(config_mod.ConfigError, match=drop):
        config_mod.from_env(env)


def test_config_refuses_a_short_bridge_token_and_bad_numbers():
    with pytest.raises(config_mod.ConfigError, match="16"):
        config_mod.from_env({**ENV, "BRIDGE_TOKEN": "short"})
    for name, val in (("SIM_NUMBER", "abc"), ("SIM_NUMBER", "0"), ("POLL_DEADLINE", "-1")):
        with pytest.raises(config_mod.ConfigError):
            config_mod.from_env({**ENV, name: val})


# --- connection handling, concurrency, entry point ------------------------------------------------------------

def test_a_refused_request_does_not_poison_a_kept_alive_connection(rig):
    with requests.Session() as s:
        bad = s.post(rig.url + "/outbound?token=wrong-token-0123456789", json=sms(), timeout=5)
        assert bad.status_code == 401
        good = s.post(rig.url + "/outbound?token=" + BRIDGE_TOKEN, json=sms(), timeout=5)
        assert good.status_code == 200
    rig.final()


def test_negative_content_length_is_400_and_does_not_hang(rig):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", rig.server.server_address[1], timeout=2)
    conn.putrequest("POST", "/outbound?token=" + BRIDGE_TOKEN)
    conn.putheader("Content-Length", "-1")
    conn.endheaders()
    assert conn.getresponse().status == 400
    conn.close()


def test_missing_content_length_is_400(rig):
    import http.client
    conn = http.client.HTTPConnection("127.0.0.1", rig.server.server_address[1], timeout=2)
    conn.putrequest("POST", "/outbound?token=" + BRIDGE_TOKEN)
    conn.endheaders()
    assert conn.getresponse().status == 400
    conn.close()


def test_the_phone_is_sent_trimmed(rig):
    assert rig.post(sms(phone=" +15864603685 ")).status_code == 200
    rig.final()
    assert rig.gw.posts[0]["phoneNumbers"] == ["+15864603685"]


def test_the_same_message_id_arriving_at_once_is_sent_once(rig):
    import threading
    rig.gw.script = ["Pending"] * 3 + ["Delivered"]
    results = []
    threads = [threading.Thread(target=lambda: results.append(rig.post(sms()).json())) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    rig.final()
    assert len(rig.gw.posts) == 1
    assert sum(1 for r in results if r.get("duplicate")) == 7


def test_build_picks_the_reporter_from_the_token(tmp_path):
    from sms_bridge import app
    from sms_bridge.highlevel import HighLevelClient, NoReporter
    base = dict(bridge_token=BRIDGE_TOKEN, gateway_url="http://127.0.0.1:1", gateway_user="u", gateway_password="p",
                store_path=str(tmp_path / "s.json"))
    assert isinstance(app.build(config_mod.Config(**base, highlevel_token="t")).reporter, HighLevelClient)
    assert isinstance(app.build(config_mod.Config(**base)).reporter, NoReporter)


def test_main_exits_2_with_the_missing_names_and_no_secret(monkeypatch, capsys):
    from sms_bridge import app
    for name in ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("GATEWAY_PASSWORD", "never-printed-password")
    assert app.main() == 2
    err = capsys.readouterr().err
    assert "BRIDGE_TOKEN" in err and "never-printed-password" not in err
