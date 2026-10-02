# Tools and MCP

A tool in Cairn is a capability with a contract: an input schema, declared side effects, the parameters that are security-sensitive sinks, how trustworthy its output is, and which secrets it needs. The runtime uses these declarations for plan validation, policy checks, approval prompts, discovery and replay fingerprints.

Code: `src/cairn/tools/` and `src/cairn/mcp/`.

## The `@tool` decorator

```python
tool(
    fn=None, *,
    name: str | None = None,              # default: function name
    description: str | None = None,       # default: first paragraph of the docstring
    effects: Iterable[str] = (),
    sensitive: Iterable[str] = (),        # parameter names; must exist
    output_trust: OutputTrust | str = "inherit",
    output_secrecy: Iterable[str] = (),
    allowed_secrecy: Iterable[str] = (),
    secrets: Iterable[str] = (),
    requires_approval: bool = False,
    timeout_s: float = 60.0,
    idempotent: bool = False,
    tags: Iterable[str] = (),
    version: str = "1",
) -> ToolSpec
```

Use it bare (`@tool`) or with arguments. It returns a `ToolSpec`, not a function.

* The input schema is derived from type hints with pydantic. Parameters without defaults are `required`. `*args`/`**kwargs` are ignored.
* A parameter named `ctx` is excluded from the schema; the runtime passes a `ToolContext` in it.
* Parameter descriptions come from an `Args:` (or `Arguments:`, `Parameters:`) section of the docstring.
* The output schema comes from the return annotation when pydantic can express it.
* Naming a `sensitive` parameter that does not exist raises `ValueError` at decoration time.
* Sync functions run in a worker thread (`asyncio.to_thread`); async functions are awaited. Both are bounded by `timeout_s`.

## ToolSpec fields

| Field | Default (dataclass) | Meaning |
|---|---|---|
| `name` | required | Registry key; dotted names (`crm.update_contact`) are conventional |
| `description` | required | Shown to planners and MCP clients |
| `input_schema` | required | JSON schema used for validation and the catalog |
| `fn` | required | The implementation |
| `effects` | `frozenset()` | Subset of `Effect`: `read`, `write`, `delete`, `network`, `send`, `execute`, `payment`. `write`, `send`, `execute`, `delete`, `payment` are privileged; `send` and `network` are egress |
| `sensitive_params` | `frozenset()` | Parameters that must not receive untrusted data without approval |
| `output_trust` | `UNTRUSTED` | `inherit` (pure function of inputs), `trusted` (authoritative internal source or a receipt), `untrusted` (attacker-influenceable content). The decorator defaults to `inherit`; constructing `ToolSpec` directly defaults to `untrusted` |
| `output_secrecy` | `frozenset()` | Secrecy tags added to every output label |
| `allowed_secrecy` | `frozenset()` | Secrecy tags this egress tool may carry out |
| `secrets` | `frozenset()` | Vault secret names the tool may read via `ctx.secrets` |
| `requires_approval` | `False` | Always ask a human |
| `timeout_s` | `60.0` | Per-invocation timeout |
| `idempotent` | `False` | Metadata only (exported as MCP `idempotentHint`); the executor does not use it |
| `tags` | `frozenset()` | Extra words for discovery |
| `version` | `"1"` | Part of the effect fingerprint: bump it when behavior changes so replays report a divergence |
| `source` | `"local"` | `mcp:<server>` for mounted MCP tools |
| `examples` | `[]` | Not used by the runtime |
| `output_schema` | `None` | From the return annotation |
| `wants_context` | `False` | Set when the function has a `ctx` parameter |

Derived members:

* `fingerprint`: hash of name, description, input schema, effects and version (16 hex characters). Used for MCP pinning.
* `catalog_entry()`: compact `{name, description, params, required, effects, returns}` shown to planners.
* `search_text()`: name (dots and underscores as spaces), description, parameter names and descriptions, tags. Indexed for discovery.

## ToolContext

What a tool may see of the runtime when it declares `ctx`:

