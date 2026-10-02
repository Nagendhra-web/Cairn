# Cairn examples

Runnable, self-contained scripts that each show one part of the runtime. Every
script has a docstring that explains what it demonstrates and why it matters,
prints its output in labeled sections, and cleans up after itself (journals,
sandboxes and pin files live in temporary directories).

## Setup

From the repository root:

```bash
pip install -e '.[server,anthropic]'   # server: example 10; anthropic: example 05
```

Examples 01-04 and 07-09 need only the base install.

## The examples

| # | Script | What it demonstrates | Needs |
|---|--------|----------------------|-------|
| 01 | [`01_first_plan.py`](01_first_plan.py) | Build a plan with `PlanBuilder` from built-in tools (`math.calculate`, `fs.write`), run it with `Cairn.create`, print the run report with provenance labels. | nothing |
| 02 | [`02_custom_tool.py`](02_custom_tool.py) | Define tools with `@tool`: effects, sensitive parameters and output trust. A refund computed from trusted data runs; one taken from a customer's ticket is held. Tool discovery ranking for task strings. | nothing |
| 03 | [`03_injection_defense.py`](03_injection_defense.py) | Support-inbox triage where an email hides an exfiltration instruction and the scripted model is fully compromised. The run suspends, the approval preview shows each argument's provenance, the reviewer rejects, the run resumes and completes without sending. Then strict mode denies outright. | nothing |
| 04 | [`04_durable_resume.py`](04_durable_resume.py) | SQLite journal, simulated process crash mid-run, resume in a new `Runtime` without repeating the charge, strict replay with zero live calls, fork with a patched prompt, hash-chain check, report. | nothing |
| 05 | [`05_claude_agent.py`](05_claude_agent.py) | A real agent on Claude: models auto-configured from `ANTHROPIC_API_KEY`, agent spec loaded from [`agents/analyst.toml`](agents/analyst.toml), planner + durable execution + critic over a sandboxed workspace, approvals on the terminal. | `ANTHROPIC_API_KEY` |
| 06 | [`06_local_ollama.py`](06_local_ollama.py) | A local model through an OpenAI-compatible server (Ollama, vLLM, LM Studio), registered in code with `OpenAICompatibleProvider` + `ModelInfo`; a hand-written plan and the agent loop on it. | a running server |
| 07 | [`07_rag_with_provenance.py`](07_rag_with_provenance.py) | Trusted and untrusted `DocumentCorpus` collections (the web one carries an injection), `retrieve` + `llm` plans with output labels, graph multi-hop retrieval, adaptive retrieval trace. | nothing |
| 08 | [`08_mcp_bridge.py`](08_mcp_bridge.py) | Starts [`mcp_server.py`](mcp_server.py) (Cairn tools served over MCP with `run_stdio_server`), mounts it with `mount_mcp_server` + `MCPServerConfig`, calls tools from a plan (outputs labeled untrusted), pins definitions, detects a rug pull and quarantines the changed tool, re-approves it. | nothing (spawns a local subprocess) |
| 09 | [`09_supervisor.py`](09_supervisor.py) | A `Supervisor` delegates to two specialist `AgentSpec`s; the delegation compiles to Plan IR with `agent` nodes that run as durable child runs with attenuated grants. Prints the compiled plan and the child runs. | nothing |
| 10 | [`10_api_client.py`](10_api_client.py) | Starts the FastAPI app with uvicorn on a free loopback port, submits a plan with an approval node, polls, approves over HTTP while streaming Server-Sent Events (journal events and live tokens), then replays over HTTP. | `[server]` extra |

"nothing" means fully offline: no API key, no network. Those examples use
`ScriptedProvider`, a deterministic model that returns scripted answers, so the
runtime behavior (policy, journaling, replay) is what you see, not model
variance.

## Commands

```bash
python examples/01_first_plan.py
python examples/02_custom_tool.py
python examples/03_injection_defense.py
python examples/04_durable_resume.py

export ANTHROPIC_API_KEY=sk-ant-...
python examples/05_claude_agent.py          # add --yes to approve held actions automatically

ollama serve & ollama pull llama3.2         # or any OpenAI-compatible server
python examples/06_local_ollama.py          # OLLAMA_HOST / CAIRN_OLLAMA_MODEL to override

python examples/07_rag_with_provenance.py
python examples/08_mcp_bridge.py
python examples/09_supervisor.py
python examples/10_api_client.py
```

Run all offline examples in one go:

```bash
for f in examples/0[1-4]_*.py examples/0[7-9]_*.py examples/10_*.py; do python "$f" || break; done
```

Examples 05 and 06 exit with status 0 and print setup instructions when the
key or the server is missing, so they are safe to include in that loop too.

## Other files

| Path | Purpose |
|------|---------|
| [`agents/analyst.toml`](agents/analyst.toml) | Declarative `AgentSpec` used by example 05; also works with `cairn run --agent examples/agents/analyst.toml "<goal>"`. |
| [`plans/research_digest.json`](plans/research_digest.json) | A tool-only plan (no models, no network) for `cairn exec`; see [`plans/README.md`](plans/README.md). |
| [`mcp_server.py`](mcp_server.py) | A small MCP server built from Cairn tools; used by example 08, runnable on its own as an stdio MCP server. |

## Where to go next

- `cairn demo` runs three built-in offline demos (injection, durability, agent).
- `cairn show <run_id> --html report.html` renders any journaled run as a timeline.
- The tests under `tests/` cover every feature shown here in more depth.
