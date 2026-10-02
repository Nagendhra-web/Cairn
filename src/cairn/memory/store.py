"""Memory persistence: a store protocol, a SQLite store and an in-memory store.

Stores are deliberately dumb: they persist records and filter by structured
fields. Ranking, decay, deduplication and provenance policy live above them
(``policy.py`` and ``manager.py``) so a different backend (Postgres, a vector
database) only has to implement CRUD to inherit every memory feature.
"""

from __future__ import annotations

import builtins
import json
import sqlite3
from collections.abc import Iterable
from typing import Any, Protocol

from cairn.memory.types import MemoryKind, MemoryRecord
from cairn.provenance.labels import Label
from cairn.storage.sqlite import SQLiteDatabase


class MemoryStore(Protocol):
    async def put(self, record: MemoryRecord) -> None:
        """Insert or replace the record with ``record.id``."""
        ...

    async def get(self, record_id: str) -> MemoryRecord | None: ...

    async def list(
        self,
        *,
        kind: MemoryKind | None = None,
        pinned: bool | None = None,
        source_run: str | None = None,
        include_superseded: bool = True,
    ) -> builtins.list[MemoryRecord]:
        """Records matching every given filter, oldest first."""
        ...

    async def delete(self, record_id: str) -> bool:
        """Remove one record; returns whether it existed."""
        ...

    async def delete_many(self, record_ids: Iterable[str]) -> int: ...

    async def export(self) -> builtins.list[dict[str, Any]]:
        """Every record (including embeddings) as JSON-safe dicts, for backup and audit."""
        ...


def _matches(
    record: MemoryRecord,
    kind: MemoryKind | None,
    pinned: bool | None,
    source_run: str | None,
    include_superseded: bool,
) -> bool:
    if kind is not None and record.kind is not kind:
        return False
    if pinned is not None and record.pinned is not pinned:
        return False
    if source_run is not None and record.source_run != source_run:
        return False
    return include_superseded or record.active


class InMemoryMemoryStore:
    """Process-local store for tests and ephemeral agents."""

    def __init__(self) -> None:
        self._records: dict[str, MemoryRecord] = {}

    def __len__(self) -> int:
        return len(self._records)

    async def put(self, record: MemoryRecord) -> None:
        self._records[record.id] = record.copy()

    async def get(self, record_id: str) -> MemoryRecord | None:
        record = self._records.get(record_id)
        return record.copy() if record is not None else None

    async def list(
        self,
        *,
        kind: MemoryKind | None = None,
        pinned: bool | None = None,
        source_run: str | None = None,
        include_superseded: bool = True,
    ) -> builtins.list[MemoryRecord]:
        found = [
            r.copy()
            for r in self._records.values()
            if _matches(r, kind, pinned, source_run, include_superseded)
        ]
        found.sort(key=lambda r: (r.created_at, r.id))
        return found

    async def delete(self, record_id: str) -> bool:
        return self._records.pop(record_id, None) is not None

    async def delete_many(self, record_ids: Iterable[str]) -> int:
        return sum(1 for rid in list(record_ids) if self._records.pop(rid, None) is not None)

    async def export(self) -> builtins.list[dict[str, Any]]:
        return [r.to_dict(include_embedding=True) for r in await self.list()]


MIGRATIONS: list[tuple[int, str]] = [
    (
        1,
        """
        CREATE TABLE IF NOT EXISTS memory_records (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            text TEXT NOT NULL,
            label TEXT NOT NULL,
            importance REAL NOT NULL,
            created_at REAL NOT NULL,
            last_accessed REAL NOT NULL,
            access_count INTEGER NOT NULL DEFAULT 0,
            pinned INTEGER NOT NULL DEFAULT 0,
            source_run TEXT,
            metadata TEXT NOT NULL DEFAULT '{}',
            embedding TEXT,
            superseded_by TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_memory_kind ON memory_records(kind);
        CREATE INDEX IF NOT EXISTS idx_memory_source_run ON memory_records(source_run);
        CREATE INDEX IF NOT EXISTS idx_memory_pinned ON memory_records(pinned)
        """,
    ),
]

