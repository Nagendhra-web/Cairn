"""Durable job queue with leases.

Why leases instead of a simple "claimed" flag: a worker that crashes (OOM,
SIGKILL, lost host) never gets to say it stopped. A lease is a claim with an
expiry; as long as the worker is alive it heartbeats to extend it, and once
it stops heartbeating the job silently becomes claimable again. Combined with
the run journal, that gives at-least-once job delivery with effectively
exactly-once effects: a re-executed run resumes from its journal instead of
repeating completed tool calls.

The SQLite implementation is safe across processes sharing one database file
(WAL mode): every state transition runs inside ``BEGIN IMMEDIATE``, which takes
the database write lock up front so two workers can never select and claim the
same row.
"""

from __future__ import annotations

import builtins
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Protocol

from cairn.core.clock import Clock, SystemClock
from cairn.core.ids import canonical_json, new_id
from cairn.storage.sqlite import SQLiteDatabase

QUEUED = "queued"
LEASED = "leased"
COMPLETED = "completed"
DEAD = "dead"
CANCELLED = "cancelled"
STATUSES = (QUEUED, LEASED, COMPLETED, DEAD, CANCELLED)


@dataclass(slots=True)
class Job:
    """A unit of background work as stored in the queue."""

    job_id: str
    job_type: str
    payload: dict[str, Any]
    status: str = QUEUED
    run_id: str | None = None
    priority: int = 0
    attempts: int = 0
    max_attempts: int = 3
    available_at: float = 0.0
    lease_owner: str | None = None
    lease_expires_at: float | None = None
    idempotency_key: str | None = None
    result: Any = None
    error: dict[str, Any] | None = None
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "job_type": self.job_type,
            "payload": dict(self.payload),
            "status": self.status,
            "run_id": self.run_id,
            "priority": self.priority,
            "attempts": self.attempts,
            "max_attempts": self.max_attempts,
            "available_at": self.available_at,
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at,
            "idempotency_key": self.idempotency_key,
            "result": self.result,
            "error": self.error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class WorkQueue(Protocol):
    """What a :class:`~cairn.workers.worker.Worker` needs from a queue backend.

    Kept narrow so a Postgres (``SELECT ... FOR UPDATE SKIP LOCKED``) or Redis
    backend can be dropped in without touching workers.
    """

    async def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        run_id: str | None = None,
        priority: int = 0,
        idempotency_key: str | None = None,
        available_at: float | None = None,
        max_attempts: int = 3,
    ) -> str: ...

    async def lease(
        self, worker_id: str, lease_s: float, job_types: builtins.list[str] | None = None
    ) -> Job | None: ...

    async def heartbeat(self, job_id: str, worker_id: str, lease_s: float) -> bool: ...

    async def complete(self, job_id: str, worker_id: str, result: Any = None) -> bool: ...

    async def fail(
        self, job_id: str, worker_id: str, error: dict[str, Any], retry_delay_s: float = 1.0
    ) -> bool: ...

    async def cancel(self, job_id: str) -> bool: ...

    async def get(self, job_id: str) -> Job: ...

    async def list(
        self, status: str | None = None, job_type: str | None = None, limit: int = 100
    ) -> builtins.list[Job]: ...

    async def stats(self) -> dict[str, int]: ...


_QUEUE_MIGRATIONS = [
    (
        1,
        """
        CREATE TABLE jobs (
            job_id TEXT PRIMARY KEY,
            job_type TEXT NOT NULL,
            payload TEXT NOT NULL,
            status TEXT NOT NULL,
            run_id TEXT,
            priority INTEGER NOT NULL DEFAULT 0,
            attempts INTEGER NOT NULL DEFAULT 0,
            max_attempts INTEGER NOT NULL DEFAULT 3,
            available_at REAL NOT NULL,
            lease_owner TEXT,
            lease_expires_at REAL,
            idempotency_key TEXT,
            result TEXT,
            error TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            seq INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX jobs_idempotency ON jobs(idempotency_key)
            WHERE idempotency_key IS NOT NULL;
        CREATE INDEX jobs_ready ON jobs(status, priority DESC, available_at, seq);
        CREATE INDEX jobs_lease ON jobs(status, lease_expires_at);
        CREATE INDEX jobs_run ON jobs(run_id)
        """,
    ),
]


class JobNotFound(LookupError):
    """Raised by :meth:`SQLiteWorkQueue.get` for an unknown job id."""


