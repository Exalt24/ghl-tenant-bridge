"""Fake Slack incoming webhook, written from docs.slack.dev (retrieved 2026-10-22, see contracts/slack/SOURCE.md).

Documented: POST JSON, 200 with the plain text body `ok`, error texts such as invalid_payload and no_service, 429 with a
Retry-After header in seconds, 50 blocks per message, section text up to 3000 characters, header text up to 150, and
the three characters & < > that must be escaped in text.

Stricter than Slack on purpose (rule names in `rules`):
  escape    a raw `<` or a bare `&` in mrkdwn text is refused, because the relay never writes Slack link syntax and an
            unescaped one means untrusted text got through. Slack itself would silently reinterpret it.
  verbatim  an mrkdwn text object without `verbatim: true` is refused, for the same reason.
Both are switched off for the documentation examples (`strict=False`), which use link syntax deliberately.
"""

from __future__ import annotations

import re

from werkzeug.wrappers import Request, Response

from ._base import FakeVendor

HOOK_PATH = "/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX"
MAX_BLOCKS = 50
MAX_SECTION_TEXT = 3000
MAX_HEADER_TEXT = 150
MAX_CONTEXT_TEXT = 2000
BAD_AMP = re.compile(r"&(?!amp;|lt;|gt;)")

# Modes
#   ok             200 ok
#   rate_limit     429 with Retry-After (self.retry_after)
#   server_error   500, nothing recorded as an effect
#   act_then_fail  the message IS posted (an effect) and the answer is a 500: the case Slack documents no key for
#   hang           holds the request until released
#   forbidden      403 action_prohibited
#   gone           404 no_service
#   invalid        400 invalid_payload (whatever the body)
#   odd_ok         200 with a body that is not `ok`


class FakeSlack(FakeVendor):
    def __init__(self, strict: bool = True) -> None:
        super().__init__()
        self.rules = {"escape", "verbatim", "limits"} if strict else {"limits"}
        self.retry_after = 7

    def reset(self) -> None:
        super().reset()
        self.retry_after = 7

    @property
    def url(self) -> str:
        return self.base + HOOK_PATH

    def validate(self, payload) -> str | None:  # type: ignore[no-untyped-def]
        if not isinstance(payload, dict):
            return "invalid_payload"
        if "text" not in payload and "blocks" not in payload:
            return "no_text"
        text = payload.get("text")
        if text is not None and self._bad_text(text):
            return "invalid_payload"
        blocks = payload.get("blocks")
        if blocks is None:
            return None
        if not isinstance(blocks, list):
            return "invalid_payload"
        if "limits" in self.rules and len(blocks) > MAX_BLOCKS:
            return "invalid_payload"
        for b in blocks:
            err = self._validate_block(b)
            if err:
                return err
        return None

    def _bad_text(self, text) -> bool:  # type: ignore[no-untyped-def]
        if not isinstance(text, str):
            return True
        if "escape" in self.rules and ("<" in text or ">" in text or BAD_AMP.search(text)):
            return True
        return False

    def _validate_text_object(self, obj, max_len: int, kinds: tuple[str, ...]) -> str | None:  # type: ignore[no-untyped-def]
        if not isinstance(obj, dict) or obj.get("type") not in kinds or not isinstance(obj.get("text"), str):
            return "invalid_payload"
        txt = obj["text"]
        if "limits" in self.rules and not (1 <= len(txt) <= max_len):
            return "invalid_payload"
        if obj["type"] == "mrkdwn":
            if "verbatim" in self.rules and obj.get("verbatim") is not True:
                return "invalid_payload"
            if self._bad_text(txt):
                return "invalid_payload"
        return None

    def _validate_block(self, b) -> str | None:  # type: ignore[no-untyped-def]
        if not isinstance(b, dict):
            return "invalid_payload"
        kind = b.get("type")
        if kind == "header":
            return self._validate_text_object(b.get("text"), MAX_HEADER_TEXT, ("plain_text",))
        if kind == "section":
            if "text" in b:
                err = self._validate_text_object(b["text"], MAX_SECTION_TEXT, ("mrkdwn", "plain_text"))
                if err:
                    return err
            for f in b.get("fields", []) or []:
                err = self._validate_text_object(f, 2000, ("mrkdwn", "plain_text"))
                if err:
                    return err
            return None
        if kind == "context":
            els = b.get("elements")
            if not isinstance(els, list) or len(els) > 10:
                return "invalid_payload"
            for e in els:
                if isinstance(e, dict) and e.get("type") in ("mrkdwn", "plain_text"):
                    err = self._validate_text_object(e, MAX_CONTEXT_TEXT, ("mrkdwn", "plain_text"))
                    if err:
                        return err
            return None
        if kind == "divider":
            return None
        return "invalid_payload"

    def handle(self, request: Request) -> Response:
        mode = self.next_mode()
        parsed = self.parse_json(request)
        if request.method != "POST" or request.path != HOOK_PATH:
            self.record(request, parsed, mode, False)
            return Response("no_service", status=404)
        if mode == "hang":
            self.record(request, parsed, mode, False)
            self.hang()
            return Response("ok", status=200)
        if mode == "rate_limit":
            self.record(request, parsed, mode, False)
            return Response("rate_limited", status=429, headers={"Retry-After": str(self.retry_after)})
        if mode == "forbidden":
            self.record(request, parsed, mode, False)
            return Response("action_prohibited", status=403)
        if mode == "gone":
            self.record(request, parsed, mode, False)
            return Response("no_service", status=404)
        if mode == "invalid":
            self.record(request, parsed, mode, False)
            return Response("invalid_payload", status=400)
        if mode == "server_error":
            self.record(request, parsed, mode, False)
            return Response("internal_error", status=500)
        error = self.validate(parsed)
        if error:
            self.record(request, parsed, mode, False)
            return Response(error, status=400)
        if mode == "act_then_fail":
            self.record(request, parsed, mode, True)
            return Response("internal_error", status=500)
        self.record(request, parsed, mode, True)
        if mode == "odd_ok":
            return Response("<html>captive portal</html>", status=200)
        return Response("ok", status=200)
