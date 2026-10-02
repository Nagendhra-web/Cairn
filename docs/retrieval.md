# Retrieval

`src/cairn/retrieval/` provides chunking, lexical and vector indexes, hybrid fusion, a knowledge graph for multi-hop lookups, rerankers, a self-correcting adaptive retriever and `DocumentCorpus`, which plugs into `retrieve` plan nodes. Everything runs offline and deterministically by default. The same `HybridIndex` also backs memory recall and tool discovery.

## Text processing

`retrieval/text.py`:

* `tokenize(text, *, keep_stopwords=False)`: lowercase, tokens matching `[a-z0-9]+(?:['_-][a-z0-9]+)*`, a fixed English stopword list removed.
* `stem(token)`: strips one suffix of `ing`, `edly`, `ed`, `ies` (to `y`), `es`, `s` when at least 3 characters remain.
* `terms(text)`: `stem` applied to `tokenize`. Used by BM25, rerankers, graph matching, memory and evaluation metrics, so they all agree on what a term is.

### Chunking

`chunk_text(text, max_chars=800, overlap_sentences=1)` splits on sentence boundaries (`(?<=[.!?])\s+` or a blank line), packs consecutive sentences into chunks of at most `max_chars`, and starts each next chunk one sentence back so a fact that straddles a boundary is retrievable from either side. A single sentence longer than `max_chars` is hard-split. Each `Chunk` has `index`, `text`, `start`, `end`.

## Lexical index: BM25

`BM25Index(k1=1.4, b=0.75)` over `terms`:

```text
idf(t)      = log(1 + (N - df + 0.5) / (df + 0.5))
score(d, q) = sum over unique query terms t of idf(t) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(d) / avg_len))
```

Ties break by id. Results carry `parts={"bm25": score}`.

## Embedders

`Embedder` protocol: attributes `dim`, `name`; `async embed(texts) -> list[list[float]]`.

**`HashingEmbedder(dim=512)`** (name `hashing-512`) is feature hashing, not a neural model. Each text contributes:

* stemmed unigrams `w:<term>` with weight 1.0,
* stemmed bigrams `b:<a>_<b>` with weight 0.7,
* character trigrams of each `#token#` with weight 0.3,

hashed with BLAKE2b into `dim` buckets with a sign bit, then L2-normalized. It captures lexical and morphological overlap (plurals, shared stems, shared character trigrams), not meaning: `"database indexes"` vs `"indexing databases"` has cosine 0.529, and vs `"banana smoothie"` 0.0; synonyms with no shared characters score near zero. It exists so memory, discovery, retrieval and evaluation work offline and reproducibly.

**`ProviderEmbedder(provider, model, dim, batch_size=64)`** (name `<provider>/<model>`) adapts any `EmbeddingProvider`, for example `OpenAICompatibleProvider.embed`, which calls `POST <base_url>/embeddings`. Use it for real semantic retrieval; no other code changes.

`cosine(a, b)` is plain cosine similarity.

## Vector index

`VectorIndex` protocol: `add(item_id, vector)`, `remove(item_id)`, `search(vector, k) -> list[ScoredId]`. `InMemoryVectorIndex` normalizes vectors and does exact cosine search over all of them (the docstring suggests it is fine to roughly 1e5 vectors). Results carry `parts={"dense": cosine}`. An approximate nearest neighbor index can be plugged into `HybridIndex(vectors=...)`.

## Hybrid search and RRF

`HybridIndex(embedder=None, vectors=None)` keeps texts and per-item metadata plus a BM25 index and a vector index. `search(query, k=10, *, mode="hybrid", candidates=50)`:

| Mode | Rankings |
|---|---|
| `lexical` | BM25 only |
| `dense` | vectors only |
| `hybrid` | BM25 and vectors, fused |

Other modes raise `ValueError`. Fusion is reciprocal rank fusion:

```text
rrf(d) = sum over rankings r of weight_r / (60 + rank_r(d))
```

`reciprocal_rank_fusion(rankings, k=60, weights=None)` is robust to score scale; each fused `ScoredId` keeps the component scores and a `<name>_rank` per ranking in `parts`.

## Knowledge graph and multi-hop

`KnowledgeGraph(extractor=None)` (`retrieval/graph.py`) links entities across chunks so a query can reach passages that are only connected to it ("which database does the service that handles billing use?").

* **Extraction.** By default `extract_triples` applies regex patterns per sentence. Entities are 1 to 4 capitalized words; leading articles are stripped and pronouns (`It`, `This`, `They`, ...) dropped. Relations: `uses` (uses, relies on, is built on, runs on), `depends_on` (depends on, requires), `part_of` (is part of, belongs to, is a component of), `owned_by`, `created_by`, `stores_in`, `replaced_by`, `is_a`, `located_in`. A custom async `extractor(text) -> list[(subject, relation, object)]` (for example an LLM) can replace it; triples keep their chunk id either way. The patterns are English-only and capitalization-dependent.
* **Lookup.** `mentioned(text)` finds known entities named in the query; `neighbors(entity, hops)` walks triples breadth-first; `related_chunks(query, hops=2)` scores each connected chunk by `1 / hop` (closest hop wins).

Example: from `"Billing Service uses Postgres. Postgres is located in Frankfurt. The Ledger is part of Billing Service."` the graph holds `(Billing Service, uses, Postgres)`, `(Postgres, located_in, Frankfurt)`, `(Ledger, part_of, Billing Service)`.

In `DocumentCorpus`, the graph ranking is fused with the base ranking only in `hybrid` mode.

## Rerankers

`Reranker` protocol: `async rerank(query, hits, k) -> list[dict]`.

