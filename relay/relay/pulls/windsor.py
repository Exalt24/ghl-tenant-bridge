"""Windsor.ai pull: daily Facebook campaign spend into `spend_daily` with source `windsor`. Contract: contracts/windsor/SOURCE.md.

  GET {base}/facebook?fields=date,campaign_id,campaign,spend,impressions,clicks,account_currency&date_from=..&date_to=..&api_key=..

  - The API key is a QUERY parameter and nothing else: Windsor documents that header authentication is not supported and
    answers {"error": "Not authorized"}. So the key sits in every URL, and the URL is therefore treated as a secret: no
    message, exception or log line in this module carries a URL, and a vendor error body is never copied anywhere (a vendor
    error page can echo the request URL back).
  - `spend` is a JSON NUMBER here (Meta's is a string). It goes through `Decimal(str(x))`, never `Decimal(x)` and never float
    arithmetic.
  - 429 backs off: `Retry-After` when the reply carries it, exponential otherwise. Windsor states 600 requests a minute and
    10,000 a day; one pull is one request, so the limit is a matter of how often the pull is scheduled.
  - 401 or 403 is a refused key: stop, alert, write nothing.
  - All or nothing, like the Meta pull.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode

from ..config import PullCfg
from ..http import PULL_BODY_TEXT, HttpError
from ..money import MoneyError, check_currency, to_minor_from_number
from ..store.base import SqlStore
from .common import MAX_TRIES, PullAbort, PullEnv, PullError, PullResult, PullThrottled, Waiter, backoff_s

__all__ = ["pull_windsor"]

SOURCE = "windsor"
FIELDS = "date,campaign_id,campaign,spend,impressions,clicks,account_currency"


def _count(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise PullError("impressions or clicks is not a whole number")
    return value


def parse_row(raw: Any, label: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PullError("a Windsor row is not an object")
    campaign = raw.get("campaign_id")
    if isinstance(campaign, bool) or not isinstance(campaign, (str, int)) or str(campaign) == "":
        raise PullError("a Windsor row has no campaign_id")
    try:
        day = date.fromisoformat(str(raw.get("date")))
    except ValueError:
        raise PullError("a Windsor row has no valid date") from None
    spend = raw.get("spend")
    if isinstance(spend, bool) or not isinstance(spend, (int, float)):
        raise PullError("spend is not a JSON number")
    try:
        currency = check_currency(raw.get("account_currency"))
        minor = to_minor_from_number(spend, currency)
    except MoneyError as exc:
        raise PullError(f"spend or account_currency refused: {exc}") from None
    return {
        "source": SOURCE, "account": label, "campaign_id": str(campaign), "day": day.isoformat(), "spend_minor": minor,
        "currency": currency, "impressions": _count(raw.get("impressions")), "clicks": _count(raw.get("clicks")),
    }


def _retry_after_s(headers: dict[str, str]) -> float | None:
    raw = headers.get("retry-after", "")
    if raw.isascii() and raw.isdigit() and int(raw) > 0:
        return float(int(raw))
    return None


def pull_windsor(store: SqlStore, cfg: PullCfg, env: PullEnv, *, today: date | None = None) -> PullResult:
    day = today or env.today()
    params = {
        "fields": FIELDS, "date_from": (day - timedelta(days=cfg.days - 1)).isoformat(), "date_to": day.isoformat(), "api_key": cfg.key,
    }
    url = f"{cfg.base_url.rstrip('/')}/facebook?{urlencode(params)}"
    result = PullResult()
    waiter = Waiter(env, result)
    last = "no answer"
    body: dict[str, Any] | None = None
    for attempt in range(MAX_TRIES):
        result.requests += 1
        try:
            res = env.http.request("GET", url, headers={"Accept": "application/json"}, timeout=cfg.timeout_s, max_text=PULL_BODY_TEXT)
        except HttpError as exc:
            last = exc.kind
            waiter.wait(backoff_s(attempt, env.rng), f"transport failure: {exc.kind}")
            continue
        if res.status == 200:
            try:
                parsed = json.loads(res.text)
            except ValueError:
                raise PullError("the response is not JSON") from None
            if not isinstance(parsed, dict):
                raise PullError("the response is not an object")
            body = parsed
            break
        last = f"http {res.status}"
        if res.status in (401, 403):
            raise PullAbort(f"the API key was refused ({res.status})")
        if res.status == 429:
            waiter.wait(_retry_after_s(res.headers) or backoff_s(attempt, env.rng), "rate limited (429)")
            continue
        if res.status >= 500:
            waiter.wait(backoff_s(attempt, env.rng), last)
            continue
        raise PullError(f"the vendor refused the request: {last}")
    if body is None:
        raise PullThrottled(backoff_s(MAX_TRIES, env.rng), f"gave up after {MAX_TRIES} tries: {last}")
    data = body.get("data")
    if not isinstance(data, list):
        raise PullError("the response has no data list")
    result.pages = 1
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    for raw in data:
        row = parse_row(raw, cfg.account)
        key = (row["campaign_id"], row["day"])
        if key in rows:
            raise PullError("two rows share a campaign and a day, so one of them is a breakdown the pull did not ask for")
        rows[key] = row
    result.rows = store.upsert_spend(list(rows.values()))
    return result
