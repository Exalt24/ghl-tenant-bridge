"""Fake Make custom webhook, written from help.make.com/webhooks (updated 21 Oct 2026, retrieved 2026-10-22, see
contracts/make/SOURCE.md).

Documented: 200 `Accepted` (a queue receipt only), 400 `Queue is full`, 429 `Too many requests` above 300 requests per
10 seconds, 410 Gone for a hook inactive for 5 days, an optional `x-make-apikey` header, and a default response that a
Webhook response module can replace.

ASSUMED (not in the evidence, tagged in SOURCE.md): the status for a wrong or missing api key (the fake answers 401),
and any size cap.

Modes: accepted, accepted_custom (200 with a JSON body a Webhook response module might send), queue_full, rate_limit,
gone, server_error, act_then_fail (queued and then a 500), hang, forbidden (403), bad_request (400 other body).
"""

from __future__ import annotations

from werkzeug.wrappers import Request, Response

from ._base import FakeVendor

HOOK_PATH = "/hook/abcdefghijklmnopqrstuvwxyz012345"


class FakeMake(FakeVendor):
    default_mode = "accepted"

    def __init__(self, apikey: str | None = None) -> None:
        super().__init__()
        self.rules = {"apikey"} if apikey else set()
        self.apikey = apikey

    @property
    def url(self) -> str:
        return self.base + HOOK_PATH

    def handle(self, request: Request) -> Response:
        mode = self.next_mode()
        parsed = self.parse_json(request)
        if request.method != "POST" or request.path != HOOK_PATH:
            self.record(request, parsed, mode, False)
            return Response("Not found", status=404)
        if "apikey" in self.rules and request.headers.get("x-make-apikey") != self.apikey:
            self.record(request, parsed, mode, False)
            return Response("Unauthorized", status=401)
        if mode == "hang":
            self.record(request, parsed, mode, False)
            self.hang()
            return Response("Accepted", status=200)
        if mode == "queue_full":
            self.record(request, parsed, mode, False)
            return Response("Queue is full", status=400)
        if mode == "bad_request":
            self.record(request, parsed, mode, False)
            return Response("Bad request", status=400)
        if mode == "rate_limit":
            self.record(request, parsed, mode, False)
            return Response("Too many requests", status=429)
        if mode == "gone":
            self.record(request, parsed, mode, False)
            return Response("Gone", status=410)
        if mode == "forbidden":
            self.record(request, parsed, mode, False)
            return Response("Forbidden", status=403)
        if mode == "server_error":
            self.record(request, parsed, mode, False)
            return Response("Internal error", status=500)
        if parsed is None:
            self.record(request, parsed, mode, False)
            return Response("Bad request", status=400)
        if mode == "act_then_fail":
            self.record(request, parsed, mode, True)
            return Response("Internal error", status=500)
        self.record(request, parsed, mode, True)
        if mode == "accepted_custom":
            return Response('{"result":"queued"}', status=200, mimetype="application/json")
        return Response("Accepted", status=200)
