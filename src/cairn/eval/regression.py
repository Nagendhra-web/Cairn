"""Replay-based regression testing.

Every effect of a recorded run (model calls, tool calls, retrieval, memory)
is in the journal together with a fingerprint of its request. Re-executing a
run in strict replay mode serves every effect from that recording and fails
on the first request that differs. That turns a behavioral change, such as an
edited prompt, a changed system prompt, a new tool version or a different
plan, into a precise divergence report ("node ``summarize`` sent a different
model request") without calling a single model or tool.

Two entry points:

* :func:`replay_regression` replays stored runs with their own recorded plan.
  It detects changes in runtime code, tool definitions and default prompts.
* :func:`replay_with_plan` replays a stored run's effects against a
  *candidate* plan. It detects what a plan or prompt edit would change, and
  where, before the edit ships.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from cairn.core.errors import error_payload
from cairn.core.ids import canonical_json, new_id
from cairn.journal.events import EventDraft, EventType
from cairn.journal.store import InMemoryJournal
from cairn.runtime.engine import ReplayReport, Runtime
from cairn.runtime.plan import Plan
from cairn.runtime.recorder import Mode
from cairn.runtime.state import RunState


@dataclass
class RunCheck:
    run_id: str
    matched: bool
    status: str
    output_equal: bool
    divergences: list[dict[str, Any]] = field(default_factory=list)
    effects_replayed: int = 0
    error: dict[str, Any] | None = None


@dataclass
class RegressionReport:
    checks: list[RunCheck]

    @property
    def checked(self) -> int:
        return len(self.checks)

    @property
    def matched(self) -> int:
        return sum(1 for c in self.checks if c.matched)

    @property
    def diverged(self) -> list[RunCheck]:
        return [c for c in self.checks if not c.matched]

    def exit_code(self) -> int:
        return 1 if self.diverged else 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "checked": self.checked,
            "matched": self.matched,
            "diverged": [c.run_id for c in self.diverged],
            "checks": [asdict(c) for c in self.checks],
        }

    def to_markdown(self) -> str:
        lines = [
            "# Replay regression report",
            "",
            f"{self.matched}/{self.checked} recorded runs replayed identically.",
            "",
        ]
        if self.diverged:
            lines += ["| run | status | output equal | first divergence |", "|---|---|---|---|"]
            for c in self.diverged:
                first = c.divergences[0] if c.divergences else (c.error or {})
                where = f"{first.get('node_id')} {first.get('key') or first.get('message', '')}"
                lines.append(
                    f"| {c.run_id} | {c.status} | {c.output_equal} | {where.strip()} |"
                )
        return "\n".join(lines) + "\n"


def _check(report: ReplayReport) -> RunCheck:
    return RunCheck(
        run_id=report.source_run_id,
        matched=report.matched,
        status=report.status,
        output_equal=report.output_equal,
        divergences=list(report.divergences),
        effects_replayed=report.effects_replayed,
        error=report.error,
    )


async def select_runs(
    runtime: Runtime, *, status: str | None = "completed", limit: int = 100
) -> list[str]:
    """Run ids from the runtime's journal to use as a regression corpus.

    Replays (``replay_*``) are excluded because they live in throwaway
    journals and are never a source of truth.
    """
    records = await runtime.journal.list_runs(limit=limit, status=status)
    return [r.run_id for r in records if not r.run_id.startswith("replay_")]


async def replay_regression(
    runtime: Runtime,
    run_ids: Iterable[str] | None = None,
    *,
    plan_for: Callable[[RunState], Plan | None] | None = None,
) -> RegressionReport:
    """Replay recorded runs and report which ones no longer reproduce.

    ``plan_for`` optionally maps a recorded run to a candidate plan (return
    ``None`` to keep the recorded one), which is how a batch of plan or prompt
    edits is regression tested against many recordings at once.
    """
    ids: Sequence[str] = list(run_ids) if run_ids is not None else await select_runs(runtime)
    checks: list[RunCheck] = []
    for run_id in ids:
        try:
            candidate = plan_for(await runtime.load(run_id)) if plan_for else None
            report = (
                await replay_with_plan(runtime, run_id, candidate)
                if candidate is not None
                else await runtime.replay(run_id)
            )
            checks.append(_check(report))
        except Exception as exc:
            checks.append(RunCheck(run_id, False, "error", False, error=error_payload(exc)))
    return RegressionReport(checks)


async def replay_with_plan(runtime: Runtime, run_id: str, plan: Plan) -> ReplayReport:
    """Strictly replay ``run_id``'s recorded effects under a different plan.

    Mirrors :meth:`Runtime.replay` (isolated in-memory journal, no approval
    handler, strict mode) but substitutes ``plan``. Effect keys are
    ``node@attempt/kind#n``, so an unchanged node finds its recording and a
    changed request is reported as a divergence on exactly that node.
    """
    source = await runtime.load(run_id)
    shadow = Runtime(replace(runtime.services, journal=InMemoryJournal(), approval_handler=None))
    replay_id = new_id("replay")
    await shadow.create_run(
        plan, inputs=source.inputs, budget=source.budget, grants=source.grants,
        label=source.label, agent=source.agent, depth=source.depth, run_id=replay_id,
    )
    await _copy_decisions(shadow, source, replay_id)
    result = await shadow.execute(replay_id, mode=Mode.STRICT, source=dict(source.effects))
    state = await shadow.load(replay_id)
    divergences = [
        {"key": ev.data.get("key"), "node_id": ev.node_id, **ev.data}
        for ev in await shadow.journal.read(replay_id)
        if ev.type == EventType.EFFECT_DIVERGED
    ]
    if result.error and result.error.get("code") == "replay_divergence":
        divergences.append({"node_id": result.error.get("node_id"),
                            **result.error.get("details", {}),
                            "message": result.error.get("message")})
    out_src = source.output.value if source.output else None
    out_new = state.output.value if state.output else None
    output_equal = canonical_json(out_src) == canonical_json(out_new)
    return ReplayReport(
        source_run_id=run_id,
        replay_run_id=replay_id,
        matched=output_equal and not divergences and result.status == source.status,
        output_equal=output_equal,
        status=result.status,
        divergences=divergences,
        effects_replayed=state.usage.replayed_effects,
        error=result.error,
    )


async def _copy_decisions(shadow: Runtime, source: RunState, target: str) -> None:
    """Carry recorded human decisions into the replay so it does not stop at approvals."""
    drafts: list[EventDraft] = []
    for a in source.approvals.values():
        if a.status == "pending":
            continue
        drafts.append(EventDraft(
            type=EventType.APPROVAL_REQUESTED, node_id=a.node_id,
            data={"request_id": a.request_id, "reason": a.reason, "preview": a.preview,
                  "copied_from": source.run_id},
        ))
        drafts.append(EventDraft(
            type=EventType.APPROVAL_DECIDED, node_id=a.node_id,
            data={"request_id": a.request_id, "approved": a.status == "approved",
                  "by": a.decided_by, "note": a.note, "copied_from": source.run_id},
        ))
    if drafts:
        await shadow.journal.append(target, drafts)
