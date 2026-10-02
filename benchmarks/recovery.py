"""Fault-injection and recovery benchmark.

Measures whether the runtime's retry, fallback and tier-escalation machinery
turns transient failures into successful runs, and at what cost in extra
attempts. Faults are injected deterministically:

* **flaky model**: a :class:`~cairn.models.scripted.ScriptedProvider` rule with
  ``fail_times=N`` raises a retryable error the first N times it matches.
* **flaky tool**: a tool that fails its first N calls (per process) before
  succeeding, simulating a transient network or service error.

Scenarios pair a fault with a recovery policy and compare the outcome to a
control with no recovery policy, so the measured recovery is attributable to
the policy and not to luck. "Extra attempts" counts node attempts beyond the
one a clean run would need.
"""

from __future__ import annotations

import asyncio
from typing import Any

from _common import env_markdown, fmt, pct, stamp, write_results

from cairn.eval.metrics import trajectory_metrics
from cairn.models import ModelInfo, ModelRouter, ScriptedProvider
from cairn.runtime import Fallback, PlanBuilder, RetryPolicy, Runtime, Services, ref
from cairn.runtime.budget import Budget
from cairn.runtime.plan import ToolNode
from cairn.tools.decorator import tool
from cairn.tools.registry import ToolRegistry

REPEATS = 5


def flaky_tool(fail_times: int) -> Any:
    """A tool that raises a retryable error on its first ``fail_times`` calls."""
    state = {"calls": 0}

    @tool(output_trust="trusted")
    async def flaky(x: int) -> int:
        """Return x, after failing transiently a few times."""
        state["calls"] += 1
        if state["calls"] <= fail_times:
            from cairn.core.errors import ToolError

            raise ToolError("transient tool failure", tool="flaky")
        return x

    return flaky


def tool_runtime(fail_times: int) -> Runtime:
    reg = ToolRegistry()
    reg.register(flaky_tool(fail_times))
    router = ModelRouter().register(ScriptedProvider(default="ok"),
                                    ModelInfo(name="m", provider="scripted"))
    return Runtime(Services(router=router, tools=reg))


def model_runtime(fail_times: int) -> Runtime:
    sp = ScriptedProvider().on("Answer", "the answer", fail_times=fail_times)
    router = ModelRouter().register(sp, ModelInfo(name="m", provider="scripted"))
    return Runtime(Services(router=router, tools=ToolRegistry()))


async def run_scenario(name: str, build: Any, plan_fn: Any, fail_times: int,
                       expect_ok: bool) -> dict[str, Any]:
    oks = 0
    extra_attempts: list[int] = []
    recovered: list[int] = []
    statuses: list[str] = []
    for _ in range(REPEATS):
        rt = build(fail_times)
        result = await rt.run(plan_fn(), budget=Budget(max_wall_s=30.0))
        statuses.append(result.status)
        counters = trajectory_metrics(await rt.journal.read(result.run_id))
        if result.status == "completed":
            oks += 1
        # A clean run uses one attempt per node; everything past that is recovery cost.
        extra_attempts.append(counters["node_attempts"] - counters["node_count"])
        recovered.append(counters["recovered_failures"])
    return {
        "scenario": name, "fault_count": fail_times, "repeats": REPEATS,
        "expected_success": expect_ok,
        "success_rate": oks / REPEATS,
        "mean_extra_attempts": sum(extra_attempts) / len(extra_attempts),
        "mean_recovered_nodes": sum(recovered) / len(recovered),
        "statuses": statuses,
    }


def _tool_plan(goal: str, **node_kw: Any) -> Any:
    b = PlanBuilder(goal)
    b.add(ToolNode(id="t", tool="flaky", args={"x": 1}, **node_kw))
    return b.build(output=ref("t"))


def tool_plan_retry() -> Any:
    return _tool_plan("flaky tool with retries", retry=RetryPolicy(max_attempts=4, backoff_s=0))


def tool_plan_no_retry() -> Any:
    return _tool_plan("flaky tool no retries", retry=RetryPolicy(max_attempts=1, backoff_s=0))


def tool_plan_default() -> Any:
    return _tool_plan("flaky tool with default on error",
                      retry=RetryPolicy(max_attempts=1, backoff_s=0), on_error="default", default=-1)


def model_plan_retry() -> Any:
    b = PlanBuilder("flaky model with retries")
    b.llm("a", "Answer the question.", retry=RetryPolicy(max_attempts=4, backoff_s=0))
    return b.build(output=ref("a"))


def model_plan_no_retry() -> Any:
    b = PlanBuilder("flaky model no retries")
    b.llm("a", "Answer the question.", retry=RetryPolicy(max_attempts=1, backoff_s=0))
    return b.build(output=ref("a"))


def model_plan_fallback() -> Any:
    """Primary attempt fails once; a fallback strategy retries the same model."""
    b = PlanBuilder("flaky model with fallback")
    b.llm("a", "Answer the question.", retry=RetryPolicy(max_attempts=1, backoff_s=0),
          fallbacks=[Fallback(tier="balanced")])
    return b.build(output=ref("a"))


async def main() -> None:
    scenarios = [
        await run_scenario("tool_no_retry_1fault", tool_runtime, tool_plan_no_retry, 1, False),
        await run_scenario("tool_retry_2faults", tool_runtime, tool_plan_retry, 2, True),
        await run_scenario("tool_retry_exhausted_5faults", tool_runtime, tool_plan_retry, 5, False),
        await run_scenario("tool_default_1fault", tool_runtime, tool_plan_default, 1, True),
        await run_scenario("model_no_retry_1fault", model_runtime, model_plan_no_retry, 1, False),
        await run_scenario("model_retry_2faults", model_runtime, model_plan_retry, 2, True),
        await run_scenario("model_fallback_1fault", model_runtime, model_plan_fallback, 1, True),
    ]
    recovery_scenarios = [s for s in scenarios if s["expected_success"]]
    overall_recovery = (
        sum(s["success_rate"] for s in recovery_scenarios) / len(recovery_scenarios)
    )
    header = stamp("recovery", {"repeats": REPEATS})
    payload = {**header, "scenarios": scenarios,
               "overall_recovery_rate_where_expected": overall_recovery}
    md = _markdown(header, scenarios, overall_recovery)
    jp, mp = write_results("recovery", payload, md)
    print(f"recovery: wrote {jp} and {mp}")
    for s in scenarios:
        print(f"  {s['scenario']:30s} success {pct(s['success_rate'])}  "
              f"extra attempts {s['mean_extra_attempts']:.1f}")


def _markdown(header: dict[str, Any], scenarios: list[dict[str, Any]], overall: float) -> str:
    lines = ["# Fault-injection recovery benchmark", ""]
    lines += env_markdown(header)
    lines += [
        f"- Repetitions per scenario: {header['config']['repeats']}",
        "",
        "Faults are injected deterministically (scripted model `fail_times`, a tool that fails its "
        "first N calls). Each scenario pairs a fault with a recovery policy; controls with no "
        "policy show the fault is real. 'Extra attempts' counts node attempts beyond a clean run.",
        "",
        f"Recovery rate where a policy should recover: {pct(overall)}.",
        "",
        "| scenario | faults | expected | success | mean extra attempts | mean recovered nodes |",
        "|---|---|---|---|---|---|",
    ]
    for s in scenarios:
        lines.append(
            f"| {s['scenario']} | {s['fault_count']} | "
            f"{'ok' if s['expected_success'] else 'fail'} | {pct(s['success_rate'])} | "
            f"{fmt(s['mean_extra_attempts'])} | {fmt(s['mean_recovered_nodes'])} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    asyncio.run(main())
