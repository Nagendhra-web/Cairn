"""Executor behavior: scheduling, branches, retries, fallbacks, timeouts, budgets, map/loop/verify."""

from __future__ import annotations

import asyncio
import time

import pytest

from cairn.core.errors import PlanValidationError
from cairn.journal import EventType
from cairn.models import ScriptedProvider
from cairn.runtime import (
    Budget,
    Check,
    Condition,
    Fallback,
    LLMNode,
    LoopNode,
    MapNode,
    Plan,
    PlanBuilder,
    RetryPolicy,
    ToolNode,
    VerifyNode,
    ref,
    tmpl,
)


async def test_sequential_refs_and_templates(runtime):
    b = PlanBuilder("arith")
    b.tool("a", "add", a=1, b=2)
    b.tool("b", "add", a=ref("a"), b=10)
    b.tool("c", "echo", text=tmpl("total={{b}}"))
    result = await runtime.run(b.build(output=ref("c")))
    assert result.ok
    assert result.output == "total=13"
    assert result.nodes == {"a": "completed", "b": "completed", "c": "completed"}


async def test_independent_nodes_run_concurrently(runtime):
    plan = Plan(goal="parallel", nodes=[
        ToolNode(id=f"s{i}", tool="slow", args={"seconds": 0.2}) for i in range(4)
    ])
    started = time.perf_counter()
    result = await runtime.run(plan)
    elapsed = time.perf_counter() - started
    assert result.ok
    assert elapsed < 0.6, f"4 x 0.2s nodes took {elapsed:.2f}s; expected parallel execution"


async def test_concurrency_limit_is_respected(runtime):
    plan = Plan(goal="limited", nodes=[
        ToolNode(id=f"s{i}", tool="slow", args={"seconds": 0.1}) for i in range(4)
    ])
    started = time.perf_counter()
    result = await runtime.run(plan, budget=Budget(max_concurrency=1))
    assert result.ok
    assert time.perf_counter() - started >= 0.38


async def test_conditional_branches(runtime):
    plan = Plan(goal="branch", nodes=[
        ToolNode(id="n", tool="add", args={"a": 2, "b": 3}),
        ToolNode(id="big", tool="echo", args={"text": "big"},
                 when=Condition(op="gt", left=ref("n"), right=4)),
        ToolNode(id="small", tool="echo", args={"text": "small"},
                 when=Condition(op="lte", left=ref("n"), right=4)),
        ToolNode(id="after_small", tool="echo", args={"text": "x"}, deps=["small"]),
        ToolNode(id="join", tool="echo", args={"text": "joined"}, deps=["big", "small"], join="any"),
    ])
    result = await runtime.run(plan)
    assert result.ok
    assert result.nodes["big"] == "completed"
    assert result.nodes["small"] == "skipped"
    assert result.nodes["after_small"] == "skipped"  # skip propagates
    assert result.nodes["join"] == "completed"  # join=any runs if one branch ran


async def test_retry_with_backoff_recovers(runtime, counter):
    plan = Plan(goal="retry", nodes=[
        ToolNode(id="f", tool="flaky", args={"fail_times": 2},
                 retry=RetryPolicy(max_attempts=3, backoff_s=0.01)),
    ])
    result = await runtime.run(plan)
    assert result.ok
    assert result.output == "ok after 3"
    events = await runtime.journal.read(result.run_id)
    assert sum(e.type == EventType.NODE_RETRYING for e in events) == 2


async def test_non_retryable_error_goes_straight_to_fallback(runtime, counter):
    plan = Plan(goal="fallback", nodes=[
        ToolNode(id="f", tool="broken", retry=RetryPolicy(max_attempts=5, backoff_s=0),
                 fallbacks=[Fallback(tool="echo", args={"text": "fallback used"})]),
    ])
    # ValueError from a tool is wrapped as a retryable ToolError, so all 5 attempts run.
    result = await runtime.run(plan)
    assert result.ok and result.output == "fallback used"
    assert counter.calls["broken"] == 5


