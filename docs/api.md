# HTTP API

`src/cairn/api/app.py` builds a FastAPI application around a `Cairn` instance. Install the extra with `pip install 'cairn-runtime[server]'` and start it with `cairn serve [--host H] [--port P]` (defaults from `[api]`: `127.0.0.1:8787`), or embed it:

```python
from cairn.api import create_app
from cairn.sdk import Cairn

cairn = await Cairn.create()
app = create_app(cairn, api_keys=["..."])   # api_keys=None reads the env var named by api.api_keys_env
```

Runs started over HTTP execute as background tasks in the API process. On shutdown the app cancels in-flight executions; their journals keep them resumable.

## Authentication and rate limits

* Keys: comma-separated in the environment variable named by `api.api_keys_env` (default `CAIRN_API_KEYS`), or `create_app(..., api_keys=[...])`.
* Send `Authorization: Bearer <key>` or `X-API-Key: <key>`. Missing or wrong key: `401 {"detail": "missing or invalid API key"}`.
* No keys configured: only clients at `127.0.0.1`, `::1`, `localhost` or `testclient` are served; others get `403 {"detail": "no API keys configured; only loopback clients are allowed"}`.
* Each identity (`key:<index>` or `ip:<address>`) has a token bucket with `api.requests_per_second` (default 5) and burst `api.burst` (default 20). Excess: `429 {"detail": "rate limit exceeded"}`.
* `GET /health` and `GET /` (the static dashboard page) are unauthenticated; `/health` returns only `{"status": "ok"}`. The dashboard's data calls go to authenticated endpoints.
* The WebSocket endpoint applies the same rules with its own close codes: a missing or wrong `?key=` closes with 4401 when keys are configured; a non-loopback client closes with 4403 when none are; an exhausted rate limit closes with 4429. Each connection attempt consumes one token from the identity's bucket.

## Errors

| Source | Status | Body |
|---|---|---|
| `NotFound` | 404 | `{"error": {"code": "not_found", "message", "details"}}` |
| `PolicyViolation` | 403 | `{"error": {...}}` |
| `PlanValidationError` | 422 | `{"error": {"code": "plan_invalid", "message", "details": {"problems": [...]}}}` |
| `ConfigError` | 400 | `{"error": {...}}` (for example no models configured) |
| `BudgetExceeded` | 429 | `{"error": {...}}` |
| `ApprovalRequired` | 409 | `{"error": {...}}` |
| other `CairnError` | 400 | `{"error": {...}}` |
| pydantic `ValidationError` (for example a `plan` that does not parse as Plan IR) | 422 | `{"error": {"code": "invalid_request", "message": "request failed validation", "details": {"problems": [...]}}}` (at most 20 problems) |
| explicit checks | 401, 403, 404, 422, 429 | `{"detail": "..."}` |
| request body that does not match the endpoint's model | 422 | FastAPI validation error |

A plan with an unknown node kind:

```json
{"error": {"code": "invalid_request", "message": "request failed validation",
           "details": {"problems": ["nodes.0: Input tag 'nope' found using 'kind' does not match any of the expected tags: 'tool', 'llm', ..."]}}}
```

A plan that parses but fails static validation returns 422 with `plan_invalid` as above.

## Endpoints

### `GET /health`

No authentication; reveals nothing about the deployment.

```json
{"status": "ok"}
```

### `GET /v1/info`

Authenticated. Registered model names, the number of registered (non-quarantined) tools, and the number of background tasks running in this API process.

```json
{"models": ["claude-haiku-4-5", "claude-sonnet-5-5", "claude-opus-5-5"], "tools": 6, "active_runs": 0}
```

### `GET /v1/runs?status=&limit=50`

Run records, newest first; `limit` is capped at 500.

```json
[{"run_id": "run_0b51e04770dc4e7c9ce9", "goal": "w", "status": "completed",
  "created_at": 1790956492.80, "updated_at": 1790956493.11, "parent_run_id": null, "agent": null, "tags": [],
  "summary": {"tokens": 0, "cost_usd": 0.0, "model_calls": 0, "tool_calls": 0, "duration_s": 0.3085,
              "nodes": {"p": "completed"}, "...": "..."}}]
```

