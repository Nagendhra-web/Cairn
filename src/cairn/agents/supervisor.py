"""Supervisor: hierarchical multi-agent orchestration compiled to Plan IR.

Instead of a chat room where agents message each other indefinitely, the
supervisor asks a model to *assign* sub-goals to named specialist agents with
explicit dependencies, then compiles that assignment into an ordinary plan of
``agent`` nodes plus a final synthesis node. Consequences:

* specialists run in parallel where independent, sequentially where not;
* each specialist runs as a durable child run with only its own tool grants;
* the whole hierarchy is resumable, replayable and inspectable like any run;
* agent-to-agent communication is explicit data flow (``$ref`` between nodes),
  with provenance labels, rather than free-form messages.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from cairn.agents.agent import AgentSpec
from cairn.core.errors import PlanValidationError
from cairn.models.structured import extract_json
from cairn.models.types import Message, ModelRequest, Tier
from cairn.provenance.labels import USER
from cairn.runtime.budget import Budget
from cairn.runtime.engine import RunResult, Runtime
from cairn.runtime.plan import AgentNode, LLMNode, Plan, ref


class Assignment(BaseModel):
    id: str
    agent: str
    goal: str
    depends_on: list[str] = Field(default_factory=list)


class Delegation(BaseModel):
    tasks: list[Assignment]
    synthesis: str = "Combine the specialists' results into a final answer to the goal."


DELEGATION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "tasks": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string"}, "agent": {"type": "string"}, "goal": {"type": "string"},
            "depends_on": {"type": "array", "items": {"type": "string"}}},
            "required": ["id", "agent", "goal"]}},
        "synthesis": {"type": "string"},
    },
    "required": ["tasks"],
}


@dataclass
class SupervisorResult:
    run: RunResult
    delegation: Delegation
    plan: Plan


class Supervisor:
    def __init__(
        self,
        runtime: Runtime,
        specialists: list[AgentSpec],
        *,
        tier: Tier = Tier.BALANCED,
        max_tasks: int = 8,
    ) -> None:
        if not specialists:
            raise ValueError("a supervisor needs at least one specialist")
        self.runtime = runtime
        self.specialists = {s.name: s for s in specialists}
        self.tier = tier
        self.max_tasks = max_tasks

    async def delegate(self, goal: str, feedback: str | None = None) -> Delegation:
        roster = "\n".join(
            f"- {s.name}: {s.description or s.instructions[:200]} (tools: {', '.join(s.tools)})"
            for s in self.specialists.values()
        )
        prompt = (
            f"Goal:\n{goal}\n\nSpecialists:\n{roster}\n\n"
            f"Split the goal into at most {self.max_tasks} self-contained tasks, each assigned to "
            "one specialist by exact name. Use depends_on (task ids) only when a task needs "
            "another task's result; independent tasks run in parallel. Return JSON: "
            '{"tasks": [{"id", "agent", "goal", "depends_on"}], "synthesis": instructions}'
        )
        if feedback:
            prompt += f"\n\nThe previous delegation was invalid: {feedback}"
        response, _ = await self.runtime.services.router.complete(
            ModelRequest(messages=[Message(role="user", content=prompt)],
                         response_schema=DELEGATION_SCHEMA, max_tokens=2000),
            tier=self.tier,
        )
        return Delegation.model_validate(extract_json(response.text))

    def compile(self, goal: str, delegation: Delegation) -> Plan:
        problems = []
        ids = {t.id for t in delegation.tasks}
        for task in delegation.tasks:
            if task.id == "synthesis":
                problems.append("task id 'synthesis' is reserved for the final synthesis node")
            if task.agent not in self.specialists:
                problems.append(f"task '{task.id}' assigned to unknown agent '{task.agent}'")
            for dep in task.depends_on:
                if dep not in ids:
                    problems.append(f"task '{task.id}' depends on unknown task '{dep}'")
        if len(delegation.tasks) > self.max_tasks:
            problems.append(f"{len(delegation.tasks)} tasks exceed the limit of {self.max_tasks}")
        if problems:
            raise PlanValidationError("delegation is invalid", problems)
        nodes: list[Any] = []
        for task in delegation.tasks:
            spec = self.specialists[task.agent]
            context = "".join(f"\n\nResult of task {d}:\n{{{{{d}}}}}" for d in task.depends_on)
            nodes.append(AgentNode(
                id=task.id,
                goal={"$tmpl": task.goal + context} if context else task.goal,
                tools=spec.tools,
                tier=spec.planner_tier,
                instructions=spec.instructions or None,
                deps=list(task.depends_on),
                description=f"delegated to {spec.name}",
            ))
        results = "\n\n".join(f"### {t.id} ({t.agent})\n{{{{{t.id}}}}}" for t in delegation.tasks)
        nodes.append(LLMNode(
            id="synthesis",
            prompt=f"Goal: {goal}\n\n{delegation.synthesis}\n\nSpecialist results:\n{results}",
            tier=self.tier,
        ))
        return Plan(goal=goal, nodes=nodes, output=ref("synthesis"),
                    metadata={"supervisor": True, "specialists": sorted(self.specialists)})

    async def run(self, goal: str, *, budget: Budget | None = None, retries: int = 1) -> SupervisorResult:
        feedback = None
        for _ in range(retries + 1):
            delegation = await self.delegate(goal, feedback)
            try:
                plan = self.compile(goal, delegation)
            except PlanValidationError as exc:
                feedback = "; ".join(exc.problems)
                continue
            grants = sorted({g for s in self.specialists.values() for g in s.tools})
            result = await self.runtime.run(plan, budget=budget, grants=grants, label=USER,
                                            agent="supervisor")
            return SupervisorResult(result, delegation, plan)
        raise PlanValidationError("supervisor could not produce a valid delegation", [feedback or ""])
