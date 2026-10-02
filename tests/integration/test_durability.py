"""Durability: crash resume, strict replay, divergence detection, forking, approvals, sub-agents."""

from __future__ import annotations

from typing import Any

import pytest

from cairn.journal import EventType, SQLiteJournal, verify_chain
from cairn.runtime import (
    AgentNode,
    ApprovalDecision,
    ApprovalNode,
    LLMNode,
    Plan,
    PlanBuilder,
    RunResult,
    Runtime,
    Services,
    ToolNode,
    ref,
)
from cairn.storage import SQLiteDatabase
from cairn.tools import ToolRegistry, tool

pytestmark = pytest.mark.integration


class Crash(BaseException):
    """Simulates the process dying mid-run (not an ordinary, catchable failure)."""


def crash_registry(calls: dict[str, int], crash: dict[str, bool]) -> ToolRegistry:
    reg = ToolRegistry()

    @tool(name="step", output_trust="trusted")
    def step(name: str) -> str:
        """A side-effecting step that records how often it ran."""
        calls[name] = calls.get(name, 0) + 1
        if crash.get(name):
            raise Crash(name)
        return f"{name} done"

    reg.register(step)
    return reg


async def test_crash_resume_never_repeats_completed_effects(tmp_path):
    calls: dict[str, int] = {}
    crash = {"second": True}
    db_path = tmp_path / "cairn.db"
    plan = Plan(goal="pipeline", nodes=[
        ToolNode(id="first", tool="step", args={"name": "first"}),
        ToolNode(id="second", tool="step", args={"name": "second"}, deps=["first"]),
        ToolNode(id="third", tool="step", args={"name": "third"}, deps=["second"]),
    ])
    rt1 = Runtime(Services(journal=SQLiteJournal(SQLiteDatabase(db_path)),
                           tools=crash_registry(calls, crash)))
    run_id = await rt1.create_run(plan)
    with pytest.raises(Crash):
        await rt1.execute(run_id)

    # A brand new "process": fresh database connection, fresh runtime.
    crash["second"] = False
    rt2 = Runtime(Services(journal=SQLiteJournal(SQLiteDatabase(db_path)),
                           tools=crash_registry(calls, crash)))
    result = await rt2.resume(run_id)
    assert result.ok
    assert calls == {"first": 1, "second": 2, "third": 1}
    assert verify_chain(await rt2.journal.read(run_id)) == []


async def test_strict_replay_is_model_free_and_matches(runtime, scripted):
    scripted.on("Summarize", "a summary")
    b = PlanBuilder("replay")
    b.tool("page", "web.get", url="https://example.test")
    b.llm("summary", "Summarize: {{page}}")
    result = await runtime.run(b.build(output=ref("summary")))
    calls_before = len(scripted.requests)
    report = await runtime.replay(result.run_id)
    assert report.matched and report.output_equal
    assert report.effects_replayed == 2
    assert len(scripted.requests) == calls_before, "replay must not call the model"


async def test_replay_pinpoints_prompt_regressions(runtime_factory, scripted):
    scripted.on("Summarize", "a summary")
    rt = runtime_factory()
    b = PlanBuilder("regression")
    b.tool("page", "web.get", url="https://example.test")
    b.llm("summary", "Summarize: {{page}}")
    result = await rt.run(b.build())

    # Simulate a code change: the same recorded run, but the prompt template changed.
    events = await rt.journal.read(result.run_id)
    plan_data = events[0].data["plan"]
    plan_data["nodes"][1]["prompt"] = "Summarize in one sentence: {{page}}"
    edited = Plan.model_validate(plan_data)
    rid = await rt.create_run(edited, run_id="run_edited")
    source_effects = (await rt.load(result.run_id)).effects

    from cairn.runtime.recorder import Mode

    replay_result = await rt.execute(rid, mode=Mode.STRICT, source=source_effects)
    assert replay_result.status == "failed"
    assert replay_result.error["code"] == "replay_divergence"
    assert replay_result.error["node_id"] == "summary"


async def test_fork_reuses_unchanged_effects(runtime, scripted, counter):
    scripted.on("Summarize", "short summary")
    scripted.on("Write a long", "long summary")
    b = PlanBuilder("fork")
    b.tool("page", "web.get", url="https://example.test")
    b.llm("summary", "Summarize: {{page}}", tier="fast")
    b.tool("out", "echo", text=ref("summary"))
    original = await runtime.run(b.build(output=ref("out")))
    assert original.output == "short summary"
    web_calls = counter.calls["web.get"]

    forked = await runtime.fork(original.run_id, patches={"summary": {"prompt": "Write a long summary: {{page}}"}})
    assert forked.ok
    assert forked.output == "long summary"
    assert counter.calls["web.get"] == web_calls, "unchanged upstream tool must be reused"
    assert forked.usage["replayed_effects"] >= 1
    assert forked.usage["model_calls"] == 1

    resampled = await runtime.fork(original.run_id, invalidate=["page"])
    assert resampled.ok
    assert counter.calls["web.get"] == web_calls + 1