### `POST /v1/runs` (202)

Body (`StartRun`): exactly one of `goal` or `plan`, plus `inputs` (object, default `{}`) and `agent` (object of `AgentSpec` field overrides, goal runs only).

Plan run: the plan is validated and executed with `cairn.budget()` (the `[budget]` configuration) and grants `cairn.grants()`: the patterns in `config.tools`, plus `<name>.*` for each configured MCP server, plus the names of tools registered in code (`register_tool`, `Cairn.create(tools=...)`). With the default configuration that is `["math.*", "fs.*", "http.fetch", "comms.*"]`. A plan that uses a tool outside these patterns fails validation with `uses tool '...' which is not granted`.

```json
{"plan": {"goal": "calc", "nodes": [{"id": "c", "kind": "tool", "tool": "math.calculate", "args": {"expression": "6*7"}}],
          "output": {"$ref": "c"}},
 "inputs": {}}
```

Goal run: requires configured models; runs `cairn.agent(**agent).run(goal, inputs=..., run_id=...)`. The returned `run_id` is the first attempt's run; a replan creates further runs (listed in the agent result, not returned here).

```json
{"goal": "Summarize workspace/notes.md", "agent": {"tools": ["fs.read", "math.*"], "max_replans": 0}}
```

Response:

```json
{"run_id": "run_9047975c03ed4182a4ed", "status": "accepted"}
```

Errors: `422 {"detail": "provide exactly one of 'goal' or 'plan'"}`; `422` with `plan_invalid` problems; `400 config_error` when a goal is given and no models are configured.

### `GET /v1/runs/{run_id}`

