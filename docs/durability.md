# Durability, replay and fork

Every run is event sourced. The journal is the only source of truth: run state is rebuilt by folding events, every nondeterministic effect is recorded with a fingerprint of its request, and runs can be resumed after a crash, replayed with zero live calls, or forked with changes.

Code: `src/cairn/journal/`, `src/cairn/runtime/state.py`, `src/cairn/runtime/recorder.py`, `src/cairn/runtime/engine.py`, `src/cairn/runtime/executor.py`, `src/cairn/workers/`.

## Event sourcing

A run is a `runs` row (`RunRecord`: `run_id`, `goal`, `status`, timestamps, `parent_run_id`, `agent`, `tags`, `summary`) plus an append-only list of events. The row is a cache for listing; `Runtime._sync_record` refreshes `status`, `updated_at` and `summary` (usage, node statuses, duration) after each execution. Everything else comes from `fold(run_id, events)` in `runtime/state.py`, which the executor, CLI, API and reports all share.

Appends use optimistic concurrency. `JournalStore.append(run_id, drafts, expected_seq)` fails with `ConcurrentAppend` if another writer appended first. The recorder handles that by reading and folding the other writer's events (for example an operator's approval decision) and retrying, up to 20 times.

## Event types

Defined in `src/cairn/journal/events.py` (`EventType`):

| Type | Emitted when | Main `data` fields |
|---|---|---|
| `run.created` | `Runtime.create_run` | `plan`, `goal`, `inputs`, `budget`, `grants`, `label`, `agent`, `parent_run_id`, `depth`; agents add `planning`, `agent_attempt`; forks add `forked_from`, `patches`, `invalidated` |
| `run.started` | each execution session starts | `mode` (`live` or `strict`) |
| `run.suspended` | nothing can progress and some nodes wait | `waiting` (node ids), `approvals` (request ids) |
| `run.completed` | all nodes terminal | `output`, `label` |
| `run.failed` | a node failed fatally | `error` (includes `node_id`) |
| `run.cancelled` | cancel requested | `reason` |
| `run.forked` | defined, not emitted by current code | |
| `node.started` | each attempt | `attempt`, `strategy`, `control` |
| `node.completed` | attempt succeeded, `on_error="default"`, or a verifier revision | `output`, `label`, `control`, `attempt`; `defaulted`, `error` for defaults |
| `node.failed` | attempt failed | `error`, `attempt`, `final` |
| `node.retrying` | before a retry | `attempt` (next), `delay_s`, `reason` |
| `node.skipped` | condition false, dependency skipped, or `on_error="skip"` | `reason`, `control`, optional `error` |
| `node.waiting` | a node needs an approval | `request_id` |
| `effect.completed` | an effect returned (or was copied from a recording) | `key`, `kind`, `fingerprint`, `request` (bounded preview), `result`, `label`, `latency_ms`, plus kind-specific metadata; `replayed: true` when copied |
| `effect.failed` | an effect raised | `key`, `kind`, `fingerprint`, `error`, `latency_ms`; `replayed` when copied |
| `effect.diverged` | live mode found a recording with a different fingerprint | `key`, `kind`, `expected`, `actual` |
| `policy.decision` | before every tool call | `tool`, `verdict`, `rule`, `reason`, `details`, `arg_labels`, `control` |
| `approval.requested` | first time an approval is needed | `request_id`, `reason`, `rule`, `preview`; `copied_from` when copied |
| `approval.decided` | a decision is recorded | `request_id`, `approved`, `by`, `note`; `copied_from` when copied |
| `verify.result` | each verification round | `target`, `round`, `passed`, `issues`, `score` |
| `route.decision` | model router picked an endpoint (live calls only) | `requested_tier`, `needs`, `chosen`, `attempts` |
| `subrun.linked` | an `agent` node links a child run | `child_run_id` |
| `note` | diagnostics, for example a `when` condition error | `message` |

Kind-specific `effect.completed` metadata: tools add `secrets_used`, `notes` and, for untrusted output, `injection_signals`; models add `usage`, `cost_usd`, `model_latency_ms`, `ttft_ms`, `tier`; retrieval adds `hits`; memory recall adds `count`; sub-agents add `child_run_id`, `child_usage`.

