"""Client for the capcom6 android-sms-gateway local server API (basic auth)."""

from __future__ import annotations

import time

import requests

TERMINAL = ("Delivered", "Failed")


class GatewayRefused(Exception):
    """The gateway answered with a non-2xx status, so nothing was queued."""

    def __init__(self, status: int):
        super().__init__("gateway answered %d" % status)
        self.status = status


class GatewayUnreachable(Exception):
    """No usable answer. The text may or may not have been queued, so it is never sent again."""


class Gateway:
    def __init__(self, url, user, password, sim_number=None, timeout=10.0, session=None):
        self.url = url.rstrip("/")
        self.auth = (user, password)
        self.sim_number = sim_number
        self.timeout = timeout
        self.session = session or requests.Session()

    def send(self, phone: str, text: str) -> dict:
        body = {"message": text, "phoneNumbers": [phone]}
        if self.sim_number is not None:
            body["simNumber"] = self.sim_number
        try:
            resp = self.session.post(self.url + "/message", json=body, auth=self.auth, timeout=self.timeout)
        except requests.RequestException as exc:
            raise GatewayUnreachable(type(exc).__name__) from None
        if resp.status_code not in (200, 201, 202):
            raise GatewayRefused(resp.status_code)
        try:
            data = resp.json()
            return {"id": str(data["id"]), "state": str(data.get("state", "Pending"))}
        except (ValueError, KeyError, TypeError, AttributeError):
            raise GatewayUnreachable("unreadable answer") from None

    def status(self, gateway_id: str) -> dict:
        resp = self.session.get(self.url + "/message/" + gateway_id, auth=self.auth, timeout=self.timeout)
        if resp.status_code != 200:
            raise GatewayRefused(resp.status_code)
        return resp.json()

    def wait(self, gateway_id: str, interval: float, deadline: float) -> dict:
        """Poll until Delivered or Failed or the deadline. Returns state, error and timedOut."""
        end = time.monotonic() + deadline
        state, error = "Pending", None
        while True:
            try:
                data = self.status(gateway_id)
                state = str(data.get("state", state))
                error = _recipient_error(data) or error
            except (requests.RequestException, GatewayRefused, ValueError, AttributeError) as exc:
                error = error or "status check failed: " + type(exc).__name__
            if state in TERMINAL:
                return {"state": state, "error": error, "timedOut": False}
            if time.monotonic() >= end:
                return {"state": state, "error": error, "timedOut": True}
            time.sleep(interval)  # WAIT-OK: a poll of the gateway, bounded by the deadline above


def _recipient_error(data: dict) -> str | None:
    for rec in data.get("recipients") or []:
        if isinstance(rec, dict) and rec.get("error"):
            return str(rec["error"])
    return None
