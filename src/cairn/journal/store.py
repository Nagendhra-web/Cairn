"""Journal store protocol and implementations (in-memory and SQLite).

Appends use optimistic concurrency: the caller states the sequence number it
expects to write next. If another writer appended first (for example an
operator recording an approval while a worker is still flushing), the append
fails with :class:`ConcurrentAppend` and the caller re-reads and retries.
That makes the journal safe to share between API processes and workers.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from collections import defaultdict
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

from cairn.core.errors import CairnError, NotFound
from cairn.core.ids import canonical_json
from cairn.journal.events import GENESIS_HASH, Event, EventDraft, seal
from cairn.storage.sqlite import SQLiteDatabase


class ConcurrentAppend(CairnError):
    code = "concurrent_append"
    retryable = True


@dataclass(slots=True)
class RunRecord:
    run_id: str
    goal: str
    status: str = "created"
    created_at: float = 0.0
    updated_at: float = 0.0
    parent_run_id: str | None = None
    agent: str | None = None
    tags: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "goal": self.goal,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "parent_run_id": self.parent_run_id,
            "agent": self.agent,
            "tags": list(self.tags),
            "summary": dict(self.summary),
        }


class JournalStore(Protocol):
    async def create_run(self, record: RunRecord) -> None: ...

    async def get_run(self, run_id: str) -> RunRecord: ...

    async def update_run(self, run_id: str, **fields: Any) -> None: ...

    async def list_runs(
        self, limit: int = 50, status: str | None = None, parent_run_id: str | None = None
    ) -> list[RunRecord]: ...

    async def append(
        self, run_id: str, drafts: Sequence[EventDraft], expected_seq: int | None = None
    ) -> list[Event]: ...

    async def read(self, run_id: str, after_seq: int = 0) -> list[Event]: ...

    async def last_seq(self, run_id: str) -> int: ...

    def subscribe(self, run_id: str) -> AsyncIterator[Event]: ...


class _Bus:
    """In-process fan-out of appended events for live streaming."""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[Event | None]]] = defaultdict(set)

    def publish(self, events: Sequence[Event]) -> None:
        for ev in events:
            for queue in list(self._subs.get(ev.run_id, ())):
                queue.put_nowait(ev)

    async def subscribe(self, run_id: str) -> AsyncIterator[Event]:
        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        self._subs[run_id].add(queue)
        try:
            while True:
                item = await queue.get()
                if item is None:
                    return
                yield item
        finally:
            self._subs[run_id].discard(queue)


class InMemoryJournal:
    def __init__(self) -> None:
        self._runs: dict[str, RunRecord] = {}
        self._events: dict[str, list[Event]] = defaultdict(list)
        self._lock = asyncio.Lock()
        self._bus = _Bus()

    async def create_run(self, record: RunRecord) -> None:
        self._runs[record.run_id] = replace(record)

    async def get_run(self, run_id: str) -> RunRecord:
        if run_id not in self._runs:
            raise NotFound(f"run '{run_id}' not found", run_id=run_id)
        return replace(self._runs[run_id])

    async def update_run(self, run_id: str, **fields: Any) -> None:
        record = self._runs.get(run_id)
        if record is None:
            raise NotFound(f"run '{run_id}' not found", run_id=run_id)
        for key, value in fields.items():
            setattr(record, key, value)

    async def list_runs(
        self, limit: int = 50, status: str | None = None, parent_run_id: str | None = None
    ) -> list[RunRecord]:
        rows = sorted(self._runs.values(), key=lambda r: r.created_at, reverse=True)
        rows = [r for r in rows if status is None or r.status == status]
        rows = [r for r in rows if parent_run_id is None or r.parent_run_id == parent_run_id]
        return [replace(r) for r in rows[:limit]]

    async def append(
        self, run_id: str, drafts: Sequence[EventDraft], expected_seq: int | None = None
    ) -> list[Event]:
        async with self._lock:
            existing = self._events[run_id]
            last = existing[-1].seq if existing else 0
            if expected_seq is not None and expected_seq != last + 1:
                raise ConcurrentAppend(
                    f"expected seq {expected_seq} but next is {last + 1}", run_id=run_id
                )
            prev = existing[-1].hash if existing else GENESIS_HASH
            out: list[Event] = []
            for i, draft in enumerate(drafts, start=last + 1):
                ev = seal(run_id, i, draft, prev)
                prev = ev.hash
                out.append(ev)
            existing.extend(out)
        self._bus.publish(out)
        return out

    async def read(self, run_id: str, after_seq: int = 0) -> list[Event]:
        return [e for e in self._events.get(run_id, []) if e.seq > after_seq]

    async def last_seq(self, run_id: str) -> int:
        events = self._events.get(run_id)
        return events[-1].seq if events else 0

    def subscribe(self, run_id: str) -> AsyncIterator[Event]:
        return self._bus.subscribe(run_id)


_JOURNAL_MIGRATIONS = [
    (
        1,
        """
        CREATE TABLE runs (
            run_id TEXT PRIMARY KEY,
            goal TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            parent_run_id TEXT,
            agent TEXT,
            tags TEXT NOT NULL DEFAULT '[]',
            summary TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX runs_status ON runs(status, created_at);
        CREATE INDEX runs_parent ON runs(parent_run_id);
        CREATE TABLE events (
            run_id TEXT NOT NULL,
            seq INTEGER NOT NULL,
            type TEXT NOT NULL,
            node_id TEXT,
            ts REAL NOT NULL,
            data TEXT NOT NULL,
            prev_hash TEXT NOT NULL,
            hash TEXT NOT NULL,
            PRIMARY KEY (run_id, seq)
        );
        CREATE INDEX events_type ON events(run_id, type)
        """,
    ),
]


class SQLiteJournal:
    def __init__(self, db: SQLiteDatabase) -> None:
        self.db = db
        db.migrate("journal", _JOURNAL_MIGRATIONS)
        self._bus = _Bus()

    async def create_run(self, record: RunRecord) -> None:
        def op(c: sqlite3.Connection) -> None:
            c.execute(
                "INSERT INTO runs(run_id, goal, status, created_at, updated_at, parent_run_id,"
                " agent, tags, summary) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    record.run_id,
                    record.goal,
                    record.status,
                    record.created_at,
                    record.updated_at,
                    record.parent_run_id,
                    record.agent,
                    json.dumps(record.tags),
                    canonical_json(record.summary),
                ),
            )

        await self.db.run(op)

    async def get_run(self, run_id: str) -> RunRecord:
        rows = await self.db.run(
            lambda c: list(c.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)))
        )
        if not rows:
            raise NotFound(f"run '{run_id}' not found", run_id=run_id)
        return _row_to_run(rows[0])

    async def update_run(self, run_id: str, **fields: Any) -> None:
        allowed = {"status", "updated_at", "summary", "tags", "goal", "agent"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"cannot update run fields {sorted(unknown)}")
        cols, params = [], []
        for key, value in fields.items():
            cols.append(f"{key}=?")
            params.append(canonical_json(value) if key in {"summary", "tags"} else value)
        params.append(run_id)
        sql = f"UPDATE runs SET {', '.join(cols)} WHERE run_id=?"  # noqa: S608 - columns allowlisted
        count = await self.db.run(lambda c: c.execute(sql, params).rowcount)
        if count == 0:
            raise NotFound(f"run '{run_id}' not found", run_id=run_id)

    async def list_runs(
        self, limit: int = 50, status: str | None = None, parent_run_id: str | None = None
    ) -> list[RunRecord]:
        sql = "SELECT * FROM runs WHERE 1=1"
        params: list[Any] = []
        if status:
            sql += " AND status=?"
            params.append(status)
        if parent_run_id:
            sql += " AND parent_run_id=?"
            params.append(parent_run_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = await self.db.run(lambda c: list(c.execute(sql, params)))
        return [_row_to_run(r) for r in rows]

    async def append(
        self, run_id: str, drafts: Sequence[EventDraft], expected_seq: int | None = None
    ) -> list[Event]:
        def op(c: sqlite3.Connection) -> list[Event]:
            row = c.execute(
                "SELECT seq, hash FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,)
            ).fetchone()
            last, prev = (row[0], row[1]) if row else (0, GENESIS_HASH)
            if expected_seq is not None and expected_seq != last + 1:
                raise ConcurrentAppend(
                    f"expected seq {expected_seq} but next is {last + 1}", run_id=run_id
                )
            out: list[Event] = []
            for i, draft in enumerate(drafts, start=last + 1):
                ev = seal(run_id, i, draft, prev)
                prev = ev.hash
                out.append(ev)
            c.executemany(
                "INSERT INTO events(run_id, seq, type, node_id, ts, data, prev_hash, hash)"
                " VALUES (?,?,?,?,?,?,?,?)",
                [
                    (e.run_id, e.seq, e.type, e.node_id, e.ts, canonical_json(e.data),
                     e.prev_hash, e.hash)
                    for e in out
                ],
            )
            return out

        try:
            events = await self.db.transaction(op)
        except sqlite3.IntegrityError as exc:
            raise ConcurrentAppend(str(exc), run_id=run_id) from exc
        self._bus.publish(events)
        return events

    async def read(self, run_id: str, after_seq: int = 0) -> list[Event]:
        rows = await self.db.run(
            lambda c: list(
                c.execute(
                    "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq", (run_id, after_seq)
                )
            )
        )
        return [
            Event(
                run_id=r["run_id"],
                seq=r["seq"],
                type=r["type"],
                data=json.loads(r["data"]),
                node_id=r["node_id"],
                ts=r["ts"],
                prev_hash=r["prev_hash"],
                hash=r["hash"],
            )
            for r in rows
        ]

    async def last_seq(self, run_id: str) -> int:
        row = await self.db.run(
            lambda c: c.execute("SELECT MAX(seq) FROM events WHERE run_id=?", (run_id,)).fetchone()
        )
        return int(row[0] or 0)

    def subscribe(self, run_id: str) -> AsyncIterator[Event]:
        return self._bus.subscribe(run_id)


def _row_to_run(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=row["run_id"],
        goal=row["goal"],
        status=row["status"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        parent_run_id=row["parent_run_id"],
        agent=row["agent"],
        tags=json.loads(row["tags"]),
        summary=json.loads(row["summary"]),
    )
