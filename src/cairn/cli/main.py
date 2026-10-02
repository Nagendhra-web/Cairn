"""``cairn`` command-line interface.

Commands map one-to-one onto runtime capabilities so that everything an agent
did can be inspected, approved, replayed or forked from a terminal.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from cairn import __version__
from cairn.core.errors import CairnError

Handler = Callable[[argparse.Namespace], Awaitable[int]]
COMMANDS: dict[str, Handler] = {}


def command(name: str) -> Callable[[Handler], Handler]:
    def register(fn: Handler) -> Handler:
        COMMANDS[name] = fn
        return fn

    return register


def _print(data: Any, as_json: bool) -> None:
    if as_json or not isinstance(data, str):
        print(json.dumps(data, indent=2, default=str))
    else:
        print(data)


async def _app(args: argparse.Namespace, **kwargs: Any) -> Any:
    from cairn.config import load_config
    from cairn.observability import configure_logging
    from cairn.sdk import Cairn

    config = load_config(getattr(args, "config", None))
    if not os.environ.get("CAIRN_LOG_LEVEL"):
        configure_logging(config.log_level if config.log_level != "INFO" else "WARNING",
                          json_logs=config.json_logs or bool(os.environ.get("CAIRN_JSON_LOGS")))
    return await Cairn.create(config, **kwargs)


def _color() -> bool:
    return sys.stdout.isatty() and not os.environ.get("NO_COLOR")


async def _terminal_approval(request: Any) -> Any:
    from cairn.runtime import ApprovalDecision

    if not sys.stdin.isatty():
        return None  # defer: the run suspends and can be approved later
    print("\n--- approval required ---")
    print(f"reason: {request.reason}")
    print(json.dumps(request.subject, indent=2, default=str))
    answer = await asyncio.to_thread(input, "approve? [y/N] ")
    return ApprovalDecision(approved=answer.strip().lower() in ("y", "yes"), by="terminal")


# ---------------------------------------------------------------------------- setup


CONFIG_TEMPLATE = """\
# Cairn configuration. Secrets never go here: models name the env var holding their key.
[cairn]
data_dir = ".cairn"
tools = ["math.*", "fs.*", "http.fetch", "comms.*"]

[cairn.policy]
enabled = true      # provenance rules (capability grants are always enforced)
strict = false      # true: anything needing approval is denied (unattended jobs)

[cairn.security]
sandbox_roots = ["./workspace"]
network_allow = []  # e.g. ["*.wikipedia.org", "api.github.com"]

[cairn.budget]
max_cost_usd = 1.0
max_tokens = 200000
max_wall_s = 900