class SQLiteWorkQueue:
    """:class:`WorkQueue` stored in a shared :class:`SQLiteDatabase`.

    ``max_backoff_s`` caps the exponential retry delay so a flapping
    dependency does not push a job days into the future.
    """

    def __init__(
        self,
        db: SQLiteDatabase,
        *,
        clock: Clock | None = None,
        max_backoff_s: float = 300.0,
    ) -> None:
        self.db = db
        self.clock: Clock = clock or SystemClock()
        self.max_backoff_s = max_backoff_s
        db.migrate("queue", _QUEUE_MIGRATIONS)

    # ------------------------------------------------------------------ writes

    async def enqueue(
        self,
        job_type: str,
        payload: dict[str, Any],
        *,
        run_id: str | None = None,
        priority: int = 0,
        idempotency_key: str | None = None,
        available_at: float | None = None,
        max_attempts: int = 3,
    ) -> str:
        """Add a job; with ``idempotency_key`` a duplicate returns the original id.

        Idempotency matters because callers (an API handler, a scheduler)
        may retry after a timeout without knowing whether the first enqueue
        landed; the key turns that retry into a no-op instead of a second
        execution.
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        body = canonical_json(payload)
        now = self.clock.now()
        job_id = new_id("job")

        def op(c: sqlite3.Connection) -> str:
            if idempotency_key is not None:
                row = c.execute(
                    "SELECT job_id FROM jobs WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if row is not None:
                    return str(row[0])
            seq = c.execute("SELECT COALESCE(MAX(seq), 0) + 1 FROM jobs").fetchone()[0]
            c.execute(
                "INSERT INTO jobs(job_id, job_type, payload, status, run_id, priority, attempts,"
                " max_attempts, available_at, idempotency_key, created_at, updated_at, seq)"
                " VALUES (?,?,?,?,?,?,0,?,?,?,?,?,?)",
                (
                    job_id, job_type, body, QUEUED, run_id, priority, max_attempts,
                    now if available_at is None else available_at, idempotency_key, now, now,
                    seq,
                ),
            )
            return job_id

        return await self.db.transaction(op)

    async def lease(
        self, worker_id: str, lease_s: float, job_types: builtins.list[str] | None = None
    ) -> Job | None:
        """Atomically claim the best available job for ``worker_id``.

        Candidates are queued jobs whose ``available_at`` has passed and
        leased jobs whose lease has expired (their worker died or hung).
        Order: highest priority first, then earliest available, then FIFO.
        An expired job that has already used all its attempts is moved to
        the dead letter state instead of being handed out again, so a job
        that reliably crashes its worker cannot take the fleet down forever.
        """
        if lease_s <= 0:
            raise ValueError("lease_s must be positive")
        types = list(job_types) if job_types else None

        def op(c: sqlite3.Connection) -> Job | None:
            now = self.clock.now()
            sql = (
                "SELECT * FROM jobs WHERE ((status=? AND available_at<=?)"
                " OR (status=? AND lease_expires_at<=?))"
            )
            params: builtins.list[Any] = [QUEUED, now, LEASED, now]
            if types:
                sql += f" AND job_type IN ({','.join('?' * len(types))})"
                params.extend(types)
            sql += " ORDER BY priority DESC, available_at ASC, seq ASC LIMIT 1"
            while True:
                row = c.execute(sql, params).fetchone()
                if row is None:
                    return None
                if row["status"] == LEASED and row["attempts"] >= row["max_attempts"]:
                    c.execute(
                        "UPDATE jobs SET status=?, lease_owner=NULL, lease_expires_at=NULL,"
                        " error=?, updated_at=? WHERE job_id=?",
                        (
                            DEAD,
                            canonical_json({
                                "code": "lease_expired",
                                "message": f"lease held by '{row['lease_owner']}' expired"
                                " on the final attempt",
                            }),
                            now,
                            row["job_id"],
                        ),
                    )
                    continue
                c.execute(
                    "UPDATE jobs SET status=?, lease_owner=?, lease_expires_at=?,"
                    " attempts=attempts+1, updated_at=? WHERE job_id=?",
                    (LEASED, worker_id, now + lease_s, now, row["job_id"]),
                )
                fresh = c.execute("SELECT * FROM jobs WHERE job_id=?", (row["job_id"],)).fetchone()
                return _row_to_job(fresh)

        return await self.db.transaction(op)

    async def heartbeat(self, job_id: str, worker_id: str, lease_s: float) -> bool:
        """Extend the lease; ``False`` means this worker no longer owns the job.

        Ownership is checked in the same statement as the update, so a worker
        whose lease already expired and was re-leased elsewhere cannot steal
        the job back. Expiry is not checked: an owner that heartbeats late
        but before anyone else claimed the job keeps it, which is harmless.
        """
        now = self.clock.now()

        def op(c: sqlite3.Connection) -> bool:
            cur = c.execute(
                "UPDATE jobs SET lease_expires_at=?, updated_at=?"
                " WHERE job_id=? AND status=? AND lease_owner=?",
                (now + lease_s, now, job_id, LEASED, worker_id),
            )
            return cur.rowcount == 1

        return await self.db.transaction(op)

    async def complete(self, job_id: str, worker_id: str, result: Any = None) -> bool:
        """Mark the job done; ignored (``False``) unless ``worker_id`` holds the lease."""
        now = self.clock.now()
        body = canonical_json(result)

        def op(c: sqlite3.Connection) -> bool:
            cur = c.execute(
                "UPDATE jobs SET status=?, result=?, error=NULL, lease_owner=NULL,"
                " lease_expires_at=NULL, updated_at=?"
                " WHERE job_id=? AND status=? AND lease_owner=?",
                (COMPLETED, body, now, job_id, LEASED, worker_id),
            )
            return cur.rowcount == 1

        return await self.db.transaction(op)

    async def fail(
        self, job_id: str, worker_id: str, error: dict[str, Any], retry_delay_s: float = 1.0
    ) -> bool:
        """Record a failed attempt: requeue with backoff, or dead-letter it.

        The delay is ``retry_delay_s * 2 ** (attempts - 1)`` capped at
        ``max_backoff_s``, so transient outages are retried quickly at first
        and then progressively less aggressively. Once ``max_attempts`` is
        reached the job moves to ``dead`` where an operator can inspect it.
        Returns ``False`` if ``worker_id`` no longer holds the lease.
        """
        now = self.clock.now()
        body = canonical_json(error)

        def op(c: sqlite3.Connection) -> bool:
            row = c.execute(
                "SELECT attempts, max_attempts FROM jobs"
                " WHERE job_id=? AND status=? AND lease_owner=?",
                (job_id, LEASED, worker_id),
            ).fetchone()
            if row is None:
                return False
            attempts, max_attempts = int(row[0]), int(row[1])
            if attempts >= max_attempts:
                c.execute(
                    "UPDATE jobs SET status=?, error=?, lease_owner=NULL, lease_expires_at=NULL,"
                    " updated_at=? WHERE job_id=?",
                    (DEAD, body, now, job_id),
                )
            else:
                delay = min(retry_delay_s * 2 ** (attempts - 1), self.max_backoff_s)
                c.execute(
                    "UPDATE jobs SET status=?, error=?, lease_owner=NULL, lease_expires_at=NULL,"
                    " available_at=?, updated_at=? WHERE job_id=?",
                    (QUEUED, body, now + delay, now, job_id),
                )
            return True

        return await self.db.transaction(op)

    async def cancel(self, job_id: str) -> bool:
        """Cancel a queued or leased job.

        A worker currently running it learns about it on its next heartbeat
        (which returns ``False``) and abandons the work.
        """
        now = self.clock.now()

        def op(c: sqlite3.Connection) -> bool:
            cur = c.execute(
                "UPDATE jobs SET status=?, lease_owner=NULL, lease_expires_at=NULL, updated_at=?"
                " WHERE job_id=? AND status IN (?, ?)",
                (CANCELLED, now, job_id, QUEUED, LEASED),
            )
            return cur.rowcount == 1

        return await self.db.transaction(op)

    # ------------------------------------------------------------------- reads

    async def get(self, job_id: str) -> Job:
        row = await self.db.run(
            lambda c: c.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        )
        if row is None:
            raise JobNotFound(job_id)
        return _row_to_job(row)

    async def stats(self) -> dict[str, int]:
        """Job counts per status (every status present, zero if empty)."""
        rows = await self.db.run(
            lambda c: c.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall()
        )
        out = dict.fromkeys(STATUSES, 0)
        for status, count in rows:
            out[str(status)] = int(count)
        return out

    async def list(
        self, status: str | None = None, job_type: str | None = None, limit: int = 100
    ) -> builtins.list[Job]:
        sql = "SELECT * FROM jobs WHERE 1=1"
        params: builtins.list[Any] = []
        if status is not None:
            sql += " AND status=?"
            params.append(status)
        if job_type is not None:
            sql += " AND job_type=?"
            params.append(job_type)
        sql += " ORDER BY priority DESC, available_at ASC, seq ASC LIMIT ?"
        params.append(limit)
        rows = await self.db.run(lambda c: c.execute(sql, params).fetchall())
        return [_row_to_job(r) for r in rows]


def _row_to_job(row: sqlite3.Row) -> Job:
    return Job(
        job_id=row["job_id"],
        job_type=row["job_type"],
        payload=json.loads(row["payload"]),
        status=row["status"],
        run_id=row["run_id"],
        priority=row["priority"],
        attempts=row["attempts"],
        max_attempts=row["max_attempts"],
        available_at=row["available_at"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        idempotency_key=row["idempotency_key"],
        result=json.loads(row["result"]) if row["result"] is not None else None,
        error=json.loads(row["error"]) if row["error"] is not None else None,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
