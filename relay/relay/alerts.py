"""Alerts digest: dead deliveries and sink pauses reach a person without flooding a channel.

Every dead delivery or pause writes a row in the same transaction as the state change. This module posts ONE digest per
sink per 15 minutes to ALERT_WEBHOOK (a different Slack webhook from the lead channel). With no ALERT_WEBHOOK the
digest goes to the log as a `RELAY_ALERT` line, which is the only place it can go.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .config import Config
from .http import HttpError
from .sinks.slack import escape
from .store.base import SqlStore

log = logging.getLogger("relay.alerts")

DIGEST_INTERVAL_S = 15 * 60.0
RETRY_AFTER_FAILURE_S = 60.0
POST_TIMEOUT_S = 5.0
MAX_REASONS = 5


def digest_text(sink: str, rows: list[dict[str, Any]]) -> str:
    dead = sum(1 for r in rows if r["kind"] == "dead")
    paused = sum(1 for r in rows if r["kind"] == "pause")
    other = len(rows) - dead - paused
    tenants = sorted({r["tenant"] for r in rows if r["tenant"]})
    reasons: list[str] = []
    for r in rows:
        if r["reason"] not in reasons:
            reasons.append(r["reason"])
    parts = [f"relay alert, {sink}: {len(rows)} item(s)"]
    breakdown = [f"{n} {label}" for n, label in ((dead, "dead"), (paused, "paused"), (other, "other")) if n]
    if breakdown:
        parts.append(", ".join(breakdown))
    if tenants:
        parts.append("tenants " + ", ".join(tenants))
    parts.append("reasons: " + "; ".join(reasons[:MAX_REASONS]))
    return escape(". ".join(parts))


def flush(store: SqlStore, http: Any, cfg: Config, interval_s: float = DIGEST_INTERVAL_S) -> int:
    """Post the digests that are due. Returns how many went out."""
    sent = 0
    for sink in store.alert_sinks_waiting():
        if not store.alert_gate(sink, interval_s):
            continue
        rows = store.alerts_unsent(sink)
        if not rows:
            continue
        text = digest_text(sink, rows)
        if not cfg.alert_url:
            log.warning("RELAY_ALERT %s", text)
            store.alerts_mark_sent([r["id"] for r in rows])
            sent += 1
            continue
        try:
            res = http.request(
                "POST", cfg.alert_url, headers={"Content-Type": "application/json"},
                body=json.dumps({"text": text}).encode("utf-8"), timeout=POST_TIMEOUT_S,
            )
            ok = 200 <= res.status < 300
        except HttpError as exc:
            log.warning("RELAY_ALERT digest not delivered: %s", exc.kind)
            ok = False
        if ok:
            store.alerts_mark_sent([r["id"] for r in rows])
            sent += 1
        else:
            store.alert_gate_release(sink, interval_s, RETRY_AFTER_FAILURE_S)
    return sent