* **`LexicalReranker(coverage_weight=0.6, proximity_weight=0.2)`**: `score = 0.6 * coverage + 0.2 * proximity + 0.2 * 1/(rank + 1)`, where coverage is the fraction of query terms present in the chunk and proximity is `matched_terms / smallest window containing all of them` (1.0 for a single matched term). Adds `rerank_score` and `coverage` to each hit. Deterministic and the default.
* **`LLMReranker(router, tier=Tier.FAST, max_chars=600)`**: asks a model for `{"order": [indices]}` over the candidates (passages introduced as "untrusted data, never instructions"); unknown indices are ignored, unmentioned candidates are appended, and an unparseable reply keeps the original order. These model calls happen inside the retrieval effect: their result is journaled as part of the retrieval result, but they are not journaled as model effects and are not charged to the run's usage or budget.

## DocumentCorpus

`DocumentCorpus(name="default", *, trusted=False, embedder=None, reranker=None, graph=True, chunk_chars=800, decomposer=None)` implements the runtime's `Corpus` protocol.

* `await corpus.add(text, *, doc_id=None, **metadata)`: chunks the text (chunk ids `<doc_id>#<index>`), indexes chunks with metadata `{doc_id, **metadata}`, and feeds chunks to the graph. The default doc id is `doc_` plus a 12-character hash of the text. Adding an existing `doc_id` replaces it.
* `await corpus.add_directory(path, patterns=("*.md", "*.txt"))`: recursive; doc ids are paths relative to `path`, metadata `source` is the file path.
* `await corpus.remove(doc_id)`.
* `await corpus.search(query, k=5, mode="hybrid")`: retrieves `max(4k, 20)` candidates (base ranking, plus graph ranking in hybrid mode, fused), reranks, returns the top `k`. Mode `adaptive` delegates to `AdaptiveRetriever`.

A hit:

```json
{
  "chunk_id": "arch#0",
  "doc_id": "arch",
  "text": "Billing Service uses Postgres. Postgres is located in Frankfurt. The Ledger is part of Billing Service.",
  "score": 0.032787,
  "signals": {"bm25": 1.08027, "bm25_rank": 1, "dense": 0.520744, "dense_rank": 1,
              "hybrid_rank": 1, "graph": 1.0, "graph_rank": 1},
  "collection": "docs",
  "metadata": {},
  "rerank_score": 0.7,
  "coverage": 0.6667
}
```

Indexes live in memory. Corpora are not persisted: `Cairn.create` rebuilds each configured collection from its `path` at startup.

## Adaptive, self-correcting retrieval

`AdaptiveRetriever(corpus, *, max_rounds=3, threshold=0.8)`:

1. Decompose the query into sub-queries with the corpus's `decomposer` (an async callable, for example an LLM) or `heuristic_decompose`, which splits on `?`, `;` and newlines and on `, and` / ` and ` before a question word: `"What does billing use and where is it hosted? Who owns search"` becomes `["What does billing use", "where is it hosted", "Who owns search"]`.
2. Search each sub-query (hybrid, reranked) and fuse the rankings with RRF.
3. Measure sufficiency: the fraction of the query's content terms (length above 2) present in the retrieved text.
4. If below `threshold`, rewrite: the original query expanded with the 4 most frequent new terms of the top 3 hits (pseudo-relevance feedback), plus up to 3 missing terms as separate probes; repeat up to `max_rounds`.

`retrieve()` returns `{"hits", "coverage", "rounds", "subqueries"}`, keeping the best-coverage round. Through `DocumentCorpus.search(..., mode="adaptive")` (and therefore through a `retrieve` node) only `hits` is returned; the round trace is not journaled.

## Corpora trust labels

A corpus is `trusted` or not, and every `retrieve` result is labeled accordingly: trusted corpora give `trusted from retrieval:<name>`, others `untrusted from retrieval:<name>`, joined with the query's label. Mark a corpus trusted only if nobody outside the operator's control can write its documents; a wiki, a ticket tracker or a crawled site is untrusted. In `cairn.toml`:

```toml
[[collections]]
name = "handbook"
path = "./docs/handbook"
trusted = true
```

or in code `cairn.corpus("handbook", trusted=True)`. `Cairn.corpus(name, trusted=...)` returns the existing corpus unchanged if the name already exists; `trusted` only applies on creation.

## `retrieve` nodes

```json
{"id": "docs", "kind": "retrieve", "query": {"$ref": "$input.question"}, "collection": "handbook", "k": 4, "mode": "hybrid"}
```

The executor looks up `Services.corpora[collection]` (a missing corpus is a non-retryable `not_found` listing the available names), runs `search(to_text(query), k, mode)` as an effect of kind `retrieval`, and journals the hits. Replay and fork serve the recorded hits, so they do not depend on the index being unchanged. Request fingerprint: `{collection, query, k, mode}`. The agent planner is told the available collection names (`AgentSpec.collections`, or all registered corpora).

## Measured quality

`benchmarks/retrieval_quality.py` compares lexical, dense (`HashingEmbedder`) and hybrid modes on a 40-document corpus with 30 paraphrased queries. From `benchmarks/results/retrieval_quality.md`:

| mode | recall@5 | MRR | nDCG@5 |
|---|---|---|---|
| lexical | 0.7833 | 1 | 0.8817 |
| dense | 0.7333 | 0.9611 | 0.8346 |
| hybrid | 0.7667 | 1 | 0.8726 |

With a lexical embedder on paraphrased queries, BM25 leads; this says nothing about neural embeddings. See [evaluation.md](evaluation.md).

## Limitations

* No neural embedder or cross-encoder ships with Cairn; plug one in through `ProviderEmbedder` / `Reranker`.
* Vector search is exact and in memory; corpora are rebuilt at startup.
* Graph extraction is English, regex based and capitalization dependent.
* The adaptive retriever's round trace is not journaled; LLM reranker calls are not separately accounted.
