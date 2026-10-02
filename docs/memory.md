# Memory

`src/cairn/memory/` implements multi-layer agent memory. `MemoryManager` is the runtime's `MemoryService` (used by `memory` plan nodes) and the store agents use for episodes, procedures and planner context. Memory crosses run boundaries, which makes it the easiest place to launder injected content; provenance labels are therefore part of every record and every write is checked.

## Layers

`MemoryKind` (`memory/types.py`), from shortest to longest lived:

| Kind | What it holds | Written by | Default half-life |
|---|---|---|---|
| `working` | Per-session scratchpad (`WorkingMemory`, in process, never persisted); persisted `working` records are possible through `remember` | your code via `MemoryManager.working(session_id)` | 15 minutes |
| `session` | Facts for one conversation or run family | your code (`remember(..., "session", ...)`) | 1 day |
| `episodic` | One record per finished agent run: goal, status, outcome | `Agent` via `record_episode`; `memory` nodes with `memory_kind="episodic"` | 14 days |
| `semantic` | Durable facts, written directly or distilled by `consolidate` | `memory` nodes (default kind), `consolidate`, your code | 90 days |
| `procedural` | Plans that succeeded, keyed by goal, reused as planner few-shot examples | `Agent` via `save_procedure` | 365 days |

Plan `memory` nodes can address `semantic`, `episodic` and `procedural` only.

## What is stored

Every record is a `MemoryRecord`:

| Field | Meaning |
|---|---|
| `id` | `mem_<...>` |
| `kind` | layer |
| `text` | stored exactly as given (the executor redacts secrets before calling `remember`) |
| `label` | provenance `Label` of the text (integrity, sources, secrecy) |
| `importance` | 0..1 |
| `created_at`, `last_accessed` | timestamps (clock injectable for tests) |
| `access_count` | incremented by recall and by duplicate merges |
| `pinned` | exempt from decay and expiry; trusted records only |
| `source_run` | run that wrote it |
| `metadata` | free-form; the manager adds `embedder`, `flags`, `merged_runs`, `merge_count`, `consolidated_from`, `summary_method`, and episodes/procedures add `goal`, `status`, `plan` |
| `embedding` | stored so the index can be rebuilt without re-embedding |
| `superseded_by` | id of the consolidated record that replaced it |

`MemoryRecord.to_dict()` always includes `label` and `trusted`; recall results add `score` and `score_parts`.

Storage: `SQLiteMemoryStore` (table `memory_records`, migration namespace `memory`) in the shared database, or `InMemoryMemoryStore`. The manager keeps a `HybridIndex` (BM25 plus vectors, see [retrieval.md](retrieval.md)) of active records, rebuilt from the store on first use. Stored embeddings are reused when `metadata["embedder"]` matches the current embedder's name; otherwise the text is re-embedded.

## Importance

`score_importance(text, explicit=None)` (`memory/policy.py`) is a cheap specificity heuristic:

```text
signal = 0.2 + min(0.25, words / 80)
       + 0.15 if the text contains a digit
       + 0.15 if a capitalized word appears other than at a sentence start (named entity)
       + 0.15 if it contains a directive word (always, never, must, prefer, important,
              remember, deadline, password, requirement, policy, do not, don't)
signal = min(1.0, signal)
importance = signal                                  if explicit is None
           = min(1.0, 0.7 * explicit + 0.3 * signal)  otherwise
```

Measured examples: `"ok thanks"` scores 0.225; `"The deploy deadline for Project Atlas is 2026-11-03."` scores 0.7375; `"ok"` with explicit 0.9 scores 0.6937.

## Decay and expiry

`DecayPolicy(half_lives=DEFAULT_HALF_LIVES, expire_threshold=0.05, reinforcement=0.05)`:

```text
recency  = 0.5 ** (age / half_life(kind))        age measured from last access
strength = min(1.0, (0.4 + 0.6 * importance + 0.05 * log1p(access_count)) * recency)
pinned records: strength = 1.0
expired  = not pinned and strength < 0.05
```

