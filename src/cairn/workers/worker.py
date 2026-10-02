"""Background worker: lease jobs, run them, heartbeat, report the outcome.

A worker is deliberately stateless: everything it needs to continue lives in
the queue (who owns which job, until when) and in the run journal (what a run
has already done). That is what makes it safe to kill one at any moment. Its
jobs' leases expire, another worker leases them, and because ``Runtime``
folds the journal before executing, the re-execution resumes the run instead
of repeating effects that were already recorded.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import socket
from collections.abc import Awaitable, Callable
from typing import Any

from cairn.core.errors import error_payload
from cairn.core.ids import new_id
from cairn.runtime.engine import Runtime
from cairn.workers.queue import Job, WorkQueue

log = logging.getLogger("cairn.workers")

JobHandler = Callable[[Job], Awaitable[Any]]

RUN_EXECUTE = "run.execute"
RUN_RESUME = "run.resume"


class LeaseLost(Exception):
    """The job's lease was taken away (cancelled, or expired and re-leased)."""


class Worker:
    """Async job processor bound to one :class:`Runtime` and one queue.

    Args:
        runtime: executes ``run.execute`` / ``run.resume`` jobs.
        queue: where jobs are leased from.
        worker_id: stable identity for lease ownership; defaults to
            ``host:pid:random`` so log lines point at a real process.
        concurrency: maximum jobs processed at once by this worker.
        lease_s: lease length; heartbeats renew it every ``lease_s / 3``.
        poll_interval_s: idle wait between empty lease attempts.
        job_types: only lease these types (``None`` means every type that
            has a registered handler, so a worker never leases work it
            cannot do).
        retry_delay_s: base delay for the queue's exponential backoff.
        shutdown_timeout_s: default grace period for :meth:`stop`.
    """

    def __init__(
        self,
        runtime: Runtime,
        queue: WorkQueue,
        worker_id: str | None = None,
        concurrency: int = 4,
        lease_s: float = 30.0,
        poll_interval_s: float = 0.5,
        job_types: list[str] | None = None,
        *,
        retry_delay_s: float = 1.0,
        shutdown_timeout_s: float = 30.0,
    ) -> None:
        if concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        self.runtime = runtime
        self.queue = queue
        self.worker_id = worker_id or (
            f"{socket.gethostname()}:{os.getpid()}:{new_id('w').split('_', 1)[1][:8]}"
        )
        self.concurrency = concurrency
        self.lease_s = lease_s
        self.poll_interval_s = poll_interval_s
        self.job_types = list(job_types) if job_types else None
        self.retry_delay_s = retry_delay_s
        self.shutdown_timeout_s = shutdown_timeout_s
        self.handlers: dict[str, JobHandler] = {
            RUN_EXECUTE: self._run_job,
            RUN_RESUME: self._run_job,
        }
        self.processed = 0
        self._stopping = asyncio.Event()
        self._loop_task: asyncio.Task[None] | None = None
        self._inflight: dict[asyncio.Task[None], Job] = {}
        self._slots = asyncio.Semaphore(concurrency)
        self._leasing = False

    # ---------------------------------------------------------------- handlers

    def handler(self, job_type: str) -> Callable[[JobHandler], JobHandler]:
        """Register ``fn`` as the handler for ``job_type``.

        The handler receives the leased :class:`Job` and returns a
        JSON-serializable result. Raising marks the attempt failed (retried
        with backoff until the job's ``max_attempts``).
        """

        def register(fn: JobHandler) -> JobHandler:
            self.handlers[job_type] = fn
            return fn

        return register

    async def _run_job(self, job: Job) -> dict[str, Any]:
        """Drive a run forward. Suspension is a successful job outcome.

        A run waiting for approval is not an error: the job completes with
        status ``suspended`` and whoever records the decision enqueues a
        ``run.resume`` job. Retrying would only spin on the same approval.
        A run that fails is likewise a final business outcome, not a
        reason to retry the job.
        """
        run_id = job.payload.get("run_id") or job.run_id
        if not run_id:
            raise ValueError(f"job '{job.job_id}' has no run_id")
        result = await self.runtime.execute(str(run_id))
        return {
            "run_id": result.run_id,
            "status": result.status,
            "error": result.error,
            "pending_approvals": [a["request_id"] for a in result.pending_approvals],
        }

    # -------------------------------------------------------------- lifecycle

    @property
    def running(self) -> bool:
        return self._loop_task is not None and not self._loop_task.done()

    @property
    def inflight(self) -> list[Job]:
        return list(self._inflight.values())

    def start(self) -> asyncio.Task[None]:
        """Start the lease loop in the background and return its task."""
        if self.running:
            raise RuntimeError(f"worker '{self.worker_id}' is already running")
        self._stopping.clear()
        self._loop_task = asyncio.create_task(self.run(), name=f"cairn-worker:{self.worker_id}")
        return self._loop_task

    async def run(self) -> None:
        """Lease and dispatch jobs until :meth:`stop` is called."""
        types = self.job_types or sorted(self.handlers)
        log.info("worker started", extra={"worker_id": self.worker_id, "job_types": types,
                                          "concurrency": self.concurrency})
        try:
            while not self._stopping.is_set():
                await self._slots.acquire()
                if self._stopping.is_set():
                    self._slots.release()
                    break
                self._leasing = True
                try:
                    job = await self.queue.lease(self.worker_id, self.lease_s, types)
                except Exception:
                    self._slots.release()
                    log.exception("lease failed", extra={"worker_id": self.worker_id})
                    await self._idle()
                    continue
                finally:
                    self._leasing = False
                if job is None:
                    self._slots.release()
                    await self._idle()
                    continue
                task = asyncio.create_task(self._process(job), name=f"cairn-job:{job.job_id}")
                self._inflight[task] = job
                task.add_done_callback(self._finished)
        finally:
            log.info("worker loop exited", extra={"worker_id": self.worker_id})

    async def run_once(self) -> Job | None:
        """Lease and fully process at most one job (handy for tests and cron)."""
        types = self.job_types or sorted(self.handlers)
        job = await self.queue.lease(self.worker_id, self.lease_s, types)
        if job is not None:
            await self._process(job)
        return job

    async def stop(self, timeout: float | None = None) -> None:  # noqa: ASYNC109
        """Stop leasing, let in-flight jobs finish for ``timeout``, cancel the rest.

        Cancelled jobs are not failed or released: their leases are left to
        expire so they behave exactly like a crashed worker's jobs. That
        keeps one recovery path (lease expiry) instead of two, and the run
        journal guarantees the next worker resumes rather than repeats.
        """
        timeout = self.shutdown_timeout_s if timeout is None else timeout
        self._stopping.set()
        if self._loop_task is not None:
            # Never cancel a lease call midway: the row would be claimed in
            # the database but no task would run it until the lease expired.
            # Between leases the loop only waits, so cancelling there is safe.
            if not self._leasing:
                self._loop_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._loop_task
        pending = set(self._inflight)
        if pending:
            log.info("waiting for in-flight jobs", extra={
                "worker_id": self.worker_id, "count": len(pending), "timeout_s": timeout})
            _, still = await asyncio.wait(pending, timeout=timeout)
            for task in still:
                log.warning("cancelling in-flight job at shutdown", extra={
                    "worker_id": self.worker_id, "job_id": self._inflight[task].job_id})
                task.cancel()
            if still:
                await asyncio.gather(*still, return_exceptions=True)
        log.info("worker stopped", extra={"worker_id": self.worker_id,
                                          "processed": self.processed})

    async def run_until_signal(
        self, signals: tuple[signal.Signals, ...] = (signal.SIGINT, signal.SIGTERM)
    ) -> None:
        """Run until SIGINT/SIGTERM, then shut down gracefully.

        Intended as the body of a ``cairn worker`` process. A second signal
        during shutdown is not special-cased: the default grace period then
        cancellation still applies, and leases make even a hard kill safe.
        """
        loop = asyncio.get_running_loop()
        stop_requested = asyncio.Event()
        installed: list[signal.Signals] = []
        for sig in signals:
            try:
                loop.add_signal_handler(sig, stop_requested.set)
                installed.append(sig)
            except (NotImplementedError, RuntimeError):  # pragma: no cover - Windows
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop_requested.set))
        loop_task = self.start()
        try:
            waiter = asyncio.ensure_future(stop_requested.wait())
            await asyncio.wait({waiter, loop_task}, return_when=asyncio.FIRST_COMPLETED)
            waiter.cancel()
            log.info("shutdown requested", extra={"worker_id": self.worker_id})
        finally:
            await self.stop()
            for sig in installed:
                loop.remove_signal_handler(sig)

    # ------------------------------------------------------------- internals

    async def _idle(self) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), self.poll_interval_s)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self._inflight.pop(task, None)
        self._slots.release()
        if not task.cancelled() and task.exception() is not None:  # pragma: no cover
            log.error("job task crashed", exc_info=task.exception())

    async def _process(self, job: Job) -> None:
        """Run one leased job with a heartbeat, then complete or fail it."""
        ctx = {"worker_id": self.worker_id, "job_id": job.job_id, "job_type": job.job_type,
               "run_id": job.run_id, "attempt": job.attempts}
        handler = self.handlers.get(job.job_type)
        if handler is None:
            log.error("no handler for job type", extra=ctx)
            await self.queue.fail(
                job.job_id, self.worker_id,
                {"code": "no_handler", "message": f"no handler for '{job.job_type}'"},
                self.retry_delay_s,
            )
            return
        log.info("job started", extra=ctx)
        # ensure_future (not create_task) because handlers may return any awaitable.
        work: asyncio.Future[Any] = asyncio.ensure_future(handler(job))
        beat = asyncio.create_task(self._heartbeat(job, work))
        try:
            result = await work
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                # stop() (or a crash-like shutdown) cancelled us: make sure the
                # handler has really unwound before reporting we are done.
                if not work.done():
                    work.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await work
                log.warning("job interrupted; lease left to expire", extra=ctx)
                raise
            log.warning("job abandoned: lease lost", extra=ctx)
            return
        except Exception as exc:
            payload = error_payload(exc)
            log.warning("job failed", extra={**ctx, "error": payload})
            owned = await self.queue.fail(job.job_id, self.worker_id, payload, self.retry_delay_s)
            if not owned:
                log.warning("failure not recorded: lease no longer held", extra=ctx)
            return
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError, LeaseLost):
                await beat
        self.processed += 1
        if await self.queue.complete(job.job_id, self.worker_id, result):
            log.info("job completed", extra={**ctx, "result": result})
        else:
            log.warning("completion not recorded: lease no longer held", extra=ctx)

    async def _heartbeat(self, job: Job, work: asyncio.Future[Any]) -> None:
        """Renew the lease; if it was lost, stop the work so two workers never overlap."""
        interval = max(self.lease_s / 3.0, 0.01)
        while True:
            await asyncio.sleep(interval)
            try:
                owned = await self.queue.heartbeat(job.job_id, self.worker_id, self.lease_s)
            except Exception:
                log.exception("heartbeat failed", extra={"job_id": job.job_id})
                continue
            if not owned:
                work.cancel()
                raise LeaseLost(job.job_id)