async def test_on_error_skip_and_default(runtime):
    plan = Plan(goal="tolerant", nodes=[
        ToolNode(id="skipme", tool="broken", on_error="skip"),
        ToolNode(id="defaulted", tool="broken", on_error="default", default="fallback-value"),
        ToolNode(id="uses", tool="echo", args={"text": ref("defaulted")}),
    ])
    result = await runtime.run(plan)
    assert result.ok
    assert result.nodes["skipme"] == "skipped"
    assert result.output == "fallback-value"


async def test_failure_is_fatal_and_reported(runtime):
    plan = Plan(goal="fail", nodes=[ToolNode(id="b", tool="broken")])
    result = await runtime.run(plan)
    assert result.status == "failed"
    assert result.error["node_id"] == "b"
    assert "permanently broken" in result.error["message"]


async def test_node_timeout(runtime):
    plan = Plan(goal="timeout", nodes=[
        ToolNode(id="s", tool="slow", args={"seconds": 2}, timeout_s=0.05, on_error="skip"),
    ])
    result = await runtime.run(plan)
    assert result.nodes["s"] == "skipped"
    events = await runtime.journal.read(result.run_id)
    assert any(e.type == EventType.NODE_FAILED and e.data["error"]["code"] == "node_timeout" for e in events)


async def test_tool_call_budget_stops_runaway(runtime):
    plan = Plan(goal="budget", nodes=[ToolNode(id=f"e{i}", tool="echo", args={"text": "x"}) for i in range(5)])
    result = await runtime.run(plan, budget=Budget(max_tool_calls=2, max_concurrency=1))
    assert result.status == "failed"
    assert result.error["code"] == "budget_exceeded"


async def test_model_token_budget(runtime, scripted):
    scripted.default = "x" * 4000
    plan = Plan(goal="tokens", nodes=[LLMNode(id=f"l{i}", prompt="write a lot") for i in range(3)])
    result = await runtime.run(plan, budget=Budget(max_tokens=500, max_concurrency=1))
    assert result.status == "failed"
    assert result.error["details"]["limit"] == "tokens"


async def test_map_fans_out(runtime):
    plan = Plan(goal="map", nodes=[
        ToolNode(id="xs", tool="list.items", args={"n": 5}),
        MapNode(id="doubled", over=ref("xs"), body=ToolNode(id="body", tool="add",
                                                           args={"a": ref("$item"), "b": ref("$item")})),
    ], output=ref("doubled"))
    result = await runtime.run(plan)
    assert result.output == [0, 2, 4, 6, 8]


async def test_loop_until_condition(runtime, scripted):
    responses = iter(["draft one", "draft two", "FINAL draft"])
    scripted.on("Improve", lambda req: next(responses))
    plan = Plan(goal="loop", nodes=[
        LoopNode(id="refine", body=LLMNode(id="b", prompt="Improve: {{$last}}"),
                 until=Condition(op="contains", left=ref("$last"), right="FINAL"), max_iterations=5),
    ])
    result = await runtime.run(plan)
    assert result.output["iterations"] == 3
    assert result.output["converged"] is True
    assert result.output["value"] == "FINAL draft"


async def test_structured_output_with_repair(runtime, scripted):
    answers = iter(["not json at all", '{"city": "Paris", "population": 2100000}'])
    scripted.on("capital", lambda req: next(answers))
    schema = {"type": "object", "properties": {"city": {"type": "string"},
                                               "population": {"type": "integer"}},
              "required": ["city", "population"]}
    plan = Plan(goal="json", nodes=[LLMNode(id="q", prompt="What is the capital of France?",
                                            output_schema=schema)])
    result = await runtime.run(plan)
    assert result.ok
    assert result.output == {"city": "Paris", "population": 2100000}
    assert result.usage["model_calls"] == 2


