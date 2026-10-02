"""Lexical (BM25), dense (cosine) and hybrid (reciprocal rank fusion) indexes.

The hybrid index is used by document retrieval, memory recall and tool
discovery alike, so improvements here benefit every subsystem.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol

from cairn.retrieval.embeddings import Embedder, HashingEmbedder
from cairn.retrieval.text import terms


@dataclass
class ScoredId:
    id: str
    score: float
    parts: dict[str, float] = field(default_factory=dict)


class BM25Index:
    def __init__(self, k1: float = 1.4, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._docs: dict[str, Counter[str]] = {}
        self._lengths: dict[str, int] = {}
        self._df: Counter[str] = Counter()
        self._postings: dict[str, set[str]] = defaultdict(set)

    def __len__(self) -> int:
        return len(self._docs)

    def add(self, doc_id: str, text: str) -> None:
        if doc_id in self._docs:
            self.remove(doc_id)
        counts = Counter(terms(text))
        self._docs[doc_id] = counts
        self._lengths[doc_id] = sum(counts.values())
        for term in counts:
            self._df[term] += 1
            self._postings[term].add(doc_id)

    def remove(self, doc_id: str) -> None:
        counts = self._docs.pop(doc_id, None)
        if counts is None:
            return
        self._lengths.pop(doc_id, None)
        for term in counts:
            self._df[term] -= 1
            self._postings[term].discard(doc_id)
            if self._df[term] <= 0:
                del self._df[term]
                self._postings.pop(term, None)

    def search(self, query: str, k: int = 10) -> list[ScoredId]:
        if not self._docs:
            return []
        n = len(self._docs)
        avg = sum(self._lengths.values()) / n
        scores: dict[str, float] = defaultdict(float)
        for term in set(terms(query)):
            df = self._df.get(term, 0)
            if not df:
                continue
            idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
            for doc_id in self._postings[term]:
                tf = self._docs[doc_id][term]
                norm = tf + self.k1 * (1 - self.b + self.b * self._lengths[doc_id] / avg)
                scores[doc_id] += idf * tf * (self.k1 + 1) / norm
        ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:k]
        return [ScoredId(d, s, {"bm25": s}) for d, s in ranked]


class VectorIndex(Protocol):
    async def add(self, item_id: str, vector: list[float]) -> None: ...

    async def remove(self, item_id: str) -> None: ...

    async def search(self, vector: list[float], k: int = 10) -> list[ScoredId]: ...


class InMemoryVectorIndex:
    """Exact cosine search. Fine to roughly 1e5 vectors; plug an ANN index beyond that."""

    def __init__(self) -> None:
        self._vectors: dict[str, list[float]] = {}

    def __len__(self) -> int:
        return len(self._vectors)

    async def add(self, item_id: str, vector: list[float]) -> None:
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        self._vectors[item_id] = [v / norm for v in vector]

    async def remove(self, item_id: str) -> None:
        self._vectors.pop(item_id, None)

    async def search(self, vector: list[float], k: int = 10) -> list[ScoredId]:
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        q = [v / norm for v in vector]
        scored = [
            (item_id, sum(a * b for a, b in zip(q, vec, strict=False)))
            for item_id, vec in self._vectors.items()
        ]
        scored.sort(key=lambda kv: (-kv[1], kv[0]))
        return [ScoredId(i, s, {"dense": s}) for i, s in scored[:k]]


def reciprocal_rank_fusion(
    rankings: dict[str, list[ScoredId]], k: int = 60, weights: dict[str, float] | None = None
) -> list[ScoredId]:
    """Fuse ranked lists by ``sum(weight / (k + rank))``; robust to score scale."""
    fused: dict[str, ScoredId] = {}
    for name, ranking in rankings.items():
        weight = (weights or {}).get(name, 1.0)
        for rank, item in enumerate(ranking, start=1):
            entry = fused.setdefault(item.id, ScoredId(item.id, 0.0, {}))
            entry.score += weight / (k + rank)
            entry.parts.update(item.parts)
            entry.parts[f"{name}_rank"] = rank
    return sorted(fused.values(), key=lambda s: (-s.score, s.id))


class HybridIndex:
    """BM25 + dense vectors fused with RRF, with optional per-item metadata."""

    def __init__(self, embedder: Embedder | None = None, vectors: VectorIndex | None = None) -> None:
        self.embedder: Embedder = embedder or HashingEmbedder()
        self.lexical = BM25Index()
        self.vectors: VectorIndex = vectors or InMemoryVectorIndex()
        self.texts: dict[str, str] = {}
        self.meta: dict[str, dict[str, Any]] = {}

    def __len__(self) -> int:
        return len(self.texts)

    async def add(self, item_id: str, text: str, meta: dict[str, Any] | None = None) -> None:
        self.texts[item_id] = text
        self.meta[item_id] = dict(meta or {})
        self.lexical.add(item_id, text)
        [vec] = await self.embedder.embed([text])
        await self.vectors.add(item_id, vec)

    async def add_many(self, items: list[tuple[str, str, dict[str, Any] | None]]) -> None:
        if not items:
            return
        vecs = await self.embedder.embed([text for _, text, _ in items])
        for (item_id, text, meta), vec in zip(items, vecs, strict=True):
            self.texts[item_id] = text
            self.meta[item_id] = dict(meta or {})
            self.lexical.add(item_id, text)
            await self.vectors.add(item_id, vec)

    async def remove(self, item_id: str) -> None:
        self.texts.pop(item_id, None)
        self.meta.pop(item_id, None)
        self.lexical.remove(item_id)
        await self.vectors.remove(item_id)

    async def search(
        self, query: str, k: int = 10, *, mode: str = "hybrid", candidates: int = 50
    ) -> list[ScoredId]:
        rankings: dict[str, list[ScoredId]] = {}
        if mode in ("hybrid", "lexical"):
            rankings["bm25"] = self.lexical.search(query, candidates)
        if mode in ("hybrid", "dense"):
            [qvec] = await self.embedder.embed([query])
            rankings["dense"] = await self.vectors.search(qvec, candidates)
        if mode not in ("hybrid", "lexical", "dense"):
            raise ValueError(f"unknown retrieval mode '{mode}'")
        if len(rankings) == 1:
            return next(iter(rankings.values()))[:k]
        return reciprocal_rank_fusion(rankings)[:k]
