"""Two stores, one set of semantics. `open_store` picks by what the config resolved."""

from __future__ import annotations

import threading

from .base import Claim, FinishPlan, IngestResult, SqlStore

__all__ = ["Claim", "FinishPlan", "IngestResult", "SqlStore", "open_store"]

SCHEMA_TRIES = 8
SCHEMA_RETRY_WAIT_S = 0.25


def _init_with_retry(store: SqlStore) -> None:
    """Several gunicorn workers start at once and all create the tables. CREATE TABLE IF NOT EXISTS can still collide
    on a brand new database (SQLite reports a locked file, Postgres a duplicate type), so a failed attempt is retried:
    by the next try the other process has finished and the statements find everything already there."""
    pause = threading.Event()
    for attempt in range(SCHEMA_TRIES):
        try:
            store.init_schema()
            return
        except Exception:
            if attempt == SCHEMA_TRIES - 1:
                raise
            pause.wait(SCHEMA_RETRY_WAIT_S)


def open_store(database_url: str | None, db_path: str | None) -> SqlStore:
    """Postgres when a DATABASE_URL is set, otherwise SQLite at db_path. Creates the tables if they are missing."""
    if database_url:
        from .postgres import PostgresStore

        store: SqlStore = PostgresStore(database_url)
    else:
        from .sqlite import SqliteStore

        store = SqliteStore(db_path or "relay.db")
    _init_with_retry(store)
    return store
