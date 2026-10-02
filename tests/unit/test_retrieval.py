"""Retrieval: BM25, dense, hybrid RRF, chunking, reranking, knowledge graph, adaptive loop."""

from __future__ import annotations

from cairn.provenance import Label
from cairn.retrieval import (
    AdaptiveRetriever,
    BM25Index,
    DocumentCorpus,
    HashingEmbedder,
    HybridIndex,
    KnowledgeGraph,
    LexicalReranker,
    ScoredId,
    chunk_text,
    cosine,
    extract_triples,
    heuristic_decompose,
    reciprocal_rank_fusion,
)
from cairn.runtime import Plan, RetrieveNode

DOCS = {
    "pg": "Postgres is a relational database. It supports transactions and MVCC concurrency control.",
    "redis": "Redis is an in-memory key value store often used as a cache with TTL expiration.",
    "kafka": "Kafka is a distributed log used for event streaming between services.",
    "billing": "The Billing Service uses Postgres. The Billing Service is owned by the Payments Team.",
    "payments": "The Payments Team is located in Berlin.",
}


def test_bm25_ranks_exact_terms():
    idx = BM25Index()
    for k, v in DOCS.items():
        idx.add(k, v)
    assert idx.search("in-memory cache", 1)[0].id == "redis"
    idx.remove("redis")
    assert all(h.id != "redis" for h in idx.search("cache", 5))


async def test_hashing_embedder_similarity_is_meaningful():
    emb = HashingEmbedder()
    a, b, c = await emb.embed(["database transactions", "transactional databases", "event streaming"])
    assert cosine(a, b) > cosine(a, c)


async def test_hybrid_index_fuses_signals():
    idx = HybridIndex()
    await idx.add_many([(k, v, None) for k, v in DOCS.items()])
    hits = await idx.search("which store keeps data in memory", 2)
    assert hits[0].id == "redis"
    assert {"bm25_rank", "dense_rank"} & set(hits[0].parts)


def test_rrf_rewards_agreement():
    fused = reciprocal_rank_fusion({
        "a": [ScoredId("x", 1), ScoredId("y", 0.5)],
        "b": [ScoredId("y", 1), ScoredId("x", 0.9)],
        "c": [ScoredId("y", 1)],
    })
    assert fused[0].id == "y"


def test_chunking_respects_size_and_overlap():
    text = " ".join(f"Sentence number {i} talks about topic {i % 3}." for i in range(40))
    chunks = chunk_text(text, max_chars=200)
    assert all(len(c.text) <= 200 for c in chunks)
    assert len(chunks) > 5
    # one-sentence overlap between consecutive chunks
    assert chunks[0].text.split(". ")[-1].rstrip(".") in chunks[1].text


async def test_reranker_prefers_full_coverage():
    hits = [{"text": "postgres is fast"}, {"text": "postgres supports transactions and mvcc"}]
    ranked = await LexicalReranker().rerank("postgres transactions mvcc", hits, 2)
    assert ranked[0]["text"].startswith("postgres supports")


def test_triple_extraction_and_graph_hops():
    triples = extract_triples(DOCS["billing"])
    assert ("Billing Service", "uses", "Postgres") in triples
    assert ("Billing Service", "owned_by", "Payments Team") in triples


async def test_graph_finds_multi_hop_evidence():
    g = KnowledgeGraph()
    for k, v in DOCS.items():
        await g.add_chunk(k, v)
    chunks = dict(g.related_chunks("Where is the team that owns the Billing Service located?"))
    assert "billing" in chunks and "payments" in chunks
    assert chunks["billing"] > chunks["payments"]  # one hop beats two hops


async def test_corpus_search_and_trust_labels(runtime):
    corpus = DocumentCorpus("kb", trusted=False)
    for k, v in DOCS.items():
        await corpus.add(v, doc_id=k)
    hits = await corpus.search("Where is the Payments Team located?", 2)
    assert hits[0]["doc_id"] == "payments"
    runtime.services.corpora["kb"] = corpus
    plan = Plan(goal="r", nodes=[RetrieveNode(id="r", query="Kafka event streaming", collection="kb", k=1)])
    result = await runtime.run(plan)
    assert result.output[0]["doc_id"] == "kafka"
    assert not Label.from_dict(result.label).trusted
    assert "retrieval:kb" in result.label["sources"]


async def test_corpus_remove_document():
    corpus = DocumentCorpus()
    await corpus.add(DOCS["redis"], doc_id="redis")
    await corpus.remove("redis")
    assert await corpus.search("redis cache", 3) == []


def test_heuristic_decomposition():
    parts = heuristic_decompose("What database does billing use and who owns the billing service?")
    assert len(parts) == 2


async def test_adaptive_retrieval_reports_coverage_and_rounds():
    corpus = DocumentCorpus()
    for k, v in DOCS.items():
        await corpus.add(v, doc_id=k)
    result = await AdaptiveRetriever(corpus).retrieve(
        "Which database does the billing service use and where is the payments team located?", k=3)
    docs = {h["doc_id"] for h in result["hits"]}
    assert {"billing", "payments"} <= docs
    assert result["coverage"] > 0.6
    assert result["rounds"][0]["queries"]
