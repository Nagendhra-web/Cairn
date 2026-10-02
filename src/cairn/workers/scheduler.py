"""Helpers that turn runtime intents into queue jobs.

They exist so API handlers and CLIs enqueue work the same way everywhere,
in particular with the same idempotency keys, which is what makes a retried
"submit" request safe.
"""

from __future__ import annotations

from typing import Any

from cairn.runtime.engine import Runtime
from cairn.runtime.plan import Plan
from cairn.workers.queue import WorkQueue
from cairn.workers.worker import RUN_EXECUTE, RUN_RESUME


async def submit_run(
    runtime: Runtime,
    queue: WorkQueue,
    plan: Plan,
    *,
    priority: int = 0,
    max_attempts: int = 3,
    **kwargs: Any,
) -> tuple[str, str]:
    """Create a run and enqueue its execution; returns ``(run_id, job_id)``.

    ``kwargs`` go to :meth:`Runtime.create_run`. The job's idempotency key
    is the run id, so if this call is retried with an explicit ``run_id``
    after the run was created, no second execution job is queued.
    """
    run_id = kwargs.get("run_id")
    if run_id is None:
        run_id = await runtime.create_run(plan, **kwargs)
    else:
        try:
            await runtime.load(str(run_id))
        except Exception:  # not created yet: create it with the requested id
            run_id = await runtime.create_run(plan, **kwargs)
    job_id = await queue.enqueue(
        RUN_EXECUTE,
        {"run_id": run_id},
        run_id=run_id,
        priority=priority,
        idempotency_key=run_id,
        max_attempts=max_attempts,
    )
    return run_id, job_id


async def resume_when_approved(
    queue: WorkQueue,
    run_id: str,
    *,
    priority: int = 0,
    idempotency_key: str | None = None,
    max_attempts: int = 3,
) -> str:
    """Enqueue a ``run.resume`` job, typically right after ``Runtime.decide``.

    No idempotency key by default because a run can suspend and be resumed
    several times; pass one (for example the approval request id) to make a
    retried resume a no-op.
    """
    return await queue.enqueue(
        RUN_RESUME,
        {"run_id": run_id},
        run_id=run_id,
        priority=priority,
        idempotency_key=idempotency_key,
        max_attempts=max_attempts,
    )
