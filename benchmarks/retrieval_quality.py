"""Retrieval quality benchmark.

Compares three retrieval modes on the same hand-labeled corpus and queries:

* ``lexical``  - BM25 over stemmed terms;
* ``dense``    - cosine over :class:`~cairn.retrieval.embeddings.HashingEmbedder`
  vectors;
* ``hybrid``   - reciprocal rank fusion of the two.

Metrics: recall@5, MRR and nDCG@5 (graded labels), averaged over queries.

Honesty note: ``HashingEmbedder`` is a feature-hashing embedder over stemmed
unigrams, bigrams and character trigrams. It is lexical-ish, not a neural
semantic model, so "dense" here measures morphological and n-gram overlap, not
deep meaning. The queries are deliberately paraphrased to reduce exact word
overlap, which is the hardest setting for all three modes and keeps the
comparison meaningful. Swap in a real embedding model (ProviderEmbedder) to
measure semantic retrieval; the harness does not change.
"""

from __future__ import annotations

import asyncio
from typing import Any

from _common import env_markdown, fmt, stamp, write_results

from cairn.eval.dataset import load_jsonl
from cairn.eval.metrics import mean, ndcg_at_k, recall_at_k, reciprocal_rank
from cairn.retrieval.index import HybridIndex

K = 5
MODES = ["lexical", "dense", "hybrid"]


async def build_index(corpus_rows: list[dict[str, Any]]) -> HybridIndex:
    index = HybridIndex()
    await index.add_many([
        (row["id"], f"{row['title']}. {row['text']}", {"title": row["title"]})
        for row in corpus_rows
    ])
    return index


async def evaluate(index: HybridIndex, queries: list[dict[str, Any]]) -> dict[str, Any]:
    per_mode: dict[str, dict[str, Any]] = {}
    for mode in MODES:
        recalls: list[float | None] = []
        rrs: list[float] = []
        ndcgs: list[float | None] = []
        for q in queries:
            labels: dict[str, int] = q["relevant"]
            relevant = set(labels)
            hits = await index.search(q["text"], k=K, mode=mode)
            ranked = [h.id for h in hits]
            recalls.append(recall_at_k(ranked, relevant, K))
            rrs.append(reciprocal_rank(ranked, relevant))
            ndcgs.append(ndcg_at_k(ranked, labels, K))
        per_mode[mode] = {
            "queries": len(queries),
            "recall_at_5": mean(recalls),
            "mrr": mean(rrs),
            "ndcg_at_5": mean(ndcgs),
        }
    return per_mode


async def main() -> None:
    corpus = load_jsonl("benchmarks/datasets/retrieval_corpus.jsonl")
    queries = load_jsonl("benchmarks/datasets/retrieval_queries.jsonl")
    index = await build_index(corpus.rows)
    results = await evaluate(index, queries.rows)

    header = stamp("retrieval_quality", {
        "k": K, "modes": MODES, "embedder": HybridIndex().embedder.name,
        "corpus": corpus.ref(), "queries": queries.ref(),
    })
    payload = {**header, "by_mode": results}
    md = _markdown(header, corpus, queries, results)
    jp, mp = write_results("retrieval_quality", payload, md)
    print(f"retrieval_quality: wrote {jp} and {mp}")
    for mode in MODES:
        r = results[mode]
        print(f"  {mode:8s} recall@5 {fmt(r['recall_at_5'])}  mrr {fmt(r['mrr'])}  "
              f"ndcg@5 {fmt(r['ndcg_at_5'])}")


def _markdown(header: dict[str, Any], corpus: Any, queries: Any, results: dict[str, Any]) -> str:
    lines = ["# Retrieval quality benchmark", ""]
    lines += env_markdown(header)
    lines += [
        f"- Corpus: {corpus.name} v{corpus.version}, {len(corpus.rows)} docs, "
        f"sha256 `{corpus.hash}`",
        f"- Queries: {queries.name} v{queries.version}, {len(queries.rows)} queries, "
        f"sha256 `{queries.hash}`",
        f"- Embedder for dense/hybrid: `{header['config']['embedder']}`",
        "",
        "HashingEmbedder is feature hashing over stemmed n-grams and character trigrams: it is "
        "lexical-ish, NOT a neural semantic model. 'dense' here measures morphological and n-gram "
        "overlap. Queries are paraphrased to limit exact word overlap. Swap in a real embedding "
        "model to measure semantic retrieval; the harness is unchanged.",
        "",
        "| mode | recall@5 | MRR | nDCG@5 |",
        "|---|---|---|---|",
    ]
    for mode in MODES:
        r = results[mode]
        lines.append(
            f"| {mode} | {fmt(r['recall_at_5'])} | {fmt(r['mrr'])} | {fmt(r['ndcg_at_5'])} |"
        )
    lines.append("")
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    asyncio.run(main())
