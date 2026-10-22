"""Fake Fathom, written from developers.fathom.ai (retrieved 2026-10-22, see contracts/fathom/SOURCE.md).

Documented: GET /external/v1/meetings authenticated by an `X-Api-Key` header; `include_action_items`, `include_summary`,
`include_transcript` and `created_after`; the reply {"limit", "next_cursor", "items"} with a `next_cursor` that is null on
the last page and 10 meetings a page; 60 calls in 60 seconds, 30 for a "heavy" call (one that sets include_summary or
include_transcript), headers RateLimit-Limit, RateLimit-Remaining, RateLimit-Reset and, on a 429 only, Retry-After in
seconds.

The fake enforces the documented window with its own clock (`self.clock`), which a test can share with the client under
test, so a client that ignores its own limiter is answered with a 429 and shows up in `self.limited`.

Also here: `sign_webhook`, a Standard Webhooks signer written in this file (not imported from the relay), so a webhook
test does not pass because the relay and the test agree on the same mistake.

Modes: ok, rate_limit (429 with Retry-After when `retry_after` is set), unauthorized, server_error, hang.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections import deque
from typing import Any, Callable

from werkzeug.wrappers import Request, Response

from ._base import FakeVendor

API_KEY = "fake-fathom-key-0123456789abcdef"
PAGE_SIZE = 10
WINDOW_S = 60.0
LIMIT = 60
HEAVY_LIMIT = 30
# A made up webhook secret: `whsec_` and base64 of 32 bytes that spell nothing real.
WEBHOOK_SECRET = "whsec_" + base64.b64encode(b"fake-fathom-hook-secret-bytes-32!").decode("ascii")


def sign_webhook(secret: str, message_id: str, timestamp: int | str, body: bytes, *, versions: tuple[str, ...] = ("v1",)) -> str:
    """The `webhook-signature` header value, built independently of relay.fathom_hook."""
    key = base64.b64decode(secret.removeprefix("whsec_"))
    mac = hmac.new(key, f"{message_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    sig = base64.b64encode(mac).decode()
    return " ".join(f"{v},{sig}" for v in versions)


def webhook_headers(secret: str, message_id: str, timestamp: int, body: bytes) -> dict[str, str]:
    return {
        "webhook-id": message_id, "webhook-timestamp": str(timestamp), "webhook-signature": sign_webhook(secret, message_id, timestamp, body),
        "Content-Type": "application/json",
    }


def make_meeting(i: int, *, summary: bool = True, items: bool = True, created: str | None = None) -> dict[str, Any]:
    """A meeting shaped like the OpenAPI example (made up content)."""
    m: dict[str, Any] = {
        "title": f"Weekly sync {i}", "meeting_title": f"Sync {i}", "meeting_type": None, "recording_id": 5000 + i,
        "url": f"https://fathom.video/calls/fake{i}", "share_url": f"https://fathom.video/share/fake{i}",
        "created_at": created or f"2026-10-{(i % 20) + 1:02d}T17:01:30Z", "scheduled_start_time": "2026-10-01T16:00:00Z",
        "scheduled_end_time": "2026-10-01T17:00:00Z", "recording_start_time": "2026-10-01T16:01:12Z",
        "recording_end_time": "2026-10-01T17:00:55Z", "calendar_invitees_domains_type": "one_or_more_external",
        "shared_with": "single_team", "transcript_language": "en", "calendar_invitees": [], "recorded_by": {"name": "Pat Host", "email": "host@example.test"},
        "transcript": None, "default_summary": None, "action_items": None, "highlights": None, "crm_matches": None,
    }
    if summary:
        m["default_summary"] = {"template_name": "general", "markdown_formatted": f"## Summary\nMeeting {i} went through the plan."}
    if items:
        m["action_items"] = [
            {"description": f"Send recap {i}", "user_generated": False, "completed": False, "recording_timestamp": "00:10:45",
             "recording_playback_url": f"https://fathom.video/calls/fake{i}?timestamp=645", "assignee": {"name": "Jane Doe", "email": "jane@example.test", "team": "Ops"}}
        ]
    return m


class FakeFathom(FakeVendor):
    def __init__(self, key: str = API_KEY, clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__()
        self.rules = {"apikey", "window"}
        self.key = key
        self.clock = clock
        self.meetings: list[dict[str, Any]] = []
        self.retry_after: int | None = 3
        self.calls: deque[tuple[float, bool]] = deque()  # (time, heavy) of every call that was answered
        self.limited = 0  # how many 429s the window rule produced
        self.queries: list[dict[str, str]] = []

    def reset(self) -> None:
        super().reset()
        self.meetings = []
        self.retry_after = 3
        self.calls.clear()
        self.limited = 0
        self.queries = []

    @property
    def url(self) -> str:
        return self.base + "/external/v1"

    def _limited(self, retry_after: int | None) -> Response:
        headers = {"RateLimit-Limit": str(LIMIT), "RateLimit-Remaining": "0", "RateLimit-Reset": "1"}
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        return Response("rate limited", status=429, headers=headers)

    def handle(self, request: Request) -> Response:
        mode = self.next_mode()
        args = {k: v for k, v in request.args.items()}
        self.queries.append(args)
        if request.method != "GET" or request.path != "/external/v1/meetings":
            self.record(request, None, mode, False)
            return Response("not found", status=404)
        if "apikey" in self.rules and request.headers.get("X-Api-Key") != self.key:
            self.record(request, None, mode, False)
            return Response("unauthorized", status=401)
        if mode == "hang":
            self.record(request, None, mode, False)
            self.hang()
            return Response("{}", status=200, mimetype="application/json")
        if mode == "unauthorized":
            self.record(request, None, mode, False)
            return Response("unauthorized", status=401)
        if mode == "server_error":
            self.record(request, None, mode, False)
            return Response("error", status=500)
        heavy = args.get("include_summary") == "true" or args.get("include_transcript") == "true"
        now = self.clock()
        if mode == "rate_limit":
            self.record(request, None, mode, False)
            self.limited += 1
            return self._limited(self.retry_after)
        if "window" in self.rules:
            while self.calls and now - self.calls[0][0] >= WINDOW_S:
                self.calls.popleft()
            over = len(self.calls) >= LIMIT or (heavy and sum(1 for _t, h in self.calls if h) >= HEAVY_LIMIT)
            if over:
                self.record(request, None, "window", False)
                self.limited += 1
                return self._limited(max(1, int(WINDOW_S - (now - self.calls[0][0])) + 1))
        self.calls.append((now, heavy))
        rows = list(self.meetings)
        created_after = args.get("created_after")
        if created_after:
            rows = [m for m in rows if str(m.get("created_at", "")) > created_after]
        cursor = args.get("cursor")
        offset = 0
        if cursor:
            try:
                offset = int(json.loads(base64.b64decode(cursor))["page_num"]) * PAGE_SIZE
            except (ValueError, KeyError, TypeError):
                self.record(request, None, mode, False)
                return Response("bad cursor", status=400)
        page = []
        for m in rows[offset : offset + PAGE_SIZE]:
            item = dict(m)
            if args.get("include_action_items") != "true":
                item["action_items"] = None
            if args.get("include_summary") != "true":
                item["default_summary"] = None
            page.append(item)
        nxt = None
        if offset + PAGE_SIZE < len(rows):
            nxt = base64.b64encode(json.dumps({"page_num": offset // PAGE_SIZE + 1}).encode()).decode()
        self.record(request, None, mode, True)
        return Response(json.dumps({"limit": PAGE_SIZE, "next_cursor": nxt, "items": page}), status=200, mimetype="application/json")
