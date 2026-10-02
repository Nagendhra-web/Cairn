"""04: Durable execution: crash, resume, replay and fork.

Scenario
    An order-fulfilment run reserves stock, charges the customer, has a model
    write a confirmation, then prints a shipping label. The process dies
    while printing the label (simulated with a ``BaseException`` subclass,
    which, like a real ``SIGKILL`` or power loss, bypasses normal error
    handling).

What it demonstrates
    * A SQLite journal in a temporary directory: every event and every side
      effect's result is appended (hash-chained) before the run moves on.
    * A brand-new ``Runtime`` (as if after a process restart) resumes the run
      from the journal. Completed effects are not repeated: the customer is
      charged exactly once and the model is not called again.
    * ``replay`` re-executes the recorded run with zero live tool or model
      calls and checks that it reproduces the same output.
    * ``fork`` with a patched prompt: only the changed node and its
      dependents run live; the charge is reused from the journal.
    * The journal hash chain verifies, and the run report is printed.

Why it matters
    Agents that take real actions must not double-charge, double-send or
    double-book after a crash. Replay and fork turn production runs into
    reproducible test cases and cheap "what if" experiments.

Run it (no API key needed)
    python examples/04_durable_resume.py
"""

from __future__ import annotations

import asyncio
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from cairn import PlanBuilder, ref
from cairn.journal import SQLiteJournal, verify_chain
from cairn.models import ModelInfo, ModelRouter, ScriptedProvider, Tier
from cairn.observability import render_text, run_report
from cairn.runtime import Runtime, Services
from cairn.storage import SQLiteDatabase
from cairn.tools import ToolRegistry, tool

CALLS: Counter[str] = Counter()
CRASH = {"armed": True}


class SimulatedCrash(BaseException):
    """Stands in for the process dying (not an Exception, so nothing catches it)."""


def build_tools() -> ToolRegistry:
    """A fresh registry, as a restarted process would build it."""

    @tool(name="inventory.reserve", effects={"write"}, output_trust="trusted")
    def reserve(sku: str, qty: int) -> dict[str, Any]:
        """Reserve stock for an order."""
        CALLS["reserve"] += 1
        return {"sku": sku, "qty": qty, "reservation": "rsv_551"}

    @tool(name="billing.charge", effects={"payment"}, sensitive={"customer", "cents"},
          output_trust="trusted")
    def charge(customer: str, cents: int) -> dict[str, Any]:
        """Charge the customer's card. Must never happen twice for one order."""
        CALLS["charge"] += 1
        return {"customer": customer, "cents": cents, "charge_id": "ch_9001"}

    @tool(name="shipping.print_label", effects={"write"}, output_trust="trusted")
    def print_label(order: str, note: str) -> str:
        """Print a shipping label. The first call kills the process."""
        CALLS["label"] += 1
        if CRASH["armed"]:
            CRASH["armed"] = False
            raise SimulatedCrash("power cut while printing the label")
        return f"label printed for {order} ({len(note)} chars of notes)"

    registry = ToolRegistry()
    registry.register_all([reserve, charge, print_label])
    return registry


def build_runtime(db_path: Path, model: ScriptedProvider) -> Runtime:
    router = ModelRouter()
    router.register(model, ModelInfo(name="scripted-fast", provider="scripted", tier=Tier.FAST,
                                     input_price_per_mtok=0.0, output_price_per_mtok=0.0))
    journal = SQLiteJournal(SQLiteDatabase(db_path))
    return Runtime(Services(journal=journal, router=router, tools=build_tools()))


def header(title: str) -> None:
    print(f"\n=== {title} ===")


async def main() -> None:
    model = ScriptedProvider()
    model.on("Write a one-line order confirmation",
             "Order A-1001 confirmed: 2 x Trail Lamp, $59.00 charged.")
    model.on("Write a cheerful one-line order confirmation",
             "Woohoo! Your 2 Trail Lamps are on the way. $59.00 charged. Happy trails!")

    b = PlanBuilder("Fulfil order A-1001")
    b.tool("reserve", "inventory.reserve", sku="LAMP-TRAIL", qty=2)
    b.tool("charge", "billing.charge", customer="cus_ada", cents=5900, deps=["reserve"])
    b.llm("confirm", "Write a one-line order confirmation for {{reserve}} and {{charge}}",
          tier="fast")
    b.tool("label", "shipping.print_label", order="A-1001", note=ref("confirm"))
    plan = b.build(output={"confirmation": ref("confirm"), "label": ref("label")})

    with tempfile.TemporaryDirectory(prefix="cairn-ex04-") as tmp:
        db_path = Path(tmp) / "journal.db"

        header("1. First process: run until it crashes")
        rt1 = build_runtime(db_path, model)
        run_id = await rt1.create_run(plan)
        try:
            await rt1.execute(run_id)
        except SimulatedCrash as exc:
            print(f"  process died: {exc}")
        state = await rt1.load(run_id)
        print(f"  journaled status: {state.status}; nodes: "
              f"{ {k: v.status for k, v in state.nodes.items()} }")
        print(f"  side effects so far: {dict(CALLS)}; model calls: {len(model.requests)}")

        header("2. New process: resume from the SQLite journal")
        rt2 = build_runtime(db_path, model)  # new runtime, new DB connection, new tools
        model_calls_before = len(model.requests)
        result = await rt2.resume(run_id)
        print(f"  status: {result.status}")
        print(f"  side effects total: {dict(CALLS)}")
        print(f"  charge executed {CALLS['charge']} time(s); model calls during resume: "
              f"{len(model.requests) - model_calls_before}")
        print("  note: the label call that was in flight when the process died ran again.")
        print("  Completed effects are exactly-once; an interrupted one is retried, so tools")
        print("  that can be interrupted mid-call should be idempotent (e.g. keyed by order).")
        print(f"  output: {result.output}")

        header("3. Strict replay (zero live calls)")
        before = (dict(CALLS), len(model.requests))
        replay = await rt2.replay(run_id)
        print(f"  matched={replay.matched} output_equal={replay.output_equal} "
              f"effects served from journal={replay.effects_replayed}")
        print(f"  live calls during replay: tools={dict(CALLS) != before[0]} "
              f"model={len(model.requests) - before[1]}")

        header("4. Fork with a patched prompt")
        fork = await rt2.fork(run_id, patches={
            "confirm": {"prompt": "Write a cheerful one-line order confirmation for {{reserve}} "
                                  "and {{charge}}"},
        })
        print(f"  fork status: {fork.status}")
        print(f"  new confirmation: {fork.output['confirmation']!r}")
        print(f"  side effects total: {dict(CALLS)}  (reserve and charge were reused, "
              "only the label was re-printed with the new text)")

        header("5. Journal integrity")
        events = await rt2.journal.read(run_id)
        problems = verify_chain(events)
        print(f"  {len(events)} events, hash chain {problems or 'OK'}")

        header("6. Run report")
        print(render_text(run_report(run_id, events)))


if __name__ == "__main__":
    asyncio.run(main())
