"""Agents: goal -> plan -> durable execution -> evaluation -> memory.

An :class:`Agent` is configuration plus a loop, not a chat transcript:

1. Build planner context (instructions, discovered tools, similar successful
   procedures from memory, trusted memories) under a token budget.
2. Plan (with validation and repair), then execute the plan as a durable run.
3. If the run suspends for approval, return; ``resume`` continues later.
4. Evaluate the result with the critic against the agent's criteria.
5. On failure, replan with concrete feedback (bounded by ``max_replans``).
6. Record an episode; store the plan as a reusable procedure if it passed and
   its provenance is trusted (untrusted runs never become procedures).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from cairn.agents.critic import Critic, Verdict
from cairn.agents.planner import Planner, PlanningResult, plan_to_json
from cairn.core.errors import CairnError, NotFound, PlanValidationError
from cairn.models.types import Tier
from cairn.provenance.labels import USER, Label
from cairn.runtime.budget import Budget
from cairn.runtime.engine import RunResult, Runtime
from cairn.runtime.services import SubagentRequest

log = logging.getLogger("cairn.agents")


class AgentSpec(BaseModel):
    """Declarative agent definition (loadable from TOML/JSON)."""

    name: str = "agent"
    description: str = ""
    instructions: str = ""
    tools: list[str] = Field(default_factory=lambda: ["*"])
    collections: list[str] = Field(default_factory=list)
    planner_tier: Tier = Tier.BALANCED
    critic_tier: Tier = Tier.BALANCED
    criteria: str | None = None
    max_replans: int = Field(default=1, ge=0, le=5)
    use_memory: bool = True
    isolate_untrusted_context: bool = True
    budget: dict[str, Any] = Field(default_factory=dict)


@dataclass
class AgentResult:
    status: str
    output: Any
    run_ids: list[str]
    label: dict[str, Any]
    verdict: Verdict | None = None
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)
    error: dict[str, Any] | None = None
    planning: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, Any] = field(default_factory=dict)

    @property
    def run_id(self) -> str:
        return self.run_ids[-1]

    @property
    def ok(self) -> bool:
        return self.status == "completed"

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["verdict"] = self.verdict.to_dict() if self.verdict else None
        return data


class Agent:
    def __init__(self, spec: AgentSpec, runtime: Runtime, memory: Any | None = None) -> None:
        self.spec = spec
        self.runtime = runtime
        self.memory = memory if spec.use_memory else None
        services = runtime.services
        self.planner = Planner(services.router, services.tools, tier=spec.planner_tier)
        self.critic = Critic(services.router, spec.critic_tier)

    async def _memory_sections(self, goal: str) -> list[tuple[str, str, int, Label]]:
        sections: list[tuple[str, str, int, Label]] = []
        if self.memory is None:
            return sections
        find = getattr(self.memory, "find_procedures", None)
        if find is not None:
            for proc in (await find(goal, 2)) or []:
                label = Label.from_dict(proc.get("label"))
                plan_json = proc.get("plan")
                rendered = plan_json if isinstance(plan_json, str) else plan_to_json_safe(plan_json)
                text = f"Goal: {proc.get('goal')}\nPlan: {rendered}"
                sections.append(("Similar plan that succeeded before", text, 40, label))
        try:
            facts = await self.memory.recall(goal, "semantic", 5)
        except Exception:  # memory is advisory; never block planning on it
            log.exception("memory recall failed")
            facts = []
        for item in facts:
            sections.append(("Relevant memory", str(item.get("text", "")), 30,
                             Label.from_dict(item.get("label"))))
        return sections

    async def plan(
        self, goal: str, *, feedback: str | None = None, base_label: Label = USER,
        grants: list[str] | None = None,
    ) -> PlanningResult:
        budget = Budget.from_dict(self.spec.budget)
        return await self.planner.plan(
            goal,
            grants=grants or self.spec.tools,
            instructions=self.spec.instructions or None,
            feedback=feedback,
            extra_sections=await self._memory_sections(goal),
            isolate_untrusted=self.spec.isolate_untrusted_context,
            collections=self.spec.collections or sorted(self.runtime.services.corpora),
            base_label=base_label,
            max_nodes=budget.max_nodes,
        )

    async def run(
        self,
        goal: str,
        *,
        inputs: dict[str, Any] | None = None,
        label: Label = USER,
        run_id: str | None = None,
        parent_run_id: str | None = None,
        depth: int = 0,
        budget: Budget | None = None,
        grants: list[str] | None = None,
    ) -> AgentResult:
        budget = budget or Budget.from_dict(self.spec.budget)
        grants = grants or self.spec.tools
        run_ids: list[str] = []
        planning: list[dict[str, Any]] = []
        feedback: str | None = None
        last: RunResult | None = None
        verdict: Verdict | None = None
        for attempt in range(self.spec.max_replans + 1):
            try:
                planned = await self.plan(goal, feedback=feedback, base_label=label, grants=grants)
            except PlanValidationError as exc:
                return AgentResult("failed", None, run_ids, label.to_dict(),
                                   error=exc.to_dict(), planning=planning)
            planning.append(planned.to_meta())
            rid = await self.runtime.create_run(
                planned.plan,
                inputs=inputs,
                budget=budget,
                grants=grants,
                label=planned.label,
                agent=self.spec.name,
                parent_run_id=parent_run_id,
                depth=depth,
                run_id=run_id if attempt == 0 else None,
                tags=[f"attempt:{attempt + 1}"],
                extra={"planning": planned.to_meta(), "agent_attempt": attempt + 1},
            )
            run_ids.append(rid)
            last = await self.runtime.execute(rid)
            outcome = await self._after_run(goal, last, planned)
            if outcome is not None:
                result, verdict = outcome
                if result is not None:
                    result.run_ids = run_ids
                    result.planning = planning
                    return result
            feedback = self._feedback(last, verdict)
            log.info("agent %s replanning after attempt %d: %s", self.spec.name, attempt + 1, feedback)
        assert last is not None
        return AgentResult(
            "failed" if last.status != "completed" else "rejected",
            last.output, run_ids, last.label, verdict, error=last.error, planning=planning,
            usage=last.usage,
        )

    async def resume(self, run_id: str) -> AgentResult:
        result = await self.runtime.execute(run_id)
        state = await self.runtime.load(run_id)
        planned = PlanningResult(state.plan, state.label, 0) if state.plan else None
        outcome = await self._after_run(state.goal, result, planned)
        if outcome is not None and outcome[0] is not None:
            outcome[0].run_ids = [run_id]
            return outcome[0]
        return AgentResult(result.status, result.output, [run_id], result.label,
                           outcome[1] if outcome else None, result.pending_approvals,
                           result.error, usage=result.usage)

    async def _after_run(
        self, goal: str, result: RunResult, planned: PlanningResult | None
    ) -> tuple[AgentResult | None, Verdict | None] | None:
        """Return (final result or None to replan, verdict)."""
        if result.status == "suspended":
            return AgentResult("suspended", None, [result.run_id], result.label,
                               pending_approvals=result.pending_approvals, usage=result.usage), None
        if result.status != "completed":
            await self._record_episode(goal, result, None)
            return None, None
        verdict = None
        if self.spec.criteria:
            verdict = await self.critic.evaluate(goal, result.output, self.spec.criteria,
                                                 trusted=result.trusted)
            if not verdict.passed:
                await self._record_episode(goal, result, verdict)
                return None, verdict
        await self._record_episode(goal, result, verdict)
        if planned is not None:
            await self._save_procedure(goal, planned)
        return AgentResult("completed", result.output, [result.run_id], result.label, verdict,
                           usage=result.usage), verdict

    def _feedback(self, result: RunResult, verdict: Verdict | None) -> str:
        if verdict is not None and not verdict.passed:
            return "The result was rejected by review: " + "; ".join(verdict.issues)
        if result.error:
            node = result.error.get("node_id")
            return f"Execution failed at node '{node}': {result.error.get('message')}"
        return f"The run ended with status {result.status}."

    async def _record_episode(self, goal: str, result: RunResult, verdict: Verdict | None) -> None:
        record = getattr(self.memory, "record_episode", None)
        if record is None:
            return
        outcome = (
            f"status={result.status}; output={str(result.output)[:300]}"
            + (f"; review issues: {'; '.join(verdict.issues)}" if verdict and verdict.issues else "")
        )
        try:
            status = "succeeded" if result.status == "completed" and not (verdict and not verdict.passed) else "failed"
            await record(goal=goal, outcome=outcome, status=status, run_id=result.run_id,
                         label=Label.from_dict(result.label))
        except Exception:
            log.exception("failed to record episode")

    async def _save_procedure(self, goal: str, planned: PlanningResult) -> None:
        save = getattr(self.memory, "save_procedure", None)
        if save is None or not planned.label.trusted:
            return  # plans influenced by untrusted context never become procedures
        try:
            await save(goal=goal, plan_json=plan_to_json(planned.plan), label=planned.label)
        except CairnError as exc:
            log.info("procedure not saved: %s", exc.message)


def plan_to_json_safe(value: Any) -> str:
    import json

    try:
        return json.dumps(value)[:2000]
    except (TypeError, ValueError):
        return str(value)[:2000]


class AgentSubagentRunner:
    """Runs ``agent`` plan nodes as child runs with attenuated grants.

    Child run ids are derived deterministically from (parent run, node,
    attempt), so if the process crashes while a child is running, resuming the
    parent resumes the same child instead of starting a duplicate.
    """

    def __init__(self, runtime: Runtime, memory: Any | None = None, base: AgentSpec | None = None) -> None:
        self.runtime = runtime
        self.memory = memory
        self.base = base or AgentSpec(name="subagent", max_replans=0)

    async def __call__(self, request: SubagentRequest) -> RunResult:
        try:
            await self.runtime.journal.get_run(request.run_id)
        except NotFound:
            pass
        else:
            return await self.runtime.execute(request.run_id)
        spec = self.base.model_copy(update={
            "name": f"{self.base.name}@d{request.depth}",
            "instructions": request.instructions or self.base.instructions,
            "tools": request.grants,
            "planner_tier": Tier(request.tier),
            "max_replans": 0,
        })
        agent = Agent(spec, self.runtime, self.memory)
        planned = await agent.plan(request.goal, base_label=request.label, grants=request.grants)
        await self.runtime.create_run(
            planned.plan,
            budget=Budget.from_dict(request.budget),
            grants=request.grants,
            label=planned.label,
            agent=spec.name,
            parent_run_id=request.parent_run_id,
            depth=request.depth,
            run_id=request.run_id,
            extra={"planning": planned.to_meta()},
        )
        return await self.runtime.execute(request.run_id)
