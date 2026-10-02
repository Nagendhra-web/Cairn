"""06: Local models through any OpenAI-compatible server (Ollama, vLLM, LM Studio...).

What it demonstrates
    * Registering a local model in code with ``OpenAICompatibleProvider`` and
      ``ModelInfo`` (zero price, ``local=True``) and handing it to
      ``Cairn.create(providers=...)``. No API key, no data leaves the machine.
    * A hand-written plan that reads a file from the sandbox and asks the
      local model to summarize it (robust even with small models).
    * The full agent loop (planner, durable execution, critic) on the same
      local model. Small models sometimes produce invalid plans; the planner
      repairs them with validation feedback, and failures are reported
      cleanly instead of crashing.

Why it matters
    The runtime is model-agnostic: plans ask for a tier, the router picks a
    registered model. Swapping a hosted model for a local one is a
    configuration change, and provenance, journaling and policy behave the
    same.

Run it
    ollama serve &                 # or any OpenAI-compatible server
    ollama pull llama3.2
    python examples/06_local_ollama.py

    Settings (environment variables, all optional):
      OLLAMA_HOST          server root, default http://localhost:11434
      CAIRN_OLLAMA_MODEL   model name, default llama3.2

    If the server is not reachable, the script explains how to start one and
    exits with status 0.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

import httpx

from cairn import PlanBuilder, ref
from cairn.config import CairnConfig
from cairn.core.errors import CairnError
from cairn.models import ModelInfo, OpenAICompatibleProvider, Tier
from cairn.sdk import Cairn

HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434").rstrip("/")
if not HOST.startswith("http"):
    HOST = "http://" + HOST
BASE_URL = HOST + "/v1"
MODEL = os.environ.get("CAIRN_OLLAMA_MODEL", "llama3.2")

NOTES = """Incident 2026-09-14: checkout latency rose from 300 ms to 4 s for 25 minutes.
Root cause: a cache node was replaced and started cold; the database absorbed all reads.
Fix: warm caches before routing traffic; add an alert on cache hit ratio below 80%.
"""


def header(title: str) -> None:
    print(f"\n=== {title} ===")


async def server_models() -> list[str] | None:
    """Return model ids served at BASE_URL, or None if the server is unreachable."""
    try:
        async with httpx.AsyncClient(timeout=2.0) as client:
            resp = await client.get(BASE_URL + "/models")
            resp.raise_for_status()
            return [m.get("id", "") for m in resp.json().get("data", [])]
    except (httpx.HTTPError, ValueError):
        return None


async def main() -> int:
    models = await server_models()
    if models is None:
        print(f"No OpenAI-compatible server answered at {BASE_URL}/models.")
        print("Start one, for example with Ollama:")
        print("  ollama serve &")
        print(f"  ollama pull {MODEL}")
        print("  python examples/06_local_ollama.py")
        print("Set OLLAMA_HOST / CAIRN_OLLAMA_MODEL to use another server or model.")
        return 0
    if not any(m == MODEL or m.split(":")[0] == MODEL for m in models):
        print(f"The server at {BASE_URL} does not serve '{MODEL}'. Available: {models or 'none'}")
        print(f"Pull it with 'ollama pull {MODEL}' or set CAIRN_OLLAMA_MODEL to one of the above.")
        return 0

    # Small local models handle json_object more reliably than full JSON schema.
    provider = OpenAICompatibleProvider(BASE_URL, name="ollama", timeout_s=300.0,
                                        native_json_schema=False)
    info = ModelInfo(name=MODEL, provider="ollama", tier=Tier.FAST, local=True,
                     input_price_per_mtok=0.0, output_price_per_mtok=0.0)

    try:
        with tempfile.TemporaryDirectory(prefix="cairn-ex06-") as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            (workspace / "incident.md").write_text(NOTES, encoding="utf-8")
            config = CairnConfig(
                data_dir=str(Path(tmp) / "data"),
                tools=["fs.*", "math.*"],
                security={"sandbox_roots": [str(workspace)]},
                budget={"max_cost_usd": None, "max_wall_s": 600},
            )
            async with await Cairn.create(config, providers=[(provider, info)]) as cairn:
                header("Model")
                print(f"  {info.provider}/{info.name} at {BASE_URL} (local, tier={info.tier.value})")

                header("1. Hand-written plan: summarize a file with the local model")
                b = PlanBuilder("Summarize the incident notes")
                b.tool("notes", "fs.read", path="incident.md")
                b.llm("summary", "Summarize these incident notes in two sentences:\n\n{{notes}}",
                      tier="fast", max_tokens=300)
                result = await cairn.run(b.build(output=ref("summary")))
                print(f"  status: {result.status}")
                print(f"  label : {result.label.get('integrity')} from {result.label.get('sources')}")
                if result.ok:
                    print(f"  output: {str(result.output).strip()}")
                else:
                    print(f"  error : {result.error}")

                header("2. Agent loop on the local model")
                goal = "Compute 17.5% of 2480 and also the square root of 2025, and report both numbers."
                print(f"  goal: {goal}")
                try:
                    agent_result = await cairn.agent(tools=["math.*"], max_replans=1).run(goal)
                except CairnError as exc:
                    print(f"  the local model could not complete the task: {exc.code}: {exc.message}")
                    return 0
                print(f"  status: {agent_result.status}")
                print(f"  output: {str(agent_result.output).strip()[:500]}")
                for planned in agent_result.planning:
                    print(f"  planner attempts={planned['attempts']} (repairs={len(planned['repairs'])})")
                if agent_result.run_ids:
                    print()
                    print(cairn.render(await cairn.report(agent_result.run_id)))
    finally:
        await provider.aclose()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