async def test_verify_reruns_target_with_feedback_and_escalation(runtime, scripted):
    scripted.on("A reviewer rejected", "The answer mentions Paris.")
    scripted.on("Name the capital", "I am not sure.")
    plan = Plan(goal="verified", nodes=[
        LLMNode(id="answer", prompt="Name the capital of France.", tier="fast"),
        VerifyNode(id="check", target="answer", checks=[Check(type="contains", value="Paris")]),
        ToolNode(id="use", tool="echo", args={"text": ref("answer")}),
    ], output=ref("use"))
    result = await runtime.run(plan)
    assert result.ok
    assert result.output == "The answer mentions Paris."
    requests = scripted.requests
    assert requests[-1][0] == "scripted-balanced"  # escalated from fast after rejection
    events = await runtime.journal.read(result.run_id)
    verdicts = [e.data["passed"] for e in events if e.type == EventType.VERIFY_RESULT]
    assert verdicts == [False, True]


async def test_verify_failure_is_fatal_when_unrecoverable(runtime, scripted):
    scripted.on("capital", "no idea")
    plan = Plan(goal="v", nodes=[
        LLMNode(id="answer", prompt="capital?"),
        VerifyNode(id="check", target="answer", checks=[Check(type="contains", value="Paris")], max_rounds=2),
    ])
    result = await runtime.run(plan)
    assert result.status == "failed"
    assert result.error["code"] == "verification_failed"


async def test_model_fallback_across_providers(runtime, scripted):
    scripted.on("hello", "hi there", fail_times=1)
    plan = Plan(goal="fb", nodes=[LLMNode(id="h", prompt="hello", tier="fast")])
    result = await runtime.run(plan)
    assert result.ok and result.output == "hi there"
    events = await runtime.journal.read(result.run_id)
    route = next(e for e in events if e.type == EventType.ROUTE_DECISION)
    assert route.data["attempts"][0].get("error") == "model_error"
    assert route.data["chosen"] == "scripted/scripted-balanced"


def test_validation_catches_planner_mistakes(runtime):
    from cairn.runtime import validate_plan

    plan = Plan(goal="bad", nodes=[
        ToolNode(id="a", tool="does.not.exist"),
        ToolNode(id="b", tool="echo", args={"text": ref("ghost.output"), "bogus": 1}),
        ToolNode(id="c", tool="echo", args={"text": "x"}, deps=["d"]),
        ToolNode(id="d", tool="echo", args={"text": "x"}, deps=["c"]),
    ])
    with pytest.raises(PlanValidationError) as info:
        validate_plan(plan, runtime.services.tools)
    problems = "\n".join(info.value.problems)
    assert "unknown tool 'does.not.exist'" in problems
    assert "unknown node 'ghost'" in problems
    assert "no parameter 'bogus'" in problems


def test_validation_detects_cycles(runtime):
    from cairn.runtime import validate_plan

    plan = Plan(goal="cycle", nodes=[
        ToolNode(id="c", tool="echo", args={"text": "x"}, deps=["d"]),
        ToolNode(id="d", tool="echo", args={"text": "x"}, deps=["c"]),
    ])
    with pytest.raises(PlanValidationError) as info:
        validate_plan(plan, runtime.services.tools)
    assert "cycle" in info.value.problems[0]


def test_validation_enforces_grants(runtime):
    from cairn.runtime import validate_plan

    plan = Plan(goal="g", nodes=[ToolNode(id="m", tool="mail.send", args={"to": "a@b.c", "body": "x"})])
    with pytest.raises(PlanValidationError) as info:
        validate_plan(plan, runtime.services.tools, grants=["echo", "add"])
    assert "not granted" in info.value.problems[0]


async def test_cancellation(runtime):
    plan = Plan(goal="cancel", nodes=[ToolNode(id="s", tool="slow", args={"seconds": 5})])
    run_id = await runtime.create_run(plan)
    task = asyncio.create_task(runtime.execute(run_id))
    await asyncio.sleep(0.1)
    assert await runtime.cancel(run_id)
    result = await asyncio.wait_for(task, 2)
    assert result.status == "cancelled"


async def test_journal_is_hash_chained(runtime):
    from cairn.journal import verify_chain

    b = PlanBuilder("chain")
    b.tool("a", "echo", text="hi")
    result = await runtime.run(b.build())
    events = await runtime.journal.read(result.run_id)
    assert verify_chain(events) == []
