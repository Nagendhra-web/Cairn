# Observability

Everything observable about a run is a projection of its journal. There is no separate instrumentation path: reports, traces, the terminal view, the HTML timeline and OTLP exports are all computed from the same events the executor wrote (`src/cairn/observability/`).

## Run reports

`run_report(run_id, events) -> dict` (`observability/trace.py`), also `Cairn.report(run_id)`, `cairn show --json`, and `GET /v1/runs/{id}`:

| Field | Meaning |
|---|---|
| `run_id`, `goal`, `status`, `agent`, `parent_run_id` | From the folded state |
| `children` | Child run ids linked by `agent` nodes |
| `plan_label` | Description of the run's plan label, for example `trusted from user` |
| `output`, `output_label`, `output_trusted` | Final output, its label description, and whether it is trusted |
| `error` | Run error payload (`code`, `message`, `details`, `retryable`, `node_id`) |
| `duration_s` | From the first `run.started` to the terminal event |
| `usage` | `Usage.to_dict()`: `input_tokens`, `output_tokens`, `tokens`, `cost_usd`, `unpriced_calls`, `model_calls`, `tool_calls`, `retrieval_calls`, `memory_ops`, `replayed_effects`, `by_model` (per model name, plus `planner` for planning usage) |
| `planning` | The agent's planning metadata from `run.created` (`attempts`, `usage`, `context`, `repairs`), or `null` |
| `forked_from` | Source run id for forks |
| `nodes` | Per node: `status`, `attempts`, `duration_ms`, `label` (output label description), `control` (control label description), `error`, and live `tokens`, `cost_usd`, `model_calls`, `tool_calls` |
| `policy_decisions` | Every non-`allow` decision: `node_id`, `tool`, `verdict`, `rule`, `reason` |
| `approvals` | `node_id`, `request_id`, `reason`, `status` |
| `retries` | `node_id`, `attempt`, `reason`, `delay_s` |
| `verifications` | `node_id`, `target`, `round`, `passed`, `issues` |
| `divergences` | `node_id`, `key` of `effect.diverged` events |
| `injection_signals` | `node_id` and the signals recorded on untrusted tool outputs |
| `events` | Event count |
| `trace` | Span tree (below) |

Per-node token and cost counters exclude replayed effects; the run-level `usage` counts replayed effects in `replayed_effects` and does not charge them.

## Trace spans

`build_trace(run_id, events) -> Span` builds a three-level tree:

* **run** span: id and name = run id, attributes `goal`, `agent`; ends at the terminal or `run.suspended` event with status `completed`, `failed`, `cancelled` or `suspended`.
* **node** spans: one per attempt, id `<node>@<attempt>`, attributes `attempt`, `strategy`; status `ok`, `error` (with `error` attribute), `waiting`, `skipped`, or `interrupted` when a new attempt of the same node started before this one ended (the process died).
* **effect** spans: one per `effect.completed`/`effect.failed`, named `tool <name>`, `model <model>` or the kind; start = event time minus `latency_ms`; status `ok` or `error`; attributes `kind`, `key`, `usage`, `cost_usd`, `ttft_ms`, `tier`, `replayed`, `injection_signals`, and `model` or `tool`.

`Span.to_dict()` adds `duration_ms`.

## CLI views

All read the journal of `<data_dir>/cairn.db`:

* `cairn runs [--status S] [--limit 20] [--json]`: one line per run with status, tokens, cost and goal (from the `runs` row summary).
* `cairn show <run_id>` renders the text tree; `--json` prints the full report; `--html FILE` writes the HTML timeline.

  ```text
  run run_637794ec7e8741ae8e85  [completed]  add
    agent=cairn  plan label: trusted from user
    tokens=61 cost=$0.000000 (+1 unpriced) model_calls=1 tool_calls=1 replayed=0 duration=0.0049s

    ok   calc  attempt 1  1.9ms  [trusted from tool:math.calculate,user]
         - tool math.calculate  0.5ms
    ok   say  attempt 1  1.5ms  [trusted from llm:fast,tool:math.calculate,user]
         - model s-fast  0.5ms  57+4 tok

    output (trusted from llm:fast,tool:math.calculate,user):
      The result is 5.
  ```

  Node status markers: `ok`, `FAIL`, `skip`, `WAIT`, `run`, `...` (pending), `INTR` (interrupted). Effect lines show token counts, `replayed` and `injection signals` when present; a failed node shows `! <code>: <message>`. Then lists of policy decisions, approvals, retries, verifications and divergences, the error, and up to 40 lines of output with its label. Colors are used on a TTY unless `NO_COLOR` is set.
