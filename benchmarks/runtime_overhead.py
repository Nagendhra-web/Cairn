"""Runtime overhead benchmark.

Measures the fixed costs the runtime adds on top of the work a plan actually
does, using trivial tools so the measurement is dominated by the runtime, not
the payload:

* journal append throughput, in-memory and SQLite (events per second);
* executor overhead per node for N-node plans, sequential and parallel;
* replay speed (effects reused per second, no tools or models invoked);
* fork reuse ratio (how many recorded effects a patched fork reuses).

All timings use :func:`time.perf_counter` over several repetitions and report
mean / p50 / p95. Absolute numbers depend on the machine, which is recorded in
the result header, so compare across runs on the same machine.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path
from typing import Any

from _common import env_markdown, fmt, stamp, write_results

from cairn.core.clock import SystemClock
from cairn.eval.metrics import percentile
from cairn.journal.events import EventDraft, EventType
from cairn.journal.store import InMemoryJournal, RunRecord, SQLiteJournal
from cairn.models import ModelInfo, ModelRouter, ScriptedProvider
from cairn.runtime import PlanBuilder, Runtime, Services, ref
from cairn.runtime.budget import Budget
from cairn.storage.sqlite import SQLiteDatabase
from cairn.tools.decorator import tool

REPEATS = 7


def trivial_tools() -> Any:
    @tool(output_trust="trusted")
    async def identity(x: int) -> int:
        """Return the input unchanged (a near-zero-cost tool)."""
        return x

    from cairn.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.register(identity)
    return reg


def runtime_for() -> Runtime:
    router = ModelRouter().register(
        ScriptedProvider(default="ok"), ModelInfo(name="m", provider="scripted")
    )
    return Runtime(Services(router=router, tools=trivial_tools()))


async def bench_journal_append(make_journal: Any, batches: int, per_batch: int) -> dict[str, Any]:
    """Append ``batches`` x ``per_batch`` events and report events/second."""
    throughputs: list[float] = []
    for rep in range(REPEATS):
        journal = make_journal()
        run_id = f"run_bench_{rep}"
        now = SystemClock().now()
        await journal.create_run(RunRecord(run_id=run_id, goal="bench", created_at=now,
                                           updated_at=now))
        await journal.append(run_id, [EventDraft(type=EventType.RUN_CREATED, ts=now,
                                                 data={"plan": {"goal": "b", "nodes": []}})])
        start = time.perf_counter()
        for _b in range(batches):
            drafts = [EventDraft(type=EventType.NOTE, ts=now, data={"i": i})
                      for i in range(per_batch)]
            await journal.append(run_id, drafts)
        elapsed = time.perf_counter() - start
        throughputs.append((batches * per_batch) / elapsed)
    return {
        "events": batches * per_batch, "batch_size": per_batch,
        "events_per_s_mean": sum(throughputs) / len(throughputs),
        "events_per_s_p50": percentile(throughputs, 50),
        "events_per_s_p95": percentile(throughputs, 95),
    }


def sequential_plan(n: int) -> Any:
    b = PlanBuilder(f"chain of {n}")
    b.tool("n0", "identity", x=0)
    for i in range(1, n):
        b.tool(f"n{i}", "identity", x=ref(f"n{i - 1}"))
    return b.build(output=ref(f"n{n - 1}"))


def parallel_plan(n: int) -> Any:
    b = PlanBuilder(f"fan of {n}")
    for i in range(n):
        b.tool(f"n{i}", "identity", x=i)
    return b.build()


async def bench_executor(plan_fn: Any, n: int, concurrency: int) -> dict[str, Any]:
    per_node: list[float] = []
    wall: list[float] = []
    for _ in range(REPEATS):
        rt = runtime_for()
        plan = plan_fn(n)
        start = time.perf_counter()
        result = await rt.run(plan, budget=Budget(max_nodes=n + 5, max_concurrency=concurrency,
                                                  max_wall_s=120.0))
        elapsed = time.perf_counter() - start
        assert result.status == "completed", result.error
        wall.append(elapsed)
        per_node.append(elapsed / n)
    return {
        "nodes": n, "concurrency": concurrency,
        "wall_s_mean": sum(wall) / len(wall), "wall_s_p50": percentile(wall, 50),
        "wall_s_p95": percentile(wall, 95),
        "per_node_ms_mean": 1000 * sum(per_node) / len(per_node),
        "per_node_ms_p50": 1000 * (percentile(per_node, 50) or 0.0),
    }


async def bench_replay(n: int) -> dict[str, Any]:
    rt = runtime_for()
    result = await rt.run(sequential_plan(n), budget=Budget(max_nodes=n + 5, max_wall_s=120.0))
    assert result.status == "completed"
    speeds: list[float] = []
    reused = 0
    for _ in range(REPEATS):
        start = time.perf_counter()
        report = await rt.replay(result.run_id)
        elapsed = time.perf_counter() - start
        assert report.matched, report.divergences
        reused = report.effects_replayed
        speeds.append(reused / elapsed if elapsed else 0.0)
    return {
        "nodes": n, "effects_replayed": reused,
        "effects_per_s_mean": sum(speeds) / len(speeds),
        "effects_per_s_p50": percentile(speeds, 50),
    }


async def bench_fork(n: int) -> dict[str, Any]:
    """Fork a sequential plan, patching the first node; measure reuse ratio.

    Patching node 0 invalidates it and everything downstream (every node
    depends transitively on it), so reuse is low; patching the last node
    reuses all upstream effects. We report both to bracket the ratio.
    """
    rt = runtime_for()
    result = await rt.run(sequential_plan(n), budget=Budget(max_nodes=n + 5, max_wall_s=120.0))
    assert result.status == "completed"
    total_effects = n

    fork_last = await rt.fork(result.run_id, patches={f"n{n - 1}": {"args": {"x": 999}}})
    reused_last = fork_last.usage["replayed_effects"]
    fork_first = await rt.fork(result.run_id, patches={"n0": {"args": {"x": 999}}})
    reused_first = fork_first.usage["replayed_effects"]
    return {
        "nodes": n, "total_effects": total_effects,
        "reused_when_patching_last_node": reused_last,
        "reuse_ratio_patching_last": reused_last / total_effects,
        "reused_when_patching_first_node": reused_first,
        "reuse_ratio_patching_first": reused_first / total_effects,
    }


async def main() -> None:
    inmem = await bench_journal_append(InMemoryJournal, batches=200, per_batch=50)

    tmpdir = tempfile.mkdtemp(prefix="cairn-bench-")
    db_path = str(Path(tmpdir) / "journal.db")

    def make_sqlite() -> SQLiteJournal:
        return SQLiteJournal(SQLiteDatabase(db_path))

    sqlite = await bench_journal_append(make_sqlite, batches=50, per_batch=50)

    exec_seq = [await bench_executor(sequential_plan, n, 1) for n in (10, 25, 50)]
    exec_par = [await bench_executor(parallel_plan, n, c) for n, c in ((25, 8), (50, 8), (50, 16))]
    replay = [await bench_replay(n) for n in (25, 50)]
    fork = [await bench_fork(n) for n in (25, 50)]

    header = stamp("runtime_overhead", {"repeats": REPEATS})
    payload = {
        **header,
        "journal_append": {"in_memory": inmem, "sqlite": sqlite},
        "executor_sequential": exec_seq,
        "executor_parallel": exec_par,
        "replay": replay,
        "fork": fork,
    }
    md = _markdown(header, payload)
    jp, mp = write_results("runtime_overhead", payload, md)
    print(f"runtime_overhead: wrote {jp} and {mp}")
    print(f"  journal in-memory: {inmem['events_per_s_mean']:.0f} events/s, "
          f"sqlite: {sqlite['events_per_s_mean']:.0f} events/s")
    for row in exec_seq:
        print(f"  sequential n={row['nodes']}: {row['per_node_ms_mean']:.3f} ms/node")
    for row in exec_par:
        print(f"  parallel n={row['nodes']} c={row['concurrency']}: "
              f"{row['per_node_ms_mean']:.3f} ms/node")


def _ja_row(name: str, j: dict[str, Any]) -> str:
    return (
        f"| {name} | {j['events']} | {j['batch_size']} | {fmt(j['events_per_s_mean'], 5)} | "
        f"{fmt(j['events_per_s_p50'], 5)} | {fmt(j['events_per_s_p95'], 5)} |"
    )


def _markdown(header: dict[str, Any], payload: dict[str, Any]) -> str:
    lines = ["# Runtime overhead benchmark", ""]
    lines += env_markdown(header)
    lines += [f"- Repetitions per measurement: {header['config']['repeats']}", ""]
    ja = payload["journal_append"]
    lines += [
        "## Journal append throughput", "",
        "| store | events | batch | events/s mean | events/s p50 | events/s p95 |",
        "|---|---|---|---|---|---|",
        _ja_row("in-memory", ja["in_memory"]),
        _ja_row("sqlite", ja["sqlite"]),
        "",
        "## Executor overhead per node", "",
        "Trivial tools (identity), so the time is runtime scheduling, policy checks, labeling "
        "and journaling, not tool work.",
        "",
        "| shape | nodes | concurrency | wall s mean | ms/node mean | ms/node p50 |",
        "|---|---|---|---|---|---|",
    ]
    for row in payload["executor_sequential"]:
        lines.append(f"| sequential | {row['nodes']} | {row['concurrency']} | "
                     f"{fmt(row['wall_s_mean'])} | {fmt(row['per_node_ms_mean'])} | "
                     f"{fmt(row['per_node_ms_p50'])} |")
    for row in payload["executor_parallel"]:
        lines.append(f"| parallel | {row['nodes']} | {row['concurrency']} | "
                     f"{fmt(row['wall_s_mean'])} | {fmt(row['per_node_ms_mean'])} | "
                     f"{fmt(row['per_node_ms_p50'])} |")
    lines += ["", "## Replay speed (no tools or models called)", "",
              "| nodes | effects replayed | effects/s mean | effects/s p50 |", "|---|---|---|---|"]
    for row in payload["replay"]:
        lines.append(f"| {row['nodes']} | {row['effects_replayed']} | "
                     f"{fmt(row['effects_per_s_mean'],5)} | {fmt(row['effects_per_s_p50'],5)} |")
    lines += ["", "## Fork reuse ratio", "",
              "Patching the last node of a chain reuses every upstream effect; patching the first "
              "node invalidates the whole chain.",
              "",
              "| nodes | reuse patching last | reuse patching first |", "|---|---|---|"]
    for row in payload["fork"]:
        lines.append(f"| {row['nodes']} | "
                     f"{row['reused_when_patching_last_node']}/{row['total_effects']} "
                     f"({row['reuse_ratio_patching_last']:.0%}) | "
                     f"{row['reused_when_patching_first_node']}/{row['total_effects']} "
                     f"({row['reuse_ratio_patching_first']:.0%}) |")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    asyncio.run(main())
