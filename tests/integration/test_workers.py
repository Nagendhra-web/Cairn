"""Integration tests for the leased work queue and background workers.

These use a real SQLite file (WAL) and a real Runtime with tool-only plans,
so they exercise the same code paths a deployed worker fleet does.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest

from cairn.core.clock import ManualClock
from cairn.journal.store import SQLiteJournal
from cairn.runtime import PlanBuilder, Runtime, Services, ref
from cairn.runtime.plan import Plan
from cairn.storage.sqlite import SQLiteDatabase
from cairn.tools.decorator import tool
from cairn.tools.registry import ToolRegistry
from cairn.workers import (
    COMPLETED,
    DEAD,
    LEASED,
    QUEUED,
    Job,
    SQLiteWorkQueue,
    Worker,
    resume_when_approved,
    submit_run,
)

pytestmark = pytest.mark.integration


@pytest.fixture
def db(tmp_path: Path) -> SQLiteDatabase:
    return SQLiteDatabase(tmp_path / "x.db")


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def queue(db: SQLiteDatabase, clock: ManualClock) -> SQLiteWorkQueue:
    return SQLiteWorkQueue(db, clock=clock)


async def wait_for(
    predicate: Callable[[], Awaitable[bool]], limit_s: float = 10.0, step: float = 0.02
) -> None:
    deadline = asyncio.get_running_loop().time() + limit_s
    while not await predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(step)


# ------------------------------------------------------------------- queue only


async def test_concurrent_leases_are_exclusive(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    clock = ManualClock()
    # Separate connections to the same file behave like separate processes:
    # only BEGIN IMMEDIATE stops two of them claiming the same row.
    queues = [SQLiteWorkQueue(SQLiteDatabase(path), clock=clock) for _ in range(4)]
    ids = {await queues[0].enqueue("t", {"i": i}) for i in range(40)}

    async def grab(n: int) -> Job | None:
        return await queues[n % len(queues)].lease(f"w{n}", 30)

    leased = await asyncio.gather(*(grab(n) for n in range(80)))
    got = [j.job_id for j in leased if j is not None]
    assert len(got) == 40
    assert set(got) == ids
    stats = await queues[1].stats()
    assert stats[LEASED] == 40
    assert stats[QUEUED] == 0


async def test_expired_lease_is_reclaimed(queue: SQLiteWorkQueue, clock: ManualClock) -> None:
    job_id = await queue.enqueue("t", {})
    first = await queue.lease("w1", 10)
    assert first is not None and first.job_id == job_id and first.attempts == 1
    assert await queue.lease("w2", 10) is None

    clock.advance(10.5)  # w1 "crashed": no heartbeat
    second = await queue.lease("w2", 10)
    assert second is not None and second.job_id == job_id
    assert second.lease_owner == "w2" and second.attempts == 2

    # The old owner can no longer extend, complete or fail it.
    assert await queue.heartbeat(job_id, "w1", 10) is False
    assert await queue.complete(job_id, "w1", {"x": 1}) is False
    assert await queue.fail(job_id, "w1", {"code": "x"}) is False
    assert await queue.complete(job_id, "w2", {"ok": True}) is True
    done = await queue.get(job_id)
    assert done.status == COMPLETED and done.result == {"ok": True}


async def test_heartbeat_extends_only_for_owner(
    queue: SQLiteWorkQueue, clock: ManualClock
) -> None:
    job_id = await queue.enqueue("t", {})
    assert await queue.lease("w1", 10) is not None
    clock.advance(8)
    assert await queue.heartbeat(job_id, "w2", 10) is False
    assert await queue.heartbeat(job_id, "w1", 10) is True
    clock.advance(8)  # 16s after lease, but only 8s after the heartbeat
    assert await queue.lease("w2", 10) is None
    assert (await queue.get(job_id)).lease_expires_at == pytest.approx(clock.now() + 2)
    clock.advance(3)
    reclaimed = await queue.lease("w2", 10)
    assert reclaimed is not None and reclaimed.job_id == job_id


async def test_expired_final_attempt_goes_to_dead_letter(
    queue: SQLiteWorkQueue, clock: ManualClock
) -> None:
    job_id = await queue.enqueue("t", {}, max_attempts=1)
    assert await queue.lease("w1", 5) is not None
    clock.advance(6)
    assert await queue.lease("w2", 5) is None
    job = await queue.get(job_id)
    assert job.status == DEAD
    assert job.error is not None and job.error["code"] == "lease_expired"


async def test_retry_backoff_until_dead_letter(
    queue: SQLiteWorkQueue, clock: ManualClock
) -> None:
    job_id = await queue.enqueue("t", {}, max_attempts=3)
    t0 = clock.now()
    for attempt, expected_delay in ((1, 2.0), (2, 4.0)):
        job = await queue.lease("w", 30)
        assert job is not None and job.attempts == attempt
        assert await queue.fail(job_id, "w", {"code": "boom"}, retry_delay_s=2.0)
        requeued = await queue.get(job_id)
        assert requeued.status == QUEUED
        assert requeued.available_at == pytest.approx(clock.now() + expected_delay)
        clock.advance(expected_delay - 0.1)
        assert await queue.lease("w", 30) is None  # still backing off
        clock.advance(0.1)
    job = await queue.lease("w", 30)
    assert job is not None and job.attempts == 3
    assert await queue.fail(job_id, "w", {"code": "boom", "n": 3}, retry_delay_s=2.0)
    dead = await queue.get(job_id)
    assert dead.status == DEAD and dead.error == {"code": "boom", "n": 3}
    assert clock.now() - t0 == pytest.approx(6.0)
    clock.advance(1000)
    assert await queue.lease("w", 30) is None
    assert (await queue.stats())[DEAD] == 1
    assert [j.job_id for j in await queue.list(status=DEAD)] == [job_id]


async def test_backoff_is_capped(db: SQLiteDatabase, clock: ManualClock) -> None:
    q = SQLiteWorkQueue(db, clock=clock, max_backoff_s=5.0)
    job_id = await q.enqueue("t", {}, max_attempts=10)
    for _ in range(4):
        clock.advance(100)
        assert await q.lease("w", 30) is not None
        await q.fail(job_id, "w", {"code": "x"}, retry_delay_s=2.0)
    assert (await q.get(job_id)).available_at == pytest.approx(clock.now() + 5.0)


async def test_idempotent_enqueue(queue: SQLiteWorkQueue) -> None:
    a = await queue.enqueue("t", {"v": 1}, idempotency_key="k1")
    b = await queue.enqueue("t", {"v": 2}, idempotency_key="k1")
    c = await queue.enqueue("t", {"v": 3}, idempotency_key="k2")
    assert a == b != c
    assert (await queue.get(a)).payload == {"v": 1}
    assert len(await queue.list()) == 2

    # Concurrent duplicates still collapse to one job.
    many = await asyncio.gather(*(queue.enqueue("t", {}, idempotency_key="k3") for _ in range(10)))
    assert len(set(many)) == 1


async def test_priority_fifo_delay_and_type_filter(
    queue: SQLiteWorkQueue, clock: ManualClock
) -> None:
    low1 = await queue.enqueue("t", {}, priority=0)
    high = await queue.enqueue("t", {}, priority=10)
    low2 = await queue.enqueue("t", {}, priority=0)
    later = await queue.enqueue("t", {}, priority=100, available_at=clock.now() + 60)
    other = await queue.enqueue("other", {}, priority=50)

    order = []
    while (job := await queue.lease("w", 30, job_types=["t"])) is not None:
        order.append(job.job_id)
    assert order == [high, low1, low2]
    clock.advance(60)
    assert (j := await queue.lease("w", 30, job_types=["t"])) is not None and j.job_id == later
    assert (j := await queue.lease("w", 30)) is not None and j.job_id == other


async def test_cancel(queue: SQLiteWorkQueue) -> None:
    job_id = await queue.enqueue("t", {})
    assert await queue.cancel(job_id) is True
    assert await queue.cancel(job_id) is False
    assert await queue.lease("w", 30) is None
    assert (await queue.get(job_id)).status == "cancelled"


async def test_queue_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "x.db"
    job_id = await SQLiteWorkQueue(SQLiteDatabase(path)).enqueue("t", {"a": [1, 2]})
    reopened = SQLiteWorkQueue(SQLiteDatabase(path))
    job = await reopened.lease("w", 30)
    assert job is not None and job.job_id == job_id and job.payload == {"a": [1, 2]}


# ---------------------------------------------------------------- with runtime


def make_runtime(db: SQLiteDatabase, *specs: Any) -> Runtime:
    registry = ToolRegistry()
    for spec in specs:
        registry.register(spec)
    return Runtime(Services(journal=SQLiteJournal(db), tools=registry))


def counting_tools(calls: dict[str, int]) -> list[Any]:
    @tool
    async def add(a: int, b: int) -> int:
        """Add two numbers."""
        calls["add"] = calls.get("add", 0) + 1
        return a + b

    @tool
    async def double(x: int) -> int:
        """Double a number."""
        calls["double"] = calls.get("double", 0) + 1
        return x * 2

    return [add, double]


def arithmetic_plan() -> Plan:
    b = PlanBuilder("arithmetic")
    b.tool("sum", "add", a=2, b=3)
    b.tool("twice", "double", deps=["sum"], x=ref("sum"))
    return b.build(output=ref("twice"))


async def test_worker_executes_run_end_to_end(db: SQLiteDatabase) -> None:
    calls: dict[str, int] = {}
    runtime = make_runtime(db, *counting_tools(calls))
    queue = SQLiteWorkQueue(db)
    run_id, job_id = await submit_run(runtime, queue, arithmetic_plan())
    # Resubmitting with the same run id does not queue a second execution.
    again = await submit_run(runtime, queue, arithmetic_plan(), run_id=run_id)
    assert again == (run_id, job_id)

    worker = Worker(runtime, queue, worker_id="w1", concurrency=2, poll_interval_s=0.02)
    worker.start()
    try:
        await wait_for(lambda: _status_is(queue, job_id, COMPLETED))
    finally:
        await worker.stop(timeout=5)

    job = await queue.get(job_id)
    assert job.result["status"] == "completed"
    assert job.result["run_id"] == run_id
    state = await runtime.load(run_id)
    assert state.status == "completed"
    assert state.output is not None and state.output.value == 10
    assert calls == {"add": 1, "double": 1}
    assert (await runtime.journal.get_run(run_id)).status == "completed"
    assert not worker.running


async def test_many_runs_across_two_workers(db: SQLiteDatabase) -> None:
    calls: dict[str, int] = {}
    runtime = make_runtime(db, *counting_tools(calls))
    queue = SQLiteWorkQueue(db)
    submitted = [await submit_run(runtime, queue, arithmetic_plan()) for _ in range(8)]
    workers = [
        Worker(runtime, queue, worker_id=f"w{i}", concurrency=3, poll_interval_s=0.02)
        for i in range(2)
    ]
    for w in workers:
        w.start()
    try:
        await wait_for(lambda: _count(queue, COMPLETED, 8))
    finally:
        for w in workers:
            await w.stop(timeout=5)
    assert calls == {"add": 8, "double": 8}
    for run_id, _ in submitted:
        assert (await runtime.load(run_id)).status == "completed"


async def test_suspended_run_completes_job_and_resumes(db: SQLiteDatabase) -> None:
    calls: dict[str, int] = {}
    runtime = make_runtime(db, *counting_tools(calls))
    queue = SQLiteWorkQueue(db)
    b = PlanBuilder("needs sign-off")
    b.tool("sum", "add", a=1, b=1)
    b.approval("ok", "Proceed?", deps=["sum"])
    b.tool("twice", "double", deps=["ok"], x=ref("sum"))
    run_id, job_id = await submit_run(runtime, queue, b.build(output=ref("twice")))

    worker = Worker(runtime, queue, worker_id="w1")
    assert (await worker.run_once()) is not None
    job = await queue.get(job_id)
    assert job.status == COMPLETED
    assert job.result["status"] == "suspended"
    [request_id] = job.result["pending_approvals"]

    await runtime.decide(run_id, request_id, approved=True)
    resume_id = await resume_when_approved(queue, run_id, idempotency_key=request_id)
    assert await resume_when_approved(queue, run_id, idempotency_key=request_id) == resume_id
    await worker.run_once()
    resumed = await queue.get(resume_id)
    assert resumed.status == COMPLETED and resumed.result["status"] == "completed"
    assert (await runtime.load(run_id)).output.value == 4  # type: ignore[union-attr]
    assert calls == {"add": 1, "double": 1}


async def test_custom_handler_and_failures(db: SQLiteDatabase) -> None:
    runtime = make_runtime(db)
    queue = SQLiteWorkQueue(db)
    worker = Worker(runtime, queue, worker_id="w1", retry_delay_s=0.0, poll_interval_s=0.01)
    seen: list[dict[str, Any]] = []

    @worker.handler("email.send")
    async def send(job: Job) -> dict[str, Any]:
        seen.append(job.payload)
        return {"sent": True}

    @worker.handler("always.fails")
    async def boom(job: Job) -> None:
        raise RuntimeError(f"attempt {job.attempts}")

    ok = await queue.enqueue("email.send", {"to": "a@example.com"})
    bad = await queue.enqueue("always.fails", {}, max_attempts=2)
    ignored = await queue.enqueue("not.mine", {})
    worker.start()
    try:
        await wait_for(lambda: _status_is(queue, bad, DEAD))
        await wait_for(lambda: _status_is(queue, ok, COMPLETED))
    finally:
        await worker.stop(timeout=5)
    assert seen == [{"to": "a@example.com"}]
    dead = await queue.get(bad)
    assert dead.attempts == 2 and dead.error is not None
    assert dead.error["message"] == "attempt 2"
    # Workers only lease job types they have handlers for.
    assert (await queue.get(ignored)).status == QUEUED


async def test_cancelled_job_is_abandoned_by_worker(db: SQLiteDatabase) -> None:
    runtime = make_runtime(db)
    queue = SQLiteWorkQueue(db)
    worker = Worker(runtime, queue, worker_id="w1", lease_s=0.3, poll_interval_s=0.01)
    started = asyncio.Event()
    stopped = asyncio.Event()

    @worker.handler("slow")
    async def slow(job: Job) -> None:
        started.set()
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            stopped.set()
            raise

    job_id = await queue.enqueue("slow", {})
    worker.start()
    try:
        await asyncio.wait_for(started.wait(), 5)
        assert await queue.cancel(job_id)
        # The next heartbeat sees the lease is gone and stops the handler.
        await asyncio.wait_for(stopped.wait(), 5)
        await wait_for(lambda: _no_inflight(worker))
    finally:
        await worker.stop(timeout=1)
    assert (await queue.get(job_id)).status == "cancelled"


async def test_crashed_worker_run_resumes_without_repeating_effects(tmp_path: Path) -> None:
    db = SQLiteDatabase(tmp_path / "x.db")
    clock = ManualClock()
    calls: dict[str, int] = {}
    first_call_started = asyncio.Event()
    gate = asyncio.Event()

    @tool
    async def fast(n: int) -> int:
        """Quick step that must not run twice."""
        calls["fast"] = calls.get("fast", 0) + 1
        return n + 1

    @tool
    async def slow(n: int) -> int:
        """Slow step: the first call hangs (the crash window), later calls return."""
        calls["slow"] = calls.get("slow", 0) + 1
        if calls["slow"] == 1:
            first_call_started.set()
            await gate.wait()
        return n * 10

    queue = SQLiteWorkQueue(db, clock=clock)
    b = PlanBuilder("crash me")
    b.tool("first", "fast", n=1)
    b.tool("second", "slow", deps=["first"], n=ref("first"))
    plan = b.build(output=ref("second"))

    runtime1 = make_runtime(db, fast, slow)
    run_id, job_id = await submit_run(runtime1, queue, plan)
    w1 = Worker(runtime1, queue, worker_id="w1", lease_s=30, poll_interval_s=0.01)
    w1.start()
    await asyncio.wait_for(first_call_started.wait(), 10)
    await w1.stop(timeout=0.05)  # shutdown deadline hits mid-tool: job is cancelled

    job = await queue.get(job_id)
    assert job.status == LEASED and job.lease_owner == "w1"
    state = await runtime1.load(run_id)
    assert state.status not in ("completed", "failed", "cancelled")
    assert state.node("first").status == "completed"
    assert state.node("second").status != "completed"

    # A second worker process (fresh runtime, separate connection) cannot take
    # the job while w1's lease is live, and takes it once the lease expires.
    db2 = SQLiteDatabase(tmp_path / "x.db")
    runtime2 = make_runtime(db2, fast, slow)
    queue2 = SQLiteWorkQueue(db2, clock=clock)
    w2 = Worker(runtime2, queue2, worker_id="w2", lease_s=30, poll_interval_s=0.01)
    assert await w2.run_once() is None
    clock.advance(31)
    w2.start()
    try:
        await wait_for(lambda: _status_is(queue2, job_id, COMPLETED))
    finally:
        await w2.stop(timeout=5)
        # Runtime.execute does not cancel its node tasks when it is itself
        # cancelled, so w1's first ``slow`` call is still parked on the gate.
        # Cancel it rather than release it, so it cannot journal into the
        # finished run (a real crashed process would simply be gone).
        for task in asyncio.all_tasks():
            if task is not asyncio.current_task() and "_run_node" in repr(task.get_coro()):
                task.cancel()

    done = await queue2.get(job_id)
    assert done.attempts == 2 and done.lease_owner is None
    assert done.result["status"] == "completed"
    final = await runtime2.load(run_id)
    assert final.status == "completed"
    assert final.output is not None and final.output.value == 20
    assert calls["fast"] == 1  # the completed node was not re-executed
    assert calls["slow"] == 2  # the interrupted node ran again, once


async def _status_is(queue: SQLiteWorkQueue, job_id: str, status: str) -> bool:
    return (await queue.get(job_id)).status == status


async def _count(queue: SQLiteWorkQueue, status: str, n: int) -> bool:
    return (await queue.stats())[status] == n


async def _no_inflight(worker: Worker) -> bool:
    return not worker.inflight


async def test_run_until_signal_shuts_down_on_sigterm(db: SQLiteDatabase) -> None:
    import os
    import signal

    worker = Worker(make_runtime(db), SQLiteWorkQueue(db), worker_id="w1", poll_interval_s=0.01)
    task = asyncio.create_task(worker.run_until_signal())
    await wait_for(lambda: _is_running(worker))
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 5)
    assert not worker.running


async def _is_running(worker: Worker) -> bool:
    return worker.running
