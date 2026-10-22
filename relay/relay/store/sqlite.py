"""SQLite store. Fine for one container that runs both the web process and the worker, and for fast tests.

One connection per thread (a SQLite connection must not hop threads), WAL so readers do not block the writer, and
`BEGIN IMMEDIATE` for every write so a claim cannot interleave with another writer.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .base import SqlStore

SCHEMA = Path(__file__).with_name("schema_sqlite.sql")
BUSY_TIMEOUT_S = 30.0


class _Tx:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)


class SqliteStore(SqlStore):
    dialect = "sqlite"
    greatest = "max"

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path

    # ---- dialect -------------------------------------------------------------------------------------------------
    def now(self, plus_s: float = 0.0) -> str:
        off = self.offset_s + plus_s
        return f"strftime('%Y-%m-%d %H:%M:%f', 'now', '{off:+.3f} seconds')"

    def age_s(self, col: str) -> str:
        return f"CAST((julianday({self.now()}) - julianday({col})) * 86400.0 AS DOUBLE PRECISION)"

    def epoch(self, expr: str) -> str:
        return f"(julianday({expr}) * 86400.0)"

    def ts_param(self, dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        u = dt.astimezone(timezone.utc)
        return u.strftime("%Y-%m-%d %H:%M:%S.") + f"{u.microsecond // 1000:03d}"

    # ---- connections ---------------------------------------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=BUSY_TIMEOUT_S, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
            with self._conns_lock:
                self._conns.append(conn)
        return conn

    @contextmanager
    def tx(self, write: bool = True) -> Iterator[_Tx]:
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE" if write else "BEGIN")
        try:
            yield _Tx(conn)
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        else:
            conn.execute("COMMIT")

    def init_schema(self) -> None:
        conn = self._conn()
        conn.executescript(SCHEMA.read_text(encoding="utf-8"))

    def close(self) -> None:
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for c in conns:
            try:
                c.close()
            except Exception:
                pass
        self._local = type(self._local)()

    def _table_names(self, t: Any) -> list[str]:
        rows = t.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'").fetchall()
        return [r["name"] for r in rows]
