"""The status callback to HighLevel: PUT /conversations/messages/{messageId}/status."""

from __future__ import annotations

from urllib.parse import quote

import requests


class HighLevelError(Exception):
    pass


class HighLevelClient:
    def __init__(self, token, base_url="https://services.leadconnectorhq.com", version="2021-04-15", timeout=10.0, session=None):
        self._token = token
        self.base_url = base_url.rstrip("/")
        self.version = version
        self.timeout = timeout
        self.session = session or requests.Session()

    def update_status(self, message_id: str, status: str, error: dict | None = None) -> None:
        body: dict = {"status": status}
        if error is not None:
            body["error"] = error
        headers = {"Authorization": "Bearer " + self._token, "Version": self.version, "Accept": "application/json"}
        url = "%s/conversations/messages/%s/status" % (self.base_url, quote(message_id, safe=""))
        try:
            resp = self.session.put(url, json=body, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise HighLevelError(type(exc).__name__) from None
        if resp.status_code != 200:
            raise HighLevelError("HighLevel answered %d" % resp.status_code)


class NoReporter:
    """Used when HIGHLEVEL_ACCESS_TOKEN is not set: the text is still sent, the status is only stored here."""

    def update_status(self, message_id, status, error=None):
        raise HighLevelError("HIGHLEVEL_ACCESS_TOKEN is not set")
