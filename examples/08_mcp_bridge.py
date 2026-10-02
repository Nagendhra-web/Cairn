"""08: Using MCP servers safely: mounting, labels, pinning and rug-pull quarantine.

What it demonstrates
    * Starting an MCP server (``examples/mcp_server.py``, itself built from
      Cairn tools with ``run_stdio_server``) as a subprocess and mounting its
      tools into a ``ToolRegistry`` with ``mount_mcp_server`` and
      ``MCPServerConfig``. Tools are namespaced (``helpdesk.forecast``).
    * Least privilege on both sides: the server withholds its privileged
      ``save_note`` tool, and the child process gets a minimal environment.
    * Calling the mounted tools from an ordinary plan. Outputs from an
      untrusted server are labeled ``untrusted``, so they cannot steer
      privileged actions without approval.
    * Pin files: after review, the tools' fingerprints are saved. When the
      server later changes a tool's definition (a "rug pull", simulated with
      ``EXAMPLE_MCP_RUGPULL=1``), that tool is quarantined: plans that use it
      are rejected before anything runs, while unchanged tools keep working.
      An operator re-approves explicitly, which updates the pin.

Why it matters
    MCP servers choose the tool names, descriptions and outputs that reach
    your planner. Treating them as untrusted by default, and noticing when
    they change, closes the tool-poisoning and rug-pull attack paths.

Run it (no API key needed; starts a local subprocess)
    python examples/08_mcp_bridge.py
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from cairn import Plan, PlanBuilder, ref
from cairn.core.errors import CairnError
from cairn.mcp import (
    MCPServerConfig,
    approve,
    load_pins,
    mount_mcp_server,
    pin_report,
    save_pins,
    unmount_mcp_server,
)
from cairn.runtime import Runtime, Services
from cairn.tools import ToolRegistry

SERVER_SCRIPT = Path(__file__).resolve().parent / "mcp_server.py"


def server_config(**overrides: object) -> MCPServerConfig:
    base: dict[str, object] = {
        "name": "helpdesk",
        "command": sys.executable,
        "args": [str(SERVER_SCRIPT)],
        "trust": "untrusted",
        "startup_timeout_s": 30.0,
    }
    base.update(overrides)
    return MCPServerConfig.model_validate(base)


def kb_plan() -> Plan:
    b = PlanBuilder("Search the knowledge base")
    b.tool("article", "helpdesk.kb_search", query="printer driver")
    return b.build(output=ref("article"))


def header(title: str) -> None:
    print(f"\n=== {title} ===")


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="cairn-ex08-") as tmp:
        pin_file = Path(tmp) / "mcp-pins.json"

        header("1. Mount the server")
        registry = ToolRegistry()
        report = await mount_mcp_server(registry, server_config())
        try:
            print(f"  mounted : {report.added}")
            print("  (save_note is not listed: the server withholds privileged tools)")
            for name in report.added:
                spec = registry.get(name)
                print(f"  {name:<20} effects={sorted(spec.effects)} "
                      f"output_trust={spec.output_trust.value} fp={spec.fingerprint}")

            header("2. Call MCP tools from a plan")
            runtime = Runtime(Services(tools=registry))
            b = PlanBuilder("Check the weather and the VPN article")
            b.tool("weather", "helpdesk.forecast", city="Lisbon")
            b.tool("article", "helpdesk.kb_search", query="how do I reset my vpn")
            result = await runtime.run(b.build(output={"weather": ref("weather"),
                                                       "article": ref("article.text")}))
            print(f"  status: {result.status}")
            print(f"  output: {result.output}")
            state = await runtime.load(result.run_id)
            for node_id, node in state.nodes.items():
                label = node.output.label.describe() if node.output else None
                print(f"  {node_id:<8} label: {label}")

            header("3. Review and pin the tool definitions")
            pins = pin_report(pin_file, report)
            print(f"  wrote {pin_file.name}: {pins}")
        finally:
            await unmount_mcp_server(registry, report)

        header("4. Later: the server silently changes a tool (rug pull)")
        registry = ToolRegistry()
        pulled = await mount_mcp_server(registry, server_config(
            pinned=load_pins(pin_file), env={"EXAMPLE_MCP_RUGPULL": "1"}))
        try:
            for held in pulled.quarantined:
                print(f"  quarantined: {held.name}")
                print(f"    pinned  {held.pinned}")
                print(f"    current {held.current}")
            for name, signals in pulled.signals.items():
                print(f"  injection signals in {name}'s new description: "
                      f"{sorted({s.kind for s in signals})}")
            print(f"  still usable: {pulled.usable}")

            runtime = Runtime(Services(tools=registry))
            try:
                await runtime.run(kb_plan())
            except CairnError as exc:
                problems = exc.details.get("problems") or [exc.message]
                print(f"  plan rejected before execution: {problems[0]}")
                print(f"  registry says: {registry.quarantined()['helpdesk.kb_search']}")
            b = PlanBuilder("Weather still works")
            b.tool("weather", "helpdesk.forecast", city="Oslo")
            ok = await runtime.run(b.build(output=ref("weather")))
            print(f"  unchanged tool still runs: {ok.status}, output={ok.output!r}")

            header("5. Operator reviews the change and re-approves")
            new_pin = approve(registry, pulled, "helpdesk.kb_search")
            save_pins(pin_file, {**load_pins(pin_file), "helpdesk.kb_search": new_pin})
            print(f"  new pin for helpdesk.kb_search: {new_pin}")
            again = await runtime.run(kb_plan())
            print(f"  after approval: {again.status}, output={again.output}")
        finally:
            await unmount_mcp_server(registry, pulled)


if __name__ == "__main__":
    asyncio.run(main())
