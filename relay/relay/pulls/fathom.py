"""Fathom puller: the fallback for meetings the webhook never delivered. Contract: contracts/fathom/SOURCE.md.

Fathom fires no webhook for a call without a summary, so this lists meetings and stores the same reduced record the webhook
receiver stores (title, summary when asked for, action items; never the transcript).

  GET {base}/meetings?include_action_items=true&created_after=..&cursor=..     header X-Api-Key

  - Pages by `next_cursor` until it is null. A cursor that comes back twice ends the run (a loop).
  - A client side limiter keeps the calls inside Fathom's stated windows: 60 calls per 60 seconds, and 30 per 60 seconds for
    a heavy call (one that sets include_summary or include_transcript). A heavy call counts in BOTH windows. The numbers are
    Fathom's current defaults, which it may lower under load, so a 429 is still answered with its `Retry-After`.
  - 429: wait `Retry-After` seconds (60 when the reply has none), up to the run's wait budget, then try again.
  - 401: the key was refused, stop and alert.
  - The X-Api-Key never appears in a log line, an exception or a stored column: it is a header, this module never logs a
    header, and the HTTP seam never puts one in an exception.
  - All or nothing: meetings are written in one transaction after the last page.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

from ..config import PullCfg
from ..fathom_hook import action_items_text, normalise_meeting
from ..http import PULL_BODY_TEXT, HttpError
from ..store.base import SqlStore
from .common import MAX_PAGES, MAX_TRIES, SLEEP_PAD_S, PullAbort, PullEnv, PullError, PullResult, PullThrottled, Waiter, WindowLimiter, backoff_s

__all__ = ["FathomLimiter", "pull_fathom"]

CALLS_PER_WINDOW = 60
HEAVY_CALLS_PER_WINDOW = 30
WINDOW_S = 60.0
DEFAULT_RETRY_AFTER_S = 60.0


class FathomLimiter:
    """Both windows at once. A light call needs a slot in the global window; a heavy call needs one in the global AND the
    heavy window, and takes both."""

    def __init__(self, env: PullEnv) -> None:
        self.env = env
        self.global_window = WindowLimiter(CALLS_PER_WINDOW, WINDOW_S, env.monotonic, env.sleeper)
        self.heavy_window = WindowLimiter(HEAVY_CALLS_PER_WINDOW, WINDOW_S, env.monotonic, env.sleeper)

    def acquire(self, heavy: bool) -> float:
        waited = 0.0
        while True:
            need = max(self.global_window.ready_in(), self.heavy_window.ready_in() if heavy else 0.0)
            if need <= 0:
                self.global_window.record()
                if heavy:
                    self.heavy_window.record()
                return waited
            self.env.sleeper(need + SLEEP_PAD_S)
            waited += need + SLEEP_PAD_S


def _retry_after_s(headers: dict[str, str]) -> float:
    raw = headers.get("retry-after", "")
    if raw.isascii() and raw.isdigit() and int(raw) > 0:
        return float(int(raw))
    return DEFAULT_RETRY_AFTER_S


def pull_fathom(store: SqlStore, cfg: PullCfg, env: PullEnv, *, tenant: str, today: date | None = None, limiter: FathomLimiter | None = None) -> PullResult:
    day = today or env.today()
    since = f"{(day - timedelta(days=cfg.days)).isoformat()}T00:00:00Z"
    include_summary = bool(cfg.extra.get("include_summary"))
    limiter = limiter or FathomLimiter(env)
    result = PullResult()
    waiter = Waiter(env, result)
    base = cfg.base_url.rstrip("/")
    meetings: dict[str, dict[str, Any]] = {}
    cursor: str | None = None
    seen: set[str] = set()
    while True:
        params = {"include_action_items": "true", "created_after": since}
        if include_summary:
            params["include_summary"] = "true"
        if cursor is not None:
            params["cursor"] = cursor
        url = f"{base}/meetings?{urlencode(params)}"
        body = _get_page(env, cfg, url, include_summary, limiter, waiter, result)
        items = body.get("items")
        if not isinstance(items, list):
            raise PullError("the response has no items list")
        result.pages += 1
        for item in items:
            meeting = normalise_meeting(item)
            if meeting is None:
                result.skipped += 1
                continue
            meetings[meeting["recording_id"]] = {
                "recording_id": meeting["recording_id"], "title": meeting["title"],
                "summary": meeting["summary"], "action_items": action_items_text(meeting),
            }
        nxt = body.get("next_cursor")
        if nxt is None:
            break
        if not isinstance(nxt, str) or nxt == "":
            raise PullError("next_cursor is not text")
        if nxt in seen:
            raise PullError("next_cursor repeated a cursor it had already returned")
        if len(seen) >= MAX_PAGES:
            raise PullError("paging did not end")
        seen.add(nxt)
        cursor = nxt
    result.rows = store.upsert_meetings(tenant, list(meetings.values()))
    return result


def _get_page(env: PullEnv, cfg: PullCfg, url: str, heavy: bool, limiter: FathomLimiter, waiter: Waiter, result: PullResult) -> dict[str, Any]:
    last = "no answer"
    for attempt in range(MAX_TRIES):
        # Pacing under the stated windows is not a failure wait, so it does not spend the run's wait budget.
        result.detail["paced_s"] = result.detail.get("paced_s", 0.0) + limiter.acquire(heavy)
        result.requests += 1
        try:
            res = env.http.request("GET", url, headers={"X-Api-Key": cfg.key, "Accept": "application/json"}, timeout=cfg.timeout_s, max_text=PULL_BODY_TEXT)
        except HttpError as exc:
            last = exc.kind
            waiter.wait(backoff_s(attempt, env.rng), f"transport failure: {exc.kind}")
            continue
        if res.status == 200:
            try:
                body = json.loads(res.text)
            except ValueError:
                raise PullError("the response is not JSON") from None
            if not isinstance(body, dict):
                raise PullError("the response is not an object")
            return body
        last = f"http {res.status}"
        if res.status == 401:
            raise PullAbort("the API key was refused (401)")
        if res.status == 429:
            waiter.wait(_retry_after_s(res.headers), "rate limited (429)")
            continue
        if res.status >= 500:
            waiter.wait(backoff_s(attempt, env.rng), last)
            continue
        raise PullError(f"the vendor refused the request: {last}")
    raise PullThrottled(backoff_s(MAX_TRIES, env.rng), f"gave up after {MAX_TRIES} tries: {last}")