The full run report (see [observability.md](observability.md#run-reports)): `run_id`, `goal`, `status`, `agent`, `parent_run_id`, `children`, `plan_label`, `output`, `output_label`, `output_trusted`, `error`, `duration_s`, `usage`, `planning`, `forked_from`, `nodes`, `policy_decisions`, `approvals`, `retries`, `verifications`, `divergences`, `injection_signals`, `events`, `trace`. 404 for unknown runs.

### `GET /v1/runs/{run_id}/html`

The self-contained HTML timeline.

### `GET /v1/runs/{run_id}/events?after=0`

Journal events with `seq > after`:

```json
[{"run_id": "run_9047975c03ed4182a4ed", "seq": 4, "type": "policy.decision", "node_id": "c", "ts": 1790955584.16,
  "data": {"tool": "math.calculate", "verdict": "allow", "rule": "default", "reason": "no rule objected", "details": {},
           "arg_labels": {"expression": {"integrity": "trusted", "sources": ["user"], "secrecy": []}},
           "control": {"integrity": "trusted", "sources": ["user"], "secrecy": []}},
  "prev_hash": "...", "hash": "..."}]
```

### Streaming

`GET /v1/runs/{run_id}/stream?after=0` is a server-sent event stream (`text/event-stream`, `Cache-Control: no-cache`). It replays journal events after `after`, then polls the journal and forwards live token messages, until it reads a terminal or `run.suspended` event and the run is no longer `running`; then it sends `end`.

```text
event: event
data: {"kind": "event", "run_id": "run_...", "seq": 1, "type": "run.created", "node_id": null, "ts": ..., "data": {...}, "prev_hash": "000...", "hash": "b44b..."}

event: live
data: {"kind": "live", "type": "token", "node_id": "summary", "text": "Acme sells"}

event: end
data: {}
```

`event` items are durable journal events (resume a dropped stream with `after=<last seq>`); `live` items are ephemeral token deltas, sent only while an `llm` node streams. 404 if the run does not exist.

`WS /v1/runs/{run_id}/ws?key=<api key>&after=0` sends the same items as JSON text frames (`{"kind": "event", ...}` and `{"kind": "live", ...}`), then `{"kind": "end"}`, and closes. Before accepting, it closes with 4401 (keys configured, missing or wrong `key`), 4403 (no keys configured and a non-loopback client) or 4429 (rate limit exceeded).

### `GET /v1/runs/{run_id}/approvals`

All approval requests of the run:

```json
[{"request_id": "apr_9fdbc53e4c3bce92c2d4", "node_id": "p", "status": "pending", "reason": "Proceed?",
  "preview": {"labels": {}, "message": "Proceed?", "show": {}}, "decided_by": null, "note": null}]
```

For tool calls the preview holds `tool`, `args` (secrets redacted), `arg_labels` (descriptions) and `effects`.

### `POST /v1/runs/{run_id}/approvals/{request_id}`

Body `{"approved": true, "note": "ok"}` (`note` optional). Records the decision with `by` = the caller's identity (`key:0`, `ip:127.0.0.1`) and resumes the run in the background.

```json
{"run_id": "run_0b51e04770dc4e7c9ce9", "request_id": "apr_9fdbc53e4c3bce92c2d4", "approved": true, "resuming": true}
```

Deciding twice: `400 {"error": {"code": "cairn_error", "message": "approval 'apr_...' was already approved"}}`. Unknown request: 404.

### `POST /v1/runs/{run_id}/resume` (202)

```json
{"run_id": "run_...", "status": "resuming"}
```

Resuming a terminal run is a no-op.

### `POST /v1/runs/{run_id}/cancel`

```json
{"run_id": "run_...", "cancelled": false}
```

`true` if the run was executing in this process (cancel signaled) or was not terminal (a `run.cancelled` event was appended); `false` if it had already finished. See [durability.md](durability.md#cancellation).

### `POST /v1/runs/{run_id}/replay`

Strict replay, executed within the request:

```json
{"source_run_id": "run_9047975c03ed4182a4ed", "replay_run_id": "replay_c9079301ac59467caef1", "matched": true,
 "output_equal": true, "status": "completed", "divergences": [], "effects_replayed": 1, "error": null}
```

### `POST /v1/runs/{run_id}/fork`

Body `{"patches": {"<node_id>": {"<field>": <value>}}, "invalidate": ["<node_id>"]}` (both optional). Creates and executes the fork within the request and returns its `RunResult`:

```json
{"run_id": "run_73d443bfddc844dabd24", "status": "completed", "output": 2,
 "label": {"integrity": "trusted", "sources": ["tool:math.calculate", "user"], "secrecy": []},
 "error": null, "usage": {"tool_calls": 1, "replayed_effects": 0, "divergences": 1, "...": "..."},
 "pending_approvals": [], "nodes": {"c": "completed"}}
```

`usage.divergences` appears when some recorded effect's request changed.

### `GET /v1/tools?q=`

All registered, non-quarantined tools, or the top 20 discovery matches for `q`:

```json
[{"name": "math.calculate", "description": "Evaluate an arithmetic expression such as '(3 + 4) * sqrt(16)'.",
  "params": {"expression": {"type": "string", "description": "arithmetic using + - * / // % ** and sqrt, log, exp, min, max, round"}},
  "required": ["expression"], "effects": [], "returns": null,
  "output_trust": "inherit", "sensitive": [], "source": "local"}]
```

### `GET /v1/memory?kind=`

Every stored memory record (including superseded ones), optionally filtered by kind. 404 `{"detail": "memory disabled"}` when memory is off.

### `POST /v1/memory/search`

Body `{"query": "...", "kind": "any", "k": 10}` (`k` 1..100). Returns recall results (records plus `score` and `score_parts`). This is a real recall: returned records' access counts and recency are updated.

### `DELETE /v1/memory/{memory_id}`

```json
{"deleted": true}
```

### `GET /`

The bundled dashboard (`api/static/dashboard.html`), a static page that calls `/v1/info`, `/v1/runs`, `/v1/runs/{id}/html`, `/v1/runs/{id}/stream`, `/v1/runs/{id}/approvals` and `/v1/runs/{id}/replay`.

## Not available over HTTP

Enqueueing runs for workers (`cairn submit`, `Cairn.submit`), mounting MCP servers, pinning or consolidating memory, journal verification and listing quarantined tools are available through the CLI or Python only.
