"""09: Multi-agent supervision compiled to a plan.

What it demonstrates
    * Two specialist ``AgentSpec`` definitions with different tool grants:
      ``researcher`` (math only) and ``writer`` (file tools only).
    * A ``Supervisor`` asks a model to delegate sub-goals to the specialists
      by name, with explicit dependencies, then *compiles* the delegation
      into an ordinary plan of ``agent`` nodes plus a synthesis node.
    * Each ``agent`` node runs as a durable child run (via
      ``AgentSubagentRunner``, wired by ``Cairn.create``): the child plans
      its own sub-goal with only its own grants, and its output flows to the
      dependent task as labeled data.
    * The compiled plan and every child run (plan, labels, usage) are
      printed. Everything is scripted, so no API key is needed.

Why it matters
    Instead of agents chatting in an open loop, delegation becomes a
    dependency graph you can validate, run in parallel, resume after a crash,
    replay and audit. A specialist can never use a tool outside its grants.

Run it (no API key needed)
    python examples/09_supervisor.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from cairn.agents import AgentSpec, plan_to_json
from cairn.config import CairnConfig
from cairn.models import ModelInfo, ScriptedProvider, Tier
from cairn.sdk import Cairn

GOAL = ("Estimate the 2027 market size for smart bike locks in our three launch cities "
        "and produce a short brief saved as briefs/bike_locks.md")

DELEGATION = {
    "tasks": [
        {"id": "research", "agent": "researcher",
         "goal": "Estimate the 2027 market size: 120000 + 85000 + 64000 cyclists, 6% adoption, "
                 "at 89 USD per lock."},
        {"id": "brief", "agent": "writer", "depends_on": ["research"],
         "goal": "Write a three-sentence brief about the estimate and save it as "
                 "briefs/bike_locks.md."},
    ],
    "synthesis": "Report the estimate and where the brief was saved.",
}

RESEARCH_PLAN = {
    "goal": "market size",
    "nodes": [
        {"id": "cyclists", "kind": "tool", "tool": "math.calculate",
         "args": {"expression": "120000 + 85000 + 64000"}},
        {"id": "buyers", "kind": "tool", "tool": "math.calculate",
         "args": {"expression": {"$tmpl": "{{cyclists}} * 0.06"}}},
        {"id": "revenue", "kind": "tool", "tool": "math.calculate",
         "args": {"expression": {"$tmpl": "round({{buyers}} * 89)"}}},
    ],
    "output": {"cyclists": {"$ref": "cyclists"}, "buyers": {"$ref": "buyers"},
               "revenue_usd": {"$ref": "revenue"}},
}

WRITER_PLAN = {
    "goal": "brief",
    "nodes": [
        {"id": "draft", "kind": "llm", "tier": "fast",
         "prompt": "Write a three-sentence brief about this estimate: 269000 cyclists, "
                   "16140 buyers at 6% adoption, 1436460 USD at 89 USD per lock."},
        {"id": "save", "kind": "tool", "tool": "fs.write",
         "args": {"path": "briefs/bike_locks.md", "content": {"$ref": "draft"}}},
    ],
    "output": {"saved": {"$ref": "save.bytes"}, "brief": {"$ref": "draft"}},
}


def scripted_model() -> ScriptedProvider:
    model = ScriptedProvider()
    model.on("Split the goal into", DELEGATION)
    model.on("## Goal\nEstimate the 2027 market size", RESEARCH_PLAN)
    model.on("## Goal\nWrite a three-sentence brief", WRITER_PLAN)
    model.on("Write a three-sentence brief about this estimate",
             "Our three launch cities have 269,000 cyclists. At 6% adoption that is about "
             "16,140 buyers. At 89 USD per lock the 2027 market is roughly 1.44M USD.")
    model.on("Specialist results:",
             "Estimated 2027 market: about 1.44M USD (16,140 locks). Brief saved to "
             "briefs/bike_locks.md.")
    return model


def header(title: str) -> None:
    print(f"\n=== {title} ===")


async def main() -> None:
    researcher = AgentSpec(name="researcher", description="Quantitative estimates with a calculator",
                           tools=["math.*"], planner_tier=Tier.BALANCED)
    writer = AgentSpec(name="writer", description="Writes short briefs and saves them as files",
                       tools=["fs.write"], planner_tier=Tier.BALANCED)
    with tempfile.TemporaryDirectory(prefix="cairn-ex09-") as tmp:
        workspace = Path(tmp) / "workspace"
        config = CairnConfig(tools=["math.*", "fs.*"], memory_enabled=False,
                             security={"sandbox_roots": [str(workspace)]})
        info = [(scripted_model(), ModelInfo(name=f"scripted-{t.value}", provider="scripted",
                                             tier=t, input_price_per_mtok=0.0,
                                             output_price_per_mtok=0.0))
                for t in (Tier.FAST, Tier.BALANCED)]
        async with await Cairn.create(config, in_memory=True, providers=info) as cairn:
            supervisor = cairn.supervisor([researcher, writer])
            result = await supervisor.run(GOAL)

            header("Delegation chosen by the supervisor model")
            for task in result.delegation.tasks:
                deps = f" (after {', '.join(task.depends_on)})" if task.depends_on else ""
                print(f"  {task.id} -> {task.agent}{deps}: {task.goal[:70]}...")

            header("Compiled plan (ordinary Plan IR)")
            print(plan_to_json(result.plan))

            header("Supervisor run")
            run = result.run
            print(f"  status: {run.status}")
            print(f"  nodes : {run.nodes}")
            print(f"  output: {run.output}")
            print(f"  label : {run.label['integrity']} from {run.label['sources']}")

            header("Child runs (one durable run per agent node, with attenuated grants)")
            state = await cairn.runtime.load(run.run_id)
            for child_id in state.children:
                child = await cairn.runtime.load(child_id)
                tools = sorted({n.tool for n in child.plan.nodes if n.kind == "tool"})
                print(f"  {child_id}  runner={child.agent}  status={child.status}  "
                      f"grants={child.grants}  tools used={tools}")
                print(f"    output: {json.dumps(child.output.value if child.output else None)[:160]}")

            header("File written by the writer specialist")
            print((workspace / "briefs" / "bike_locks.md").read_text(encoding="utf-8"))

            header("Supervisor run report")
            print(cairn.render(await cairn.report(run.run_id)))


if __name__ == "__main__":
    asyncio.run(main())
