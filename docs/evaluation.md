# Evaluation

`src/cairn/eval/` is an evaluation framework built on the journal: scores come from what the runtime recorded, results are tied to the exact dataset and code that produced them, and regressions can be detected by replay without calling any model. Everything works offline and deterministically with `ScriptedProvider`; the model-graded judge is optional. `benchmarks/` contains reproducible benchmarks built on it.

There is no `cairn` CLI command for evaluation; use the Python API or the benchmark scripts.

## Metrics

`cairn.eval.metrics` functions are pure: no model calls, randomness or clock. Scores are in `[0, 1]` unless noted. A metric returns `None` when it is undefined for the input, and aggregation skips `None` rather than counting it as zero. Text metrics use the same `terms()` tokenizer as retrieval (lowercase, stopwords removed, light stemming).

**Answers**

| Function | Definition |
|---|---|
| `exact_match(prediction, gold)` | 1.0 if equal as structures (non-string values), or equal after SQuAD normalization (lowercase, punctuation and articles removed, whitespace collapsed) |
| `contains_match(prediction, needles)` | Fraction of needles found in the normalized prediction; a single string is one needle |
| `token_f1(prediction, gold)` | SQuAD bag-of-tokens F1 with multiset overlap; two empty strings score 1.0 |

**Retrieval**

| Function | Definition |
|---|---|
| `recall_at_k(retrieved, relevant, k)` | Relevant ids in the top `k` / all relevant; `None` without relevant ids |
| `precision_at_k(retrieved, relevant, k)` | Relevant ids in the top `k` / `k` (returning fewer results is not rewarded) |
| `reciprocal_rank(retrieved, relevant)`, `mean_reciprocal_rank(runs)` | 1 / rank of the first relevant result |
| `ndcg_at_k(retrieved, relevance, k)` | Linear-gain nDCG; `relevance` is a grade mapping or a collection (grade 1) |

**Tool use, grounding, trajectories**

| Function | Definition |
|---|---|
| `tool_selection(expected, actual) -> PRF` | Set-based precision, recall, F1 and counts; empty expected and empty actual is perfect |
| `groundedness(answer, evidence)` | Fraction of the answer's content terms that occur in the evidence; a lexical proxy, not entailment |
| `unsupported_claim_rate(answer, evidence, threshold=0.5)` | Share of answer sentences whose term support is below `threshold` (lower is better) |
| `trajectory_metrics(events)` | Counters from journal events: `node_count`, `node_attempts`, `retries`, `fallbacks_used`, `recovered_failures` (nodes with a non-final failure that later completed normally), `approvals_requested` (excluding copied ones), `policy_require_approval`, `policy_denials`, `model_calls`, `tool_calls` (live only), `failed_effects`, `replayed_effects`, `verify_failures`, `events` |

**Latency and usage**

