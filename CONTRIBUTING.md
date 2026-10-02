# Contributing to Cairn

Thanks for helping build a runtime that makes agents inspectable, reproducible and safe.

## Development setup

```bash
git clone https://github.com/nagendhra-web/New-Repo.git cairn && cd cairn
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,anthropic]"
make check        # em dash guard, ruff, mypy --strict, full test suite
cairn demo all    # offline demos, no API key required
```

| Command | What it runs |
| --- | --- |
| `make lint` | `ruff check` over src, tests, examples, benchmarks, scripts |
| `make typecheck` | strict mypy over `src/cairn` |
| `make test` | unit, integration and e2e tests |
| `make examples` | every runnable example |
| `make bench` | all benchmarks (writes `benchmarks/results/`) |
| `make check` | everything CI runs |

## Architecture rules that keep the system honest

1. **Every nondeterministic operation is an effect.** Model calls, tool calls, retrieval, memory
   reads and writes, clocks and randomness that influence a run go through
   `Recorder.effect(key, kind, request, run)`. If you bypass it, resume and replay silently break.
2. **Every new data source assigns a provenance label.** Ask "could an attacker influence this
   value?" If yes, it is `UNTRUSTED`. Labels must survive every transformation (use `join`).
3. **Every side-effecting tool declares its effects and sensitive parameters.** Recipients, paths,
   URLs, commands and payment targets are sensitive sinks.
4. **Run state is a fold over events.** Never keep execution state only in memory; add an event
   type and handle it in `runtime/state.py`.
5. **Prefer protocols at boundaries.** New backends implement the protocols in
   `runtime/services.py`, `journal/store.py`, `memory/store.py`, `retrieval/index.py`,
   `models/base.py` and `workers/queue.py`.
6. **Never invent numbers.** Benchmarks and docs report only measured values with the command
   that produced them.

## Tests

- Unit tests live in `tests/unit`, integration tests (`@pytest.mark.integration`) in
  `tests/integration`, end-to-end tests (`@pytest.mark.e2e`) in `tests/e2e`.
- Use `ScriptedProvider` for deterministic model behavior; tests must not need network access.
- Security-relevant changes need an adversarial test: assume the model is fully compromised.

## Style

- Python 3.11+, full type hints, `mypy --strict` clean.
- Docstrings explain *why* a component exists and which invariant it protects.
- No em dash characters anywhere (`python scripts/check_no_em_dash.py`). Use commas, colons,
  semicolons, parentheses or hyphens.

## Good first issues

Look for the `good first issue` label. Well-scoped starter ideas:

- A Postgres `JournalStore` implementing `journal/store.py` (the SQLite version is the reference).
- A `VectorIndex` adapter for an external vector database.
- A cross-encoder `Reranker`.
- More cases for `benchmarks/datasets/injection_cases.jsonl` (new channels or attack goals).
- A `cairn eval` CLI command wrapping `EvalRunner` for JSONL datasets.

## Pull requests

Keep changes focused, add tests and docs, update `CHANGELOG.md` under "Unreleased", and fill in
the pull request checklist. CI must be green.
