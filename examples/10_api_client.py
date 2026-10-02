"""10: Driving Cairn over its HTTP API.

What it demonstrates
    * Starting the FastAPI app (``cairn.api.create_app``) in-process with
      uvicorn on a free loopback port, exactly what ``cairn serve`` does.
    * Submitting a plan that contains an ``approval`` node with
      ``POST /v1/runs``, then polling ``GET /v1/runs/{id}`` until the run
      suspends.
    * Listing pending approvals and approving one via
      ``POST /v1/runs/{id}/approvals/{request_id}``; the server resumes the
      run in the background.
    * Streaming the run with Server-Sent Events (``GET /v1/runs/{id}/stream``):
      durable journal events plus live model tokens, until the run ends.
    * Strict replay over HTTP (``POST /v1/runs/{id}/replay``).

Why it matters
    Human-in-the-loop approvals are just another API call, so a chat bot,
    a ticketing system or a dashboard can sit between the agent and a
    privileged action. Without API keys configured, the server only accepts
    loopback clients, so a default install is never an open agent endpoint.

Run it (no API key needed; needs the server extra)
    pip install 'cairn-runtime[server]'
    python examples/10_api_client.py
"""

from __future__ import annotations

import asyncio
import json
import socket
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

from cairn.config import CairnConfig
from cairn.models import ModelInfo, ScriptedProvider, Tier
from cairn.sdk import Cairn

PLAN = {
    "goal": "Approve a discount, then compute and explain the new price",
    "nodes": [
        {"id": "gate", "kind": "approval",
         "message": "Apply a 15% discount to order A-1001 (list price 240 USD)?"},
        {"id": "price", "kind": "tool", "tool": "math.calculate", "deps": ["gate"],
         "args": {"expression": "240 * (1 - 0.15)"}},
        {"id": "explain", "kind": "llm", "tier": "fast",
         "prompt": "In one sentence, tell the customer their new price is {{price}} USD."},
    ],
    "output": {"price": {"$ref": "price"}, "message": {"$ref": "explain"}},
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def header(title: str) -> None:
    print(f"\n=== {title} ===")


async def wait_for(client: httpx.AsyncClient, run_id: str, wanted: set[str]) -> dict[str, Any]:
    for _ in range(200):
        report: dict[str, Any] = (await client.get(f"/v1/runs/{run_id}")).json()
        if report["status"] in wanted:
            return report
        await asyncio.sleep(0.05)
    raise TimeoutError(f"run {run_id} never reached {wanted}")


async def main() -> int:
    try:
        import uvicorn

        from cairn.api import create_app
    except ImportError:
        print("This example needs the server extra: pip install 'cairn-runtime[server]'")
        return 0

    model = ScriptedProvider(latency_s=0.05)
    model.on("tell the customer their new price",
             "Good news: with your 15% discount, order A-1001 now costs 204.0 USD.")
    info = ModelInfo(name="scripted-fast", provider="scripted", tier=Tier.FAST,
                     input_price_per_mtok=0.0, output_price_per_mtok=0.0)

    with tempfile.TemporaryDirectory(prefix="cairn-ex10-") as tmp:
        config = CairnConfig(
            data_dir=str(Path(tmp) / "data"),
            tools=["math.*"],
            memory_enabled=False,
            security={"sandbox_roots": [str(Path(tmp) / "workspace")]},
            api={"requests_per_second": 200, "burst": 400},
        )
        cairn = await Cairn.create(config, providers=[(model, info)])
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(create_app(cairn, api_keys=[]), host="127.0.0.1",
                                               port=port, log_level="warning"))
        serve_task = asyncio.create_task(server.serve())
        while not server.started:
            await asyncio.sleep(0.02)
        base = f"http://127.0.0.1:{port}"
        try:
            # trust_env=False keeps any HTTP(S)_PROXY settings away from loopback traffic.
            async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=30) as client:
                header(f"Server up at {base}")
                print(f"  GET /health -> {(await client.get('/health')).json()}")

                header("1. Submit a plan with an approval gate")
                resp = await client.post("/v1/runs", json={"plan": PLAN})
                run_id = resp.json()["run_id"]
                print(f"  POST /v1/runs -> {resp.status_code} {resp.json()}")
                report = await wait_for(client, run_id, {"suspended", "failed", "completed"})
                print(f"  polled status: {report['status']}  nodes: "
                      f"{ {k: v['status'] for k, v in report['nodes'].items()} }")

                header("2. Inspect and approve")
                approvals = (await client.get(f"/v1/runs/{run_id}/approvals")).json()
                pending = [a for a in approvals if a["status"] == "pending"]
                for a in pending:
                    print(f"  pending {a['request_id']} on node '{a['node_id']}': "
                          f"{a['preview']['message']}")
                last_seq = (await client.get(f"/v1/runs/{run_id}/events")).json()[-1]["seq"]

                header("3. Approve while streaming events (SSE)")

                async def stream() -> list[dict[str, Any]]:
                    items: list[dict[str, Any]] = []
                    async with client.stream("GET", f"/v1/runs/{run_id}/stream",
                                             params={"after": last_seq}) as sse:
                        kind = ""
                        async for line in sse.aiter_lines():
                            if line.startswith("event:"):
                                kind = line[6:].strip()
                            elif line.startswith("data:") and kind != "end":
                                item = json.loads(line[5:])
                                items.append(item)
                                if kind == "live":
                                    print(f"  live  token {item.get('text')!r}")
                                else:
                                    print(f"  event #{item['seq']:<3} {item['type']:<20} "
                                          f"{item.get('node_id') or ''}")
                    return items

                streamer = asyncio.create_task(stream())
                await asyncio.sleep(0.2)  # let the stream subscribe before work resumes
                decision = await client.post(
                    f"/v1/runs/{run_id}/approvals/{pending[0]['request_id']}",
                    json={"approved": True, "note": "discount within policy"})
                print(f"  POST approval -> {decision.status_code} {decision.json()}")
                events = await asyncio.wait_for(streamer, timeout=30)
                tokens = sum(1 for e in events if e.get("kind") == "live")
                print(f"  stream closed: {len(events) - tokens} journal events, {tokens} live tokens")

                header("4. Final state")
                report = await wait_for(client, run_id, {"completed", "failed"})
                print(f"  status: {report['status']}")
                print(f"  output: {report['output']}")
                print(f"  output label: {report['output_label']}")
                print(f"  approvals: {report['approvals']}")

                header("5. Strict replay over HTTP")
                replay = (await client.post(f"/v1/runs/{run_id}/replay")).json()
                print(f"  matched={replay['matched']} effects_replayed={replay['effects_replayed']}")
        finally:
            server.should_exit = True
            await serve_task
            await cairn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
