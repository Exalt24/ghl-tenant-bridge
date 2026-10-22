"""Shared fixtures.

The `store` fixture runs every store backed test on SQLite and on Postgres. Postgres is skipped, loudly, only when
DATABASE_URL_TEST is unset; RELAY_REQUIRE_POSTGRES=1 (set in CI) turns that skip into a failure so CI can never go
green without having run the Postgres half.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest

from relay import redact
from relay.http import Http
from relay.store.sqlite import SqliteStore

from .fakes.make import FakeMake
from .fakes.meta_graph import PIXEL_ID, TOKEN, FakeMeta
from .fakes.sheets import SPREADSHEET_ID, FakeSheets, service_account_json
from .fakes.slack import FakeSlack

PG_URL = os.environ.get("DATABASE_URL_TEST")
REQUIRE_PG = os.environ.get("RELAY_REQUIRE_POSTGRES") == "1"
# The mutation harness sets this to run a mutant against one store only.
STORES = [s for s in os.environ.get("RELAY_TEST_STORES", "sqlite,postgres").split(",") if s]


def pytest_terminal_summary(terminalreporter) -> None:  # type: ignore[no-untyped-def]
    if not PG_URL:
        terminalreporter.write_line("POSTGRES TESTS SKIPPED: DATABASE_URL_TEST is not set, CI runs them against postgres:17")
    else:
        terminalreporter.write_line("Postgres tests ran against DATABASE_URL_TEST")


@pytest.fixture(autouse=True)
def _clean_redaction():
    redact.forget_all()
    yield
    redact.forget_all()


def _pg_store():
    from relay.store.postgres import PostgresStore
    import psycopg

    schema = "t_" + uuid.uuid4().hex[:12]
    with psycopg.connect(PG_URL, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    store = PostgresStore(PG_URL, schema=schema)
    store.init_schema()
    return store, schema


@pytest.fixture(params=STORES)
def store(request, tmp_path):
    kind = request.param
    if kind == "sqlite":
        s = SqliteStore(str(tmp_path / "relay.db"))
        s.init_schema()
        yield s
        s.close()
        return
    if not PG_URL:
        if REQUIRE_PG:
            pytest.fail("RELAY_REQUIRE_POSTGRES=1 but DATABASE_URL_TEST is not set")
        pytest.skip("DATABASE_URL_TEST is not set: Postgres half not run here")
    s, schema = _pg_store()
    yield s
    s.close()
    import psycopg

    with psycopg.connect(PG_URL, autocommit=True) as admin:
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')


@pytest.fixture
def sqlite_store(tmp_path):
    s = SqliteStore(str(tmp_path / "relay.db"))
    s.init_schema()
    yield s
    s.close()


@pytest.fixture(scope="session")
def _slack_server():
    s = FakeSlack().start()
    yield s
    s.stop()


@pytest.fixture(scope="session")
def _make_server():
    s = FakeMake().start()
    yield s
    s.stop()


@pytest.fixture(scope="session")
def _meta_server():
    s = FakeMeta().start()
    yield s
    s.stop()


def _valid_meta_event(event_id: str = "control-1") -> dict:
    return {
        "event_name": "Lead", "event_time": int(__import__("time").time()) - 60, "event_id": event_id, "action_source": "system_generated",
        "user_data": {"em": "62a14e44f765419d10fea99367361a727c12365e2520f32218d505ed9aa0f62f", "fbc": "fb.1.1554763741205.AbCdEf"},
    }


@pytest.fixture
def meta(_meta_server):
    """The Meta fake, after a POSITIVE CONTROL: a valid event must be accepted before any test may rely on a refusal."""
    import time

    m = _meta_server
    m.reset()
    m.clock = time.time
    res = httpx.post(f"{m.url}/{PIXEL_ID}/events", params={"access_token": TOKEN}, json={"data": [_valid_meta_event()]})
    assert res.status_code == 200 and res.json()["events_received"] == 1, "positive control failed: the Meta fake rejects a valid event"
    assert m.effects == 1
    m.reset()
    yield m
    m.release.set()
    m.reset()
    m.clock = time.time


@pytest.fixture(scope="session")
def rsa_key():
    """One throwaway RSA key for the whole run. It signs service account assertions and is thrown away with the process."""
    from cryptography.hazmat.primitives.asymmetric import rsa

    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def _sheets_server(rsa_key):
    s = FakeSheets(rsa_key.public_key()).start()
    yield s
    s.stop()


def _control_jwt(key, email: str, aud: str, now: float) -> str:
    """A valid assertion built here with its own few lines, never with the relay's code, so the positive control cannot
    pass because the relay and the fake agree on the same mistake."""
    import base64
    import json as _json

    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    def enc(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    head = enc(_json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
    body = enc(_json.dumps({"iss": email, "scope": "https://www.googleapis.com/auth/spreadsheets", "aud": aud, "iat": int(now), "exp": int(now) + 3600}).encode())
    sig = key.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    return f"{head}.{body}.{enc(sig)}"


@pytest.fixture
def sheets(_sheets_server, rsa_key):
    """The Sheets fake after a POSITIVE CONTROL: a correctly signed assertion earns a token and a valid append lands."""
    import time

    s = _sheets_server
    s.reset()
    s.clock = time.time
    tok = httpx.post(s.token_url, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": _control_jwt(rsa_key, s.email, s.token_url, time.time())})
    assert tok.status_code == 200, "positive control failed: the Sheets fake rejects a valid assertion"
    res = httpx.post(
        f"{s.url}/v4/spreadsheets/{SPREADSHEET_ID}/values/Sheet1:append", params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"},
        json={"values": [["control", "x"]]}, headers={"Authorization": "Bearer " + tok.json()["access_token"]},
    )
    assert res.status_code == 200 and s.effects == 1, "positive control failed: the Sheets fake rejects a valid append"
    s.reset()
    s.clock = time.time
    yield s
    s.release.set()
    s.reset()
    s.clock = time.time


@pytest.fixture
def sa_json(sheets, rsa_key):
    return service_account_json(rsa_key, sheets.token_url, sheets.email)


@pytest.fixture
def slack(_slack_server):
    """The Slack fake, after a POSITIVE CONTROL: a valid message must be accepted before any test may rely on a refusal."""
    s = _slack_server
    s.reset()
    res = httpx.post(s.url, json={"text": "control", "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "control", "verbatim": True}}]})
    assert res.status_code == 200 and res.text == "ok", "positive control failed: the Slack fake rejects a valid message"
    assert s.effects == 1
    s.reset()
    yield s
    s.release.set()
    s.reset()


@pytest.fixture
def make(_make_server):
    """The Make fake, after a positive control."""
    m = _make_server
    m.reset()
    res = httpx.post(m.url, json={"control": True})
    assert res.status_code == 200 and res.text == "Accepted", "positive control failed: the Make fake rejects a valid call"
    assert m.effects == 1
    m.reset()
    yield m
    m.release.set()
    m.reset()


@pytest.fixture
def http():
    h = Http()
    yield h
    h.close()
