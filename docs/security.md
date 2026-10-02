# Security model

Cairn executes actions chosen by language models. This page states what it defends against, where each boundary is enforced in code, and where the boundaries end. The prompt-injection mechanism itself is described in [provenance.md](provenance.md).

## Threat model

Trusted:

* the operator who configures Cairn, writes `cairn.toml`, registers tools and approves requests;
* the code of locally registered tools (a tool runs in-process with full Python privileges);
* the host, the Python environment and the SQLite database file.

Untrusted:

* content read at runtime: web pages, retrieved documents, files, emails, MCP tool outputs, memories derived from them;
* the model's outputs, to the extent they are derived from untrusted content (Cairn assumes a fully compromised model in its benchmarks);
* MCP servers mounted with `trust = "untrusted"` (the default): their names, descriptions, schemas, annotations and outputs;
* HTTP API clients until they authenticate.

Out of scope: what a model says (Cairn constrains what models can cause, not their text), a compromised host, malicious code in tools the operator registered, and side channels.

## Prompt injection

Handled by provenance labels and the flow policy, not by detection: untrusted data cannot reach a declared sensitive parameter, or decide that a privileged action happens, without an approval; secret-tagged data cannot leave through egress tools. See [provenance.md](provenance.md) for rules, examples and the attacker capability table. Injection heuristics (`security/injection.py`) only annotate traces.

## Tool abuse

Layers applied to every tool call:

1. **Capability grants.** A run has a list of grant patterns (`fnmatch`). Validation rejects plans that use ungranted tools; the `capability` policy rule rejects them again at call time. Grants come from `AgentSpec.tools` for agent runs (the SDK uses `config.tools`), from the `grants` argument of `Runtime.create_run` (default `("*")`), and from a node's `tools` for sub-agents.
2. **Contracts.** `ToolRegistry.invoke` validates arguments against the tool's input schema before calling it (`tool_arguments_invalid`, non-retryable). Each tool declares its effects, sensitive parameters, output trust and secrets (see [tools-and-mcp.md](tools-and-mcp.md)).
3. **Policy.** Every call is evaluated and the decision journaled before execution.
4. **Approvals.** Bound to the exact call: the request id hashes the node, tool, (redacted) arguments, argument labels and effects, so approving one call does not approve a different one.
5. **Budgets and timeouts.** See [Resource exhaustion](#resource-exhaustion).

The registry only knows what tools declare. A tool that performs a network send while declaring no effects is invisible to the policy; review tool declarations like any other security-relevant code.

## Privilege escalation

* **Grant attenuation for sub-agents.** An `agent` node's `tools` patterns must each be covered by the parent's grants: a pattern is accepted if some parent grant is `*` or the pattern matches a grant as a glob. A parent with `["fs.*"]` can delegate `fs.read` or `fs.*`, not `*` or `http.fetch` (`agent node 's' requests tools '*' beyond parent grants`). The child run is created with exactly those patterns as its grants.
* **Taint cannot be laundered through delegation.** The child's label is the node's control label joined with the goal's label; the child planner starts from it, so a sub-goal derived from untrusted data yields an untrusted child plan.
* **Budgets are inherited.** The child gets what the parent has left (`Budget.child`: remaining cost, tokens, model calls and tool calls; same wall, node and concurrency limits; `max_depth - 1`). Depth beyond `max_depth` (default 3) is `budget_exceeded`.
* **Supervisor.** A supervisor run is granted the union of its specialists' tool patterns, and each delegated task is an `agent` node limited to its specialist's tools.
* **Memory.** Untrusted content cannot be written to procedural memory (planner few-shots) or pinned (see [memory.md](memory.md)).

## Secret isolation

`src/cairn/security/secrets.py`.

* **Vault.** `SecretVault.from_env(prefix="CAIRN_SECRET_")` loads every non-empty `CAIRN_SECRET_<NAME>` variable as secret `<NAME>`. `Cairn.create(vault=...)` accepts your own vault.
* **Scopes.** A tool declares the secrets it needs (`@tool(secrets={"GITHUB_TOKEN"})`). At call time it receives `ctx.secrets`, a `SecretScope` limited to those names. `ctx.secrets.get(name)` raises `PolicyViolation` for an undeclared name (`tool did not declare secret '...'`) or an unset one (`secret '...' is not configured`). Accessed names (never values) are journaled as `secrets_used`.
* **Secrets never enter prompts or plans.** Plans and prompts have no syntax for secrets; only tool code can read them.
* **Redaction.** `Redactor` replaces every vault value of 4 characters or more with `[REDACTED:<NAME>]`, longest first. It is applied to: tool results before they are journaled or labeled, LLM prompts and system prompts before they are sent, model response text, approval previews, text passed to memory `remember`, and MCP server responses.

Limits:

* Redaction is exact substring matching. A transformed secret (base64, URL-encoded, split across fields, reversed) is not caught, and values shorter than 4 characters are never redacted.
* Values the operator puts in a plan or in run `inputs` are journaled as given in `run.created`; tool arguments appear in the `effect.completed` request preview. Do not pass secrets as plan literals or inputs.
* Using a secret does not tag the tool's output with secrecy. To stop secret-derived data from leaving through egress tools, declare `output_secrecy` on the tool (see [provenance.md](provenance.md#secret-egress-deny)).
* Child processes get `safe_env()`: only `PATH`, `LANG=C.UTF-8`, `PYTHONHASHSEED=0` plus explicitly passed variables, so credentials in Cairn's own environment are not inherited by `code.python` or MCP servers.

## Filesystem sandbox

`PathSandbox(roots, read_only=False)` (`security/sandbox.py`), configured by `security.sandbox_roots` (default `["./workspace"]`, created at startup) and `security.sandbox_read_only`.

* Relative paths resolve against the first root; absolute paths are allowed if they resolve inside some root.
* Paths are fully resolved (symlinks included) before the containment check, so `../` traversal and symlinks pointing outside a root are rejected (`sandbox_violation`).
* Any path component named `.env`, `.git`, `id_rsa`, `id_ed25519`, `.netrc` or `.pypirc` is refused even inside a root.
* In read-only mode, `resolve(..., write=True)` raises.
* The `fs.*` built-in tools refuse to run without a sandbox.

Limits: the check resolves the path, then the tool opens it; a process that can modify the sandbox concurrently could swap a component for a symlink in between. Only tools that call `ctx.sandbox.resolve` are confined; your own tools must do the same.

## Network policy and SSRF

`NetworkPolicy(allow_domains, *, allow_private=False, schemes=("https", "http"))`, configured by `security.network_allow` (env `CAIRN_NETWORK_ALLOW`, comma-separated) and `security.allow_private_network`.

`check(url)` enforces, in order:

1. scheme in `schemes`;
2. a host is present;
3. the host matches an allowlist glob (`*.wikipedia.org`); an **empty allowlist allows nothing**;
4. unless `allow_private`, the host is resolved and every address is rejected if private, loopback, link-local, reserved, multicast or unspecified (this blocks names that resolve to `127.0.0.1` or cloud metadata addresses).

`http.fetch` and `http.post_json` call `check` before connecting, do not follow redirects (`follow_redirects=False`), check a redirect's target against the policy and then fail with `redirect to <location> must be fetched explicitly`, and require a configured policy.

Limits:

* DNS is resolved once for the check and again by `httpx` for the connection, so a DNS-rebinding server can return a public address to the check and a private one to the connection.
* If resolution fails during the check, no addresses are inspected and the check passes (the request then fails to connect, normally).
* IP-literal URLs must themselves match the allowlist.
* Only tools that call `ctx.network.check` are restricted. `code.python` and MCP server processes are not subject to the policy.

## Code execution isolation and its limits

`code.python` (`tools/builtin/compute.py`, not in the default `tools` list) runs code with `sys.executable -I -c <code>` in a fresh temporary directory, with `safe_env()`, and on non-Windows platforms `setrlimit` for CPU seconds (`timeout_s + 1`), address space (`memory_mb`, default 512), file size (10 MiB) and open files (64). A wall-clock timeout (`timeout_s`, default 10 s) kills the process. Output is truncated to the last 20,000 characters of stdout and 5,000 of stderr. It is declared `effects={"execute"}`, `sensitive={"code"}`, output `untrusted`, so code derived from untrusted data, or run because of untrusted control flow, requires approval.

What this is not:

* **No network isolation.** The child can open sockets to anywhere the host can reach.
* **No filesystem isolation.** The working directory is a temporary directory, but the child can read and write any path the Cairn process's user can.
* **No syscall filtering, user separation or VM boundary.** It is process isolation with resource limits.
* On Windows no resource limits are applied.

For untrusted code, run Cairn inside a container or VM without network access and with a read-only, minimal filesystem, and keep `code.python` behind approvals.

Related: tool timeouts use `asyncio.wait_for`. A synchronous tool runs in a worker thread, and on timeout the thread is not stopped; it keeps running in the background.

## Untrusted MCP servers

`src/cairn/mcp/bridge.py` mounts a server's tools as `<server>.<tool>`. Defenses:

* **Process environment.** The server runs with `safe_env()` plus only the `env` given in its configuration; nothing else from Cairn's environment is inherited.
* **Transport hardening** (`mcp/client.py`, `mcp/jsonrpc.py`): every request has a timeout and a timed-out request is announced with `notifications/cancelled`; messages larger than 16 MiB are refused; JSON-RPC batches are rejected; server-to-client requests other than `ping` (sampling, roots, elicitation) are refused with `method not found`; stderr is drained to logging, never to a model; protocol versions outside `2025-06-18`, `2025-03-26`, `2024-11-05` are rejected at handshake; shutdown closes stdin, then sends SIGTERM, then SIGKILL.
* **Tool poisoning.** For untrusted servers, descriptions and every `description`/`title` in the input schema are sanitized: Unicode categories Cc, Cf, Co and Cs are removed (keeping newline and tab), whitespace is collapsed and length capped at `max_description_chars` (default 1000). The raw texts are scanned with the injection heuristics; findings appear in `MountReport.signals`. Tool names must match `^[A-Za-z0-9_.\-]{1,128}$`; duplicates and names colliding with a tool from another source are skipped.
* **Untrusted outputs.** Tools from untrusted servers have `output_trust="untrusted"`.
* **Effect laundering.** Effects are inferred from annotations conservatively: `network` is always present for untrusted servers, `readOnlyHint` adds `read`, `destructiveHint` adds `write` (for untrusted servers even when also read-only), `openWorldHint` keeps `network`. Only a trusted server can narrow a read-only, non-open-world tool to `read` alone. Operators can pin exact effects with `effects_override` and declare `sensitive_params`, `requires_approval` and `allowed_tools`.
* **Rug pulls.** A mounted tool's fingerprint covers its name, sanitized description, schema, effects and a version string containing a hash of the raw remote definition (name, title, description, input and output schema, annotations). If `pinned[name]` is set and differs, the tool is registered but quarantined (`ToolRegistry.get` raises `NotFound: tool '...' is quarantined: mcp rug-pull: definition changed ...`) until re-approved. With `quarantine_unpinned=True` and at least one pin, new unpinned tools are quarantined too. `approve(registry, report, name)` releases one; `pin_report(path, report, include_quarantined=False)` merges current fingerprints into a pin file (`{"version": 1, "tools": {...}}`, written atomically).

Limits:

* `cairn.toml` `[[mcp_servers]]` entries accept only `name`, `command`, `args`, `env`, `trust` and `allowed_tools`; other keys (`pinned`, `effects_override`, `sensitive_params`, `requires_approval`, `quarantine_unpinned`, timeouts) are silently ignored. Use `Cairn.mount_mcp({...})` or `mount_mcp_server` in Python to set them.
* Pins are checked at mount time. A server that changes its tools later sends `notifications/tools/list_changed`, which only sets `MCPClient.tools_changed`; nothing re-lists or re-verifies automatically, and calls are made by tool name.
* The server process is not sandboxed beyond its environment: it can use the network and filesystem with the user's privileges.

### Exposing Cairn as an MCP server

`cairn mcp-serve` / `MCPServer` exposes only tools matching `--expose` globs and, unless `--allow-privileged`, withholds tools with privileged effects or `requires_approval`. Each call gets a secret scope limited to the tool's declared secrets and responses are redacted. Calls through the MCP server bypass the provenance policy and the journal: the MCP client is the decision maker. The server passes no sandbox or network policy to tools, so `fs.*` and `http.*` tools are listed but fail with `filesystem tools require a configured sandbox root` / `network tools require a configured NetworkPolicy allowlist`.

## Resource exhaustion

`Budget` fields and defaults (`runtime/budget.py`; the config defaults in [configuration.md](configuration.md) differ):

| Field | `Budget()` default | `[budget]` config default | Enforced |
|---|---|---|---|
| `max_cost_usd` | `None` | `1.0` | before every effect |
| `max_tokens` | `200000` | `200000` | before model effects |
| `max_model_calls` | `200` | `100` | before model effects |
| `max_tool_calls` | `200` | `100` | before tool effects |
| `max_wall_s` | `900.0` | `900.0` | before every effect, per execution session |
| `max_nodes` | `64` | `64` | plan validation |
| `max_depth` | `3` | `3` | sub-agent creation |
| `max_concurrency` | `8` | `8` | executor semaphore |

Limits are checked before an effect starts, so the call that crosses a limit completes and the next one fails with `budget_exceeded`, which fails the run. Cost only accumulates for priced models (see [models.md](models.md#cost-accounting)); with no prices configured, `max_cost_usd` never triggers. Usage (including planner usage) is rebuilt from the journal, so resuming does not reset it.

Other bounds: `map.max_items` (default 100) and `max_parallel` (4), `loop.max_iterations` (5, at most 50), node `timeout_s`, tool `timeout_s` (default 60 s), model router per-endpoint `requests_per_second`, MCP message size, `http.fetch` `max_chars` (default 20,000), `fs.read` `max_bytes` (default 200,000).

## API authentication and rate limits

`src/cairn/api/app.py`:

* API keys come from the environment variable named by `api.api_keys_env` (default `CAIRN_API_KEYS`, comma-separated) or `create_app(..., api_keys=[...])`. Clients send `Authorization: Bearer <key>` or `X-API-Key: <key>`; anything else is 401.
* With no keys configured, only clients whose address is `127.0.0.1`, `::1`, `localhost` or `testclient` are accepted (403 otherwise).
* Every identity (`key:<index>` or `ip:<address>`) has a token bucket of `api.requests_per_second` (default 5) with burst `api.burst` (default 20); excess requests get 429.

Gaps to account for in deployment:

* `GET /health` and `GET /` (dashboard HTML) are not authenticated; `/health` reveals model names, the tool count and the number of active runs.
* The WebSocket endpoint checks `?key=` only when keys are configured, is not rate limited, and when no keys are configured it accepts any client address (no loopback check). Keys in query strings can end up in proxy logs.
* Behind a reverse proxy on the same host every client appears as `127.0.0.1`, which defeats the loopback-only default. Always configure keys behind a proxy.
* Key comparison is a plain list membership test, not constant time.
* Any authenticated client can approve any pending request (there is no separation between the client that started a run and the approver), and the recorded approver is the key index.
* A plan submitted with `POST /v1/runs` runs with grants `["*"]`: it may call every registered tool. Goal-based runs use the agent's `tools` (default `config.tools`). Register only tools you are willing to expose.
* No TLS termination is built in; put the API behind a TLS proxy.

## Journal tamper evidence

Events are hash-chained per run (see [durability.md](durability.md#hash-chain)); `cairn verify <run_id>` exits 2 on any modified, reordered, inserted or deleted event within the chain. What it does not provide:

* Truncation of the newest events leaves a valid shorter chain.
* Deleting a whole run, or the `runs` row and its `summary`, is not detected (the `runs` table is not covered by the chain).
* Anyone with write access to the database can rewrite events and recompute all following hashes. There is no signing key or external anchor; export and store the last hash elsewhere if you need that.

## Production hardening checklist

* [ ] Run Cairn in a container or VM without network egress except what `network_allow` needs; mount only the sandbox roots writable.
* [ ] Keep `code.python` out of `tools` unless needed; if needed, isolate the host as above and keep approvals on.
* [ ] Set `CAIRN_API_KEYS` (or your `api_keys_env`) and terminate TLS in front of the API; do not rely on the loopback default behind a proxy; do not expose `/v1/runs/{id}/ws` publicly.
* [ ] Keep `policy.enabled = true`. Use `policy.strict = true` for unattended workers and batch jobs.
* [ ] Keep `allow_private_network = false`; keep `network_allow` minimal and specific (avoid `*`).
* [ ] Review every custom tool's `effects`, `sensitive` parameters, `output_trust`, `secrets` and `output_secrecy`; mark recipients, paths, URLs, commands, payees and amounts sensitive.
* [ ] Give agents least-privilege `tools` patterns; avoid `*`.
* [ ] Provide secrets only through `CAIRN_SECRET_*` or a `SecretVault`, never in plans, inputs or `cairn.toml`.
* [ ] Mount MCP servers as `untrusted`, set `allowed_tools`, `effects_override`, `sensitive_params` and `requires_approval` in code, pin fingerprints with `pin_report`, and set `quarantine_unpinned=True` after the first review.
* [ ] Set explicit budgets, including `max_cost_usd`, and configure model prices so cost limits work.
* [ ] Keep `isolate_untrusted_context = true` for agents.
* [ ] Periodically run `cairn verify` on important runs and store their final hashes outside the database.
* [ ] Back up `<data_dir>/cairn.db`; it holds journals, memory and the job queue.
* [ ] Purge memory written by a compromised run with `cairn memory forget <run_id>`.