_COLUMNS = (
    "id, kind, text, label, importance, created_at, last_accessed, access_count, pinned,"
    " source_run, metadata, embedding, superseded_by"
)


def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    embedding = row["embedding"]
    return MemoryRecord(
        id=row["id"],
        kind=MemoryKind(row["kind"]),
        text=row["text"],
        label=Label.from_dict(json.loads(row["label"])),
        importance=row["importance"],
        created_at=row["created_at"],
        last_accessed=row["last_accessed"],
        access_count=row["access_count"],
        pinned=bool(row["pinned"]),
        source_run=row["source_run"],
        metadata=json.loads(row["metadata"]),
        embedding=json.loads(embedding) if embedding is not None else None,
        superseded_by=row["superseded_by"],
    )


def _record_params(record: MemoryRecord) -> tuple[Any, ...]:
    return (
        record.id,
        record.kind.value,
        record.text,
        json.dumps(record.label.to_dict(), sort_keys=True),
        record.importance,
        record.created_at,
        record.last_accessed,
        record.access_count,
        int(record.pinned),
        record.source_run,
        json.dumps(record.metadata, sort_keys=True, default=str),
        json.dumps(record.embedding) if record.embedding is not None else None,
        record.superseded_by,
    )


class SQLiteMemoryStore:
    """Durable store on the shared :class:`SQLiteDatabase` (namespace ``memory``).

    Embeddings are persisted so the search index can be rebuilt on startup
    without re-embedding, which matters when the embedder is a paid API.
    """

    def __init__(self, db: SQLiteDatabase | str) -> None:
        self.db = db if isinstance(db, SQLiteDatabase) else SQLiteDatabase(db)
        self.db.migrate("memory", MIGRATIONS)

    async def put(self, record: MemoryRecord) -> None:
        params = _record_params(record)
        placeholders = ", ".join("?" for _ in params)
        sql = f"INSERT OR REPLACE INTO memory_records ({_COLUMNS}) VALUES ({placeholders})"  # noqa: S608
        await self.db.run(lambda c: c.execute(sql, params))

    async def get(self, record_id: str) -> MemoryRecord | None:
        sql = f"SELECT {_COLUMNS} FROM memory_records WHERE id=?"  # noqa: S608
        rows = await self.db.run(lambda c: list(c.execute(sql, (record_id,))))
        return _row_to_record(rows[0]) if rows else None

    async def list(
        self,
        *,
        kind: MemoryKind | None = None,
        pinned: bool | None = None,
        source_run: str | None = None,
        include_superseded: bool = True,
    ) -> builtins.list[MemoryRecord]:
        clauses: list[str] = []
        params: list[Any] = []
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind.value)
        if pinned is not None:
            clauses.append("pinned=?")
            params.append(int(pinned))
        if source_run is not None:
            clauses.append("source_run=?")
            params.append(source_run)
        if not include_superseded:
            clauses.append("superseded_by IS NULL")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = f"SELECT {_COLUMNS} FROM memory_records{where} ORDER BY created_at, id"  # noqa: S608
        rows = await self.db.run(lambda c: list(c.execute(sql, params)))
        return [_row_to_record(r) for r in rows]

    async def delete(self, record_id: str) -> bool:
        cursor = await self.db.run(
            lambda c: c.execute("DELETE FROM memory_records WHERE id=?", (record_id,))
        )
        existed: bool = cursor.rowcount > 0
        return existed

    async def delete_many(self, record_ids: Iterable[str]) -> int:
        ids = [(rid,) for rid in record_ids]
        if not ids:
            return 0

        def run(conn: sqlite3.Connection) -> int:
            return sum(
                conn.execute("DELETE FROM memory_records WHERE id=?", p).rowcount for p in ids
            )

        removed: int = await self.db.transaction(run)
        return removed

    async def export(self) -> builtins.list[dict[str, Any]]:
        return [r.to_dict(include_embedding=True) for r in await self.list()]