* `cairn events <run_id> [--type PREFIX] [--json]`: the raw journal, one line per event (`seq type node_id data...`, data truncated to 150 characters), or full JSON objects; `--type policy` keeps `policy.decision` events.
* `cairn trace <run_id> [--otlp] [--out FILE]`: the span tree as JSON, or OTLP/JSON with `--otlp`.
* `cairn verify <run_id>`: the hash chain check (see [durability.md](durability.md#hash-chain)).
* `cairn approvals <run_id>`: approval requests with status and, for pending ones, the preview.

## HTML timeline

`render_html(report)` (`cairn show --html run.html`, `GET /v1/runs/{id}/html`) is a self-contained page with no external assets: a row per node attempt and per effect with a bar positioned on the run's time axis (colors for ok, error, waiting, skipped, effect), the node's label, and the full report (minus the trace) as escaped JSON. It follows the system light or dark preference.

## OTLP export

`to_otlp(root, service_name="cairn")` converts a span tree to OTLP/JSON (`resourceSpans` / `scopeSpans` / `spans`), which can be POSTed to a collector's `/v1/traces` endpoint (Jaeger, Tempo, Honeycomb and others accept it). Cairn does not send it itself and does not depend on the OpenTelemetry SDK.

* `traceId`: first 16 bytes of SHA-256 of the run id; `spanId`: first 8 bytes of SHA-256 of `<run id>/<span id>`; deterministic, so re-exporting a run yields the same ids.
* `kind` is `1` (internal) for every span; times are Unix nanoseconds as strings.
* `status.code` is `2` (error) for spans whose status is `error`, else `1` (ok). Only node and effect spans use `error`; the run span's status is `completed`, `failed`, `cancelled` or `suspended`, so a failed run's root span is exported with code 1.
* Attributes are `cairn.<name>` string values (non-strings JSON-encoded), including `cairn.kind` and `cairn.status`.

```sh
cairn trace run_... --otlp --out trace.json
curl -X POST -H 'Content-Type: application/json' --data @trace.json http://localhost:4318/v1/traces
```

## Live streams

Journal events are durable; token deltas are not. The API's SSE and WebSocket endpoints merge both (see [api.md](api.md#streaming)). `Services.live` is a `LiveBus`; `runtime.services.live.subscribe(run_id)` yields `{"type": "token", "node_id", "text"}` messages while an `llm` node streams. Providers stream only when there is a subscriber.

## Logging

Loggers live under `cairn` (`cairn.runtime`, `cairn.tools`, `cairn.agents`, `cairn.mcp.client`, `cairn.mcp.server`, `cairn.mcp.server.<name>` for MCP server stderr, `cairn.workers`, `cairn.api`).

`configure_logging(level="INFO", json_logs=False)` installs one stderr handler on the `cairn` logger (replacing existing ones, `propagate=False`). With `json_logs`, `JsonFormatter` emits one JSON object per line with `ts`, `level`, `logger`, `msg`, any of `run_id`, `node_id`, `job_id`, `worker_id` passed in `extra`, and `exc` for exceptions. Other `extra` fields (for example a worker's `job_type`, `attempt`, `error`) are not included by the JSON formatter.

The `cairn` CLI calls `configure_logging` with `CAIRN_LOG_LEVEL` (default `WARNING`) and JSON output when `CAIRN_JSON_LOGS` is set to any non-empty value. The `log_level` and `json_logs` fields of `cairn.toml` (and `CAIRN_LOG_LEVEL` as a config override) are validated and stored in `CairnConfig` but nothing applies them; call `configure_logging(config.log_level, config.json_logs)` yourself when embedding Cairn. `run_stdio_server` configures root logging from `CAIRN_MCP_LOG_LEVEL` (default `WARNING`) on stderr.

`cairn serve` passes `log_level="info"` to uvicorn.
