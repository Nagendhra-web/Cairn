"""HTTP API end to end: auth, rate limits, runs, approvals, streaming, replay, memory."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from cairn.config import CairnConfig
from cairn.models import ModelInfo, ScriptedProvider, Tier
from cairn.sdk import Cairn

pytestmark = pytest.mark.e2e

fastapi = pytest.importorskip("fastapi")


async def make(tmp_path: Any, **api: Any) -> tuple[Cairn, httpx.AsyncClient]:
    from cairn.api import create_app

    config = CairnConfig(data_dir=str(tmp_path / "data"),
                         security={"sandbox_roots": [str(tmp_path / "ws")]},
                         api={"requests_per_second": 1000, "burst": 1000})
    model = ScriptedProvider(default="ok")
    cairn = await Cairn.create(config, providers=[(model, ModelInfo(name="m", provider="scripted", tier=Tier.BALANCED))])
    app = create_app(cairn, **api)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 5000)),
                               base_url="http://test")
    return cairn, client


APPROVAL_PLAN = {"goal": "approve then compute", "nodes": [
    {"id": "gate", "kind": "approval", "message": "Proceed with the calculation?"},
    {"id": "calc", "kind": "tool", "tool": "math.calculate", "args": {"expression": "6*7"}, "deps": ["gate"]},
], "output": {"$ref": "calc"}}


async def wait_status(client: httpx.AsyncClient, run_id: str, wanted: set[str]) -> dict[str, Any]:
    for _ in range(100):
        body = (await client.get(f"/v1/runs/{run_id}")).json()
        if body["status"] in wanted:
            return dict(body)
        await asyncio.sleep(0.02)
    raise AssertionError(f"run never reached {wanted}")


async def test_plan_run_approval_and_replay(tmp_path):
    cairn, client = await make(tmp_path)
    async with client:
        r = await client.post("/v1/runs", json={"plan": APPROVAL_PLAN})
        assert r.status_code == 202
        run_id = r.json()["run_id"]
        report = await wait_status(client, run_id, {"suspended"})
        [approval] = (await client.get(f"/v1/runs/{run_id}/approvals")).json()
        d = await client.post(f"/v1/runs/{run_id}/approvals/{approval['request_id']}", json={"approved": True})
        assert d.status_code == 200
        report = await wait_status(client, run_id, {"completed"})
        assert report["output"] == 42
        replay = (await client.post(f"/v1/runs/{run_id}/replay")).json()
        assert replay["matched"] is True
        runs = (await client.get("/v1/runs")).json()
        assert runs[0]["run_id"] == run_id
    await cairn.close()


async def test_sse_stream_delivers_events_until_terminal(tmp_path):
    cairn, client = await make(tmp_path)
    async with client:
        plan = {"goal": "g", "nodes": [{"id": "a", "kind": "tool", "tool": "math.calculate",
                                        "args": {"expression": "1+1"}}]}
        run_id = (await client.post("/v1/runs", json={"plan": plan})).json()["run_id"]
        await wait_status(client, run_id, {"completed"})
        types = []
        async with client.stream("GET", f"/v1/runs/{run_id}/stream") as resp:
            async for line in resp.aiter_lines():
                if line.startswith("data:") and line != "data: {}":
                    types.append(json.loads(line[5:])["type"])
        assert types[0] == "run.created" and types[-1] == "run.completed"
    await cairn.close()


async def test_auth_required_when_keys_configured(tmp_path):
    cairn, client = await make(tmp_path, api_keys=["s3cret"])
    async with client:
        assert (await client.get("/v1/runs")).status_code == 401
        assert (await client.get("/v1/runs", headers={"Authorization": "Bearer nope"})).status_code == 401
        ok = await client.get("/v1/runs", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
    await cairn.close()


async def test_non_loopback_rejected_without_keys(tmp_path):
    from cairn.api import create_app

    cairn, _ = await make(tmp_path)
    app = create_app(cairn)
    remote = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("203.0.113.9", 1)),
                               base_url="http://test")
    async with remote:
        assert (await remote.get("/v1/runs")).status_code == 403
    await cairn.close()


async def test_rate_limiting(tmp_path):
    from cairn.api import create_app

    cairn, _ = await make(tmp_path)
    cairn.config.api.requests_per_second = 0.001
    cairn.config.api.burst = 2
    app = create_app(cairn)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
                               base_url="http://test")
    async with client:
        codes = [(await client.get("/v1/runs")).status_code for _ in range(4)]
    assert codes == [200, 200, 429, 429]
    await cairn.close()


async def test_errors_map_to_http_status(tmp_path):
    cairn, client = await make(tmp_path)
    async with client:
        assert (await client.get("/v1/runs/run_missing")).status_code == 404
        bad = {"goal": "g", "nodes": [{"id": "a", "kind": "tool", "tool": "nope.nope"}]}
        r = await client.post("/v1/runs", json={"plan": bad})
        assert r.status_code == 422
        assert "unknown tool" in str(r.json())
        assert (await client.post("/v1/runs", json={})).status_code == 422
    await cairn.close()


async def test_tools_memory_and_dashboard(tmp_path):
    from cairn.provenance import USER

    cairn, client = await make(tmp_path)
    async with client:
        tools = (await client.get("/v1/tools", params={"q": "arithmetic"})).json()
        assert tools[0]["name"] == "math.calculate"
        assert cairn.memory is not None
        await cairn.memory.remember("The staging database is Postgres 16", "semantic", USER, 0.7, "run_x")
        found = (await client.post("/v1/memory/search", json={"query": "staging database"})).json()
        assert found and "Postgres" in found[0]["text"]
        mem_id = found[0]["id"]
        assert (await client.delete(f"/v1/memory/{mem_id}")).json() == {"deleted": True}
        page = await client.get("/")
        assert page.status_code == 200 and "Cairn" in page.text
    await cairn.close()
