# Retrieval quality benchmark

- Generated: 2026-10-02T16:03:56Z by `benchmarks/retrieval_quality.py`
- Git commit: `d7ded04eb3cf914538a483347338afedd7155054` (uncommitted changes: True)
- Python 3.11.15 (CPython) on Linux-6.18.44-fc-v51-x86_64-with-glibc2.39, 4 CPUs
- Corpus: retrieval-corpus v1.0.0, 40 docs, sha256 `f23b641f05e0b7058d9e4a53dbeb8cd69537bc9bfa5ce6a77f2c062d5cbafafe`
- Queries: retrieval-queries v1.0.0, 30 queries, sha256 `cb1ca7638460377be4ab23903baf5102789f2e1e584278a5c93bd921769d0085`
- Embedder for dense/hybrid: `hashing-512`

HashingEmbedder is feature hashing over stemmed n-grams and character trigrams: it is lexical-ish, NOT a neural semantic model. 'dense' here measures morphological and n-gram overlap. Queries are paraphrased to limit exact word overlap. Swap in a real embedding model to measure semantic retrieval; the harness is unchanged.

| mode | recall@5 | MRR | nDCG@5 |
|---|---|---|---|
| lexical | 0.7833 | 1 | 0.8817 |
| dense | 0.7333 | 0.9611 | 0.8346 |
| hybrid | 0.7667 | 1 | 0.8726 |

