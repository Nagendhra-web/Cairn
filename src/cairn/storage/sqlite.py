"""Shared SQLite access layer with namespaced, ordered migrations.

SQLite is the default because it gives durable, transactional storage with
zero setup, and WAL mode lets several worker processes share one database
file. Subsystems (journal, memory, queue) register their own migrations under
a namespace so they can evolve independently. A Postgres backend can implement
the same store protocols (planned, see ROADMAP.md).
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, TypeVar

T = TypeVar("T")

Migration = tuple[int, str]


class SQLiteDatabase:
    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None, timeout=30.0
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=30000")
            if self.path != ":memory:":
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                " namespace TEXT NOT NULL, version INTEGER NOT NULL,"
                " PRIMARY KEY (namespace, version))"
            )

    def migrate(self, namespace: str, migrations: Sequence[Migration]) -> list[int]:
        """Apply unapplied migrations in order, each in its own transaction."""
        applied: list[int] = []
        with self._lock:
            done = {
                row[0]
                for row in self._conn.execute(
                    "SELECT version FROM schema_migrations WHERE namespace=?", (namespace,)
                )
            }
            for version, sql in sorted(migrations):
                if version in done:
                    continue
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    for statement in _split_sql(sql):
                        self._conn.execute(statement)
                    self._conn.execute(
                        "INSERT INTO schema_migrations(namespace, version) VALUES (?, ?)",
                        (namespace, version),
                    )
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise
                applied.append(version)
        return applied

    def run_sync(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        with self._lock:
            return fn(self._conn)

    def transaction_sync(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(self._conn)
                self._conn.execute("COMMIT")
                return result
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    async def run(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.to_thread(self.run_sync, fn)

    async def transaction(self, fn: Callable[[sqlite3.Connection], T]) -> T:
        return await asyncio.to_thread(self.transaction_sync, fn)

    def execute_sync(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return self.run_sync(lambda c: list(c.execute(sql, params)))

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _split_sql(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip()]
