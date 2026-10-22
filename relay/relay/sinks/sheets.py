"""Google Sheets sink: a service account signs a JWT, trades it for an access token, and appends one row per event.
Contract: contracts/google/SOURCE.md (pages retrieved 2026-10-22, RFC 7523 for the grant type).

A Sheets append has no idempotency key, so a send that ends in a 5xx or a timeout is AMBIGUOUS: the row may be there.
Three things make a duplicate row rare, and the rest is stated in the README as at least once:
  1. The ambiguous answer is resolved at once by reading column A back (the fan-out calls `reconcile`).
  2. A later try on a row that MAY have been written (an earlier ambiguous send, a read-back that found nothing yet, or a
     worker that died mid-call) reads column A again BEFORE it sends, and once more inside the append lock. A read-back
     that finds nothing counts only when 60 seconds have passed since the last try that may have written, because the
     docs say nothing we saved about when a slow append lands.
  3. One append in flight per spreadsheet across processes (a Postgres advisory lock, a lock row on SQLite).

Where a status code's meaning rests on the generic Google error format and not on a saved page it is marked ASSUMED: the
troubleshooting page lists only 400, 500 and 503, so 401, 403 and 404 below are ASSUMED. The 429 backoff formula is not.
Concurrent appends to one sheet: not found, the concurrent-edits page was not retrieved.

The append names column A as its range (`Sheet1!A:A`), not the bare sheet. The page says the range is searched for a logical
table and the row goes after that table's last row at its first column. Column A always holds the event id, so it is never
empty, and the table found there is the relay's own: a column that is empty in every row further right (a lead that never
carries utm_campaign) can no longer split the table and send the next row to another column.
"""

from __future__ import annotations

import base64
import json
import random
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import quote, urlencode

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ..http import HttpConnectError, HttpError, HttpFailure, HttpResult, HttpTimeout
from .base import Event, HttpLike, Outcome, Sink, SinkContext

SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"  # RFC 7523, the HTTP tab of Google's page was not retrieved
JWT_LIFETIME_S = 3600
MAX_JWT_LIFETIME_S = 65 * 60  # Google rejects an exp more than 65 minutes after iat
TOKEN_REFRESH_MARGIN_S = 300
ABSENT_AFTER_S = 60.0
LOCK_RETRY_S = 2.0
MAX_CELL_CHARS = 49_000
MAX_429_WAIT_S = 32.0
SHEET_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,99}")
TABLE_RANGE_RE = re.compile(r"^(?:'[^']*'|[^!]*)!\$?([A-Z]{1,3})\$?\d+(?::\$?[A-Z]{1,3}\$?\d+)?$")
TOKEN_ERRORS = {"invalid_grant", "invalid_scope", "invalid_client", "disabled_client", "deleted_client", "org_internal", "invalid_request", "unauthorized_client"}

COLUMNS: dict[str, Callable[[Event], Any]] = {
    "event_id": lambda e: e.event_id,
    "received_at": lambda e: None if e.received_epoch is None else time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(e.received_epoch)),
    "event_type": lambda e: e.lead.get("event_type"),
    "name": lambda e: e.lead.get("name"),
    "email": lambda e: e.lead.get("email"),
    "phone": lambda e: e.lead.get("phone"),
    "city": lambda e: e.lead.get("city"),
    "state": lambda e: e.lead.get("state"),
    "source": lambda e: e.lead.get("source"),
    "status": lambda e: e.lead.get("status"),
    "tags": lambda e: ", ".join(e.lead.get("tags") or []),
    "utm_source": lambda e: (e.lead.get("attribution") or {}).get("utm_source"),
    "utm_medium": lambda e: (e.lead.get("attribution") or {}).get("utm_medium"),
    "utm_campaign": lambda e: (e.lead.get("attribution") or {}).get("utm_campaign"),
    "utm_content": lambda e: (e.lead.get("attribution") or {}).get("utm_content"),
    "utm_term": lambda e: (e.lead.get("attribution") or {}).get("utm_term"),
    "fbclid": lambda e: (e.lead.get("attribution") or {}).get("fbclid"),
    "gclid": lambda e: (e.lead.get("attribution") or {}).get("gclid"),
}
DEFAULT_COLUMNS = ["event_id", "received_at", "event_type", "name", "email", "phone", "city", "state", "source", "utm_source", "utm_campaign", "fbclid"]
OPTION_KEYS = {"sheet", "columns"}