# With no [[cairn.models]], Cairn uses ANTHROPIC_API_KEY if set, or CAIRN_OLLAMA_MODEL
# for a local model via Ollama. Explicit example (OpenAI-compatible server):
# [[cairn.models]]
# name = "llama3.1:8b"
# provider = "ollama"
# base_url = "http://localhost:11434/v1"
# tier = "fast"
# local = true
"""


@command("init")
async def cmd_init(args: argparse.Namespace) -> int:
    path = Path("cairn.toml")
    if path.exists() and not args.force:
        print("cairn.toml already exists (use --force to overwrite)")
        return 1
    path.write_text(CONFIG_TEMPLATE, encoding="utf-8")
    Path("workspace").mkdir(exist_ok=True)
    print("wrote cairn.toml and ./workspace. Next: cairn doctor, then cairn demo injection")
    return 0


@command("doctor")
async def cmd_doctor(args: argparse.Namespace) -> int:
    from cairn.config import load_config

    checks: list[tuple[str, bool, str]] = []
    try:
        config = load_config(args.config)
        checks.append(("config", True, "valid"))
    except CairnError as exc:
        print(f"config: FAIL\n{exc.message}")
        return 1
    checks.append(("models", bool(config.models),
                   ", ".join(f"{m.name} ({m.provider}, {m.tier.value})" for m in config.models)
                   or "none: set ANTHROPIC_API_KEY or CAIRN_OLLAMA_MODEL (demos still work offline)"))
    for module, purpose in (("anthropic", "Claude provider"), ("fastapi", "HTTP API"),
                            ("uvicorn", "HTTP server")):
        try:
            __import__(module)
            checks.append((module, True, f"installed ({purpose})"))
        except ImportError:
            checks.append((module, False, f"missing (optional, needed for {purpose})"))
    try:
        app = await _app(args, mount_mcp=False)
        checks.append(("storage", True, str(config.db_path)))
        checks.append(("tools", True, ", ".join(t.name for t in app.tools.list())))
        await app.close()
    except CairnError as exc:
        checks.append(("storage", False, exc.message))
    checks.append(("network allowlist", True, ", ".join(config.security.network_allow) or "empty (no web access)"))
    for name, ok, detail in checks:
        print(f"{'ok  ' if ok else 'warn'} {name}: {detail}")
    return 0


@command("demo")
async def cmd_demo(args: argparse.Namespace) -> int:
    from cairn.demos import DEMOS

    names = list(DEMOS) if args.name == "all" else [args.name]
    for name in names:
        print(await DEMOS[name]())
        print()
    return 0


# ----------------------------------------------------------------------- execution


@command("run")
async def cmd_run(args: argparse.Namespace) -> int:
    from cairn.agents import AgentSpec

    app = await _app(args, approval_handler=_terminal_approval if args.interactive else None)
    try:
        app._require_models()
        spec = None
        if args.agent:
            import tomllib

            spec = AgentSpec.model_validate(tomllib.loads(Path(args.agent).read_text("utf-8")))
        agent = app.agent(spec)
        result = await agent.run(args.goal)
        if args.json:
            _print(result.to_dict(), True)
        else:
            print(app.render(await app.report(result.run_id), color=_color()))
            if result.status == "suspended":
                print(f"\nrun suspended; approve with: cairn approve {result.run_id} <request_id>")
        return 0 if result.status in ("completed", "suspended") else 1
    finally:
        await app.close()


@command("plan")
async def cmd_plan(args: argparse.Namespace) -> int:
    from cairn.agents.planner import plan_to_json

    app = await _app(args)
    try:
        app._require_models()
        planned = await app.agent().plan(args.goal)
        print(plan_to_json(planned.plan))
        print(f"\n# plan label: {planned.label.describe()}; attempts: {planned.attempts}", file=sys.stderr)
        return 0
    finally:
        await app.close()


@command("exec")
async def cmd_exec(args: argparse.Namespace) -> int:
    from cairn.runtime import Plan

    app = await _app(args, approval_handler=_terminal_approval if args.interactive else None)
    try:
        plan = Plan.model_validate_json(Path(args.plan).read_text("utf-8"))
        inputs = dict(kv.split("=", 1) for kv in args.input or [])
        result = await app.run(plan, inputs=inputs)
        if args.json:
            _print(result.to_dict(), True)
        else:
            print(app.render(await app.report(result.run_id), color=_color()))
        return 0 if result.status in ("completed", "suspended") else 1
    finally:
        await app.close()


@command("submit")
async def cmd_submit(args: argparse.Namespace) -> int:
    from cairn.runtime import Plan

    app = await _app(args, mount_mcp=False)
    try:
        plan = Plan.model_validate_json(Path(args.plan).read_text("utf-8"))
        inputs = dict(kv.split("=", 1) for kv in args.input or [])
        run_id, job_id = await app.submit(plan, inputs=inputs)
        print(f"queued run {run_id} as job {job_id}; start workers with: cairn worker")
        return 0
    finally:
        await app.close()


@command("resume")
async def cmd_resume(args: argparse.Namespace) -> int:
    app = await _app(args, approval_handler=_terminal_approval if args.interactive else None)
    try:
        result = await app.runtime.resume(args.run_id)
        print(app.render(await app.report(result.run_id), color=_color()))
        return 0 if result.status in ("completed", "suspended") else 1
    finally:
        await app.close()


@command("cancel")
async def cmd_cancel(args: argparse.Namespace) -> int:
    app = await _app(args, mount_mcp=False)
    try:
        print("cancelled" if await app.runtime.cancel(args.run_id) else "run already finished")
        return 0
    finally:
        await app.close()


# ---------------------------------------------------------------------- inspection


@command("runs")
async def cmd_runs(args: argparse.Namespace) -> int:
    app = await _app(args, mount_mcp=False)
    try:
        runs = await app.runtime.journal.list_runs(limit=args.limit, status=args.status)
        if args.json:
            _print([r.to_dict() for r in runs], True)
        else:
            for r in runs:
                usage = r.summary
                print(f"{r.run_id}  {r.status:<10} tokens={usage.get('tokens', 0):<7} "
                      f"cost=${usage.get('cost_usd', 0):.4f}  {r.goal[:70]}")
        return 0
    finally:
        await app.close()


@command("show")
async def cmd_show(args: argparse.Namespace) -> int:
    app = await _app(args, mount_mcp=False)
    try:
        report = await app.report(args.run_id)
        if args.html:
            Path(args.html).write_text(app.render_html(report), encoding="utf-8")
            print(f"wrote {args.html}")
        elif args.json:
            _print(report, True)
        else:
            print(app.render(report, color=_color()))
        return 0
    finally:
        await app.close()


@command("events")
async def cmd_events(args: argparse.Namespace) -> int:
    app = await _app(args, mount_mcp=False)
    try:
        for ev in await app.runtime.journal.read(args.run_id):
            if args.type and not ev.type.startswith(args.type):
                continue
            print(json.dumps(ev.to_dict(), default=str) if args.json else
                  f"{ev.seq:>4} {ev.type:<20} {ev.node_id or '':<16} {json.dumps(ev.data, default=str)[:150]}")
        return 0
    finally:
        await app.close()


@command("trace")
async def cmd_trace(args: argparse.Namespace) -> int:
    from cairn.observability import build_trace, to_otlp

    app = await _app(args, mount_mcp=False)
    try:
        span = build_trace(args.run_id, await app.runtime.journal.read(args.run_id))
        payload = to_otlp(span) if args.otlp else span.to_dict()
        if args.out:
            Path(args.out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"wrote {args.out}")
        else:
            _print(payload, True)
        return 0
    finally:
        await app.close()


@command("verify")
async def cmd_verify(args: argparse.Namespace) -> int:
    from cairn.journal import verify_chain

    app = await _app(args, mount_mcp=False)
    try:
        events = await app.runtime.journal.read(args.run_id)
        problems = verify_chain(events)
        print("journal intact: " + str(len(events)) + " events" if not problems else "\n".join(problems))
        return 0 if not problems else 2
    finally:
        await app.close()


@command("approvals")
async def cmd_approvals(args: argparse.Namespace) -> int:
    app = await _app(args, mount_mcp=False)
    try:
        state = await app.runtime.load(args.run_id)
        for a in state.approvals.values():
            print(f"{a.request_id}  {a.status:<9} node={a.node_id}  {a.reason}")
            if a.status == "pending":
                print("    " + json.dumps(a.preview, default=str)[:400])
        return 0
    finally:
        await app.close()


@command("approve")
async def cmd_approve(args: argparse.Namespace) -> int:
    app = await _app(args)
    try:
        await app.runtime.decide(args.run_id, args.request_id, approved=not args.reject,
                                 by=args.by or os.environ.get("USER", "operator"), note=args.note)
        print(("rejected" if args.reject else "approved") + "; resuming run")
        result = await app.runtime.resume(args.run_id)
        print(app.render(await app.report(result.run_id), color=_color()))
        return 0
    finally:
        await app.close()


@command("replay")
async def cmd_replay(args: argparse.Namespace) -> int:
    app = await _app(args, mount_mcp=False)
    try:
        report = await app.runtime.replay(args.run_id)
        _print(report.to_dict(), args.json) if args.json else print(
            f"matched={report.matched} output_equal={report.output_equal} "
            f"effects_replayed={report.effects_replayed} divergences={len(report.divergences)}"
        )
        for d in report.divergences:
            print(f"  diverged at node '{d.get('node_id')}' effect {d.get('key')}")
        return 0 if report.matched else 1
    finally:
        await app.close()


@command("fork")
async def cmd_fork(args: argparse.Namespace) -> int:
    app = await _app(args)
    try:
        patches: dict[str, dict[str, Any]] = {}
        for item in args.set or []:
            target, _, raw = item.partition("=")
            node_id, _, field = target.partition(".")
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = raw
            patches.setdefault(node_id, {})[field] = value
        result = await app.runtime.fork(args.run_id, patches=patches, invalidate=args.invalidate or [])
        print(app.render(await app.report(result.run_id), color=_color()))
        return 0 if result.status in ("completed", "suspended") else 1
    finally:
        await app.close()


# ---------------------------------------------------------------------- subsystems


@command("tools")
async def cmd_tools(args: argparse.Namespace) -> int:
    app = await _app(args)
    try:
        specs = ([m.spec for m in await app.tools.discover(args.search, k=10)]
                 if args.search else app.tools.list())
        for spec in specs:
            effects = ",".join(sorted(spec.effects)) or "pure"
            print(f"{spec.name:<24} [{effects}] trust={spec.output_trust.value:<9} {spec.description}")
        for name, reason in app.tools.quarantined().items():
            print(f"{name:<24} QUARANTINED: {reason}")
        return 0
    finally:
        await app.close()


@command("memory")
async def cmd_memory(args: argparse.Namespace) -> int:
    app = await _app(args, mount_mcp=False)
    memory = app.memory
    try:
        if memory is None:
            print("memory is disabled in the configuration")
            return 1
        op = args.op
        if op == "list":
            for r in await memory.list(kind=args.kind):
                print(f"{r['id']}  {r['kind']:<10} imp={r['importance']:.2f} "
                      f"{'pinned ' if r['pinned'] else ''}{'' if r['trusted'] else 'UNTRUSTED '}"
                      f"{r['text'][:90]}")
        elif op == "search":
            _print(await memory.recall(args.arg, args.kind or "any", 10), True)
        elif op == "delete":
            print("deleted" if await memory.delete(args.arg) else "not found")
        elif op == "pin":
            await memory.pin(args.arg)
            print("pinned")
        elif op == "forget":
            print(f"forgot {await memory.forget(source_run=args.arg)} memories")
        elif op == "consolidate":
            _print(await memory.consolidate(), True)
        elif op == "expire":
            print(f"expired {len(await memory.expire())} memories")
        elif op == "stats":
            _print(await memory.stats(), True)
        elif op == "export":
            _print(await memory.export(), True)
        return 0
    finally:
        await app.close()


@command("mcp-serve")
async def cmd_mcp_serve(args: argparse.Namespace) -> int:
    from cairn.mcp import MCPServer

    app = await _app(args, mount_mcp=False)
    try:
        server = MCPServer(app.tools, expose=tuple(args.expose or ["*"]),
                           allow_privileged=args.allow_privileged, vault=app.runtime.services.vault,
                           sandbox=app.runtime.services.sandbox, network=app.runtime.services.network)
        await server.serve_stdio()
        return 0
    finally:
        await app.close()


@command("serve")
async def cmd_serve(args: argparse.Namespace) -> int:
    try:
        import uvicorn
    except ImportError:
        print("the HTTP API needs: pip install 'cairn-runtime[server]'")
        return 1
    from cairn.api import create_app

    app = await _app(args)
    api = create_app(app)
    config = uvicorn.Config(api, host=args.host or app.config.api.host,
                            port=args.port or app.config.api.port, log_level="info")
    try:
        await uvicorn.Server(config).serve()
    finally:
        await app.close()
    return 0


@command("worker")
async def cmd_worker(args: argparse.Namespace) -> int:
    from cairn.workers import SQLiteWorkQueue, Worker

    app = await _app(args)
    try:
        worker = Worker(app.runtime, SQLiteWorkQueue(app.db), concurrency=args.concurrency)
        print(f"worker {worker.worker_id} started (Ctrl+C to stop)")
        await worker.run_until_signal()
        return 0
    finally:
        await app.close()


# ---------------------------------------------------------------------- argument parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cairn", description="Replayable, provenance-tracking agent runtime.")
    p.add_argument("--version", action="version", version=f"cairn {__version__}")
    p.add_argument("--config", help="path to cairn.toml (default: ./cairn.toml or $CAIRN_CONFIG)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="write a starter cairn.toml")
    s.add_argument("--force", action="store_true")
    sub.add_parser("doctor", help="check configuration, models and optional dependencies")
    s = sub.add_parser("demo", help="offline demos (no API key needed)")
    s.add_argument("name", nargs="?", default="all", choices=["all", "injection", "durability", "agent"])

    s = sub.add_parser("run", help="plan and execute a goal with an agent")
    s.add_argument("goal")
    s.add_argument("--agent", help="agent spec TOML file")
    s.add_argument("--interactive", "-i", action="store_true", help="approve actions in the terminal")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("plan", help="show the plan for a goal without executing it")
    s.add_argument("goal")
    s = sub.add_parser("exec", help="execute a plan JSON file")
    s.add_argument("plan")
    s.add_argument("--input", action="append", help="key=value run input")
    s.add_argument("--interactive", "-i", action="store_true")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("submit", help="enqueue a plan JSON file for background workers")
    s.add_argument("plan")
    s.add_argument("--input", action="append", help="key=value run input")
    for name, helptext in (("resume", "resume a suspended or interrupted run"), ("cancel", "cancel a run")):
        s = sub.add_parser(name, help=helptext)
        s.add_argument("run_id")
        if name == "resume":
            s.add_argument("--interactive", "-i", action="store_true")

    s = sub.add_parser("runs", help="list runs")
    s.add_argument("--status")
    s.add_argument("--limit", type=int, default=20)
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("show", help="inspect a run: nodes, labels, usage, policy, output")
    s.add_argument("run_id")
    s.add_argument("--json", action="store_true")
    s.add_argument("--html", help="write a standalone HTML timeline")
    s = sub.add_parser("events", help="print the raw journal")
    s.add_argument("run_id")
    s.add_argument("--type", help="event type prefix filter, e.g. 'policy'")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("trace", help="export the trace tree (or OTLP JSON)")
    s.add_argument("run_id")
    s.add_argument("--otlp", action="store_true")
    s.add_argument("--out")
    s = sub.add_parser("verify", help="check the journal hash chain")
    s.add_argument("run_id")
    s = sub.add_parser("approvals", help="list approval requests of a run")
    s.add_argument("run_id")
    s = sub.add_parser("approve", help="approve (or --reject) a request and resume")
    s.add_argument("run_id")
    s.add_argument("request_id")
    s.add_argument("--reject", action="store_true")
    s.add_argument("--note")
    s.add_argument("--by")
    s = sub.add_parser("replay", help="re-execute from the journal with zero live calls")
    s.add_argument("run_id")
    s.add_argument("--json", action="store_true")
    s = sub.add_parser("fork", help="counterfactual re-run: --set node.field=value")
    s.add_argument("run_id")
    s.add_argument("--set", action="append")
    s.add_argument("--invalidate", action="append")

    s = sub.add_parser("tools", help="list or search tools")
    s.add_argument("--search")
    s = sub.add_parser("memory", help="inspect and manage memory")
    s.add_argument("op", choices=["list", "search", "delete", "pin", "forget", "consolidate",
                                  "expire", "stats", "export"])
    s.add_argument("arg", nargs="?")
    s.add_argument("--kind")
    s = sub.add_parser("mcp-serve", help="expose Cairn tools as an MCP server over stdio")
    s.add_argument("--expose", action="append")
    s.add_argument("--allow-privileged", action="store_true")
    s = sub.add_parser("serve", help="run the HTTP API and dashboard")
    s.add_argument("--host")
    s.add_argument("--port", type=int)
    s = sub.add_parser("worker", help="run a background worker")
    s.add_argument("--concurrency", type=int, default=4)
    return p


async def _invoke(handler: Handler, args: argparse.Namespace) -> int:
    return await handler(args)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from cairn.observability import configure_logging

    configure_logging(os.environ.get("CAIRN_LOG_LEVEL", "WARNING"),
                      json_logs=bool(os.environ.get("CAIRN_JSON_LOGS")))
    try:
        handler = COMMANDS[args.command]
        return asyncio.run(_invoke(handler, args))
    except CairnError as exc:
        print(f"error [{exc.code}]: {exc.message}", file=sys.stderr)
        if exc.details:
            print(json.dumps(exc.details, indent=2, default=str), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
