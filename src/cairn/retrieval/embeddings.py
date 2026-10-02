"""Embedding interfaces and a dependency-free local embedder.

:class:`HashingEmbedder` is a feature-hashing embedder over stemmed unigrams,
bigrams and character trigrams. It is not a neural model: it captures lexical
and morphological similarity, not deep semantics. It exists so that the whole
system (memory, tool discovery, retrieval, evaluation) runs offline and
deterministically. Swap in :class:`ProviderEmbedder` for a real embedding
model; nothing else changes.
"""

from __future__ import annotations

import hashlib
import math
from typing import Protocol

from cairn.models.base import EmbeddingProvider
from cairn.retrieval.text import terms, tokenize


class Embedder(Protocol):
    dim: int
    name: str

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashingEmbedder:
    def __init__(self, dim: int = 512) -> None:
        self.dim = dim
        self.name = f"hashing-{dim}"

    def _bucket(self, feature: str) -> tuple[int, float]:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "little")
        return value % self.dim, 1.0 if (value >> 63) & 1 else -1.0

    def embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        words = terms(text)
        features: list[tuple[str, float]] = [(f"w:{w}", 1.0) for w in words]
        features += [(f"b:{a}_{b}", 0.7) for a, b in zip(words, words[1:], strict=False)]
        for word in tokenize(text):
            padded = f"#{word}#"
            features += [(f"c:{padded[i:i + 3]}", 0.3) for i in range(len(padded) - 2)]
        for feature, weight in features:
            idx, sign = self._bucket(feature)
            vec[idx] += sign * weight
        norm = math.sqrt(sum(v * v for v in vec))
        return [v / norm for v in vec] if norm else vec

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self.embed_one(t) for t in texts]


class ProviderEmbedder:
    """Adapter from an :class:`EmbeddingProvider` (OpenAI-compatible, local server...)."""

    def __init__(self, provider: EmbeddingProvider, model: str, dim: int, batch_size: int = 64) -> None:
        self.provider = provider
        self.model = model
        self.dim = dim
        self.batch_size = batch_size
        self.name = f"{provider.name}/{model}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            out.extend(await self.provider.embed(self.model, texts[i : i + self.batch_size]))
        return out


def cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0
