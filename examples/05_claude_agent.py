"""05: A real agent on Claude (requires ANTHROPIC_API_KEY).

What it demonstrates
    * ``Cairn.create`` with automatic model configuration: when
      ``ANTHROPIC_API_KEY`` is set, Claude models are registered for the
      ``fast``, ``balanced`` and ``frontier`` tiers with their prices.
    * Loading a declarative agent definition from TOML
      (``examples/agents/analyst.toml``) into an ``AgentSpec``: instructions,
      tool grants, tiers, acceptance criteria and a budget.
    * The agent loop: the planner turns a goal into a validated plan, the
      runtime executes it durably with provenance labels, the critic grades
      the result against the criteria, and the agent replans on failure.
    * File tools confined to a sandboxed workspace (a temporary directory
      seeded with two CSV files).

Why it matters
    The model never executes anything directly. It proposes a plan (data),
    the runtime validates it against the agent's grants and the policy, and
    the whole run is journaled, so you can inspect, replay or fork it later
    with ``cairn show`` / ``cairn replay`` / ``cairn fork``.

Run it
    export ANTHROPIC_API_KEY=sk-ant-...
    python examples/05_claude_agent.py

    If the plan writes file-derived text to disk, the policy asks for approval
    on the terminal (pass --yes to approve automatically; without a terminal
    the action is rejected). Without the key the script prints how to set it
    up and exits with status 0.
    Cost: a few cents at most (the spec caps it at $0.50).
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import tomllib
from pathlib import Path

from cairn.agents import AgentSpec
from cairn.config import CairnConfig, auto_models
from cairn.core.errors import CairnError
from cairn.sdk import Cairn

HERE = Path(__file__).resolve().parent
SPEC_FILE = HERE / "agents" / "analyst.toml"

SALES_Q1 = """region,month,revenue_usd
north,jan,12000
north,feb,13500
north,mar,11800
south,jan,9800
south,feb,10400
south,mar,12100
"""
SALES_Q2 = """region,month,revenue_usd
north,apr,14100
north,may,12900
north,jun,15200
south,apr,11000
south,may,11700
south,jun,13050
"""


async def ask(prompt: str) -> bool:
    """Ask on the terminal. ``--yes`` approves everything; without a terminal, reject."""
    if "--yes" in sys.argv:
        print(prompt + "y (--yes)")
        return True
    if not sys.stdin.isatty():
        print(prompt + "n (stdin is not a terminal; rejecting)")
        return False
    answer = await asyncio.to_thread(input, prompt)
    return answer.strip().lower() in ("y", "yes")


def header(title: str) -> None:
    print(f"\n=== {title} ===")


async def main() -> int:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("This example calls Claude and needs an Anthropic API key.")
        print("  export ANTHROPIC_API_KEY=sk-ant-...   # https://console.anthropic.com/")
        print("  python examples/05_claude_agent.py")
        print("No key found, so nothing was run. Examples 01-04 and 07-10 run fully offline.")
        return 0
    try:
        import anthropic  # noqa: F401
    except ImportError:
        print("The Anthropic provider needs the SDK: pip install 'cairn-runtime[anthropic]'")
        return 0

    spec = AgentSpec.model_validate(tomllib.loads(SPEC_FILE.read_text(encoding="utf-8")))
    with tempfile.TemporaryDirectory(prefix="cairn-ex05-") as tmp:
        workspace = Path(tmp) / "workspace"
        (workspace / "data").mkdir(parents=True)
        (workspace / "data" / "sales_q1.csv").write_text(SALES_Q1, encoding="utf-8")
        (workspace / "data" / "sales_q2.csv").write_text(SALES_Q2, encoding="utf-8")

        config = CairnConfig(
            data_dir=str(Path(tmp) / "data"),
            models=auto_models(),
            tools=spec.tools,
            security={"sandbox_roots": [str(workspace)]},
        )
        async with await Cairn.create(config) as cairn:
            header("Models (auto-configured from ANTHROPIC_API_KEY)")
            for m in cairn.router.models:
                print(f"  {m.tier.value:<9} {m.name}")

            header(f"Agent spec from {SPEC_FILE.relative_to(HERE.parent)}")
            print(f"  name={spec.name} tools={spec.tools} criteria={spec.criteria!r}")

            goal = ("Read the quarterly sales CSV files under data/, compute total revenue per "
                    "region for the first half of the year, and write a short Markdown report "
                    "to reports/summary.md that names the top region.")
            header("Goal")
            print(f"  {goal}")

            try:
                result = await cairn.agent(spec).run(goal)
            except CairnError as exc:
                print(f"\nThe agent could not run: {exc.code}: {exc.message}")
                print(f"  details: {str(exc.details)[:400]}")
                print("  Check that ANTHROPIC_API_KEY is valid and that api.anthropic.com is reachable.")
                return 1

            agent = cairn.agent(spec)
            while result.status == "suspended":
                # Writing a report derived from file contents (untrusted) into a file is a
                # sensitive sink, so the policy asks a human first.
                header("Approval needed")
                for pending in result.pending_approvals:
                    preview = pending["preview"]
                    print(f"  {pending['node_id']}: {pending['reason']}")
                    for arg, value in (preview.get("args") or {}).items():
                        print(f"    {arg} = {str(value)[:100]!r}")
                        print(f"      label: {preview.get('arg_labels', {}).get(arg)}")
                    approved = await ask("  Approve this action? [y/N] ")
                    await cairn.runtime.decide(result.run_id, pending["request_id"],
                                               approved=approved, by="terminal")
                result = await agent.resume(result.run_id)

            header("Result")
            print(f"  status : {result.status}")
            print(f"  runs   : {result.run_ids}")
            if result.verdict is not None:
                print(f"  critic : passed={result.verdict.passed} issues={result.verdict.issues}")
            if result.error:
                print(f"  error  : {result.error.get('code')}: {result.error.get('message')}")
            for planned in result.planning:
                print(f"  planner attempts={planned['attempts']} repairs={len(planned['repairs'])}")

            report_file = workspace / "reports" / "summary.md"
            header("reports/summary.md")
            if report_file.exists():
                print(report_file.read_text(encoding="utf-8"))
            else:
                print("  (the agent did not write the report)")

            header("Run report")
            print(cairn.render(await cairn.report(result.run_id)))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