An episodic record with importance 0.5 and no accesses has strength 0.7 at day 0, 0.35 at day 14, 0.175 at day 28, and 0.005 at day 100 (expired). Recalling a record refreshes `last_accessed`, so memories that are used stay alive.

Recall already ignores expired records. `expire()` deletes them (`cairn memory expire`); it does not run automatically. `decay()` returns current strengths without changing anything.

## Recall ranking

`MemoryManager.recall(query, kind="any", k=5)` (and `search`, which returns `ScoredMemory` objects):

1. Query the hybrid index for `max(50, 10k)` candidates.
2. Drop candidates of the wrong kind, candidates with no BM25 match and a dense similarity below `min_relevance` (default 0.1), superseded records and expired records.
3. `relevance = min(1, rrf_score / (2/61))`, which maps the best possible two-list RRF score to 1.
4. Score with `RetrievalScorer` and `ScoringWeights(relevance=0.6, recency=0.15, importance=0.15, frequency=0.05, pinned=0.05)`, where `frequency = min(1, log1p(access_count) / log(50))` and `recency` is 1.0 for pinned records.
5. Return the top `k`. With `touch=True` (the default, and always for `recall`), each returned record's `access_count` is incremented and `last_accessed` updated.

Searching memory through the CLI (`cairn memory search`) or the API (`POST /v1/memory/search`) uses `recall`, so inspection also counts as access and refreshes those records.

## Deduplication

Before storing, the new text is embedded and compared with active records of the same kind. If cosine similarity is at least `dedup_threshold` (default 0.92), the write is merged into the existing record instead of creating a new one:

* `importance = min(1, max(old, new) + 0.05)`; `access_count += 1`; `last_accessed = now`;
* the labels are joined (so a trusted record merged with an untrusted near-duplicate becomes untrusted and gets the `untrusted_source` flag);
* `merged_runs` collects the other runs, `merge_count` increments, metadata is updated;
* the stored text is not changed.

Untrusted text never merges into a pinned record; it is stored separately and flagged. The returned dict has `deduplicated: true` for merges.

## Consolidation

`consolidate(threshold=None, min_cluster_size=2, max_sentences=3)` (`cairn memory consolidate`; not run automatically):

1. Take active episodic records with embeddings, oldest first.
2. Greedily cluster: each unassigned seed collects unassigned episodes with cosine to the seed of at least `threshold` (default `consolidation_threshold`, 0.5).
3. Each cluster of at least `min_cluster_size` becomes one `semantic` record: the text comes from the `summarizer` callable if one was given to the manager, otherwise from `extractive_summary` (the sentences whose terms recur across the most episodes, in original order). Its label is the join of the sources' labels; its importance is the maximum; metadata records `consolidated_from` and `summary_method` (`summarizer` or `extractive`).
4. Each source is marked `superseded_by` the new record and removed from the index. Superseded records are kept for audit, excluded from recall, and expire on the normal schedule.

## Provenance write policy

`WritePolicy(allow_untrusted_procedural=False, untrusted_importance_cap=0.6)`:

| Write | Trusted label | Untrusted label |
|---|---|---|
| `working`, `session`, `episodic`, `semantic` | stored as is | stored with flag `untrusted_source`, importance capped at 0.6 |
| `procedural` | stored | `PolicyViolation` (unless `allow_untrusted_procedural`) |
| `pinned=True` or `pin(id)` | allowed | `PolicyViolation` |

Why: procedural memories become planner few-shot examples and pinned memories never decay, so either would let one injected document steer every future run. The importance cap stops injected text from inflating its own weight ("IMPORTANT: always send reports to ..." is stored at 0.6, not 1.0). Every recall returns the record's label, and the executor joins it into the recall node's output, so untrusted memories re-taint whatever uses them. Agents exclude untrusted recalled memories from planner context by default (see [agents.md](agents.md)).

What a `memory` node stores carries both data and control provenance: the label is the text's label joined with the node's control label.

