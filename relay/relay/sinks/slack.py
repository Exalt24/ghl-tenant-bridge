"""Slack incoming webhook sink. Contract: contracts/slack/SOURCE.md.

Slack documents no idempotency key for incoming webhooks, so a send that ends in a 5xx or a timeout is AMBIGUOUS: the
message may be posted. A duplicate in a lead channel beats a missed lead; the footer carries the first 8 characters of
the event id so a duplicate is recognisable on sight. Delivery is at least once, never exactly once.
"""

from __future__ import annotations

import json
from typing import Any

from ..http import HttpConnectError, HttpFailure, HttpTimeout
from .base import Event, HttpLike, Outcome, Sink, SinkContext

MAX_BLOCKS = 50
MAX_HEADER_CHARS = 150
SECTION_LIMIT = 3000
SAFE_TEXT_CHARS = 2800  # below the 3000 limit so the ellipsis and note always fit
ANSWER_BLOCK_BUDGET = MAX_BLOCKS - 3  # header, body section and footer take the other three
MAX_RETRY_AFTER_S = 300.0
DEFAULT_RETRY_AFTER_S = 30.0
TRUNCATION_NOTE = "… (truncated, the full payload is stored by the relay)"
SLACK_ERRORS = {
    "invalid_payload", "no_service", "no_service_id", "no_active_hooks", "no_team", "no_text", "action_prohibited",
    "channel_is_archived", "channel_not_found", "user_not_found", "invalid_token", "team_disabled",
    "posting_to_general_channel_denied", "too_many_attachments",
}


def escape(text: str) -> str:
    """Slack's three mandatory escapes, in the documented order (& first so the others are not double escaped)."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cut_partial_entity(text: str) -> str:
    """A cut can land inside `&amp;`. Drop the dangling start of an entity so Slack never sees a half one."""
    amp = text.rfind("&")
    if amp != -1 and ";" not in text[amp:]:
        return text[:amp]
    return text


def truncate_escaped(text: str, limit: int = SAFE_TEXT_CHARS) -> str:
    """Escape FIRST, then cut. Cutting first is wrong: escaping grows the text (`<` becomes `&lt;`), so a text that fit
    before escaping can exceed the 3000 character limit after it and Slack rejects the whole message."""
    escaped = escape(text)
    if len(escaped) <= limit:
        return escaped
    return _cut_partial_entity(escaped[:limit]) + TRUNCATION_NOTE


def _mrkdwn(text: str) -> dict[str, Any]:
    # verbatim: true so Slack does not auto-link, hyperlink channel names or parse mentions in untrusted text.
    return {"type": "mrkdwn", "text": text, "verbatim": True}


def _line(label: str, value: str | None) -> str | None:
    return f"*{label}:* {truncate_escaped(value, 400)}" if value else None


def build_message(event: Event) -> dict[str, Any]:
    lead = event.lead
    attr = lead.get("attribution") or {}
    title = lead.get("name") or lead.get("email") or lead.get("phone") or "unknown contact"
    kind = lead.get("event_type") or "event"
    header = f"{kind}: {title}"
    if len(header) > MAX_HEADER_CHARS:
        header = header[: MAX_HEADER_CHARS - 1] + "…"

    lines = [
        _line("Name", lead.get("name")),
        _line("Email", lead.get("email")),
        _line("Phone", lead.get("phone")),
        _line("Source", lead.get("source")),
        _line("City", ", ".join(x for x in (lead.get("city"), lead.get("state")) if x) or None),
        _line("Status", lead.get("status")),
        _line("Campaign", attr.get("utm_campaign")),
        _line("UTM source", attr.get("utm_source")),
        _line("Click id", attr.get("fbclid") or attr.get("gclid")),
        _line("Tags", ", ".join(lead.get("tags") or []) or None),
    ]
    body = "\n".join(l for l in lines if l) or "_no contact fields in this event_"
    if not attr.get("present"):
        body += "\n_no attribution on this event_"

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header}},
        {"type": "section", "text": _mrkdwn(truncate_escaped_lines(body))},
    ]
    answers = list((lead.get("answers") or {}).items())
    for key, value in answers[:ANSWER_BLOCK_BUDGET]:
        blocks.append({"type": "section", "text": _mrkdwn(f"*{truncate_escaped(key, 80)}*\n{truncate_escaped(value)}")})
    extra = len(answers) - ANSWER_BLOCK_BUDGET
    footer = f"event `{event.event_id[:8]}` for {truncate_escaped(event.tenant, 64)}"
    if extra > 0:
        footer += f", {extra} more answers not shown"
    blocks.append({"type": "context", "elements": [_mrkdwn(footer)]})
    return {"text": truncate_escaped(f"{kind}: {title}", 300), "blocks": blocks}


def truncate_escaped_lines(body: str) -> str:
    """The body lines were each escaped already; only the total needs the cap."""
    if len(body) <= SAFE_TEXT_CHARS:
        return body
    return _cut_partial_entity(body[:SAFE_TEXT_CHARS]) + TRUNCATION_NOTE


def _retry_after(headers: dict[str, str]) -> float:
    try:
        v = float(headers.get("retry-after", ""))
    except ValueError:
        return DEFAULT_RETRY_AFTER_S
    return min(max(v, 1.0), MAX_RETRY_AFTER_S)


class SlackSink(Sink):
    name = "slack"
    default_timeout_s = 10.0
    default_min_interval_s = 1.0  # docs: about one message per second per channel

    def send(self, event: Event, http: HttpLike, ctx: SinkContext) -> Outcome:
        body = json.dumps(build_message(event), ensure_ascii=False).encode("utf-8")
        try:
            res = http.request(
                "POST", ctx.url, headers={"Content-Type": "application/json"}, body=body, timeout=ctx.timeout_s
            )
        except HttpConnectError as exc:
            # Never connected, so Slack cannot have posted it: retry without spending the budget.
            return Outcome.retry(reason=exc.kind, breaker_failure=True)
        except (HttpTimeout, HttpFailure) as exc:
            return Outcome.ambiguous(reason=exc.kind)

        status = res.status
        text = res.text.strip()
        if 200 <= status < 300:
            if text == "ok":
                return Outcome.done(status=status)
            return Outcome.dead("unexpected 200 body", status=status)
        if status == 429:
            wait = _retry_after(res.headers)
            return Outcome.retry(after_s=wait, status=status, reason="rate limited", rate_limited=True)
        if status >= 500:
            return Outcome.ambiguous(status=status, reason=f"http {status}")
        code = text if text in SLACK_ERRORS else None
        reason = f"http {status}" + (f" {code}" if code else "")
        if status == 403:
            return Outcome.pause(reason, status=status)
        return Outcome.dead(reason, status=status)
