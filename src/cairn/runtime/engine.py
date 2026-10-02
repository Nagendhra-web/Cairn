"""Runtime facade: create, execute, resume, approve, cancel, replay and fork runs."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from cairn.core.errors import CairnError, NotFound
from cairn.core.ids import canonical_json, new_id, to_jsonable
from cairn.journal.events import EventDraft, EventType
from cairn.journal.store import ConcurrentAppend, InMemoryJournal, RunRecord
from cairn.provenance.labels import USER, Label
from cairn.runtime.budget import Budget
from cairn.runtime.executor import Executor
from cairn.runtime.plan import Plan
from cairn.runtime.recorder import Mode, Recorder
from cairn.runtime.services import Services
from cairn.runtime.state import EffectRecord, RunState, fold
from cairn.runtime.validate import validate_plan

log = logging.getLogger("cairn.runtime")

TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


@dataclass
class RunResult:
    run_id: str
    status: str
    output: Any = None
    label: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)
    nodes: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    @property
    def trusted(self) -> bool:
        return Label.from_dict(self.label).trusted

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_state(cls, st: RunState) -> RunResult:
        return cls(
            run_id=st.run_id,
            status=st.status,
            output=st.output.value if st.output else None,
            label=st.output.label.to_dict() if st.output else st.label.to_dict(),
            error=st.error,
            usage=st.usage.to_dict(),
            pending_approvals=[
                {"request_id": a.request_id, "node_id": a.node_id, "reason": a.reason,
                 "preview": a.preview}
                for a in st.pending_approvals
            ],
            nodes={nid: ns.status for nid, ns in st.nodes.items()},
        )


@dataclass
class ReplayReport:
    source_run_id: str
    replay_run_id: str
    matched: bool
    output_equal: bool
    status: str
    divergences: list[dict[str, Any]]
    effects_replayed: int
    error: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


class Runtime:
    def __init__(self, services: Services | None = None) -> None:
        self.services = services or Services()
        self._active: dict[str, asyncio.Event] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        # Effects a fork may reuse, kept for in-process resumes of that fork.
        self._fork_sources: dict[str, dict[str, EffectRecord]] = {}

    @property
    def journal(self) -> Any:
        return self.services.journal

    # ---------------------------------------------------------------- creation

    async def create_run(
        self,
        plan: Plan,
        *,
        inputs: dict[str, Any] | None = None,
        budget: Budget | None = None,
        grants: Iterable[str] = ("*",),
        label: Label = USER,
        agent: str | None = None,
        parent_run_id: str | None = None,
        depth: int = 0,
        run_id: str | None = None,
        tags: Iterable[str] = (),
        extra: dict[str, Any] | None = None,
    ) -> str:
        budget = budget or Budget()
        grants = list(grants)
        plan = validate_plan(plan, self.services.tools, grants=grants, max_nodes=budget.max_nodes)
        run_id = run_id or new_id("run")
        now = self.services.clock.now()
        await self.journal.create_run(
            RunRecord(
                run_id=run_id, goal=plan.goal, status="created", created_at=now, updated_at=now,
                parent_run_id=parent_run_id, agent=agent, tags=list(tags),
            )
        )
        await self.journal.append(
            run_id,
            [
                EventDraft(
                    type=EventType.RUN_CREATED,
                    ts=now,
                    data=to_jsonable(
                        {
                            "plan": plan.model_dump(mode="json"),
                            "goal": plan.goal,
                            "inputs": inputs or {},
                            "budget": budget.to_dict(),
                            "grants": grants,
                            "label": label.to_dict(),
                            "agent": agent,
                            "parent_run_id": parent_run_id,
                            "depth": depth,
                            **(extra or {}),
                        }
                    ),
                )
            ],
            expected_seq=1,
        )
        return run_id

    async def run(self, plan: Plan, **kwargs: Any) -> RunResult:
        run_id = await self.create_run(plan, **kwargs)
        return await self.execute(run_id)

    # --------------------------------------------------------------- execution

    async def load(self, run_id: str) -> RunState:
        events = await self.journal.read(run_id)
        if not events:
            raise NotFound(f"run '{run_id}' has no events", run_id=run_id)
        return fold(run_id, events)

    async def execute(
        self,
        run_id: str,
        *,
        mode: Mode = Mode.LIVE,
        source: dict[str, EffectRecord] | None = None,
    ) -> RunResult:
        lock = self._locks.setdefault(run_id, asyncio.Lock())
        if lock.locked():
            raise CairnError(f"run '{run_id}' is already executing in this process", run_id=run_id)
        async with lock:
            state = await self.load(run_id)
            if state.status in TERMINAL_STATUSES:
                return RunResult.from_state(state)
            cancel = asyncio.Event()
            self._active[run_id] = cancel
            if source is None:
                source = self._fork_sources.get(run_id)
            recorder = Recorder(self.journal, state, self.services.clock, mode=mode, source=source)
            try:
                await Executor(self.services, recorder, cancel).run()
            finally:
                self._active.pop(run_id, None)
                await self._sync_record(state)
            result = RunResult.from_state(state)
            if recorder.divergences:
                result.usage["divergences"] = len(recorder.divergences)
            return result

    resume = execute

    async def _sync_record(self, state: RunState) -> None:
        summary = state.usage.to_dict()
        summary["nodes"] = {nid: ns.status for nid, ns in state.nodes.items()}
        if state.started_at and state.ended_at:
            summary["duration_s"] = round(state.ended_at - state.started_at, 4)
        try:
            await self.journal.update_run(
                state.run_id, status=state.status, updated_at=self.services.clock.now(),
                summary=summary,
            )
        except NotFound:
            pass

    # ----------------------------------------------------------- human control

    async def decide(
        self,
        run_id: str,
        request_id: str,
        *,
        approved: bool,
        by: str = "operator",
        note: str | None = None,
    ) -> None:
        """Record a human decision on a pending approval (call ``resume`` after)."""
        for _ in range(10):
            state = await self.load(run_id)
            pending = state.approvals.get(request_id)
            if pending is None:
                raise NotFound(f"no approval request '{request_id}' in run '{run_id}'")
            if pending.status != "pending":
                raise CairnError(f"approval '{request_id}' was already {pending.status}")
            try:
                await self.journal.append(
                    run_id,
                    [EventDraft(
                        type=EventType.APPROVAL_DECIDED,
                        node_id=pending.node_id,
                        ts=self.services.clock.now(),
                        data={"request_id": request_id, "approved": approved, "by": by,
                              "note": note},
                    )],
                    expected_seq=state.last_seq + 1,
                )
                return
            except ConcurrentAppend:
                await asyncio.sleep(0.01)
        raise CairnError("could not record decision due to concurrent writes")

    async def cancel(self, run_id: str, reason: str = "cancelled by operator") -> bool:
        event = self._active.get(run_id)
        if event is not None:
            event.set()
            return True
        state = await self.load(run_id)
        if state.status in TERMINAL_STATUSES:
            return False
        await self.journal.append(
            run_id,
            [EventDraft(type=EventType.RUN_CANCELLED, ts=self.services.clock.now(),
                        data={"reason": reason})],
            expected_seq=state.last_seq + 1,
        )
        await self._sync_record(await self.load(run_id))
        return True

    # -------------------------------------------------------- replay and fork

    async def replay(self, run_id: str) -> ReplayReport:
        """Re-execute a recorded run with zero live effects and compare.

        Runs in an isolated in-memory journal, so replay never touches tools,
        models, memory or the real journal. Any effect whose request differs
        from the recording (because a prompt, plan, tool version or code path
        changed) is reported as a divergence with its node and key.
        """
        source = await self.load(run_id)
        if source.plan is None:
            raise NotFound(f"run '{run_id}' has no plan")
        sandbox_services = replace(
            self.services, journal=InMemoryJournal(), approval_handler=None
        )
        shadow = Runtime(sandbox_services)
        replay_id = new_id("replay")
        await shadow.create_run(
            source.plan,
            inputs=source.inputs,
            budget=source.budget,
            grants=source.grants,
            label=source.label,
            agent=source.agent,
            depth=source.depth,
            run_id=replay_id,
        )
        await shadow._copy_decisions(source, replay_id)
        result = await shadow.execute(replay_id, mode=Mode.STRICT, source=dict(source.effects))
        replay_state = await shadow.load(replay_id)
        divergences = [
            {"key": ev.data.get("key"), "node_id": ev.node_id, **ev.data}
            for ev in await shadow.journal.read(replay_id)
            if ev.type == EventType.EFFECT_DIVERGED
        ]
        if result.error and result.error.get("code") == "replay_divergence":
            divergences.append({"node_id": result.error.get("node_id"), **result.error.get("details", {}),
                                "message": result.error.get("message")})
        out_src = source.output.value if source.output else None
        out_new = replay_state.output.value if replay_state.output else None
        output_equal = canonical_json(out_src) == canonical_json(out_new)
        return ReplayReport(
            source_run_id=run_id,
            replay_run_id=replay_id,
            matched=output_equal and not divergences and result.status == source.status,
            output_equal=output_equal,
            status=result.status,
            divergences=divergences,
            effects_replayed=replay_state.usage.replayed_effects,
            error=result.error,
        )

    async def fork(
        self,
        run_id: str,
        *,
        patches: dict[str, dict[str, Any]] | None = None,
        invalidate: Iterable[str] = (),
        plan: Plan | None = None,
        execute: bool = True,
    ) -> RunResult:
        """Counterfactual re-execution.

        Creates a new run from ``run_id``'s plan (optionally patched per node,
        for example ``{"summarize": {"tier": "frontier"}}``) and pre-loads the
        source run's recorded effects. Effects whose requests are unchanged are
        reused at zero cost; changed nodes and everything downstream of them
        execute live. ``invalidate`` forces nodes (and their descendants) to
        re-execute even if their requests are identical, for example to
        resample a nondeterministic model call.
        """
        source = await self.load(run_id)
        if source.plan is None:
            raise NotFound(f"run '{run_id}' has no plan")
        base = plan or source.plan
        data = base.model_dump(mode="json")
        node_index = {n["id"]: n for n in data["nodes"]}
        for node_id, patch in (patches or {}).items():
            if node_id not in node_index:
                raise NotFound(f"cannot patch unknown node '{node_id}'")
            node_index[node_id].update(patch)
        new_plan = Plan.model_validate(data)
        invalid: set[str] = set()
        for node_id in invalidate:
            invalid |= {node_id, *new_plan.descendants(node_id)}
        reusable = {
            key: rec for key, rec in source.effects.items()
            if key.split("@", 1)[0] not in invalid
        }
        fork_id = await self.create_run(
            new_plan,
            inputs=source.inputs,
            budget=source.budget,
            grants=source.grants,
            label=source.label,
            agent=source.agent,
            depth=source.depth,
            parent_run_id=source.parent_run_id,
            tags=["fork"],
            extra={"forked_from": run_id, "patches": patches or {},
                   "invalidated": sorted(invalid)},
        )
        await self._copy_decisions(source, fork_id)
        self._fork_sources[fork_id] = reusable
        if not execute:
            return RunResult.from_state(await self.load(fork_id))
        return await self.execute(fork_id, source=reusable)

    async def _copy_decisions(self, source: RunState, target_run: str) -> None:
        decided = [a for a in source.approvals.values() if a.status != "pending"]
        if not decided:
            return
        drafts = []
        for a in decided:
            drafts.append(EventDraft(
                type=EventType.APPROVAL_REQUESTED, node_id=a.node_id, ts=self.services.clock.now(),
                data={"request_id": a.request_id, "reason": a.reason, "preview": a.preview,
                      "copied_from": source.run_id},
            ))
            drafts.append(EventDraft(
                type=EventType.APPROVAL_DECIDED, node_id=a.node_id, ts=self.services.clock.now(),
                data={"request_id": a.request_id, "approved": a.status == "approved",
                      "by": a.decided_by, "note": a.note, "copied_from": source.run_id},
            ))
        await self.journal.append(target_run, drafts)