Terminal run events (`TERMINAL_RUN_EVENTS`) are `run.completed`, `run.failed`, `run.cancelled`. `run.suspended` is not terminal.

## Hash chain

Each event is sealed with

```text
hash = sha256(prev_hash + canonical_json({"run_id", "seq", "type", "node_id", "ts", "data"}))
```

where `canonical_json` sorts keys and omits whitespace, and the first event's `prev_hash` is `GENESIS_HASH` (64 zeros). `verify_chain(events)` reports gaps or reordering in `seq`, broken `prev_hash` links and content hash mismatches. `cairn verify <run_id>` prints `journal intact: N events` and exits 0, or prints the problems and exits 2:

```text
seq 1: content hash mismatch (event was modified)
```

What the chain does and does not prove is discussed in [security.md](security.md#journal-tamper-evidence): it detects edits and deletions inside a run's event list, not truncation of the tail, deletion of a whole run, or a rewrite by someone who recomputes every hash.

## Effects, keys and fingerprints

`Recorder.effect(key, kind, request, run, node_id=...)` is the only path for nondeterminism:

```text
record = journal effects of this run, else the source recording (replay/fork)
if record exists:
    if record.fingerprint == stable_hash(request, 24): reuse it (copy into this journal if it came from a source)
    else: record a divergence (strict mode: raise ReplayDivergence)
elif strict mode: raise ReplayDivergence ("not in the recording")
check_budget(...)
outcome = run()                       # live
journal effect.completed (or effect.failed) with the result
return outcome
```

**Effect keys** identify an effect's position in the plan, not its content:

| Context | Key |
|---|---|
| Node attempt | `<node_id>@<attempt>/<kind>#<n>` |
| Map item | `<node_id>@<attempt>[<index>]/<kind>#<n>` |
| Loop iteration | `<node_id>@<attempt>~<iteration>/<kind>#<n>` |
| Verifier re-run of a target | `<target_id>@v<round>/<kind>#<n>` |

`<n>` counts every effect started within the same step (one attempt, map item or loop iteration), starting at 1, whatever its kind. For example a structured LLM call that needed one repair produces `answer@1/model#1` and `answer@1/model#2`.

**Kinds and fingerprinted requests:**

| Kind | Request that is fingerprinted |
|---|---|
| `tool` | `{"tool", "version", "args"}` (resolved arguments) |
| `model` | `{"request": ModelRequest, "tier", "model"}` (full messages, system prompt, schema, `max_tokens`) |
| `retrieval` | `{"collection", "query", "k", "mode"}` |
| `memory.recall` | `{"op": "recall", "query", "kind", "k"}` |
| `memory.write` | `{"op": "remember", "text", "kind"}` |
| `subagent` | `{"goal", "tools", "tier", "instructions"}` |

The fingerprint is `stable_hash(request, length=24)`: SHA-256 of canonical JSON, first 24 hex characters. Because the tool's `version` is part of the request, bumping a tool version changes its fingerprint. The journal stores a bounded preview of the request (`request`, truncated to 2000 characters as `{"truncated": true, "preview": ...}`); the fingerprint always covers the full request.

**What is recorded.** Results are journaled after the effect returns and before the executor sees them. Failures are recorded too (`effect.failed`), and reusing a recorded failure re-raises it with the same `code` and `retryable` flag, so retries and fallbacks follow the same path during replay. Approval requests and cancellation are not outcomes: an effect interrupted by them is not recorded and runs again on resume.

**At-least-once window.** If the process dies after a tool's side effect happened but before `effect.completed` was appended, the effect is executed again on resume. The journal makes completed-and-recorded effects exactly-once; it cannot make an external side effect atomic with the journal write. Tools whose side effects must not repeat should be idempotent on their own terms. `ToolSpec.idempotent` is descriptive metadata (used for MCP annotations); the executor does not act on it.

## Resume

`Runtime.execute(run_id)` (alias `Runtime.resume`) reads and folds the journal and:

* returns the stored result if the run is terminal (`completed`, `failed`, `cancelled`);
* skips nodes that are `completed`, `failed` or `skipped`;
* re-starts nodes that were `running` (interrupted) or `waiting` (approval) **with the same attempt number**, so their effect keys are the same and every effect that was already recorded is reused instead of executed again;
* re-evaluates conditions and scheduling for everything else.

A run interrupted while node `two` was running resumes like this (from an actual journal):

```text
 7 node.started   two  attempt=1
 8 policy.decision two
   (process died)
 9 run.started
10 node.started   two  attempt=1
11 policy.decision two
12 effect.completed two  key=two@1/tool#1
13 node.completed  two
14 run.completed
```

Usage (tokens, cost, calls) is rebuilt from the journal, including planner usage recorded in `run.created`, so resuming cannot reset token, call or cost budgets. The wall-clock budget (`max_wall_s`) is measured from the start of each execution session, so it applies per session, not across resumes.

`Runtime.execute` refuses to execute a run that is already executing in the same process (`CairnError: run '...' is already executing in this process`). There is no cross-process lock on a run; use the worker queue's leases (below) so that only one worker drives a run at a time.

If the caller abandons an execution (task cancellation, worker shutdown), the executor cancels its node tasks and re-raises; no terminal event is written and the run stays resumable.

## Strict replay

`Runtime.replay(run_id)` (CLI `cairn replay`, API `POST /v1/runs/{id}/replay`):

1. loads the source run;
2. creates a shadow `Runtime` sharing the services but with a fresh `InMemoryJournal` and no approval handler;
3. creates a run with id `replay_<...>` from the same plan, inputs, budget, grants, label, agent and depth;
4. copies the source's decided approvals (`approval.requested` and `approval.decided` with `copied_from`);
5. executes in `Mode.STRICT` with the source's effect records as the recording.

In strict mode every effect must come from the recording with a matching fingerprint; a missing or changed effect raises `ReplayDivergence`, which fails the replay run immediately. No model, tool, retrieval, memory or sub-agent call is made, and retry backoff does not sleep. The real journal is never written.

The `ReplayReport`:

| Field | Meaning |
|---|---|
| `source_run_id`, `replay_run_id` | Ids |
| `matched` | `output_equal` and no divergences and same final status |
| `output_equal` | canonical JSON of the outputs is identical |
| `status` | status of the replay run |
| `divergences` | list of `{node_id, key, kind, expected, actual}` or `{..., missing: true}` plus the error message |
| `effects_replayed` | count of effects served from the recording |
| `error` | error of the replay run, if any |

What a divergence means: the request an effect would send changed since the recording. Typical causes are an edited prompt or default system prompt, a changed plan, a new tool version, different resolved arguments, or a change in rendering code. Non-effect logic is re-executed with current code: the policy engine, conditions, schema checks and verification checks all run again, so a policy change that now requires an approval makes the replay suspend (status differs, `matched` is false) without any divergence entry.

`cairn replay` prints `matched=... output_equal=... effects_replayed=... divergences=N` and exits 0 only if `matched`. Batch regression testing over many recorded runs is in [evaluation.md](evaluation.md#regression-via-replay).

## Canonical rendering

Fingerprints are only useful if rendering is deterministic. Values reach model requests through `resolve.to_text`, which renders non-strings with `json.dumps(..., indent=2, ensure_ascii=False, sort_keys=True)`. Sorted keys matter because a value read back from the journal (stored as canonical JSON with sorted keys) must render byte-identically to the live value it came from; otherwise a resumed or replayed prompt that interpolates a dict would get a different fingerprint and diverge. Custom code that builds model requests from structured values should render them the same way.

## Fork

`Runtime.fork(run_id, *, patches=None, invalidate=(), plan=None, execute=True)` (CLI `cairn fork <run_id> --set node.field=value --invalidate node`, API `POST /v1/runs/{id}/fork`):

1. Start from the source plan (or `plan` if given), dump it to JSON, and shallow-update each patched node's fields, for example `{"summarize": {"tier": "frontier"}}` or `{"calc": {"args": {"expression": "1+1"}}}`. Patching an unknown node raises `NotFound`. The patched plan is validated again.
2. Compute the invalidated set: each node in `invalidate` plus all of its descendants.
3. Reusable effects: every source effect whose key's node prefix (the part before `@`) is not invalidated.
4. Create a new run (tag `fork`, same inputs, budget, grants, label, agent, depth, parent) with `forked_from`, `patches` and `invalidated` in `run.created`, and copy decided approvals.
5. Execute with the reusable effects as the source recording, in live mode.

Reuse rules: an effect is reused only if a reusable record exists under the same key and its fingerprint matches the new request. A changed request records `effect.diverged` and executes live. So a patched node, and any downstream node whose inputs changed as a result, execute live; unchanged upstream and downstream effects are reused at zero cost and are copied into the fork's journal (`replayed: true`) so the fork is self-contained. `invalidate` forces re-execution even of identical requests, for example to resample a model.

Consequences to keep in mind:

* Reuse is decided by request equality, not by intent. A side-effecting tool call whose arguments changed runs again in the fork; one whose arguments did not change is not run again.
* Keys include attempt numbers. If the source needed two attempts for a node and the fork succeeds on the first, the second attempt's recording is simply unused.
* The reusable effect set is kept in memory by the `Runtime` that created the fork. If a fork is suspended and resumed by a different process, source effects that were not yet copied into the fork's journal are not available and execute live.

`cairn fork` parses `--set node.field=value`, with the value parsed as JSON when possible and as a string otherwise.

## Approvals

Approvals are journaled like everything else.

1. A tool call whose policy verdict is `require_approval`, or an `approval` node, computes `request_id = "apr_" + stable_hash({"node": node_id, "subject": subject}, 20)`. For tool calls the subject is the tool name, the arguments (secret values redacted), each argument's label description and the tool's effects. The id is therefore bound to the exact call: different arguments produce a different id and a new approval.
2. If no request with that id exists, `approval.requested` is appended with `reason`, `rule` and `preview` (the subject).
3. If an `ApprovalHandler` is configured and the mode is live, it is awaited. A returned decision is journaled as `approval.decided`; approval proceeds, rejection fails the node with non-retryable `policy_violation`. Returning `None` defers.
4. Otherwise the node emits `node.waiting` and parks. Independent nodes keep running. When nothing else can progress, the run emits `run.suspended` with the waiting nodes and pending request ids.
5. An operator records a decision with `Runtime.decide(run_id, request_id, approved=..., by=..., note=...)` (CLI `cairn approve <run_id> <request_id> [--reject] [--note ...] [--by ...]`, API `POST /v1/runs/{id}/approvals/{request_id}`). Deciding twice is an error.
6. `resume` re-runs waiting nodes (each at most once per execution session), with the same attempt number. The recomputed request id finds the decision: approved calls proceed, rejected ones fail with `approval rejected by <who>: <note or reason>`.

`cairn run -i`, `cairn exec -i` and `cairn resume -i` install a terminal approval handler that prompts `approve? [y/N]` when stdin is a TTY and defers otherwise.

## Cancellation

`Runtime.cancel(run_id, reason)`:

* If the run is executing in this process, it sets the run's cancel event. The executor aborts in-flight node tasks and appends `run.cancelled` with reason `cancel requested`. Returns `True`.
* Otherwise, if the run is not terminal, it appends `run.cancelled` with the given reason and returns `True`; if terminal, returns `False`.

The second branch does not reach an executor running in a different process. That executor does not watch the journal for `run.cancelled`, so it keeps running and may append further events, including `run.completed`. To stop a run that a worker is executing, cancel its job in the queue (`SQLiteWorkQueue.cancel(job_id)`): the worker's next heartbeat fails, the worker cancels the execution, and the run stays resumable.

## Sub-agents

An `agent` node derives its child run id deterministically:

```text
child_run_id = "run_" + stable_hash({"parent": parent_run_id, "node": node_id, "attempt": attempt}, 20)
```

It emits `subrun.linked` once and calls the `SubagentRunner` with a `SubagentRequest` (`run_id`, `parent_run_id`, `goal`, `grants` = the node's `tools`, `tier`, `instructions`, `depth`, `label`, `budget` = what the parent has left). `AgentSubagentRunner` first checks whether that run already exists: if it does, it resumes it; otherwise it plans and creates it. A crash during a child run therefore resumes the same child when the parent resumes, instead of starting a duplicate.

The child call is itself an effect of kind `subagent`. A child that ends `suspended` makes the parent node wait with request id `child:<child_run_id>`; no `approval.requested` is written in the parent. Approve the child's pending requests on the child run, then resume the parent. A child that fails makes the parent node fail (non-retryable).

`depth` increases by one per level and is checked against `budget.max_depth` (default 3); exceeding it is `budget_exceeded`.

## Workers and leases

`src/cairn/workers/` provides background execution with at-least-once job delivery.

**Queue (`SQLiteWorkQueue`)**, stored in the shared database under migration namespace `queue`:

| Status | Meaning |
|---|---|
| `queued` | waiting until `available_at` |
| `leased` | claimed by `lease_owner` until `lease_expires_at` |
| `completed` | handler returned |
| `dead` | attempts exhausted |
| `cancelled` | cancelled by an operator |

* `enqueue(job_type, payload, *, run_id, priority=0, idempotency_key=None, available_at=None, max_attempts=3)`. A duplicate `idempotency_key` returns the original job id.
* `lease(worker_id, lease_s, job_types)` atomically (inside `BEGIN IMMEDIATE`) claims the best candidate: queued jobs that are available, or leased jobs whose lease expired. Order: priority descending, then `available_at`, then insertion order. Leasing increments `attempts`. An expired job already at `max_attempts` is moved to `dead` with error `lease_expired` instead of being handed out.
* `heartbeat(job_id, worker_id, lease_s)` extends the lease only if the caller still owns it.
* `complete` and `fail` succeed only for the lease owner. `fail` requeues with delay `min(retry_delay_s * 2 ** (attempts - 1), max_backoff_s)` (default `max_backoff_s=300`) or dead-letters at `max_attempts`.
* `cancel(job_id)` cancels a queued or leased job.

**Worker (`Worker`)**: `Worker(runtime, queue, worker_id=None, concurrency=4, lease_s=30.0, poll_interval_s=0.5, job_types=None, *, retry_delay_s=1.0, shutdown_timeout_s=30.0)`.

* Built-in handlers: `run.execute` and `run.resume`, both calling `Runtime.execute(payload["run_id"])`. The job result is `{run_id, status, error, pending_approvals}`. A suspended or failed run is a completed job: retrying would only spin on the same approval or repeat a business failure. Only exceptions fail the job.
* Custom handlers: `@worker.handler("my.type")`.
* Heartbeats every `lease_s / 3`. If a heartbeat reports the lease lost (cancelled, or expired and re-leased elsewhere), the handler is cancelled so two workers never drive the same job.
* `stop(timeout)` stops leasing, waits for in-flight jobs, then cancels the rest without failing or releasing them: their leases expire and another worker picks them up, resuming each run from its journal. `run_until_signal()` does this on SIGINT/SIGTERM.

**Helpers**: `submit_run(runtime, queue, plan, *, priority=0, max_attempts=3, **create_run_kwargs)` creates a run and enqueues `run.execute` with the run id as idempotency key; `resume_when_approved(queue, run_id, ...)` enqueues `run.resume`.

`cairn worker --concurrency N` runs a worker against `<data_dir>/cairn.db`. Neither the CLI nor the HTTP API currently enqueues jobs: runs started with `cairn run`/`cairn exec` execute in the CLI process, and runs started through the API execute as background tasks in the API process. Enqueue from Python with `submit_run` to use workers.

## Limitations

* No cross-process cancellation of a running execution through `Runtime.cancel` (use job cancellation).
* Effects are exactly-once only once recorded; see the at-least-once window above.
* Fork reuse sources are process-local for unfinished forks.
* `max_wall_s` is per execution session.
* `run.forked` exists as an event type but is not emitted; forks are identified by `forked_from` in `run.created` and the `fork` tag.