# ---- the service account and the JWT --------------------------------------------------------------------------------------------


@dataclass
class Account:
    email: str
    key: rsa.RSAPrivateKey
    token_uri: str
    key_id: str | None


def parse_account(text: str) -> Account:
    """The service account JSON a key download holds. Raises ValueError with a reason that carries no key material."""
    try:
        doc = json.loads(text)
    except ValueError:
        raise ValueError("the service account value is not JSON") from None
    if not isinstance(doc, dict):
        raise ValueError("the service account value must be a JSON object")
    email, pem = doc.get("client_email"), doc.get("private_key")
    if not (isinstance(email, str) and email) or not (isinstance(pem, str) and pem):
        raise ValueError("the service account JSON needs client_email and private_key")
    try:
        key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    except (ValueError, TypeError):
        raise ValueError("the service account private_key is not a readable PEM key") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError("the service account key must be RSA")
    uri = doc.get("token_uri", DEFAULT_TOKEN_URI)
    if not isinstance(uri, str) or not uri:
        raise ValueError("token_uri must be a string")
    kid = doc.get("private_key_id")
    return Account(email=email, key=key, token_uri=uri, key_id=kid if isinstance(kid, str) and kid else None)


def b64url(data: bytes) -> str:
    """Base64url WITHOUT padding: Google rejects an assertion with padding equal signs or newlines."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_claims(account: Account, now: float) -> dict[str, Any]:
    iat = int(now)
    return {"iss": account.email, "scope": SCOPE, "aud": account.token_uri, "iat": iat, "exp": iat + JWT_LIFETIME_S}


def make_assertion(account: Account, now: float) -> str:
    """header.claims.signature, RS256 (RSASSA-PKCS1-v1_5 with SHA-256, RFC 7518, not retrieved)."""
    header: dict[str, Any] = {"alg": "RS256", "typ": "JWT"}
    if account.key_id:
        header["kid"] = account.key_id
    signing_input = b64url(json.dumps(header, separators=(",", ":")).encode()) + "." + b64url(json.dumps(build_claims(account, now), separators=(",", ":")).encode())
    signature = account.key.sign(signing_input.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return signing_input + "." + b64url(signature)


def backoff_429(retries: int, rng: random.Random) -> float:
    """The documented formula: min(2^n + a random amount up to 1 second, 32 seconds)."""
    return min(2 ** max(retries, 0) + rng.random(), MAX_429_WAIT_S)


def table_start_column(table_range: Any) -> str | None:
    """The first column letter of the `tableRange` an append answers with ("Sheet1!A1:D5" gives "A"), None when empty."""
    if not isinstance(table_range, str) or not table_range:
        return None
    m = TABLE_RANGE_RE.match(table_range)
    return m.group(1) if m else "?"


class SheetsSink(Sink):
    name = "sheets"
    default_timeout_s = 10.0
    default_min_interval_s = 1.0  # 60 writes per minute per user is the documented quota, and a service account is one user
    can_reconcile = True
    required_env = ("spreadsheet_id", "service_account")

    def __init__(self, clock: Callable[[], float] = time.time, rng: random.Random | None = None) -> None:
        self.clock = clock
        self.rng = rng or random.Random()
        self._mutex = threading.Lock()
        self._accounts: dict[str, Account] = {}
        self._tokens: dict[str, tuple[str, float]] = {}
        self._bad_start: dict[tuple[str, str], float] = {}

    # ---- config ---------------------------------------------------------------------------------------------------------
    @classmethod
    def check_options(cls, options: dict[str, Any], env_values: dict[str, str], live: bool) -> dict[str, Any]:
        unknown = sorted(set(options) - OPTION_KEYS)
        if unknown:
            raise ValueError(f"unknown option `{unknown[0]}`")
        sheet = options.get("sheet")
        if not (isinstance(sheet, str) and SHEET_RE.fullmatch(sheet)):
            raise ValueError(
                "options.sheet must be a bare sheet name (letters, digits and underscore, no spaces or quotes): "
                "the quoting rule for other titles was not retrieved"
            )
        columns = options.get("columns", DEFAULT_COLUMNS)
        if not isinstance(columns, list) or not columns or not all(isinstance(c, str) and c in COLUMNS for c in columns):
            raise ValueError(f"options.columns must be a list of: {', '.join(sorted(COLUMNS))}")
        if columns[0] != "event_id":
            raise ValueError("options.columns must start with event_id: column A always holds the event id")
        if len(set(columns)) != len(columns):
            raise ValueError("options.columns names a column twice")
        try:
            account = parse_account(env_values["service_account"])
        except ValueError as exc:
            raise ValueError(f"env.service_account: {exc}") from None
        from ..config import ConfigError, check_base_url

        try:
            check_base_url(account.token_uri, live, "the service account token_uri")
        except ConfigError as exc:
            raise ValueError(str(exc)) from None
        return {"sheet": sheet, "columns": list(columns), "token_uri": account.token_uri}

    @classmethod
    def secret_values(cls, env_values: dict[str, str]) -> list[str]:
        """The JSON, the private key, and each line of the PEM body (a log line can hold one line of it)."""
        out = list(env_values.values())
        try:
            pem = json.loads(env_values["service_account"])["private_key"]
        except (ValueError, KeyError, TypeError):
            return out
        out.append(pem)
        out.extend(line.strip() for line in pem.splitlines() if len(line.strip()) >= 16 and "PRIVATE KEY" not in line)
        return out

    # ---- the row ----------------------------------------------------------------------------------------------------------
    @staticmethod
    def row_for(event: Event, columns: list[str]) -> list[str]:
        """One string per configured column. A cell is cut to 49,000 characters (the cell limit is 50,000) and sent RAW, so
        a value that starts with `=` stays text."""
        row: list[str] = []
        for col in columns:
            value = COLUMNS[col](event)
            row.append("" if value is None else str(value)[:MAX_CELL_CHARS])
        return row

    # ---- the token ---------------------------------------------------------------------------------------------------------
    def _account(self, ctx: SinkContext) -> Account:
        text = ctx.extra["service_account"]
        with self._mutex:
            acct = self._accounts.get(text)
            if acct is None:
                acct = self._accounts[text] = parse_account(text)
            return acct

    def _token(self, http: HttpLike, ctx: SinkContext, force: bool = False) -> str | Outcome:
        """A cached access token (refreshed 5 minutes before it expires), or the Outcome that explains why there is none."""
        acct = self._account(ctx)
        now = self.clock()
        with self._mutex:
            cached = self._tokens.get(acct.email)
            if cached is not None and not force and cached[1] - TOKEN_REFRESH_MARGIN_S > now:
                return cached[0]
            self._tokens.pop(acct.email, None)
        body = urlencode({"grant_type": GRANT_TYPE, "assertion": make_assertion(acct, now)}).encode("ascii")
        try:
            res = http.request("POST", acct.token_uri, headers={"Content-Type": "application/x-www-form-urlencoded"}, body=body, timeout=ctx.timeout_s)
        except HttpError as exc:
            # The token call writes nothing to the sheet, so every failure here is a plain retry.
            return Outcome.retry(reason=f"token {exc.kind}", breaker_failure=True)
        if res.status == 200:
            try:
                doc = json.loads(res.text)
                token, ttl = doc["access_token"], float(doc.get("expires_in", 3600))
            except (ValueError, KeyError, TypeError):
                return Outcome.retry(status=res.status, reason="token answer unreadable", breaker_failure=True)
            if not isinstance(token, str) or not token:
                return Outcome.retry(status=res.status, reason="token answer unreadable", breaker_failure=True)
            with self._mutex:
                self._tokens[acct.email] = (token, now + ttl)
            return token
        if res.status == 429 or res.status >= 500:
            return Outcome.retry(status=res.status, reason=f"token endpoint http {res.status}", breaker_failure=res.status >= 500, rate_limited=res.status == 429)
        code = _token_error(res.text)
        return Outcome.pause(f"token endpoint refused: http {res.status}" + (f" {code}" if code else ""), status=res.status)

    def _call(self, http: HttpLike, ctx: SinkContext, method: str, url: str, body: bytes | None) -> HttpResult | Outcome:
        """One authorised call. A 401 gets ONE refreshed token and one more try (ASSUMED: the saved pages do not say)."""
        res: HttpResult | None = None
        for refreshed in (False, True):
            token = self._token(http, ctx, force=refreshed)
            if isinstance(token, Outcome):
                return token
            headers = {"Authorization": f"Bearer {token}"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            res = http.request(method, url, headers=headers, body=body, timeout=ctx.timeout_s)
            if res.status != 401:
                return res
        assert res is not None
        return res

    # ---- urls ----------------------------------------------------------------------------------------------------------------
    @staticmethod
    def _values_url(ctx: SinkContext, rng: str, suffix: str = "") -> str:
        sid = quote(ctx.extra["spreadsheet_id"], safe="")
        return f"{ctx.url.rstrip('/')}/v4/spreadsheets/{sid}/values/{quote(rng, safe='!:')}{suffix}"

    # ---- read back ---------------------------------------------------------------------------------------------------------------
    def _read_ids(self, http: HttpLike, ctx: SinkContext) -> set[str] | Outcome | None:
        """The event ids in column A. None when the read failed in a way that proves nothing, an Outcome when the failure
        is one the tenant has to fix (a revoked grant, a forbidden sheet)."""
        url = self._values_url(ctx, f"{ctx.extra['sheet']}!A:A")
        try:
            res = self._call(http, ctx, "GET", url, None)
        except HttpError:
            return None
        if isinstance(res, Outcome):
            return res if res.kind == "PAUSE" else None
        if res.status == 403:
            return Outcome.pause("forbidden (403): the sheet is not shared with the service account", status=403)
        if res.status != 200:
            return None
        try:
            values = json.loads(res.text).get("values", [])
        except (ValueError, AttributeError):
            return None
        if not isinstance(values, list):
            return None
        return {row[0] for row in values if isinstance(row, list) and row and isinstance(row[0], str)}

    def reconcile(self, event: Event, http: HttpLike, ctx: SinkContext) -> str:
        ids = self._read_ids(http, ctx)
        if not isinstance(ids, set):
            return "unknown"
        return "found" if event.event_id in ids else "absent"

    # ---- send --------------------------------------------------------------------------------------------------------------------
    def send(self, event: Event, http: HttpLike, ctx: SinkContext) -> Outcome:
        ex = ctx.extra
        sid, sheet = ex["spreadsheet_id"], ex["sheet"]
        now = self.clock()
        refused_at = self._bad_start.get((sid, sheet))
        if refused_at is not None and now - refused_at < 900.0:
            return Outcome.pause("table does not start at column A: fix the sheet, then replay", pause_s=900.0)
        if ctx.lock is None:
            raise RuntimeError("the sheets sink needs the append lock")
        may_have_written = ctx.maybe_acted_before or ctx.prior_claim_lost

        if may_have_written:
            early = self._lookup_before_send(event, http, ctx)
            if early is not None:
                return early

        with ctx.lock(f"sheets:{sid}", ctx.timeout_s * 4 + 10) as got:
            if not got:
                return Outcome.retry(after_s=LOCK_RETRY_S, reason="another append is in flight for this spreadsheet")
            if may_have_written:
                # Looked again right before the append: a late landing append may have shown up while we waited.
                inside = self._lookup_before_send(event, http, ctx, settled=True)
                if inside is not None:
                    return inside
            return self._append(event, http, ctx)

    def _lookup_before_send(self, event: Event, http: HttpLike, ctx: SinkContext, settled: bool = False) -> Outcome | None:
        """None means the row is certainly not there and it is time to append. Anything else is the outcome to return."""
        ids = self._read_ids(http, ctx)
        if isinstance(ids, Outcome):
            return ids
        if ids is None:
            return Outcome.retry(reason="could not read the sheet back to check for a late append", breaker_failure=True)
        if event.event_id in ids:
            return Outcome.done(effect_ref="reconciled")
        if settled or self._quiet_long_enough(ctx):
            return None
        wait = max(1.0, ABSENT_AFTER_S - (ctx.since_maybe_acted_s or 0.0))
        return Outcome.retry(after_s=wait, reason="not in the sheet yet, waiting in case the earlier append lands late")

    @staticmethod
    def _quiet_long_enough(ctx: SinkContext) -> bool:
        """An absent read-back counts only after 60 seconds since the last try that may have written. A worker that died
        mid call has held its lease for at least the lease length (5 minutes or more), which is longer still."""
        if ctx.prior_claim_lost:
            return True
        since = ctx.since_maybe_acted_s
        return since is None or since >= ABSENT_AFTER_S

    def _append(self, event: Event, http: HttpLike, ctx: SinkContext) -> Outcome:
        ex = ctx.extra
        row = self.row_for(event, ex["columns"])
        url = self._values_url(ctx, f"{ex['sheet']}!A:A", ":append?valueInputOption=RAW&insertDataOption=INSERT_ROWS")
        body = json.dumps({"values": [row]}, ensure_ascii=False).encode("utf-8")
        try:
            res = self._call(http, ctx, "POST", url, body)
        except HttpConnectError as exc:
            return Outcome.retry(reason=exc.kind, breaker_failure=True)
        except (HttpTimeout, HttpFailure) as exc:
            return Outcome.ambiguous(reason=exc.kind)
        if isinstance(res, Outcome):
            return res
        status = res.status
        if status == 200:
            return self._after_append(res, ctx)
        if status == 429:
            return Outcome.retry(after_s=backoff_429(ctx.retries, self.rng), status=status, reason="rate limited", rate_limited=True)
        if status >= 500:
            return Outcome.ambiguous(status=status, reason=f"http {status}")
        if status == 403:  # ASSUMED meaning: the sheet is not shared with the service account (a tenant level fault)
            return Outcome.pause("forbidden (403): the sheet is not shared with the service account", status=status)
        if status == 404:  # ASSUMED meaning: no such spreadsheet
            return Outcome.dead("spreadsheet not found (404)", status=status)
        return Outcome.dead(f"http {status}", status=status)  # 401 after a refreshed token lands here too (ASSUMED)

    def _after_append(self, res: HttpResult, ctx: SinkContext) -> Outcome:
        try:
            doc = json.loads(res.text) if res.text.strip() else {}
        except ValueError:
            doc = {}
        doc = doc if isinstance(doc, dict) else {}
        start = table_start_column(doc.get("tableRange"))
        if start is not None and start != "A":
            # The row has been written at the table's first column, wherever that is. Say so and stop writing more.
            with self._mutex:
                self._bad_start[(ctx.extra["spreadsheet_id"], ctx.extra["sheet"])] = self.clock()
            return Outcome.dead("table does not start at column A", status=res.status)
        updates = doc.get("updates")
        ref = updates.get("updatedRange") if isinstance(updates, dict) else None
        return Outcome.done(status=res.status, effect_ref=ref if isinstance(ref, str) and len(ref) <= 100 else None)


def _token_error(text: str) -> str | None:
    try:
        doc = json.loads(text)
    except ValueError:
        return None
    err = doc.get("error") if isinstance(doc, dict) else None
    return err if isinstance(err, str) and err in TOKEN_ERRORS else None