| Field | Meaning |
|---|---|
| `run_id`, `node_id` | Identity of the call |
| `secrets` | `SecretScope` limited to the declared `secrets`; `get(name)` raises `PolicyViolation` for undeclared or unset names |
| `sandbox` | `PathSandbox` or `None`; use `ctx.sandbox.resolve(path, write=...)` |
| `network` | `NetworkPolicy` or `None`; call `ctx.network.check(url)` before connecting |
| `services` | `Services.tool_services` dict, for your own clients (the built-in email tool uses `mail_transport` and `outbox`) |
| `notes`, `note(msg)` | Free-text notes journaled with the effect |

## Registry

`ToolRegistry`:

* `register(spec, *, replace=False)` (duplicate names raise `ValueError` unless `replace`), `register_all(specs)`, `unregister(name)`.
* `get(name)` raises `NotFound` for unknown tools (with up to three `did_you_mean` suggestions) and for quarantined tools; `peek(name)` returns even quarantined ones; `name in registry` is false for quarantined tools.
* `quarantine(name, reason)`, `release(name)`, `quarantined()`.
* `list(grants=("*",))`: non-quarantined tools matching any grant pattern, sorted by name.
* `discover(query, k=8, grants=("*",))`: ranks granted tools against a task description with a `HybridIndex` over `search_text()` (BM25 plus `HashingEmbedder` vectors, RRF). The planner shows only the top `catalog_k` (12) matches, which keeps prompts small with many tools. Returns `ToolMatch(spec, score, reason)`.
* `invoke(spec, args, ctx)`: validates `args` against the input schema (`tool_arguments_invalid`, non-retryable), injects `ctx`, enforces `timeout_s` (`tool_timeout`, retryable), and wraps any other non-Cairn exception in a retryable `ToolError` (`tool 'x' failed: ValueError: ...`). `CairnError` subclasses raised by a tool (for example `SandboxViolation`, `PolicyViolation`) keep their own code and retryability.

## Built-in tools

`cairn.tools.builtin.builtin_tools(include=("*",))` returns the built-ins matching the patterns. `Cairn.create` registers `builtin_tools(config.tools)`; the default `tools` setting is `["math.*", "fs.*", "http.fetch", "comms.*"]`, so `code.python` and `http.post_json` are not registered by default.

