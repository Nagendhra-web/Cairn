# Benchmark datasets

Versioned JSONL datasets for the benchmarks. Each file starts with a header
record `{"dataset": name, "version": ...}`; the remaining lines are data rows.
Loading computes a SHA-256 over the canonical form of the rows (not the
header), and every benchmark result records that hash, so results are tied to
the exact data and silent edits are visible as a hash change.

## Files

- `injection_cases.jsonl` (`injection-suite` v1.0.0): 26 prompt-injection
  attacks + 4 benign controls across web, retrieval, MCP, email and file
  channels. Regenerate with `python datasets/_build_injection.py`. Uses only
  fake, non-routable targets (`*.attacker.test`, `acct-fake-*`); these are test
  fixtures, not real attacks.
- `retrieval_corpus.jsonl` (`retrieval-corpus` v1.0.0): 40 short hand-written
  technical documents. Regenerate with `python datasets/_build_retrieval.py`.
- `retrieval_queries.jsonl` (`retrieval-queries` v1.0.0): 30 paraphrased
  queries with graded relevance labels (2 = primary, 1 = related). Regenerated
  together with the corpus.

## Editing

Edit the `_build_*.py` generator, not the JSONL directly, then rerun the
generator and the affected benchmark. The dataset hash will change, which is
the intended signal that the measured data changed.
