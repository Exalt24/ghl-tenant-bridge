"""Postgres store. Required whenever the worker runs as its own service, and the one to use in production.

The claim adds `FOR UPDATE SKIP LOCKED`, so overlapping workers skip a row another worker is claiming instead of
waiting on it. `schema` pins a search_path so tests can run in a throwaway schema on a shared server.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row

from .base import SqlStore

SCHEMA = Path(__file__).with_name("schema_postgres.sql")


class _Tx:
    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    def execute(self, sql: str, params: tuple = ()) -> psycopg.Cursor:
        # The shared SQL uses `?`; psycopg wants `%s`. No SQL in this package carries a literal question mark or percent.
        return self._conn.execute(sql.replace("?", "%s"), params)


class PostgresStore(SqlStore):
    dialect = "postgres"
    skip_locked = "FOR UPDATE SKIP LOCKED"

    def __init__(self, url: str, schema: str | None = None) -> None:
        super().__init__()
        self.url = url
        self.schema = schema

    # ---- dialect -------------------------------------------------------------------------------------------------
    def now(self, plus_s: float = 0.0) -> str:
        off = self.offset_s + plus_s
        return f"(clock_timestamp() + interval '{off:.3f} seconds')"

    def age_s(self, col: str) -> str:
        return f"CAST(EXTRACT(EPOCH FROM ({self.now()} - {col})) AS DOUBLE PRECISION)"

    def epoch(self, expr: str) -> str:
        return f"EXTRACT(EPOCH FROM {expr})"

    def ts_param(self, dt: datetime) -> datetime:
        return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)

    # ---- connections ---------------------------------------------------------------------------------------------
    def _conn(self) -> psycopg.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None or conn.closed:
            kwargs: dict[str, Any] = {"autocommit": True, "row_factory": dict_row}
            if self.schema:
                kwargs["options"] = f"-c search_path={self.schema}"
            conn = psycopg.connect(self.url, **kwargs)
            self._local.conn = conn
            with self._conns_lock:
                self._conns.append(conn)
        return conn

    @contextmanager
    def tx(self, write: bool = True) -> Iterator[_Tx]:
        conn = self._conn()
        with conn.transaction():
            yield _Tx(conn)

    def init_schema(self) -> None:
        conn = self._conn()
        with conn.transaction():
            conn.execute(SCHEMA.read_text(encoding="utf-8"))

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
        rows = t.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
        ).fetchall()
        return [r["table_name"] for r in rows]
