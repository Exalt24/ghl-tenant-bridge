"""The Fathom webhook: Standard Webhooks verification, and the reduced meeting record the receiver stores.

Contract: contracts/fathom/SOURCE.md (developers.fathom.ai/webhooks and the OpenAPI file, retrieved 2026-10-22).

Verification, exactly as Fathom's page lays it out:
  signed content   `{webhook-id}.{webhook-timestamp}.{raw body}`, built from the RAW bytes before any JSON parsing
  key              base64 decode of the part of the secret after `whsec_`
  signature        HMAC-SHA256 of the signed content, base64 encoded
  header           `webhook-signature` holds space delimited entries `v1,<base64>`; EVERY v1 entry is compared, in
                   constant time, and the request is authentic when any one matches
  timestamp        seconds since the epoch, within a tolerance (5 minutes by default) in BOTH directions

`webhook-id` stays the same when Fathom resends a message, so the receiver dedupes on it.

This module has no Flask and no store: the route in app.py reads the bytes, calls `verify`, and stores the result.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
from typing import Any, Mapping

__all__ = ["MAX_ID_CHARS", "build_reduced_payload", "normalise_meeting", "parse_secret", "sign", "verify"]

SECRET_PREFIX = "whsec_"
MIN_KEY_BYTES = 16
MAX_ID_CHARS = 128
MAX_TITLE_CHARS = 300
MAX_SUMMARY_CHARS = 20000
MAX_ACTION_ITEMS = 50
MAX_ACTION_CHARS = 500


def parse_secret(secret: str) -> bytes:
    """The HMAC key. Raises ValueError (never carrying the secret) for anything that is not `whsec_` plus base64."""
    if not isinstance(secret, str) or not secret.startswith(SECRET_PREFIX):
        raise ValueError("the secret must start with whsec_")
    try:
        key = base64.b64decode(secret[len(SECRET_PREFIX):], validate=True)
    except (binascii.Error, ValueError):
        raise ValueError("the secret is not base64") from None
    if len(key) < MIN_KEY_BYTES:
        raise ValueError("the secret is too short")
    return key


def sign(key: bytes, message_id: str, timestamp: str, raw: bytes) -> str:
    """base64(HMAC-SHA256(key, id.timestamp.body)). Over BYTES, so the body is never decoded and re-encoded."""
    signed = message_id.encode("utf-8") + b"." + timestamp.encode("utf-8") + b"." + raw
    return base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode("ascii")


def verify(key: bytes, headers: Mapping[str, str], raw: bytes, now: float, tolerance_s: float) -> str | None:
    """None when the delivery is authentic, otherwise a short reason: missing-header, bad-timestamp,
    timestamp-outside-window or bad-signature."""
    h = {k.lower(): v for k, v in headers.items()}
    message_id, timestamp, signature = h.get("webhook-id"), h.get("webhook-timestamp"), h.get("webhook-signature")
    if not message_id or not timestamp or not signature or len(message_id) > MAX_ID_CHARS:
        return "missing-header"
    if not (timestamp.isascii() and timestamp.isdigit()):
        return "bad-timestamp"
    if abs(now - int(timestamp)) > tolerance_s:
        return "timestamp-outside-window"
    expected = sign(key, message_id, timestamp, raw).encode("ascii")
    matched = False
    for entry in signature.split(" "):
        version, _comma, value = entry.partition(",")
        if version != "v1" or not _comma:
            continue
        # Every entry is compared and the loop never exits early, so how long this takes does not say which entry matched.
        # An entry that is not ASCII cannot equal a base64 string and must not raise.
        matched |= hmac.compare_digest(expected, value.encode("utf-8"))
    return None if matched else "bad-signature"


# ---- the meeting record ---------------------------------------------------------------------------------------------------


def _text(v: Any, limit: int) -> str | None:
    return v[:limit] if isinstance(v, str) and v.strip() else None


def _action_items(raw: Any) -> list[dict[str, Any]] | None:
    if not isinstance(raw, list):
        return None
    out: list[dict[str, Any]] = []
    for item in raw:
        if len(out) >= MAX_ACTION_ITEMS:
            break
        if not isinstance(item, dict):
            continue
        description = _text(item.get("description"), MAX_ACTION_CHARS)
        if description is None:
            continue
        assignee = item.get("assignee")
        out.append(
            {
                "description": description,
                "completed": item.get("completed") is True,
                "assignee": _text(assignee.get("name"), 100) if isinstance(assignee, dict) else None,
            }
        )
    return out


def normalise_meeting(doc: Any) -> dict[str, Any] | None:
    """The fields the relay keeps from a Fathom meeting (a webhook body or one item of the list endpoint), or None when
    there is no usable `recording_id`. The transcript, the invitee emails and the CRM matches are NOT kept: the relay
    stores a title, the summary and the action items, and nothing it does not need."""
    if not isinstance(doc, dict):
        return None
    rid = doc.get("recording_id")
    if isinstance(rid, bool) or not isinstance(rid, (int, str)) or str(rid).strip() == "":
        return None
    summary = doc.get("default_summary")
    items = _action_items(doc.get("action_items"))
    url = doc.get("url")
    return {
        "recording_id": str(rid).strip()[:MAX_ID_CHARS],
        "title": _text(doc.get("title"), MAX_TITLE_CHARS) or _text(doc.get("meeting_title"), MAX_TITLE_CHARS),
        "summary": _text(summary.get("markdown_formatted"), MAX_SUMMARY_CHARS) if isinstance(summary, dict) else None,
        "action_items": items,
        "url": url if isinstance(url, str) and url.startswith("https://") and len(url) <= 500 else None,
        "created_at": _text(doc.get("created_at"), 40),
    }


def build_reduced_payload(meeting: dict[str, Any]) -> str:
    """What goes into `events.payload` for a Fathom event: the reduced record, as JSON text."""
    return json.dumps(meeting, ensure_ascii=False)


def action_items_text(meeting: dict[str, Any]) -> str | None:
    """The `fathom_meetings.action_items` column: the action items as JSON text, or NULL when the meeting carried none."""
    items = meeting.get("action_items")
    return json.dumps(items, ensure_ascii=False) if items is not None else None
