# Configuration reference

Configuration is a TOML file plus environment variables, validated up front by pydantic models in `src/cairn/config/settings.py`. API keys never go in the file: models name the environment variable that holds their key.

## Resolution

`load_config(path=None, env=None)`:

1. The file is `path` (CLI `--config`, which must come before the subcommand: `cairn --config my.toml run ...`), else `$CAIRN_CONFIG`, else `./cairn.toml` if it exists. With none of these, built-in defaults are used. A named file that does not exist, or invalid TOML, raises `ConfigError`.
2. Settings may sit at the top level or under a `[cairn]` table (if a `cairn` table exists, only its contents are used).
3. Environment overrides are applied: `CAIRN_DATA_DIR`, `CAIRN_LOG_LEVEL`, `CAIRN_NETWORK_ALLOW`, `CAIRN_POLICY_STRICT`.
4. The result is validated; problems are reported together, for example `invalid configuration:\n  policy.strict: Input should be a valid boolean`.
5. If no models are configured, `auto_models(env)` infers them from the environment (see [models.md](models.md#auto-detection-from-the-environment)).
6. If any model names an `api_key_env` that is unset, `ConfigError: models reference unset environment variables: [...]`.

`cairn init [--force]` writes a starter `cairn.toml` and creates `./workspace`; `cairn doctor` validates the configuration and reports models, optional dependencies, storage, tools and the network allowlist.

## Top-level settings (`CairnConfig`)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `data_dir` | string | `".cairn"` | Directory of `cairn.db` (journal, memory, job queue). Env `CAIRN_DATA_DIR` |
| `tools` | list of globs | `["math.*", "fs.*", "http.fetch", "comms.*"]` | Built-in tools to register, and the default agent's grants |
| `models` | array of tables | `[]` | See `[[models]]` |
| `collections` | array of tables | `[]` | See `[[collections]]` |
| `mcp_servers` | array of tables | `[]` | See `[[mcp_servers]]` |
| `policy` | table | see below | |
| `security` | table | see below | |
| `budget` | table | see below | |
| `api` | table | see below | |
| `memory_enabled` | bool | `true` | Create the SQLite-backed `MemoryManager` |
| `log_level` | string | `"INFO"` | Env `CAIRN_LOG_LEVEL`. Stored but not applied by the CLI or SDK (see [observability.md](observability.md#logging)) |
| `json_logs` | bool | `false` | Stored but not applied |

Unknown keys are ignored.

## `[[models]]` (`ModelConfig`)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | Model name sent to the provider |
| `provider` | `"anthropic"`, `"openai-compatible"`, `"ollama"`, `"scripted"` | required | `scripted` is rejected at startup (register it in code) |
| `tier` | `"fast"`, `"balanced"`, `"frontier"` | `"balanced"` | |
| `base_url` | string | `null` | Must start with `http://` or `https://`; required for `openai-compatible` and `ollama` |
| `api_key_env` | string | `null` | Name of the env var holding the key |
| `capabilities` | list | `["text", "json"]` | Router capability filter: `vision`, `audio`, `documents`, `json` are what requests ask for |
| `context_window` | int | `128000` | Descriptive |
| `input_price_per_mtok`, `output_price_per_mtok` | float | `null` | USD per million tokens; unknown prices make the call unpriced |
| `requests_per_second` | float | `null` | Per-endpoint token-bucket rate limit |
| `local` | bool | `false` | Preferred at equal tier and price |

## `[[collections]]` (`CollectionConfig`)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `name` | string | required | Corpus name used by `retrieve` nodes |
| `path` | string | `null` | Directory loaded at startup (`*.md`, `*.txt`, recursive) |
| `trusted` | bool | `false` | Label of retrieval results |

## `[[mcp_servers]]` (`MCPServerEntry`)

| Key | Type | Default |
|---|---|---|
| `name` | string | required (must also match `^[A-Za-z0-9_\-]{1,64}$` when mounted) |
| `command` | string | required |
| `args` | list | `[]` |
| `env` | table | `{}` |
| `trust` | `"trusted"` or `"untrusted"` | `"untrusted"` |
| `allowed_tools` | list of globs | `["*"]` |

Other MCP options (`pinned`, `effects_override`, `sensitive_params`, `requires_approval`, `quarantine_unpinned`, `cwd`, timeouts, `max_description_chars`) are not read from the file; set them through `Cairn.mount_mcp` in code (see [tools-and-mcp.md](tools-and-mcp.md#mounting-mcp-servers-client-side)).

## `[policy]` (`PolicyConfig`)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `enabled` | bool | `true` | Provenance rules; capability grants are always enforced |
| `strict` | bool | `false` | Every `require_approval` becomes `deny`. Env `CAIRN_POLICY_STRICT`: `1`, `true`, `yes` (case-insensitive) mean true, any other non-empty value false |

## `[security]` (`SecurityConfig`)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `sandbox_roots` | list | `["./workspace"]` | Filesystem roots for `fs.*` tools; created at startup; the first root resolves relative paths |
| `sandbox_read_only` | bool | `false` | Refuse writes |
| `network_allow` | list of host globs | `[]` | Hosts `http.*` tools may reach; empty allows none. Env `CAIRN_NETWORK_ALLOW` (comma-separated) |
| `allow_private_network` | bool | `false` | Skip the private-address (SSRF) check |

## `[budget]` (`BudgetConfig`)

Used by `Cairn.budget()`: the default agent, `Cairn.run` and API plan runs.

| Key | Type | Default |
|---|---|---|
| `max_cost_usd` | float or null | `1.0` |
| `max_tokens` | int or null | `200000` |
| `max_model_calls` | int or null | `100` |
| `max_tool_calls` | int or null | `100` |
| `max_wall_s` | float or null | `900.0` |
| `max_nodes` | int | `64` |
| `max_depth` | int | `3` |
| `max_concurrency` | int | `8` |

TOML has no null; omit a key to keep its default. A `Budget()` built in Python without configuration has different defaults (no cost limit, 200 model and tool calls); see [security.md](security.md#resource-exhaustion).

## `[api]` (`APIConfig`)

| Key | Type | Default | Meaning |
|---|---|---|---|
| `host` | string | `"127.0.0.1"` | `cairn serve` bind address (`--host` overrides) |
| `port` | int | `8787` | `--port` overrides |
| `api_keys_env` | string | `"CAIRN_API_KEYS"` | Env var with comma-separated API keys |
| `requests_per_second` | float | `5.0` | Per-identity rate |
| `burst` | int | `20` | Bucket capacity |

## Environment variables

| Variable | Used by | Effect |
|---|---|---|
| `CAIRN_CONFIG` | `load_config` | Config file path |
| `CAIRN_DATA_DIR` | `load_config` | Overrides `data_dir` |
| `CAIRN_LOG_LEVEL` | `load_config`, CLI | Overrides `log_level` in the config; the CLI uses it directly for logging (default `WARNING`) |
| `CAIRN_JSON_LOGS` | CLI | Any non-empty value enables JSON logs |
| `CAIRN_NETWORK_ALLOW` | `load_config` | Overrides `security.network_allow` |
| `CAIRN_POLICY_STRICT` | `load_config` | Overrides `policy.strict` |
| `CAIRN_API_KEYS` (or the name in `api.api_keys_env`) | API | API keys |
| `CAIRN_SECRET_<NAME>` | `SecretVault.from_env` | Secret `<NAME>` for tools that declare it |
| `ANTHROPIC_API_KEY` | `auto_models`, Anthropic provider | Adds the three Claude models when no models are configured |
| `OPENAI_API_KEY`, `CAIRN_OPENAI_MODEL`, `CAIRN_OPENAI_TIER`, `OPENAI_BASE_URL` | `auto_models` | Adds one OpenAI-compatible model |
| `CAIRN_OLLAMA_MODEL`, `CAIRN_OLLAMA_TIER`, `OLLAMA_HOST` | `auto_models` | Adds one local Ollama model |
| `CAIRN_MCP_LOG_LEVEL` | `run_stdio_server` | Log level for that entry point |
| `NO_COLOR` | CLI | Disable colors |
| `USER` | `cairn approve` | Default approver name when `--by` is not given |

## Complete example

```toml
[cairn]
data_dir = ".cairn"
tools = ["math.*", "fs.*", "http.fetch", "comms.*"]
memory_enabled = true

[cairn.policy]
enabled = true
strict = false

[cairn.security]
sandbox_roots = ["./workspace"]
sandbox_read_only = false
network_allow = ["*.wikipedia.org", "api.github.com"]
allow_private_network = false

[cairn.budget]
max_cost_usd = 1.0
max_tokens = 200000
max_model_calls = 100
max_tool_calls = 100
max_wall_s = 900
max_nodes = 64
max_depth = 3
max_concurrency = 8

[cairn.api]
host = "127.0.0.1"
port = 8787
api_keys_env = "CAIRN_API_KEYS"
requests_per_second = 5.0
burst = 20

[[cairn.models]]
name = "llama3.1:8b"
provider = "ollama"
base_url = "http://localhost:11434/v1"
tier = "fast"
local = true
input_price_per_mtok = 0.0
output_price_per_mtok = 0.0

[[cairn.collections]]
name = "handbook"
path = "./handbook"
trusted = true

[[cairn.mcp_servers]]
name = "files"
command = "npx"
args = ["-y", "@modelcontextprotocol/server-filesystem", "./shared"]
trust = "untrusted"
allowed_tools = ["read_*", "list_*"]
```

Declaring any `[[models]]` disables auto detection; add the Claude models explicitly if you want both.
