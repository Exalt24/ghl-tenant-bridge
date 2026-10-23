"""Fake Google OAuth token endpoint and Sheets v4 values endpoints, written from the saved Google pages (retrieved
2026-10-22, see contracts/google/SOURCE.md) and RFC 7523, and corrected on 2026-10-23 against answers observed from the
real Sheets API with a USER OAuth token on a scratch spreadsheet (the `*.observed.json` fixtures; the service account
JWT exchange at the token endpoint was NOT observed and stays doc only).

Observed and enforced here (tests/test_sheets.py replays the fixtures against this fake):
  limit   a cell over 50,000 UTF-16 code units is a 400 for the whole append: 49,000 emoji (98,000 units) were refused
  read    blank cells and rows come back as [] in a one column read, a bare sheet read returns whole rows, `range` is the
          expanded A1 range, and a one cell range has no colon ("Big!A1")
  auth    a missing bearer is a 401 on a POST and a 403 on a GET, a wrong bearer is a 401 UNAUTHENTICATED
  429     the body carries the ErrorInfo details and there is no Retry-After header
  400     a bogus valueInputOption is "Invalid value at 'value_input_option'", an empty or missing `values` is a 200 that
          writes nothing

Documented and enforced here:
  token   POST form with grant_type=urn:ietf:params:oauth:grant-type:jwt-bearer and an assertion. The assertion is a JWT
          `header.claims.signature`, RS256, base64url WITHOUT padding. Claims iss, scope, aud (the token endpoint), iat,
          exp with exp no more than 65 minutes after iat. The signature is verified with the PUBLIC half of the key the
          test generated, so a wrongly signed assertion is refused with invalid_grant "Invalid JWT Signature."
  append  POST .../values/{range}:append with a required valueInputOption. RAW stores "=1+2" as the string; USER_ENTERED
          would turn it into a formula. The range only locates a "table" (a block of adjacent non empty cells) and the
          row goes to the next row of the table, starting at the table's FIRST column, which is not always column A. The
          answer carries tableRange (the table before the append, empty when none was found) and updates.updatedRange.
  read    GET .../values/{range} returns `values` (rows, trailing empty cells and rows dropped), no `values` key at all
          when the range is empty.

ASSUMED (tagged in SOURCE.md): which HTTP status each failure arrives with beyond 429 (retry later), 500 and 503, and the
body shapes of the error answers.

Rules (names in `self.rules`) the fake enforces and a mutant can delete:
  signature  the JWT signature is verified          grant_type  the literal must match          no_padding  no `=` in the JWT
  claims     iss, scope and aud are checked          lifetime    exp - iat at most 65 minutes     clock       iat/exp near now
  raw        a missing valueInputOption is a 400      cell_limit  a cell over 50,000 UTF-16 units is a 400

Append modes (script them with `queue`): ok, rate_limit (429), server_error (500, nothing written), act_then_fail (row
written, answer 500), late (the request is held, and when released the row is written and the answer is sent to a client
that has already given up), hang (held, nothing written), forbidden (403), notfound (404), bad_range (400 "Unable to
parse range", which is also what a missing tab answers), bad_request (400 for a body the API cannot read).
Token modes (`queue_token`): ok, invalid_grant, server_error.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import time
from collections import deque
from typing import Any, Callable
from urllib.parse import parse_qs, unquote

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from werkzeug.wrappers import Request, Response

from ._base import FakeVendor

SCOPE = "https://www.googleapis.com/auth/spreadsheets"
GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
SPREADSHEET_ID = "fake-spreadsheet-id-0001"
EMAIL = "relay-test@fake-project.iam.gserviceaccount.com"
MAX_LIFETIME_S = 65 * 60
TOKEN_TTL_S = 3600
AUTH_HINT = "Expected OAuth 2 access token, login cookie or other valid authentication credential. See https://developers.google.com/identity/sign-in/web/devconsole-project."
QUOTA_MESSAGE = "Quota exceeded for quota metric 'Write requests' and limit 'Write requests per minute per user' of service 'sheets.googleapis.com' for consumer 'project_number:000000000000'."
QUOTA_DETAILS = [
    {
        "@type": "type.googleapis.com/google.rpc.ErrorInfo",
        "reason": "RATE_LIMIT_EXCEEDED",
        "domain": "googleapis.com",
        "metadata": {"quota_location": "global", "quota_limit_value": "60", "consumer": "projects/000000000000", "quota_limit": "WriteRequestsPerMinutePerUser", "quota_unit": "1/min/{project}/{user}", "quota_metric": "sheets.googleapis.com/write_requests", "service": "sheets.googleapis.com"},
    }
]
CELL_LIMIT_MESSAGE = "Your input contains more than the maximum of 50000 characters in a single cell."
MAX_CELL_UNITS = 50_000  # observed: counted in UTF-16 code units, so an emoji is two
GRID_ROWS, GRID_COLS = 1000, 26  # a new tab's default grid, as the answers to a bare read show


def service_account_json(private_key: rsa.RSAPrivateKey, token_url: str, email: str = EMAIL, key_id: str = "testkey01") -> str:
    """A made up service account key file for a key generated by the test: nothing here was ever a real credential."""
    from cryptography.hazmat.primitives import serialization

    pem = private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    return json.dumps({"type": "service_account", "project_id": "fake-project", "private_key_id": key_id, "private_key": pem, "client_email": email, "token_uri": token_url})


def col_num(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + (ord(ch) - 64)
    return n


def col_letters(n: int) -> str:
    out = ""
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def a1(r1: int, c1: int, r2: int, c2: int) -> str:
    """A1 notation the way the real API answers: a one cell range has no colon ("Big!A1")."""
    first = f"{col_letters(c1)}{r1}"
    return first if (r1, c1) == (r2, c2) else f"{first}:{col_letters(c2)}{r2}"


def utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def _b64url_decode(part: str) -> bytes:
    return base64.urlsafe_b64decode(part + "=" * (-len(part) % 4))


class FakeSheets(FakeVendor):
    default_mode = "ok"

    def __init__(self, public_key: rsa.RSAPublicKey, email: str = EMAIL, spreadsheet_id: str = SPREADSHEET_ID, clock: Callable[[], float] = time.time) -> None:
        super().__init__()
        self.rules = {"signature", "grant_type", "no_padding", "claims", "lifetime", "clock", "raw", "cell_limit"}
        self.public_key = public_key
        self.email = email
        self.spreadsheet_id = spreadsheet_id
        self.clock = clock
        self.cells: dict[tuple[int, int], Any] = {}  # (row, col) -> value, for the one sheet below
        self.sheet = "Sheet1"
        self.token_script: deque[str] = deque()
        self.tokens: dict[str, float] = {}
        self.token_requests: list[dict[str, Any]] = []
        self.token_count = 0
        self.append_in_flight = 0
        self.max_in_flight = 0
        self.committed = threading.Event()

    # ---- lifecycle ---------------------------------------------------------------------------------------------------------
    def reset(self) -> None:
        super().reset()
        self.cells.clear()
        self.sheet = "Sheet1"
        self.token_script.clear()
        self.tokens.clear()
        self.token_requests.clear()
        self.token_count = 0
        self.append_in_flight = 0
        self.max_in_flight = 0
        self.committed.clear()

    @property
    def url(self) -> str:
        return self.base

    @property
    def token_url(self) -> str:
        return self.base + "/token"

    def queue_token(self, *modes: str) -> None:
        with self.lock:
            self.token_script.extend(modes)

    # ---- the sheet -----------------------------------------------------------------------------------------------------------
    def seed(self, rows: list[list[Any]], origin: str = "A1") -> None:
        m = re.fullmatch(r"([A-Z]+)(\d+)", origin)
        assert m
        c0, r0 = col_num(m.group(1)), int(m.group(2))
        with self.lock:
            for i, row in enumerate(rows):
                for j, v in enumerate(row):
                    if v not in (None, ""):
                        self.cells[(r0 + i, c0 + j)] = v

    def tables(self) -> list[tuple[int, int, int, int]]:
        """(min_row, min_col, max_row, max_col) of each block of adjacent non empty cells (4 neighbours)."""
        left = set(self.cells)
        out = []
        while left:
            start = left.pop()
            stack, block = [start], [start]
            while stack:
                r, c = stack.pop()
                for n in ((r + 1, c), (r - 1, c), (r, c + 1), (r, c - 1)):
                    if n in left:
                        left.remove(n)
                        stack.append(n)
                        block.append(n)
            rows, cols = [b[0] for b in block], [b[1] for b in block]
            out.append((min(rows), min(cols), max(rows), max(cols)))
        return sorted(out)

    @staticmethod
    def parse_range(rng: str) -> tuple[str | None, tuple[int, int, int, int] | None]:
        """(sheet, rectangle or None for the whole sheet). Handles `Sheet1`, `Sheet1!A:A`, `Sheet1!B4`, `C5:D5`."""
        sheet, _, cells = rng.partition("!") if "!" in rng else (None, "", rng)
        if sheet is None and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", rng):
            return rng, None
        if sheet is not None and not cells:
            return sheet, None
        m = re.fullmatch(r"([A-Z]+)(\d*)(?::([A-Z]+)(\d*))?", cells)
        if not m:
            raise ValueError("Unable to parse range")
        c1 = col_num(m.group(1))
        r1 = int(m.group(2)) if m.group(2) else 1
        c2 = col_num(m.group(3)) if m.group(3) else c1
        r2 = int(m.group(4)) if m.group(4) else (r1 if (m.group(3) is None and m.group(2)) else 10**6)
        return sheet, (r1, c1, max(r1, r2), max(c1, c2))

    def detect(self, rect: tuple[int, int, int, int] | None) -> tuple[int, int, int, int] | None:
        """The table an append against this range would use: the last table that intersects the range."""
        tables = self.tables()
        if rect is not None:
            r1, c1, r2, c2 = rect
            tables = [t for t in tables if not (t[2] < r1 or t[0] > r2 or t[3] < c1 or t[1] > c2)]
        return max(tables, key=lambda t: (t[2], t[3])) if tables else None

    def column_values(self, col: int) -> list[Any]:
        rows = [r for (r, c) in self.cells if c == col]
        top = max(rows) if rows else 0
        return [self.cells.get((r, col), "") for r in range(1, top + 1)]

    def read_rows(self, rect: tuple[int, int, int, int] | None) -> tuple[str, list[list[Any]]]:
        """(expanded A1 range, values) the way the real API answers a read: the range is clamped to the grid, a row stops at
        its last filled cell (a blank cell before it is ""), a row with nothing in range is [], and rows after the last
        filled one are dropped."""
        max_row = max((r for r, _ in self.cells), default=0)
        max_col = max((c for _, c in self.cells), default=0)
        rows_n, cols_n = max(GRID_ROWS, max_row), max(GRID_COLS, max_col)
        r1, c1, r2, c2 = (1, 1, rows_n, cols_n) if rect is None else (rect[0], rect[1], min(rect[2], rows_n), rect[3])
        filled = [r for (r, c) in self.cells if r1 <= r <= r2 and c1 <= c <= c2]
        values: list[list[Any]] = []
        for r in range(r1, (max(filled) if filled else r1 - 1) + 1):
            cols = [c for c in range(c1, c2 + 1) if (r, c) in self.cells]
            values.append([self.cells.get((r, c), "") for c in range(c1, (max(cols) if cols else c1 - 1) + 1)])
        return a1(r1, c1, r2, c2), values

    def rows_with_id(self, event_id: str) -> int:
        return sum(1 for (_r, c), v in self.cells.items() if c == 1 and v == event_id)

    def row_values(self, row: int) -> list[Any]:
        cols = [c for (r, c) in self.cells if r == row]
        return [self.cells.get((row, c), "") for c in range(1, (max(cols) if cols else 0) + 1)]

    # ---- the token endpoint --------------------------------------------------------------------------------------------------
    def _token_error(self, status: int, error: str, description: str) -> Response:
        return Response(json.dumps({"error": error, "error_description": description}), status=status, mimetype="application/json")

    def check_assertion(self, assertion: str) -> tuple[int, str, str] | tuple[None, dict[str, Any], dict[str, Any]]:
        parts = assertion.split(".")
        if len(parts) != 3:
            return 400, "invalid_grant", "Malformed auth code."
        if "no_padding" in self.rules and "=" in assertion:
            return 400, "invalid_grant", "Invalid JWT Signature."  # the page: encoded "without newlines or padding equal signs"
        try:
            header, claims = json.loads(_b64url_decode(parts[0])), json.loads(_b64url_decode(parts[1]))
            signature = _b64url_decode(parts[2])
        except (ValueError, UnicodeDecodeError):
            return 400, "invalid_grant", "Malformed auth code."
        if header.get("alg") != "RS256" or header.get("typ") != "JWT":
            return 400, "invalid_grant", "Invalid JWT: unsupported header."
        if "signature" in self.rules:
            try:
                self.public_key.verify(signature, f"{parts[0]}.{parts[1]}".encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
            except InvalidSignature:
                return 400, "invalid_grant", "Invalid JWT Signature."
        if "claims" in self.rules:
            if claims.get("iss") != self.email:
                return 400, "invalid_grant", "Invalid issuer."
            if claims.get("scope") != SCOPE:
                return 400, "invalid_scope", "Invalid OAuth scope or ID token audience provided."
            if claims.get("aud") != self.token_url:
                return 400, "invalid_grant", "Invalid audience."
        iat, exp = claims.get("iat"), claims.get("exp")
        if not isinstance(iat, int) or not isinstance(exp, int):
            return 400, "invalid_grant", "Invalid JWT: iat and exp must be integers."
        if "lifetime" in self.rules and (exp - iat > MAX_LIFETIME_S or exp <= iat):
            return 400, "invalid_grant", "Invalid JWT: Token must be a short-lived token (60 minutes) and in a reasonable timeframe."
        if "clock" in self.rules and (abs(iat - self.clock()) > 300 or exp < self.clock()):
            return 400, "invalid_grant", "Invalid JWT: Token must be a short-lived token (60 minutes) and in a reasonable timeframe."
        return None, header, claims

    def handle_token(self, request: Request) -> Response:
        form = {k: v[0] for k, v in parse_qs(request.get_data(as_text=True)).items()}
        with self.lock:
            mode = self.token_script.popleft() if self.token_script else "ok"
            self.token_requests.append({"form": form, "mode": mode, "headers": {k.lower(): v for k, v in request.headers.items()}})
        if mode == "server_error":
            return Response("backend error", status=500)
        if mode == "invalid_grant":
            return self._token_error(400, "invalid_grant", "Invalid JWT Signature.")
        if request.method != "POST":
            return Response("not found", status=404)
        if "grant_type" in self.rules and form.get("grant_type") != GRANT_TYPE:
            return self._token_error(400, "unsupported_grant_type", "Invalid grant_type.")
        status, a, b = self.check_assertion(form.get("assertion", ""))
        if status is not None:
            return self._token_error(status, a, b)  # type: ignore[arg-type]
        with self.lock:
            self.token_count += 1
            token = f"ya29.fake-token-{self.token_count}"
            self.tokens[token] = self.clock() + TOKEN_TTL_S
            self.token_requests[-1].update(header=a, claims=b)
        return Response(json.dumps({"access_token": token, "expires_in": TOKEN_TTL_S, "token_type": "Bearer"}), status=200, mimetype="application/json")

    # ---- the values endpoints ---------------------------------------------------------------------------------------------------
    def _google_error(self, status: int, message: str, reason: str, details: list[dict[str, Any]] | None = None) -> Response:
        err: dict[str, Any] = {"code": status, "message": message, "status": reason}
        if details:
            err["details"] = details
        return Response(json.dumps({"error": err}), status=status, content_type="application/json; charset=UTF-8")

    def _authorised(self, request: Request) -> bool:
        auth = request.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        with self.lock:
            expiry = self.tokens.get(token)
        return expiry is not None and expiry > self.clock()

    def _commit_row(self, rect: tuple[int, int, int, int] | None, row: list[Any], input_option: str) -> tuple[str, str]:
        with self.lock:
            table = self.detect(rect)
            if table is not None:
                table_range = f"{self.sheet}!{a1(table[0], table[1], table[2], table[3])}"
                r, c0 = table[2] + 1, table[1]
            else:
                table_range = ""
                r, c0 = (rect[0], rect[1]) if rect else (1, 1)
            for j, v in enumerate(row):
                if v is None or v == "":
                    continue
                if input_option == "USER_ENTERED" and isinstance(v, str) and v.startswith("="):
                    v = "#FORMULA"  # a USER_ENTERED "=1+2" becomes a formula, not the text that was sent
                self.cells[(r, c0 + j)] = v
            updated = f"{self.sheet}!{a1(r, c0, r, c0 + max(len(row) - 1, 0))}"
            self.effects += 1
            self.committed.set()
        return table_range, updated

    def handle_values(self, request: Request, tail: str) -> Response:
        mode = self.next_mode()
        parsed = self.parse_json(request)
        is_append = request.method == "POST" and tail.endswith(":append")
        rng = unquote(tail[: -len(":append")] if is_append else tail)
        if not self._authorised(request):
            self.record(request, parsed, mode, False)
            if not request.headers.get("Authorization"):
                if request.method == "GET":  # observed: an anonymous read is a 403, an anonymous write a 401
                    return self._google_error(403, "Method doesn't allow unregistered callers (callers without established identity). Please use API Key or other form of API consumer identity to call this API.", "PERMISSION_DENIED")
                return self._google_error(401, "Request is missing required authentication credential. " + AUTH_HINT, "UNAUTHENTICATED")
            return self._google_error(401, "Request had invalid authentication credentials. " + AUTH_HINT, "UNAUTHENTICATED")
        if mode == "forbidden":
            self.record(request, parsed, mode, False)
            return self._google_error(403, "The caller does not have permission", "PERMISSION_DENIED")
        if mode == "notfound":
            self.record(request, parsed, mode, False)
            return self._google_error(404, "Requested entity was not found.", "NOT_FOUND")
        if mode == "rate_limit":
            self.record(request, parsed, mode, False)
            return self._google_error(429, QUOTA_MESSAGE, "RESOURCE_EXHAUSTED", details=QUOTA_DETAILS)  # observed: no Retry-After header
        if mode == "server_error" and not is_append:
            self.record(request, parsed, mode, False)
            return self._google_error(500, "Internal error encountered.", "INTERNAL")
        try:
            sheet, rect = self.parse_range(rng)
        except ValueError:
            self.record(request, parsed, mode, False)
            return self._google_error(400, "Unable to parse range: " + rng, "INVALID_ARGUMENT")
        if mode == "bad_range" or (sheet is not None and sheet != self.sheet):
            self.record(request, parsed, mode, False)
            return self._google_error(400, "Unable to parse range: " + rng, "INVALID_ARGUMENT")
        if mode == "bad_request":  # a 400 that is not about the range: observed text for a body the API cannot parse
            self.record(request, parsed, mode, False)
            return self._google_error(400, "Invalid JSON payload received. Unexpected end of string. Expected a value or ] within an array.\n\n^", "INVALID_ARGUMENT")
        if not is_append:
            self.record(request, parsed, mode, False)
            expanded, values = self.read_rows(rect)
            body: dict[str, Any] = {"range": f"{self.sheet}!{expanded}", "majorDimension": "ROWS"}
            if values:
                body["values"] = values
            return Response(json.dumps(body), status=200, content_type="application/json; charset=UTF-8")

        option = request.args.get("valueInputOption")
        if "raw" in self.rules and option is None:
            self.record(request, parsed, mode, False)
            return self._google_error(400, "'valueInputOption' is required but not specified", "INVALID_ARGUMENT")
        if "raw" in self.rules and option not in ("RAW", "USER_ENTERED"):
            self.record(request, parsed, mode, False)
            msg = f"Invalid value at 'value_input_option' (type.googleapis.com/google.apps.sheets.v4.ValueInputOption), \"{option}\""
            return self._google_error(400, msg, "INVALID_ARGUMENT")
        values_in = parsed.get("values") if isinstance(parsed, dict) else None
        if values_in is not None and not (isinstance(values_in, list) and all(isinstance(r, list) for r in values_in)):
            self.record(request, parsed, mode, False)
            return self._google_error(400, "Invalid value at 'data.values' (type.googleapis.com/google.protobuf.ListValue)", "INVALID_ARGUMENT")
        if not values_in:
            # Observed: `{}` and `{"values": []}` are a 200 that writes nothing, with only the first cell named.
            self.record(request, parsed, mode, False)
            nothing = {"spreadsheetId": self.spreadsheet_id, "updates": {"spreadsheetId": self.spreadsheet_id, "updatedRange": f"{self.sheet}!A1"}}
            return Response(json.dumps(nothing), status=200, content_type="application/json; charset=UTF-8")
        if "cell_limit" in self.rules and any(isinstance(v, str) and utf16_units(v) > MAX_CELL_UNITS for r in values_in for v in r):
            self.record(request, parsed, mode, False)
            return self._google_error(400, CELL_LIMIT_MESSAGE, "INVALID_ARGUMENT")
        row = values_in[0]
        with self.lock:
            self.append_in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.append_in_flight)
        self.record(request, parsed, mode, False)  # `accepted` flips to True below only when the row is really written
        with self.lock:
            entry = self.requests[-1]
        try:
            if mode == "hang":
                self.hang()
                return Response("{}", status=200, mimetype="application/json")
            if mode == "server_error":
                return self._google_error(500, "Internal error encountered.", "INTERNAL")
            if mode == "late":
                self.hang()
            table_range, updated = self._commit_row(rect, row, option or "RAW")
            entry["accepted"] = True
            if mode == "act_then_fail":
                return self._google_error(500, "Internal error encountered.", "INTERNAL")
            body = {"spreadsheetId": self.spreadsheet_id, "updates": {"spreadsheetId": self.spreadsheet_id, "updatedRange": updated, "updatedRows": 1, "updatedColumns": len(row), "updatedCells": len(row)}}
            if table_range:
                body["tableRange"] = table_range
            return Response(json.dumps(body), status=200, mimetype="application/json")
        finally:
            with self.lock:
                self.append_in_flight -= 1

    def handle(self, request: Request) -> Response:
        path = request.path
        if path == "/token":
            return self.handle_token(request)
        prefix = f"/v4/spreadsheets/{self.spreadsheet_id}/values/"
        if path.startswith(prefix):
            return self.handle_values(request, path[len(prefix):])
        if path.startswith("/v4/spreadsheets/"):
            self.record(request, None, "unknown_id", False)
            return self._google_error(404, "Requested entity was not found.", "NOT_FOUND")
        return Response("not found", status=404)
