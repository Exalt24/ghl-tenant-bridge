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