| Tool | Effects | Sensitive | Output trust | Timeout | Parameters | Notes |
|---|---|---|---|---|---|---|
| `math.calculate` | none | none | `inherit` | 60 s | `expression` | AST-based evaluator: numbers, `+ - * / // % **`, unary `+ -`, constants `pi`, `e`, functions `sqrt log exp abs round min max sin cos floor ceil`; exponents above 1000 refused; no `eval`. `idempotent=True` |
| `code.python` | `execute` | `code` | `untrusted` | 60 s | `code`, `timeout_s=10.0`, `memory_mb=512` | Isolated subprocess, see [security.md](security.md#code-execution-isolation-and-its-limits). Returns `{exit_code, stdout, stderr, timed_out}` |
| `fs.read` | `read` | none | `untrusted` | 60 s | `path`, `max_bytes=200000` | Requires a sandbox |
| `fs.list` | `read` | none | `untrusted` | 60 s | `path="."` | Directories end with `/` |
| `fs.write` | `write` | `path`, `content` | `trusted` | 60 s | `path`, `content` | Creates parent directories; returns `{path, bytes}` |
| `http.fetch` | `network` | `url` | `untrusted` | 30 s | `url`, `max_chars=20000` | Requires a `NetworkPolicy`; no redirects followed; HTML converted to text; returns `{url, status, title, text, truncated}` |
| `http.post_json` | `network`, `send` | `url`, `payload` | `untrusted` | 30 s | `url`, `payload` | Returns `{status, body}` (first 5000 characters) |
| `comms.send_email` | `send` | `to` | `trusted` | 60 s | `to`, `subject`, `body` | Uses `tool_services["mail_transport"]` (an async callable returning a message id) if present, else appends to `tool_services["outbox"]`; rejects recipients without `@` or containing CR, LF, `,` or `;` |

The `comms.send_email` module docstring says bodies are sensitive sinks too; the declaration lists only `to`. An attacker-influenced `body` sent to a trusted recipient is therefore allowed without approval (see [provenance.md](provenance.md#what-an-attacker-controlling-a-web-page-can-and-cannot-cause)).

## Adding a tool

```python
from cairn import tool
from cairn.tools import ToolContext

@tool(
    name="crm.update_contact",
    effects={"write", "network"},
    sensitive={"contact_id", "email"},
    output_trust="trusted",
    secrets={"CRM_TOKEN"},
    timeout_s=20.0,
    tags={"crm", "contact"},
    version="2",
)
async def update_contact(contact_id: str, email: str, ctx: ToolContext, note: str = "") -> dict:
    """Update a contact's email address in the CRM.

    Args:
        contact_id: CRM contact identifier
        email: new email address
        note: optional audit note
    """
    token = ctx.secrets.get("CRM_TOKEN")
    ctx.network.check("https://crm.example.com/api/contacts")
    ctx.note(f"PATCH contact {contact_id}")
    # ... call the CRM with token ...
    return {"contact_id": contact_id, "updated": True}
```

The derived schema:

```json
{"properties": {"contact_id": {"type": "string", "description": "CRM contact identifier"},
                "email": {"type": "string", "description": "new email address"},
                "note": {"default": "", "type": "string", "description": "optional audit note"}},
 "required": ["contact_id", "email"], "type": "object"}
```

Register it with `cairn.register_tool(update_contact)` (replaces an existing tool of the same name), `Cairn.create(tools=[update_contact])`, or `registry.register(update_contact)`. Grant it to agents through `AgentSpec.tools` or `config.tools` patterns such as `crm.*`; note that `config.tools` is also used to select built-ins, and non-matching patterns are harmless.

Run with a vault `SecretVault({"CRM_TOKEN": "..."})`, the journaled effect carries `secrets_used: ["CRM_TOKEN"]` and the notes; if the tool returned the token, the journal would show `[REDACTED:CRM_TOKEN]`.

Checklist when declaring a tool:

* every parameter that names a destination, path, URL, command, account, amount or recipient is `sensitive`;
* every side effect is listed in `effects`;
* `output_trust="untrusted"` for anything that returns external content; `trusted` only for authoritative sources and receipts; `inherit` for pure transformations;
* `output_secrecy` for outputs derived from confidential data;
* bump `version` when behavior changes.

## Mounting MCP servers (client side)

Cairn includes its own MCP client over stdio (no MCP SDK dependency). `mount_mcp_server(registry, config)` starts the server, lists its tools and registers each as `<server>.<tool>`; threat model in [security.md](security.md#untrusted-mcp-servers).

### `MCPServerConfig`

| Field | Default | Meaning |
|---|---|---|
| `name` | required | `^[A-Za-z0-9_\-]{1,64}$`; prefix of mounted tool names |
| `command`, `args` | required, `[]` | Server process |
| `env` | `{}` | Extra environment on top of `safe_env()` |
| `cwd` | `None` | Working directory |
| `trust` | `"untrusted"` | `trusted` skips sanitizing, marks outputs trusted, lets read-only annotations narrow effects |
| `allowed_tools` | `["*"]` | Globs over remote or mounted names |
| `effects_override` | `{}` | Remote or mounted name to exact effects (validated against `Effect`) |
| `sensitive_params` | `{}` | Remote or mounted name to sensitive parameter names |
| `pinned` | `{}` | Mounted name to approved fingerprint |
| `quarantine_unpinned` | `False` | Quarantine tools without a pin when other pins exist |
| `requires_approval` | `[]` | Globs of tools that always need approval |
| `timeout_s` | `60.0` | Per-call timeout (the ToolSpec timeout is `timeout_s + 1`) |
| `startup_timeout_s` | `15.0` | Handshake timeout |
| `max_description_chars` | `1000` | Description and schema text cap |

### From configuration

```toml
[[mcp_servers]]
name = "files"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "/srv/shared"]
env = { NODE_ENV = "production" }
trust = "untrusted"
allowed_tools = ["read_*", "list_*"]
```

`Cairn.create` mounts each entry (unless `mount_mcp=False`). Only `name`, `command`, `args`, `env`, `trust` and `allowed_tools` are read from the file; other `MCPServerConfig` fields must be set in code.

### From code

```python
from cairn.mcp import load_pins, pin_report

report = await cairn.mount_mcp({
    "name": "files",
    "command": "npx",
    "args": ["-y", "@modelcontextprotocol/server-filesystem", "/srv/shared"],
    "allowed_tools": ["read_*", "list_*", "write_file"],
    "effects_override": {"write_file": ["write"]},
    "sensitive_params": {"write_file": ["path", "content"]},
    "requires_approval": ["write_file"],
    "pinned": load_pins("mcp-pins.json"),
    "quarantine_unpinned": True,
})
print(report.summary())   # added, quarantined, skipped, signals (injection kinds), unpinned
```

`MountReport` fields: `server`, `client` (keep it open while the tools are used), `added`, `quarantined` (`QuarantinedTool(name, pinned, current, reason)`), `skipped` (`SkippedTool(name, reason)`: invalid name, duplicate, not in `allowed_tools`, collision with another source, unknown sensitive parameter), `signals`, `fingerprints`, `unpinned`, and `usable` (added minus quarantined). `unmount_mcp_server(registry, report)` unregisters the tools and stops the server. `Cairn.close()` closes mounted clients.

Tool results: `structuredContent` when the server provides it, otherwise the concatenated text content (images and audio become `[image content: <mimeType>]`, resource links `[resource: <uri>]`). A result with `isError: true` raises `ToolError`.

### Pin files and the review loop

A pin file is JSON: `{"version": 1, "tools": {"files.read_file": "<fingerprint>", ...}}`.

1. First mount: nothing pinned; review the tools, descriptions and `report.signals`; then `pin_report("mcp-pins.json", report)` writes all current fingerprints (atomically).
2. Later mounts: pass `pinned=load_pins(...)`. A changed definition is registered but quarantined; `cairn tools` lists it as `QUARANTINED: mcp rug-pull: definition changed (pinned X, now Y); re-approve before use`.
3. After reviewing the change: `approve(registry, report, name)` releases it and returns the new fingerprint; `pin_report(path, report, include_quarantined=True)` records the approval. A later mount whose fingerprint matches its pin again releases a previous rug-pull quarantine automatically.

## Serving tools over MCP (server side)

`cairn mcp-serve [--expose GLOB ...] [--allow-privileged]` builds the configured `Cairn` (without mounting MCP servers) and serves its registry over stdio with `MCPServer`. In Python: `run_stdio_server(registry, expose=["math.*"])` or `MCPServer(registry, name="cairn", version="0.1.0", expose=("*",), allow_privileged=False, vault=None, instructions=None, page_size=100).serve_stdio()`.

* Only tools matching `expose` are listed or callable; a hidden tool is indistinguishable from a nonexistent one (`unknown tool: <name>`).
* Without `allow_privileged`, tools with privileged effects or `requires_approval` are withheld, because an MCP client has no channel for Cairn's approval flow.
* Supported methods: `initialize` (negotiates `2025-06-18`, `2025-03-26` or `2024-11-05`, else answers `2025-06-18`), `ping`, `tools/list` (cursor pagination, `page_size` 100), `tools/call`; notifications `notifications/initialized` and `notifications/cancelled` (cancels the in-flight call; cancelled requests get no response).
* Tool definitions include annotations derived from effects: `readOnlyHint` (effects are a subset of `{read}`), `destructiveHint` (any of `write`, `delete`, `payment`, `execute`), `idempotentHint`, `openWorldHint` (`network` or `send`). `outputSchema` is included when the tool's output schema is an object.
* Results: strings become one text content item; dicts also become `structuredContent`; lists become `structuredContent: {"result": [...]}`. Errors are returned as `isError: true` results with a redacted message.
* Each call gets a secret scope limited to the tool's declared secrets; responses are redacted with the vault.
* Stdout is reserved for protocol messages: file descriptor 1 is pointed at stderr while serving.

Limits: calls through the MCP server do not go through the provenance policy or the journal, and no sandbox or network policy is passed to tools, so `fs.*`, `http.fetch` and `http.post_json` are listed (when exposed) but fail with a configuration error. Exposing them over MCP needs a code change.
