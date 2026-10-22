"""Meta Ads Insights pull: daily campaign spend into `spend_daily`. Contract: contracts/meta/SOURCE.md.

  GET {base}/act_<id>/insights?level=campaign&time_increment=1&fields=...&time_range={"since","until"}&limit=...

What it does, each point with a test and a mutant:
  - Pages ONLY by `paging.next`, and stops only when `next` is absent. A page can be empty and still carry `next` (Meta's
    paging page says so), so an empty page is not the end. A cursor is never stored. A `next` that points at another host
    is refused, because following it would hand the access token to that host.
  - `spend` is a numeric STRING. It is parsed with Decimal to integer minor units with an ISO 4217 exponent table, never
    float. `account_currency` is stored with every row. That `spend` is in the account currency is the likely reading and
    is NOT stated by Meta (ASSUMED, see SOURCE.md).
  - Re-pulls the last 28 days (configurable) on every run and upserts on the primary key, because Meta restates recent days
    and the figures are stable after 28 days.
  - Error 190: stop, alert, write nothing. Throttle codes (4, 17, 341, 613, the 80000 series): wait for
    `estimated_time_to_regain_access` (minutes) from `x-business-use-case-usage` or `x-business-use-case` when present,
    else back off exponentially; a wait longer than the run will hold ends the run (PullThrottled).
  - Error 100 with subcode 1487534 (too much data per call): split the date range in two and pull each half, down to a
    single day.
  - All or nothing: rows are written in one transaction after every page of every range arrived.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlencode, urlsplit

from ..config import PullCfg
from ..http import PULL_BODY_TEXT, HttpError
from ..money import MoneyError, check_currency, to_minor_from_text
from ..sinks.capi import error_code, is_retry_code, is_throttle_code, regain_wait_s
from ..store.base import SqlStore
from .common import MAX_PAGES, MAX_TRIES, PullAbort, PullEnv, PullError, PullResult, PullThrottled, Waiter, backoff_s

__all__ = ["pull_meta_spend"]

SOURCE = "meta"
FIELDS = "campaign_id,campaign_name,spend,impressions,clicks,account_currency"
TOO_MUCH_DATA = (100, 1487534)


class TooMuchData(Exception):
    """Error 100, subcode 1487534: the range asked for more than one call can return. Caught here and answered by splitting."""


def normalise_account(account: str) -> str:
    return account if account.startswith("act_") else f"act_{account}"


def error_subcode(text: str) -> int | None:
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    err = doc.get("error") if isinstance(doc, dict) else None
    sub = err.get("error_subcode") if isinstance(err, dict) else None
    return sub if isinstance(sub, int) and not isinstance(sub, bool) else None


def _count(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise PullError("impressions or clicks is not a count")
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    raise PullError("impressions or clicks is not a count")


def parse_row(raw: Any, account: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise PullError("an insights row is not an object")
    campaign = raw.get("campaign_id")
    if isinstance(campaign, bool) or not isinstance(campaign, (str, int)) or str(campaign) == "":
        raise PullError("an insights row has no campaign_id")
    try:
        start, stop = date.fromisoformat(str(raw.get("date_start"))), date.fromisoformat(str(raw.get("date_stop")))
    except ValueError:
        raise PullError("an insights row has no valid date_start and date_stop") from None
    if start != stop:
        raise PullError("an insights row spans more than one day, so time_increment=1 was not honoured")
    spend = raw.get("spend")
    if not isinstance(spend, str):
        raise PullError("spend is not a numeric string")
    try:
        currency = check_currency(raw.get("account_currency"))
        minor = to_minor_from_text(spend, currency)
    except MoneyError as exc:
        raise PullError(f"spend or account_currency refused: {exc}") from None
    return {
        "source": SOURCE, "account": account, "campaign_id": str(campaign), "day": start.isoformat(), "spend_minor": minor,
        "currency": currency, "impressions": _count(raw.get("impressions")), "clicks": _count(raw.get("clicks")),
    }


class _Puller:
    def __init__(self, cfg: PullCfg, env: PullEnv) -> None:
        self.cfg = cfg
        self.env = env
        self.account = normalise_account(cfg.account)
        self.base = cfg.base_url.rstrip("/")
        self.origin = (urlsplit(self.base).scheme, urlsplit(self.base).netloc)
        self.result = PullResult()
        self.waiter = Waiter(env, self.result)
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}

    # ---- one request, with every documented error answered -------------------------------------------------------------
    def get_json(self, url: str) -> dict[str, Any]:
        last = "no answer"
        for attempt in range(MAX_TRIES):
            self.result.requests += 1
            try:
                res = self.env.http.request("GET", url, headers={"Accept": "application/json"}, timeout=self.cfg.timeout_s, max_text=PULL_BODY_TEXT)
            except HttpError as exc:
                last = exc.kind
                self.waiter.wait(backoff_s(attempt, self.env.rng), f"transport failure: {exc.kind}")
                continue
            if res.status == 200:
                try:
                    body = json.loads(res.text)
                except ValueError:
                    raise PullError("the response is not JSON") from None
                if not isinstance(body, dict):
                    raise PullError("the response is not an object")
                return body
            code, sub = error_code(res.text), error_subcode(res.text)
            if code == 190:
                raise PullAbort("access token refused (190)")
            if (code, sub) == TOO_MUCH_DATA:
                raise TooMuchData()
            last = f"http {res.status}" + (f" code {code}" if code is not None else "")
            if code is not None and is_throttle_code(code):
                wait = regain_wait_s(res.headers)
                self.waiter.wait(wait if wait is not None else backoff_s(attempt, self.env.rng), f"throttled (code {code})")
                continue
            if res.status >= 500 or (code is not None and is_retry_code(code)):
                self.waiter.wait(backoff_s(attempt, self.env.rng), last)
                continue
            raise PullError(f"the vendor refused the request: {last}")
        raise PullThrottled(backoff_s(MAX_TRIES, self.env.rng), f"gave up after {MAX_TRIES} tries: {last}")

    # ---- one date range, every page ------------------------------------------------------------------------------------
    def fetch_range(self, since: date, until: date) -> list[dict[str, Any]]:
        params = {
            "level": "campaign", "time_increment": "1", "fields": FIELDS, "limit": str(self.cfg.extra["limit"]),
            "time_range": json.dumps({"since": since.isoformat(), "until": until.isoformat()}, separators=(",", ":")),
            "access_token": self.cfg.key,
        }
        url: str | None = f"{self.base}/{self.account}/insights?{urlencode(params)}"
        seen: set[str] = set()
        out: list[dict[str, Any]] = []
        while url is not None:
            if url in seen:
                raise PullError("paging.next repeated a page it had already returned")
            if len(seen) >= MAX_PAGES:
                raise PullError("paging did not end")
            seen.add(url)
            body = self.get_json(url)
            data = body.get("data")
            if not isinstance(data, list):
                raise PullError("the response has no data list")
            self.result.pages += 1
            out.extend(parse_row(r, self.account) for r in data)
            paging = body.get("paging")
            nxt = paging.get("next") if isinstance(paging, dict) else None
            if nxt is None:
                url = None
                continue
            if not isinstance(nxt, str) or (urlsplit(nxt).scheme, urlsplit(nxt).netloc) != self.origin:
                raise PullError("paging.next points away from the configured host and was not followed")
            url = nxt
        return out

    def collect(self, since: date, until: date) -> None:
        try:
            rows = self.fetch_range(since, until)
        except TooMuchData:
            if since == until:
                raise PullError("one day is still too much data for one call") from None
            self.result.splits += 1
            middle = since + timedelta(days=(until - since).days // 2)
            self.collect(since, middle)
            self.collect(middle + timedelta(days=1), until)
            return
        for r in rows:
            self.rows[(r["campaign_id"], r["day"])] = r


def pull_meta_spend(store: SqlStore, cfg: PullCfg, env: PullEnv, *, today: date | None = None) -> PullResult:
    """Pull the last `cfg.days` days up to and including `today` and upsert them. Raises PullAbort, PullThrottled or
    PullError; in every one of those cases nothing was written."""
    day = today or env.today()
    puller = _Puller(cfg, env)
    puller.collect(day - timedelta(days=cfg.days - 1), day)
    puller.result.rows = store.upsert_spend(list(puller.rows.values()))
    return puller.result
