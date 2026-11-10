from __future__ import annotations

import json
import threading
import time

import base64

import pytest
import requests
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from sms_bridge import app
from sms_bridge.config import Config
from sms_bridge.gateway import Gateway
from sms_bridge.highlevel import HighLevelClient

from .fakes import FakeGateway, FakeHighLevel

BRIDGE_TOKEN = "bridge-secret-0123456789"


class KeyPair:
    """A key pair made here, so a test can sign. It is not HighLevel's key; HighLevel's private half is not ours."""

    def __init__(self):
        self.private = Ed25519PrivateKey.generate()
        der = self.private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.spki_b64 = base64.b64encode(der).decode()

    def sign(self, raw: bytes) -> str:
        return base64.b64encode(self.private.sign(raw)).decode()


TEST_KEY = KeyPair()
OTHER_KEY = KeyPair()


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def sms(**over):
    body = {"contactId": "c1", "locationId": "l1", "messageId": "m1", "type": "SMS", "phone": "+15864603685",
            "message": "Hi Dana, your quote is ready", "attachments": [], "userId": "u1"}
    body.update(over)
    return body


class Rig:
    def __init__(self, tmp_path, deadline=0.6, mode="off"):
        self.gw = FakeGateway()
        self.hl = FakeHighLevel()
        self.cfg = Config(bridge_token=BRIDGE_TOKEN, gateway_url=self.gw.url, gateway_user=self.gw.user,
                          gateway_password=self.gw.password, sim_number=2, highlevel_token=self.hl.token,
                          highlevel_base=self.hl.url, store_path=str(tmp_path / "store.json"),
                          poll_interval=0.02, poll_deadline=deadline,
                          ghl_signature_mode=mode, ghl_public_key_b64=TEST_KEY.spki_b64)
        gateway = Gateway(self.cfg.gateway_url, self.cfg.gateway_user, self.cfg.gateway_password, self.cfg.sim_number, timeout=0.5)
        reporter = HighLevelClient(self.hl.token, self.hl.url, self.cfg.highlevel_version, timeout=2)
        self.bridge = app.build(self.cfg, gateway, reporter)
        self.server = app.serve(self.bridge, "127.0.0.1", 0)
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]

    def post(self, body, token=BRIDGE_TOKEN, header=False, raw=None, path="/outbound", headers=None):
        url, h = self.url + path, dict(headers or {})
        if token is not None:
            if header:
                h["X-Bridge-Token"] = token
            else:
                url += "?token=" + token
        data = raw if raw is not None else json.dumps(body).encode()
        h.setdefault("Content-Type", "application/json")
        return requests.post(url, data=data, headers=h, timeout=5)

    def final(self, n=1):
        """Wait until HighLevel has been told n times."""
        assert wait_for(lambda: len(self.hl.calls) >= n), "HighLevel was never told"
        self.bridge.join()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.gw.close()
        self.hl.close()


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    yield r
    r.close()


@pytest.fixture(params=["warn", "enforce"])
def checked_rig(request, tmp_path):
    """A bridge that verifies the signature, in each mode that does."""
    r = Rig(tmp_path, mode=request.param)
    r.mode = request.param
    yield r
    r.close()


@pytest.fixture
def enforce_rig(tmp_path):
    r = Rig(tmp_path, mode="enforce")
    yield r
    r.close()


@pytest.fixture
def warn_rig(tmp_path):
    r = Rig(tmp_path, mode="warn")
    yield r
    r.close()
