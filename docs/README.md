# Cairn documentation

Cairn is a Python runtime for AI agents that executes typed plans as durable, journaled runs. Every effect is recorded with a fingerprint of its request, every value carries a provenance label, and every tool call is checked against a flow policy before it runs. Runs can be resumed after a crash, replayed with no live calls, and forked with changes.

## Start here

| Document | Read it for |
|---|---|
| [architecture.md](architecture.md) | Layers, dependency direction, the lifecycle of `Cairn.ask`, extension points |
| [plan-ir.md](plan-ir.md) | Every node kind and field, refs and templates, conditions, retries, verification, map and loop, validation |
| [durability.md](durability.md) | Event types, hash chain, effect keys, resume, strict replay, fork, approvals, cancellation, workers |
| [provenance.md](provenance.md) | Labels, propagation, control labels, every policy rule, the injection worked example |

## Subsystems

| Document | Covers |
|---|---|
| [security.md](security.md) | Threat model, secrets, sandbox, network policy, code execution limits, MCP, API auth, hardening checklist |
| [tools-and-mcp.md](tools-and-mcp.md) | `@tool`, `ToolSpec`, built-in tools, adding a tool, mounting MCP servers, serving tools over MCP, pin files |
| [agents.md](agents.md) | `AgentSpec`, planner and context building, critic, replanning, sub-agents, supervisor |
| [models.md](models.md) | Providers, tiers, routing, fallback, circuit breaker, rate limits, cost accounting, streaming, auto detection |
| [memory.md](memory.md) | Memory layers, importance, decay, dedup, consolidation, write policy, inspection |
| [retrieval.md](retrieval.md) | Chunking, BM25, embedders, hybrid RRF, knowledge graph, rerankers, adaptive retrieval, corpora |
| [evaluation.md](evaluation.md) | Metrics, datasets, runner, experiment comparison, replay regression, LLM judge, benchmarks |

## Operating Cairn

| Document | Covers |
|---|---|
| [configuration.md](configuration.md) | Every `cairn.toml` key with defaults, and environment variables |
| [api.md](api.md) | Every HTTP endpoint, auth, rate limits, SSE and WebSocket formats |
| [observability.md](observability.md) | Run reports, traces, `cairn show/events/trace`, OTLP, HTML timeline, logging |

## Quick start

```sh
pip install -e '.[server,anthropic]'
cairn init                   # writes cairn.toml and ./workspace
cairn doctor                 # validates config, lists models and tools
cairn demo injection         # offline: a compromised model is stopped by the policy
cairn demo durability        # offline: crash, resume, replay, fork
cairn run "Summarize workspace/notes.md"    # needs a model (ANTHROPIC_API_KEY or CAIRN_OLLAMA_MODEL)
cairn show <run_id>
```

From Python:

```python
from cairn.sdk import Cairn

async with await Cairn.create() as cairn:
    result = await cairn.ask("Summarize ./workspace/notes.md")
    print(result.status, result.output)
    print(cairn.render(await cairn.report(result.run_id)))
```

## CLI reference

`cairn [--config PATH] <command>`; `--config` must precede the command.

| Command | Purpose |
|---|---|
| `init [--force]` | Write a starter `cairn.toml` and `./workspace` |
| `doctor` | Check configuration, models, optional dependencies, storage, tools |
| `demo [all\|injection\|durability\|agent]` | Offline demos with a scripted model |
| `run GOAL [--agent SPEC.toml] [-i] [--json]` | Plan and execute a goal with an agent |
| `plan GOAL` | Show the plan without executing |
| `exec PLAN.json [--input k=v ...] [-i] [--json]` | Execute a plan file |
| `submit PLAN.json [--input k=v ...]` | Create a run and enqueue it for background workers |
| `resume RUN_ID [-i]` | Resume a suspended or interrupted run |
| `cancel RUN_ID` | Cancel a run |
| `runs [--status S] [--limit N] [--json]` | List runs |
| `show RUN_ID [--json] [--html FILE]` | Inspect a run |
| `events RUN_ID [--type PREFIX] [--json]` | Raw journal |
| `trace RUN_ID [--otlp] [--out FILE]` | Span tree or OTLP JSON |
| `verify RUN_ID` | Check the hash chain (exit 2 on problems) |
| `approvals RUN_ID` | List approval requests |
| `approve RUN_ID REQUEST_ID [--reject] [--note N] [--by WHO]` | Decide and resume |
| `replay RUN_ID [--json]` | Strict replay (exit 1 unless matched) |
| `fork RUN_ID [--set node.field=value ...] [--invalidate NODE ...]` | Counterfactual re-run |
| `tools [--search QUERY]` | List or search tools, including quarantined ones |
| `memory OP [ARG] [--kind K]` | `list`, `search`, `delete`, `pin`, `forget`, `consolidate`, `expire`, `stats`, `export` |
| `mcp-serve [--expose GLOB ...] [--allow-privileged]` | Serve tools over MCP stdio |
| `serve [--host H] [--port P]` | HTTP API and dashboard |
| `worker [--concurrency N]` | Background worker on the job queue |

`run`, `exec` and `resume` exit 0 for `completed` or `suspended` runs and 1 otherwise; Cairn errors exit 2 with `error [<code>]: <message>`.

## Not yet implemented

Collected from the pages above:

* Storage backends other than SQLite and in-memory (no Postgres, no external vector database).
* An HTTP endpoint that enqueues runs for workers (use `cairn submit` or `Cairn.submit`).
* A CLI command for evaluations.
* Automatic re-verification of MCP tools after `tools/list_changed`.
* Journaling of calls made through `cairn mcp-serve`.
* Network isolation for `code.python`.
* A bundled neural embedder or cross-encoder.

Known limits that remain by design: the wall-clock budget applies per execution session, fork reuse sources are held in memory by the process that created the fork, and supervisor child runs are named after the sub-agent runner's base spec (`subagent@d1`), not the specialist.