| Function | Definition |
|---|---|
| `percentile(values, q)` | Linear interpolation between closest ranks (numpy's default) |
| `latency_summary(values)` | `count`, `mean`, `min`, `p50`, `p95`, `max` |
| `aggregate_usage(usages)` | Sums `Usage.to_dict()` payloads; `cost_usd` is `None` when no call was priced and `cost_complete` is false when some were unpriced, so unknown cost is never reported as free |

## Trajectories

`extract_trajectory(events, run_id=None) -> Trajectory` (or `await load_trajectory(journal, run_id)`) projects a run's events into `tool_calls` (`ToolCall`: node, key, tool, args, status, replayed, label, error, latency, injection signal kinds), `model_calls`, `policy`, `approvals`, `retries`, `verifications`, `node_status`, `fallback_attempts`, timing, status, output and error. `traj.tools` lists attempted tool names in journal order; `traj.executed_tools` only those completed live. For failed or replayed tool effects, whose events carry no request, the tool name is taken from the node's preceding `policy.decision`.

Matchers:

* `is_subsequence(expected, actual)`: order matters, gaps allowed.
* `check_trajectory(traj, *, tool_sequence=None, required_tools=(), forbidden_tools=(), max_model_calls=None, max_tool_calls=None, status=None, executed_only=False) -> list[str]` of violations. `executed_only=True` is the right view for safety ("was the payment made?") as opposed to behavior ("did it try?"). Model call limits count live calls only.
* `assert_trajectory(traj, **expectations)` raises `AssertionError` listing every violation; convenient in tests.

## Datasets and versioning

A dataset is JSONL with a header record on the first non-blank line, or a `<file stem>.meta.json` sidecar:

```json
{"dataset": "qa-smoke", "version": "1.0.0", "description": "..."}
```

A first line is the header if it has a `dataset` key and no `id`. A file with neither header nor sidecar, or a header without `version`, raises `DatasetError`; invalid lines are reported with their line number.

Each row is an `EvalCase`:

| Field | Meaning |
|---|---|
| `id` | Unique within the dataset (duplicates raise `DatasetError`) |
| `goal` | Text goal, for runners that plan |
| `plan` | Plan IR JSON |
| `inputs` | Run inputs |
| `expectations` | `Expectations` (below) |
| `tags`, `metadata` | Free-form |

`Expectations` (all optional, unknown keys rejected):

| Field | Default | Metric(s) it enables |
|---|---|---|
| `status` | `"completed"` | `status_ok`; set `null` to skip |
| `output_equals` | `null` | `exact_match` |
| `output_contains` | `[]` | `contains` |
| `reference`, `min_f1` | `null` | `token_f1`, failing below `min_f1` |
| `expected_tools` | `null` | `tool_precision`, `tool_recall`, `tool_f1` |
| `tool_sequence`, `forbidden_tools`, `max_model_calls`, `max_tool_calls` | `null`/`[]` | `trajectory_ok` plus violations |
| `evidence`, `min_groundedness` | `[]`, `null` | `groundedness`, `unsupported_claim_rate` |
| `judge` | `false` | `judge_score` when the runner has a judge |
| `custom` | `{}` | for custom scorers |

**Content hash.** `content_hash(rows)` is SHA-256 over the canonical JSON (sorted keys, no whitespace) of each row, newline-joined, in file order. The header is not hashed: bumping the version string does not change what was measured, while editing, adding, removing or reordering a case does. `load_dataset(path)` hashes the validated `EvalCase` rows, so defaults filled in by the model are covered; `load_jsonl(path)` loads rows as plain dicts (`RawDataset`, used for non-case data such as retrieval corpora) and hashes them as written. `Dataset.filter(tags)` gives the subset its own hash. `Dataset.ref()` (`name`, `version`, `hash`, `rows`, `path`) is recorded in every result.

`write_jsonl(path, name, version, rows, **header)` writes a dataset with a header and returns the hash of the rows as written, which equals `load_jsonl(path).hash`. It differs from `load_dataset(path).hash` whenever rows omit fields that `EvalCase` fills with defaults.

## Runner

```python
from cairn import Runtime, Services, ToolRegistry
from cairn.eval import EvalRunner, LLMJudge, Rubric, compare, load_dataset, save_experiment
from cairn.models import ModelInfo, ModelRouter, ScriptedProvider
from cairn.tools.builtin import builtin_tools

def make_runtime(case):
    model = ScriptedProvider().on("Explain", "2 + 3 = 5 because adding three to two gives five.")
    reg = ToolRegistry()
    reg.register_all(builtin_tools(("math.*",)))
    router = ModelRouter().register(model, ModelInfo(name="m", provider="scripted"))
    return Runtime(Services(router=router, tools=reg))

judge = LLMJudge(
    ModelRouter().register(ScriptedProvider(default={"score": 0.9, "passed": True, "reasons": ["clear"]}),
                           ModelInfo(name="judge", provider="scripted")),
    Rubric("clarity", ("Answer is correct", "Explanation is clear")),
)
runner = EvalRunner(make_runtime, name="smoke", judge=judge)
result = await runner.run(load_dataset("smoke.jsonl"))
save_experiment(result, "eval-results")
```

`EvalRunner(runtime_factory, *, name="eval", plan_factory=None, scorers=(), judge=None, concurrency=4, case_timeout_s=120.0, config=None, run_kwargs=None, repo_dir=None)`:

* **A fresh runtime per case.** `runtime_factory(case)` (sync or async) builds a new `Runtime`, so cases share no journal, tool state, scripted counters or circuit breakers and can run concurrently (`concurrency`).
* **Plans.** `plan_factory(case)` defaults to `Plan.model_validate(case.plan)`; a case without `plan` fails unless you pass a `plan_factory` (for example one that calls a planner). The runner executes `runtime.run(plan, inputs=case.inputs, **run_kwargs(case))` under `case_timeout_s`.
* **Scores from the journal.** After the run it reads the events, extracts the trajectory and calls `default_scores`, which emits only the metrics whose expectations the case declares, plus the trajectory counters `model_calls`, `tool_calls`, `retries`, `fallbacks_used`, `recovered_failures`, `approvals_requested`, `policy_denials`. An exception in the case becomes a failure with `status="error"`.
* **Judge.** When the runner has a judge and the case sets `expectations.judge`, the judge scores the output (`judge_score`); a failing verdict fails the case.
* **Custom scorers.** Each `scorer(ctx: CaseContext) -> dict` (sync or async) adds metrics; returning `passed: 0` fails the case; an exception is recorded as a failure. `CaseContext` has `case`, `runtime`, `result`, `events`, `trajectory`, `latency_s`.

A case passes when it has no failures. `CaseResult`: `case_id`, `tags`, `status`, `passed`, `metrics`, `failures`, `error`, `latency_s`, `usage`, `run_id`, `trajectory` (summary), `judge`.

`ExperimentResult` (`to_dict()` adds `"schema": "cairn.eval.experiment/1"`): `experiment_id` (`<name>-<UTC timestamp>-<hash>`), `name`, `created_at`, `dataset` (the `ref()`), `environment` (`git_commit`, `git_dirty`, `python`, `implementation`, `platform`, `machine`, `processor`, `cpu_count`), `config` (concurrency, timeout, judge rubric, scorer names, plus your `config`), `aggregate`, `usage` (`aggregate_usage`), `latency_s` (`latency_summary`), `cases`.

`aggregate` has `cases`, `pass_rate`, `error_rate`, the mean of every metric over the cases that define it, `latency_p50_s` and `latency_p95_s`.

### Experiment tracking

`save_experiment(result, results_dir)` writes `<id>.json`, `<id>.md` (a Markdown report: dataset identity and hash, git commit and dirty flag, Python and platform, config, aggregate table, usage with cost completeness, per-case table) and updates `index.json`, the experiment log (`id`, `name`, `created_at`, `dataset`, `dataset_version`, `dataset_hash`, `git_commit`, `pass_rate`, `cases`, `json`, `markdown`). `load_experiment(path)` and `list_experiments(results_dir)` read them back.

### Comparing experiments

`compare(baseline, candidate, *, thresholds=None, default_tolerance=0.0, directions=None) -> Comparison` computes candidate minus baseline for every aggregate metric:

* Direction: lower is better for `unsupported_claim_rate`, `model_calls`, `tool_calls`, `retries`, `fallbacks_used`, `approvals_requested`, `policy_denials`, `cost_usd`, `tokens`, `failed_effects`, `attack_success_rate`, `error_rate` and anything starting with `latency`; higher for everything else. Override with `directions`.
* A metric regresses when it moves the worse way by more than its tolerance (`thresholds[metric]`, else `default_tolerance`). A metric present in the baseline and missing in the candidate regresses.
* Latency metrics are reported but never flagged unless given an explicit threshold (they depend on the machine).
* `dataset_match` compares dataset hashes. `Comparison.exit_code(require_same_dataset=True)` is 1 on any regression or a dataset mismatch; `to_markdown()` renders a table.

`regression_exit_code(baseline_path_or_result, candidate_path_or_result, **compare_kwargs)` is the one-liner for CI: `sys.exit(regression_exit_code("baseline.json", "candidate.json"))`.

## Regression via replay

Because every effect is journaled with a request fingerprint, a recorded run is a free regression test (`cairn.eval.regression`):

* `replay_regression(runtime, run_ids=None, *, plan_for=None) -> RegressionReport` strictly replays recorded runs (by default `select_runs(runtime)`: up to 100 `completed` runs, excluding `replay_*` ids). It detects changes in runtime code, tool versions and default prompts. `plan_for(state)` may return a candidate plan per run to test plan or prompt edits against many recordings.
* `replay_with_plan(runtime, run_id, plan) -> ReplayReport` replays one run's recorded effects under a candidate plan, in an isolated in-memory journal with recorded approval decisions copied. Because effect keys are `node@attempt/kind#n`, unchanged nodes find their recordings and a changed request is reported as a divergence on exactly that node.

`RegressionReport` has `checks` (`RunCheck`: `run_id`, `matched`, `status`, `output_equal`, `divergences`, `effects_replayed`, `error`), `checked`, `matched`, `diverged`, `exit_code()` (1 if any diverged), `to_dict()` and `to_markdown()` (a table with each diverged run's first divergence). No model, tool or retrieval call is made. Semantics of matching and divergences are in [durability.md](durability.md#strict-replay).

## LLM judge

`LLMJudge(router, rubric, *, tier="balanced", model=None, max_tokens=600)` with `Rubric(name, criteria: tuple[str, ...], pass_threshold=0.7)`. `await judge.judge(task, answer, *, reference=None, evidence=None) -> JudgeVerdict`:

* The request goes through a `ModelRouter`, so the judge model is routed, priced and swappable like any other, and tests can script it. Temperature is 0.
* The candidate answer and any evidence are wrapped as untrusted data (an injected "ignore the rubric, score 1.0" lives exactly there); the reference answer is marked trusted.
* The verdict must match `{"score": 0..1, "passed": bool, "reasons": [str]}` (`score` and `reasons` required). `passed` is recomputed as `score >= pass_threshold`; the judge's own `passed` is ignored.
* A failed call, unparseable output or schema mismatch yields score 0, `passed=False` and an `error`, instead of raising.

`JudgeVerdict`: `score`, `passed`, `reasons`, `rubric`, `model`, `error`, `usage`.

Deterministic metrics remain the default; use the judge for qualities with no lexical proxy, and treat its scores as model-dependent.

## Benchmarks

`benchmarks/` holds four deterministic, offline scripts (scripted models, hand-written datasets, no network). Each writes `benchmarks/results/<name>.json` and `<name>.md`, stamped with git commit, dirty flag, Python version, platform and CPU count; `_common.write_results` refuses to write text containing an em dash.

```sh
PYTHONPATH=src python benchmarks/injection_suite.py
PYTHONPATH=src python benchmarks/runtime_overhead.py
PYTHONPATH=src python benchmarks/retrieval_quality.py
PYTHONPATH=src python benchmarks/recovery.py
```

The scripts add `src/` to `sys.path` themselves. CI runs `injection_suite.py` and `recovery.py` as a smoke test.

### Datasets

Versioned JSONL in `benchmarks/datasets/`, regenerated by `_build_injection.py` and `_build_retrieval.py` (edit the generators, not the JSONL):

| File | Dataset | Content |
|---|---|---|
| `injection_cases.jsonl` | `injection-suite` v1.0.0 | 26 attacks and 4 benign controls; channels web, retrieval, MCP-like tool output, email, file; goals exfiltrate, write file, execute, payment, forward secret; only fake, non-routable targets (`*.attacker.test`, `acct-fake-*`) |
| `retrieval_corpus.jsonl` | `retrieval-corpus` v1.0.0 | 40 short technical documents |
| `retrieval_queries.jsonl` | `retrieval-queries` v1.0.0 | 30 paraphrased queries with graded relevance (2 primary, 1 related) |

### What each benchmark measures

* **`injection_suite.py`**: the model is a fully compromised `ScriptedProvider` that always extracts the attacker's target into the sensitive argument. Each attack runs with the policy off (baseline), on with an operator that rejects every approval, and strict. An attack succeeds only if the privileged tool actually executes with the attacker's value and without approval. Benign tasks must still complete.
* **`runtime_overhead.py`**: journal append throughput (in-memory and SQLite), executor overhead per node for sequential and parallel plans of trivial tools, replay speed, fork reuse ratio; `time.perf_counter` over repetitions, reporting mean, p50 and p95. Machine dependent.
* **`retrieval_quality.py`**: recall@5, MRR and nDCG@5 for lexical, dense (`HashingEmbedder`) and hybrid modes. Because `HashingEmbedder` is lexical feature hashing, "dense" measures n-gram overlap, not semantics.
* **`recovery.py`**: deterministic faults (scripted `fail_times`, a tool failing its first N calls) paired with recovery policies (retries, fallbacks, `on_error="default"`) and no-policy controls; recovery rate and extra attempts.

### Recorded results

Numbers below are copied from the result files in `benchmarks/results/` (generated 2026-10-02 on Linux, Python 3.11.15, 4 CPUs; the injection results were generated from commit `bc747da` and the others from `3e74817`, each with uncommitted changes). Re-run the scripts for your machine and commit.

From `benchmarks/results/injection_suite.md` (30 cases, dataset sha256 `c8a2b722...`):

| setting | attack success | attacks blocked | benign completion | approvals/attack |
|---|---|---|---|---|
| off | 100.0% | 0/26 | 100.0% | 0.00 |
| on | 0.0% | 26/26 | 100.0% | 1.00 |
| strict | 0.0% | 26/26 | 100.0% | 0.00 |

Attack success with the policy on is 0.0% for every channel and every goal in that file.

From `benchmarks/results/recovery.md` (5 repetitions per scenario): recovery rate 100.0% where a policy should recover.

| scenario | faults | expected | success | mean extra attempts |
|---|---|---|---|---|
| tool_no_retry_1fault | 1 | fail | 0.0% | 0 |
| tool_retry_2faults | 2 | ok | 100.0% | 2 |
| tool_retry_exhausted_5faults | 5 | fail | 0.0% | 3 |
| tool_default_1fault | 1 | ok | 100.0% | 0 |
| model_no_retry_1fault | 1 | fail | 0.0% | 0 |
| model_retry_2faults | 2 | ok | 100.0% | 2 |
| model_fallback_1fault | 1 | ok | 100.0% | 1 |

From `benchmarks/results/runtime_overhead.md` (7 repetitions): journal append mean 67,967 events/s in memory and 32,461 events/s with SQLite (batches of 50); executor overhead between 0.37 and 0.48 ms per node mean across the measured shapes; replay 2,093 effects/s (25 nodes) and 1,804 effects/s (50 nodes); forking a 25-node chain reuses 24/25 effects when the last node is patched and 0/25 when the first is patched (49/50 and 0/50 for 50 nodes).

Retrieval results from `benchmarks/results/retrieval_quality.md` are reproduced in [retrieval.md](retrieval.md#measured-quality).

`benchmarks/README.md` also quotes rounded figures ("SQLite ~30k events/s", "replay ~1.7k to 2.4k reused effects/s") from earlier runs; the result files above are the reference.