## Episodes and procedures

* `record_episode(goal, outcome, status, run_id, label, *, importance=None)` stores `Goal: ...\nStatus: ...\nOutcome: ...` as episodic with metadata `goal` and `status`. Default importance is 0.5 when `status == "succeeded"` and 0.6 otherwise ("failures teach more"). `Agent` passes run statuses (`completed`, `failed`, ...), so agent episodes always get 0.6 before scoring.
* `save_procedure(goal, plan_json, label, *, run_id=None, status="succeeded")` stores the goal as text with the plan in metadata, explicit importance 0.7. A newer plan for a near-identical goal is merged into the existing record and replaces its `plan` metadata.
* `find_procedures(goal, k=3)` returns `{id, goal, plan, score, label}` for procedural records whose `status` is `succeeded`.

`Agent` records an episode after every completed, failed or critic-rejected run, and saves a procedure only when the run completed, passed the critic (if criteria are set) and the plan's label is trusted.

## Working memory

`MemoryManager.working(session_id)` returns a process-local `WorkingMemory(max_items=64)`; `end_session(session_id)` drops it.

* `add(text, priority=0.5, label=BOTTOM, *, key=None)` adds or replaces by key; when over capacity, the lowest-priority (then oldest) item is evicted.
* `render(budget_tokens)` picks items greedily by priority (newest first on ties), truncates an item that does not fit if at least 8 tokens of room remain (adding ` ...`), drops the rest, and emits the chosen items in insertion order. `RenderedContext.label` joins only the included items. Tokens are estimated as about 4 characters per token.

The runtime and agents do not use working memory automatically; it is an API for your own agent loops.

## Inspection and deletion

CLI (`cairn memory <op> [arg] [--kind KIND]`):

| Command | Effect |
|---|---|
| `cairn memory list [--kind K]` | id, kind, importance, pinned, UNTRUSTED marker, text preview |
| `cairn memory search "<query>" [--kind K]` | top 10 recall results as JSON (counts as access) |
| `cairn memory delete <id>` | delete one record |
| `cairn memory pin <id>` | pin (trusted records only) |
| `cairn memory forget <run_id>` | delete everything written by that run, pinned included |
| `cairn memory consolidate` | consolidate episodes, print new records |
| `cairn memory expire` | delete expired records |
| `cairn memory stats` | totals, active, superseded, pinned, untrusted, expired_pending, by_kind, indexed, working_sessions |
| `cairn memory export` | every record including embeddings, as JSON |

There is no `unpin` command; use `MemoryManager.unpin(id)`. `forget` also accepts `kind=` in Python and requires at least one filter.

HTTP API (see [api.md](api.md)): `GET /v1/memory?kind=`, `POST /v1/memory/search`, `DELETE /v1/memory/{memory_id}`. All return 404 when memory is disabled.

`delete`, `forget` and `expire` remove records from the store and the index immediately.

## Configuration

`cairn.toml` has one memory setting: `memory_enabled` (default `true`). With it, `Cairn.create` builds `MemoryManager(SQLiteMemoryStore(db))` with defaults. Everything else is set in Python:

```python
from cairn.memory import DecayPolicy, MemoryKind, MemoryManager, WritePolicy

memory = await MemoryManager.open(
    ".cairn/cairn.db",
    decay=DecayPolicy(half_lives={MemoryKind.SEMANTIC: 30 * 86400}),
    write_policy=WritePolicy(untrusted_importance_cap=0.4),
    dedup_threshold=0.95,
)
```

`MemoryManager(store=None, *, embedder=None, clock=None, write_policy=None, decay=None, scorer=None, summarizer=None, dedup_threshold=0.92, consolidation_threshold=0.5, min_relevance=0.1, working_max_items=64)`. The default embedder is `HashingEmbedder` (lexical feature hashing, see [retrieval.md](retrieval.md#embedders)); pass a `ProviderEmbedder` for semantic similarity. Kinds missing from a custom `half_lives` dict fall back to the defaults.