async def test_approval_gate_suspends_and_resumes(runtime, outbox):
    plan = Plan(goal="approve", nodes=[
        ApprovalNode(id="ok_to_send", message="Send the weekly report?"),
        ToolNode(id="send", tool="mail.send", args={"to": "team@example.com", "body": "report"},
                 deps=["ok_to_send"]),
        ToolNode(id="independent", tool="echo", args={"text": "still runs"}),
    ])
    first = await runtime.run(plan)
    assert first.status == "suspended"
    assert first.nodes["independent"] == "completed"
    assert outbox == []
    [pending] = first.pending_approvals
    await runtime.decide(first.run_id, pending["request_id"], approved=True, by="alice")
    final = await runtime.resume(first.run_id)
    assert final.ok
    assert outbox == [{"to": "team@example.com", "body": "report"}]


async def test_interactive_approval_handler(runtime_factory, outbox):
    seen: list[str] = []

    async def handler(request: Any) -> ApprovalDecision:
        seen.append(request.reason)
        return ApprovalDecision(approved=False, by="bot", note="not today")

    rt = runtime_factory(approval_handler=handler)
    plan = Plan(goal="x", nodes=[ApprovalNode(id="gate", message="Deploy?", on_error="skip")])
    result = await rt.run(plan)
    assert seen == ["Deploy?"]
    assert result.nodes["gate"] == "skipped"


async def test_subagent_runs_as_durable_child(runtime_factory, scripted, counter, outbox):
    from cairn.agents import AgentSubagentRunner

    child_plan = {"goal": "add", "nodes": [{"id": "s", "kind": "tool", "tool": "add",
                                            "args": {"a": 20, "b": 22}}], "output": {"$ref": "s"}}
    scripted.on("Compute the answer", child_plan)
    rt = runtime_factory()
    rt.services.subagents = AgentSubagentRunner(rt)
    plan = Plan(goal="delegate", nodes=[
        AgentNode(id="helper", goal="Compute the answer to everything", tools=["add"]),
        ToolNode(id="report", tool="echo", args={"text": {"$tmpl": "answer={{helper}}"}}),
    ], output=ref("report"))
    result = await rt.run(plan)
    assert result.ok, result.error
    assert result.output == "answer=42"
    state = await rt.load(result.run_id)
    [child_id] = state.children
    child = await rt.journal.get_run(child_id)
    assert child.parent_run_id == result.run_id
    child_state = await rt.load(child_id)
    assert child_state.grants == ["add"]
    assert child_state.depth == 1


async def test_subagent_cannot_escalate_grants(runtime):
    from cairn.core.errors import PlanValidationError

    plan = Plan(goal="escalate", nodes=[AgentNode(id="a", goal="send mail", tools=["mail.*"])])
    with pytest.raises(PlanValidationError):
        await runtime.create_run(plan, grants=["echo"])


async def test_sqlite_journal_concurrent_append_detection(tmp_path):
    from cairn.journal import ConcurrentAppend, EventDraft, RunRecord

    j = SQLiteJournal(SQLiteDatabase(tmp_path / "j.db"))
    await j.create_run(RunRecord(run_id="r", goal="g"))
    await j.append("r", [EventDraft(type="note")], expected_seq=1)
    with pytest.raises(ConcurrentAppend):
        await j.append("r", [EventDraft(type="note")], expected_seq=1)


async def test_live_token_streaming(runtime, scripted):
    import asyncio

    scripted.on("Stream", "x" * 64)
    plan = Plan(goal="stream", nodes=[LLMNode(id="s", prompt="Stream please")])
    run_id = await runtime.create_run(plan)
    received: list[dict[str, Any]] = []

    async def listen() -> None:
        async for message in runtime.services.live.subscribe(run_id):
            received.append(message)

    listener = asyncio.create_task(listen())
    await asyncio.sleep(0)
    result: RunResult = await runtime.execute(run_id)
    listener.cancel()
    assert result.ok
    assert "".join(m["text"] for m in received) == "x" * 64
    events = await runtime.journal.read(run_id)
    assert not any(e.type == "token" for e in events)  # tokens are ephemeral, not journaled
    assert any(e.type == EventType.EFFECT_COMPLETED for e in events)



async def test_replay_after_sqlite_roundtrip_is_canonical(tmp_path):
    """Regression: dict outputs read back from SQLite (sorted keys) must render identically."""
    from cairn.models import ModelInfo, ModelRouter, ScriptedProvider

    reg = ToolRegistry()

    @tool(name="profile", output_trust="trusted")
    def profile() -> dict[str, Any]:
        """Returns a dict whose natural key order is not sorted."""
        return {"zeta": 1, "alpha": 2, "mid": {"y": 1, "b": 2}}

    reg.register(profile)
    model = ScriptedProvider(default="described")
    router = ModelRouter().register(model, ModelInfo(name="m", provider="scripted"))
    db = tmp_path / "c.db"
    plan = Plan(goal="g", nodes=[ToolNode(id="p", tool="profile"),
                                 LLMNode(id="d", prompt="Describe {{p}}")])
    rt = Runtime(Services(journal=SQLiteJournal(SQLiteDatabase(db)), router=router, tools=reg))
    result = await rt.run(plan)
    fresh = Runtime(Services(journal=SQLiteJournal(SQLiteDatabase(db)), router=router, tools=reg))
    report = await fresh.replay(result.run_id)
    assert report.matched, report.divergences
