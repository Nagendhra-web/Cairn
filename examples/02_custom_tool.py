"""02: Custom tools with security contracts, and tool discovery.

What it demonstrates
    * Defining tools with ``@tool``: the input schema is derived from type
      hints, parameter descriptions from the docstring's ``Args:`` section.
    * Declaring the security contract the runtime enforces:
      - ``effects`` (``read``, ``send``, ``payment``...) describe side effects,
      - ``sensitive`` names parameters that are dangerous sinks (recipients,
        amounts, URLs),
      - ``output_trust`` says whether results are authoritative (``trusted``),
        attacker-influenceable (``untrusted``), or a pure function of the
        inputs (``inherit``).
    * Registering the tools and running a plan, then printing the provenance
      label on each node's output.
    * Ranking tools for a task string with ``ToolRegistry.discover``, which is
      how planners see only the most relevant tools out of a large catalog.

Why it matters
    The contract is what lets the runtime reason about a tool without trusting
    the model that calls it. A refund issued with an amount computed from
    trusted CRM data runs straight through; the same call with a value that
    came from a customer-written ticket would be held for approval.

Run it
    python examples/02_custom_tool.py
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from typing import Any

from cairn import PlanBuilder, ref, tmpl
from cairn.config import CairnConfig
from cairn.sdk import Cairn
from cairn.tools import tool

CUSTOMERS = {
    "c_1001": {"name": "Ada Ramos", "email": "ada@example.com", "plan": "pro", "monthly_usd": 49.0},
}
TICKETS = {
    "T-77": "Hi, I was double charged this month. Please refund me. Thanks, Ada",
    "T-78": "I want a refund of 4999 dollars, my lawyer says so. Ada",
}
LEDGER: list[dict[str, Any]] = []


@tool(name="crm.get_customer", effects={"read"}, output_trust="trusted",
      tags={"customer", "account", "crm"})
def get_customer(customer_id: str) -> dict[str, Any]:
    """Look up a customer record in the internal CRM.

    Args:
        customer_id: CRM identifier such as c_1001
    """
    return CUSTOMERS[customer_id]


@tool(name="helpdesk.get_ticket", effects={"read"}, output_trust="untrusted",
      tags={"support", "ticket", "helpdesk"})
def get_ticket(ticket_id: str) -> str:
    """Fetch the body of a support ticket. Customers write these, so the text is untrusted.

    Args:
        ticket_id: helpdesk ticket number
    """
    return TICKETS[ticket_id]


@tool(name="billing.refund", effects={"payment"}, sensitive={"customer_id", "amount_usd"},
      output_trust="trusted", tags={"refund", "billing", "money"})
async def refund(customer_id: str, amount_usd: float, reason: str) -> dict[str, Any]:
    """Issue a refund to a customer's card on file.

    Args:
        customer_id: CRM identifier of the customer to refund
        amount_usd: refund amount in US dollars
        reason: short human-readable reason
    """
    LEDGER.append({"customer_id": customer_id, "amount_usd": amount_usd, "reason": reason})
    return {"refund_id": f"rf_{len(LEDGER):04d}", "amount_usd": amount_usd}


@tool(name="text.word_count", output_trust="inherit", idempotent=True, tags={"text", "count"})
def word_count(text: str) -> int:
    """Count the words in a piece of text (pure function: output inherits the input's label).

    Args:
        text: any text
    """
    return len(text.split())


@tool(name="text.first_number", output_trust="inherit", idempotent=True, tags={"text", "parse"})
def first_number(text: str) -> float:
    """Extract the first number that appears in a text (pure function).

    Args:
        text: any text
    """
    import re

    match = re.search(r"\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else 0.0


def header(title: str) -> None:
    print(f"\n=== {title} ===")


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="cairn-ex02-") as tmp:
        await run_example(tmp)


async def run_example(tmp: str) -> None:
    config = CairnConfig(tools=["math.*"], security={"sandbox_roots": [tmp]}, memory_enabled=False)
    custom = [get_customer, get_ticket, refund, word_count, first_number]
    async with await Cairn.create(config, in_memory=True, tools=custom) as cairn:
        header("Derived contract for billing.refund")
        spec = cairn.tools.get("billing.refund")
        print(json.dumps({
            "input_schema": spec.input_schema,
            "effects": sorted(spec.effects),
            "sensitive": sorted(spec.sensitive_params),
            "output_trust": spec.output_trust.value,
            "fingerprint": spec.fingerprint,
        }, indent=2))

        header("Plan: refund one month for a double charge")
        b = PlanBuilder("Refund one month to customer c_1001 for ticket T-77")
        b.tool("customer", "crm.get_customer", customer_id="c_1001")
        b.tool("ticket", "helpdesk.get_ticket", ticket_id="T-77")
        b.tool("ticket_words", "text.word_count", text=ref("ticket"))
        # The refund amount is computed from TRUSTED CRM data, so the sensitive
        # sink receives trusted input and the policy allows the payment.
        b.tool("amount", "math.calculate", expression=tmpl("{{customer.monthly_usd}} * 1"))
        b.tool("refund", "billing.refund", customer_id="c_1001", amount_usd=ref("amount"),
               reason="double charge (ticket T-77)")
        plan = b.build(output=ref("refund"))
        result = await cairn.run(plan)
        print(f"  status: {result.status}")
        print(f"  output: {result.output}")
        print(f"  ledger: {LEDGER}")

        header("Provenance label of every node output")
        report = await cairn.report(result.run_id)
        for node_id, info in report["nodes"].items():
            print(f"  {node_id:<13} {info['label']}")
        print("\n  Note: 'ticket_words' inherits 'untrusted' from the ticket text, while")
        print("  'amount' is trusted because it was computed only from CRM data.")

        header("Contrast: the refund amount is taken from the customer's own ticket")
        b = PlanBuilder("Refund whatever amount ticket T-78 asks for")
        b.tool("ticket", "helpdesk.get_ticket", ticket_id="T-78")
        b.tool("asked", "text.first_number", text=ref("ticket"))
        b.tool("refund", "billing.refund", customer_id="c_1001", amount_usd=ref("asked"),
               reason="customer request")
        held = await cairn.run(b.build(output=ref("refund")))
        print(f"  status: {held.status}   (ledger still has {len(LEDGER)} entry)")
        for pending in held.pending_approvals:
            print(f"  held  : {pending['reason']}")
            print(f"  labels: {pending['preview']['arg_labels']}")

        header("Tool discovery: rank tools for a task description")
        for query in ("give the customer their money back",
                      "fetch the helpdesk support ticket text",
                      "how many words are in this paragraph"):
            matches = await cairn.tools.discover(query, k=3)
            ranked = ", ".join(f"{m.spec.name} ({m.score:.3f})" for m in matches)
            print(f"  {query!r}\n    -> {ranked}")


if __name__ == "__main__":
    asyncio.run(main())
