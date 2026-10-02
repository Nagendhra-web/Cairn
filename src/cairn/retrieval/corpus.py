"""Document corpora: chunking, hybrid + graph retrieval, reranking, adaptive search.

A :class:`DocumentCorpus` satisfies the runtime's ``Corpus`` protocol, so a
``retrieve`` node can query it, and every result is journaled as an effect
(replay never depends on the index being unchanged) and labeled with the
corpus's trust level.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cairn.core.ids import stable_hash
from cairn.retrieval.embeddings import Embedder
from cairn.retrieval.graph import KnowledgeGraph
from cairn.retrieval.index import HybridIndex, ScoredId, reciprocal_rank_fusion
from cairn.retrieval.rerank import LexicalReranker, Reranker
from cairn.retrieval.text import STOPWORDS, chunk_text, terms, tokenize

Decomposer = Callable[[str], Awaitable[list[str]]]


@dataclass
class Document:
    id: str
    text: str
    metadata: dict[str, Any] = field(default_factory=dict)


class DocumentCorpus:
    def __init__(
        self,
        name: str = "default",
        *,
        trusted: bool = False,
        embedder: Embedder | None = None,
        reranker: Reranker | None = None,
        graph: bool = True,
        chunk_chars: int = 800,
        decomposer: Decomposer | None = None,
    ) -> None:
        self.name = name
        self.trusted = trusted
        self.index = HybridIndex(embedder)
        self.reranker: Reranker = reranker or LexicalReranker()
        self.graph = KnowledgeGraph() if graph else None
        self.chunk_chars = chunk_chars
        self.decomposer = decomposer
        self.documents: dict[str, Document] = {}
        self._chunks_by_doc: dict[str, list[str]] = {}

    def __len__(self) -> int:
        return len(self.index)

    async def add(self, text: str, *, doc_id: str | None = None, **metadata: Any) -> str:
        doc_id = doc_id or "doc_" + stable_hash(text, length=12)
        if doc_id in self.documents:
            await self.remove(doc_id)
        self.documents[doc_id] = Document(doc_id, text, metadata)
        items: list[tuple[str, str, dict[str, Any] | None]] = []
        chunk_ids: list[str] = []
        for chunk in chunk_text(text, self.chunk_chars):
            chunk_id = f"{doc_id}#{chunk.index}"
            chunk_ids.append(chunk_id)
            items.append((chunk_id, chunk.text, {"doc_id": doc_id, **metadata}))
            if self.graph is not None:
                await self.graph.add_chunk(chunk_id, chunk.text)
        await self.index.add_many(items)
        self._chunks_by_doc[doc_id] = chunk_ids
        return doc_id

    async def add_directory(self, path: str | Path, patterns: tuple[str, ...] = ("*.md", "*.txt")) -> int:
        count = 0
        root = Path(path)
        for pattern in patterns:
            files = await asyncio.to_thread(lambda p=pattern: sorted(root.rglob(p)))
            for file in files:
                text = await asyncio.to_thread(file.read_text, encoding="utf-8", errors="replace")
                await self.add(text, doc_id=str(file.relative_to(root)), source=str(file))
                count += 1
        return count

    async def remove(self, doc_id: str) -> None:
        chunk_ids = self._chunks_by_doc.pop(doc_id, [])
        for chunk_id in chunk_ids:
            await self.index.remove(chunk_id)
        if self.graph is not None:
            self.graph.remove_chunks(chunk_ids)
        self.documents.pop(doc_id, None)

    async def search(self, query: str, k: int = 5, mode: str = "hybrid") -> list[dict[str, Any]]:
        if mode == "adaptive":
            result = await AdaptiveRetriever(self).retrieve(query, k)
            hits: list[dict[str, Any]] = result["hits"]
            return hits
        candidates = await self._candidates(query, max(k * 4, 20), mode)
        ranked: list[dict[str, Any]] = await self.reranker.rerank(query, candidates, k)
        return ranked

    async def _candidates(self, query: str, n: int, mode: str) -> list[dict[str, Any]]:
        rankings: dict[str, list[ScoredId]] = {}
        base = await self.index.search(query, n, mode=mode)
        rankings[mode] = base
        if self.graph is not None and mode == "hybrid":
            related = self.graph.related_chunks(query)
            if related:
                rankings["graph"] = [ScoredId(cid, s, {"graph": s}) for cid, s in related[:n]]
        fused = base if len(rankings) == 1 else reciprocal_rank_fusion(rankings)
        return [self._hit(s) for s in fused[:n] if s.id in self.index.texts]

    def _hit(self, scored: ScoredId) -> dict[str, Any]:
        meta = self.index.meta.get(scored.id, {})
        return {
            "chunk_id": scored.id,
            "doc_id": meta.get("doc_id"),
            "text": self.index.texts[scored.id],
            "score": round(scored.score, 6),
            "signals": {k: round(v, 6) if isinstance(v, float) else v for k, v in scored.parts.items()},
            "collection": self.name,
            "metadata": {k: v for k, v in meta.items() if k != "doc_id"},
        }


def heuristic_decompose(query: str) -> list[str]:
    """Split compound questions into independently answerable sub-queries."""
    parts = [p.strip() for p in re.split(r"\?\s+|;\s*|\n+", query) if p.strip()]
    out: list[str] = []
    for part in parts:
        clauses = re.split(r",?\s+and\s+(?=(?:what|which|who|how|where|when|why|does|is|are)\b)",
                           part, flags=re.I)
        out.extend(c.strip(" ?") for c in clauses if c.strip(" ?"))
    return out or [query]


class AdaptiveRetriever:
    """Self-correcting retrieval loop.

    1. Decompose the query into sub-queries (heuristic, or an injected LLM decomposer).
    2. Retrieve for each sub-query and fuse the rankings.
    3. Measure *sufficiency*: the fraction of the query's content terms that
       appear in the retrieved evidence.
    4. If insufficient, rewrite the query with pseudo-relevance feedback (terms
       that co-occur with the matched evidence) plus the missing terms, and
       retry, up to ``max_rounds``.

    The full trace of rounds is returned so callers (and the journal) can see
    why the retriever stopped.
    """

    def __init__(self, corpus: DocumentCorpus, *, max_rounds: int = 3, threshold: float = 0.8) -> None:
        self.corpus = corpus
        self.max_rounds = max_rounds
        self.threshold = threshold

    async def retrieve(self, query: str, k: int = 5) -> dict[str, Any]:
        decompose = self.corpus.decomposer
        subqueries = await decompose(query) if decompose else heuristic_decompose(query)
        wanted = {t for t in terms(query) if len(t) > 2}
        trace: list[dict[str, Any]] = []
        best: list[dict[str, Any]] = []
        best_cov = -1.0
        current = list(subqueries)
        for round_no in range(1, self.max_rounds + 1):
            rankings: dict[str, list[ScoredId]] = {}
            pool: dict[str, dict[str, Any]] = {}
            for i, sub in enumerate(current):
                hits = await self.corpus.search(sub, k, "hybrid")
                rankings[f"q{i}"] = [ScoredId(h["chunk_id"], h.get("rerank_score", h["score"])) for h in hits]
                for h in hits:
                    pool.setdefault(h["chunk_id"], h)
            fused = reciprocal_rank_fusion(rankings)[:k]
            hits = [pool[s.id] for s in fused]
            evidence = set(terms(" ".join(h["text"] for h in hits)))
            covered = wanted & evidence
            coverage = len(covered) / len(wanted) if wanted else 1.0
            trace.append({"round": round_no, "queries": current, "coverage": round(coverage, 4),
                          "missing": sorted(wanted - evidence)})
            if coverage > best_cov:
                best, best_cov = hits, coverage
            if coverage >= self.threshold:
                break
            current = self._rewrite(query, hits, sorted(wanted - evidence))
        return {"hits": best, "coverage": round(best_cov, 4), "rounds": trace, "subqueries": subqueries}

    @staticmethod
    def _rewrite(query: str, hits: list[dict[str, Any]], missing: list[str]) -> list[str]:
        counts: dict[str, int] = {}
        q = set(tokenize(query))
        for h in hits[:3]:
            for tok in tokenize(h["text"]):
                if tok not in q and tok not in STOPWORDS and len(tok) > 3:
                    counts[tok] = counts.get(tok, 0) + 1
        expansion = [t for t, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:4]]
        rewritten = [f"{query} {' '.join(expansion)}".strip()]
        rewritten.extend(missing[:3])  # also probe each missing concept directly
        return rewritten
