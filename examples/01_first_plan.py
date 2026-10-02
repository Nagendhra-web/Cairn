"""01: Your first plan.

What it demonstrates
    * Building a plan in Python with ``PlanBuilder`` from built-in tools only
      (``math.calculate`` and ``fs.write``), wired together with ``ref`` and
      ``tmpl`` so values flow between nodes as data.
    * Creating a fully wired runtime with ``Cairn.create`` (in-memory journal,
      a temporary sandbox directory for file tools).
    * Running the plan and printing the rendered run report: node statuses,
      provenance labels, usage and the final output.

Why it matters
    A Cairn plan is data, not code. The runtime validates it before anything
    runs (unknown tools, bad references, cycles), journals every effect, and
    labels every value with where it came from. Independent nodes run in
    parallel automatically. No model or API key is involved here: plans are
    useful on their own for deterministic, auditable automation.

Run it
    python examples/01_first_plan.py
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from cairn import PlanBuilder, ref, tmpl
from cairn.config import CairnConfig
from cairn.sdk import Cairn


def header(title: str) -> None:
    print(f"\n=== {title} ===")


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="cairn-ex01-") as tmp:
        workspace = Path(tmp) / "workspace"
        config = CairnConfig(
            data_dir=str(Path(tmp) / "data"),
            tools=["math.*", "fs.*"],
            security={"sandbox_roots": [str(workspace)]},
        )
        async with await Cairn.create(config, in_memory=True) as cairn:
            header("Plan")
            b = PlanBuilder("Compare a 5% and a 7% savings plan over 10 years and save a note")
            # These two nodes have no dependencies on each other, so they run concurrently.
            b.tool("at_5", "math.calculate", expression="round(10000 * 1.05 ** 10, 2)")
            b.tool("at_7", "math.calculate", expression="round(10000 * 1.07 ** 10, 2)")
            # A ref to two earlier outputs makes this node depend on both.
            b.tool("gap", "math.calculate",
                   expression=tmpl("round({{at_7}} - {{at_5}}, 2)"))
            b.tool(
                "save", "fs.write",
                path="notes/savings.md",
                content=tmpl(
                    "# Savings comparison\n\n"
                    "- $10,000 at 5% for 10 years: ${{at_5}}\n"
                    "- $10,000 at 7% for 10 years: ${{at_7}}\n"
                    "- Difference: ${{gap}}\n"
                ),
            )
            plan = b.build(output={"five": ref("at_5"), "seven": ref("at_7"), "gap": ref("gap"),
                                   "file": ref("save.path")})
            for node in plan.nodes:
                print(f"  {node.id:<6} kind={node.kind:<5} tool={getattr(node, 'tool', '-')}")

            header("Run")
            result = await cairn.run(plan)
            print(f"  status : {result.status}")
            print(f"  output : {result.output}")
            print(f"  trusted: {result.trusted}  label={result.label}")

            header("File written inside the sandbox")
            saved = workspace / "notes" / "savings.md"
            print(saved.read_text(encoding="utf-8").rstrip())

            header("Run report")
            print(cairn.render(await cairn.report(result.run_id)))


if __name__ == "__main__":
    asyncio.run(main())
