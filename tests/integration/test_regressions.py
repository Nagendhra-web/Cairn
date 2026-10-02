"""Regression tests for issues found while documenting and building examples."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from cairn.core.errors import PlanValidationError
from cairn.journal import EventType, SQLiteJournal
from cairn.runtime import Fallback, MapNode, Plan, Runtime, Services, ToolNode, ref, validate_plan
from cairn.storage import SQLiteDatabase
from cairn.tools import ToolRegistry, tool

pytestmark = pytest.mark.integration


async def test_waiting_node_is_checked_once_per_session(runtime):
    plan = Plan(goal="g", nodes=[
        ToolNode(id="page", tool="web.get", args={"url": "u"}),
        ToolNode(id="send", tool="mail.send", args={"to": ref("page"), "body": "x"}),
    ])
    result = await runtime.run(plan)
    assert result.status == "suspended"
    events = await runtime.journal.read(result.run_id)
    decisions = [e for e in events if e.type == EventType.POLICY_DECISION and e.node_id == "send"]
    assert len(decisions) == 1


async def test_map_body_fallback_uses_fallback_tool(runtime):
    plan = Plan(goal="g", nodes=[
        ToolNode(id="xs", tool="list.items", args={"n": 2}),
        MapNode(id="m", over=ref("xs"), body=ToolNode(id="b", tool="broken"),
                fallbacks=[Fallback(tool="echo", args={"text": "fb"})]),
    ], output=ref("m"))
    result = await runtime.run(plan)
    assert result.ok and result.output == ["fb", "fb"]


async def test_ref_with_extra_keys_is_a_literal(runtime):
    plan = Plan(goal="g", nodes=[ToolNode(id="e", tool="echo", args={"text": {"$ref": "nope", "x": 1}})])
    with pytest.raises(PlanValidationError):  # 'text' must be a string, and no phantom dependency
        await runtime.create_run(plan)
    validated = validate_plan(Plan(goal="g", nodes=[
        ToolNode(id="a", tool="echo", args={"text": "x"}),
        ToolNode(id="b", tool="add", args={"a": 1, "b": 2}, deps=[]),
    ]), runtime.services.tools)
    assert validated.node("b").deps == []


def test_quarantined_tool_is_reported_as_quarantined(runtime):
    runtime.services.tools.quarantine("echo", "definition changed since it was pinned")
    with pytest.raises(PlanValidationError) as info:
        validate_plan(Plan(goal="g", nodes=[ToolNode(id="e", tool="echo", args={"text": "x"})]),
                      runtime.services.tools)
    assert "quarantined tool 'echo'" in info.value.problems[0]


async def test_cancel_from_another_process_stops_execution(tmp_path):
    calls: list[str] = []
    reg = ToolRegistry()

    @tool(name="tick", output_trust="trusted")
    async def tick(i: int) -> int:
        """Slow step."""
        calls.append(str(i))
        await asyncio.sleep(0.3)
        return i

    reg.register(tick)
    db = tmp_path / "c.db"
    plan = Plan(goal="g", nodes=[ToolNode(id=f"t{i}", tool="tick", args={"i": i},
                                          deps=[f"t{i-1}"] if i else []) for i in range(6)])
    worker_rt = Runtime(Services(journal=SQLiteJournal(SQLiteDatabase(db)), tools=reg))
    operator_rt = Runtime(Services(journal=SQLiteJournal(SQLiteDatabase(db)), tools=reg))
    run_id = await worker_rt.create_run(plan)
    task = asyncio.create_task(worker_rt.execute(run_id))
    await asyncio.sleep(0.45)
    assert await operator_rt.cancel(run_id)  # a different runtime: appends run.cancelled
    result = await asyncio.wait_for(task, 5)
    assert result.status == "cancelled"
    assert len(calls) <= 3
    events = await worker_rt.journal.read(run_id)
    assert not any(e.type == EventType.RUN_COMPLETED for e in events)


async def test_mcp_server_denies_untrusted_sensitive_arguments(tmp_path):
    from cairn.mcp import MCPServer
    from cairn.mcp.jsonrpc import Request

    reg = ToolRegistry()

    @tool(name="net.get", effects={"network"}, sensitive={"url"}, output_trust="untrusted")
    def get(url: str) -> str:
        """Fetch."""
        return "page"

    @tool(name="calc", output_trust="inherit")
    def calc(x: int) -> int:
        """Double."""
        return x * 2

    reg.register(get)
    reg.register(calc)
    server = MCPServer(reg)
    denied = await server.handle_request(Request(id=1, method="tools/call",
                                                 params={"name": "net.get", "arguments": {"url": "https://x"}}))
    assert denied["isError"] and "denied by policy" in denied["content"][0]["text"]
    ok = await server.handle_request(Request(id=2, method="tools/call",
                                             params={"name": "calc", "arguments": {"x": 21}}))
    assert not ok.get("isError")


async def test_api_rejects_malformed_plans_with_422_and_hides_info(tmp_path):
    from cairn.api import create_app
    from cairn.config import CairnConfig
    from cairn.sdk import Cairn

    cairn = await Cairn.create(CairnConfig(data_dir=str(tmp_path / "d"),
                                           security={"sandbox_roots": []}))
    assert cairn.runtime.services.sandbox is None  # empty roots means no filesystem access
    app = create_app(cairn)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, client=("127.0.0.1", 1)),
                                 base_url="http://t") as client:
        bad = {"plan": {"goal": "g", "nodes": [{"id": "a", "kind": "teleport"}]}}
        r = await client.post("/v1/runs", json=bad)
        assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_request"
        assert (await client.get("/health")).json() == {"status": "ok"}
        info: dict[str, Any] = (await client.get("/v1/info")).json()
        assert "tools" in info
    await cairn.close()


async def test_submit_enqueues_for_workers(tmp_path):
    from cairn.config import CairnConfig
    from cairn.sdk import Cairn
    from cairn.workers import SQLiteWorkQueue, Worker

    cairn = await Cairn.create(CairnConfig(data_dir=str(tmp_path / "d")))
    plan = Plan(goal="g", nodes=[ToolNode(id="c", tool="math.calculate", args={"expression": "6*7"})])
    run_id, _ = await cairn.submit(plan)
    worker = Worker(cairn.runtime, SQLiteWorkQueue(cairn.db), poll_interval_s=0.05)
    assert await worker.run_once()
    for _ in range(100):
        if (await cairn.runtime.journal.get_run(run_id)).status == "completed":
            break
        await asyncio.sleep(0.02)
    await worker.stop(timeout=2)
    assert (await cairn.runtime.load(run_id)).output.value == 42
    await cairn.close()
