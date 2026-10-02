"""Tokenization and chunking."""

from __future__ import annotations

import re
from dataclasses import dataclass

_TOKEN = re.compile(r"[a-z0-9]+(?:['_-][a-z0-9]+)*")

STOPWORDS = frozenset(
    [
     "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have", "in", "is", "it",
     "its", "of", "on", "or", "that", "the", "to", "was", "were", "will", "with", "what", "which",
     "who", "whom", "how", "when", "where", "why", "this", "these", "those", "do", "does", "did",
     "can", "could", "should", "would", "i", "you", "he", "she", "we", "they", "me", "my", "your",
     "our", "their", "them", "his", "her", "not", "no", "but", "if", "then", "than", "so", "such",
     "into", "about", "over", "under", "any", "all", "each", "other", "some", "more", "most", "very",
     "just", "also",
    ]
)


def tokenize(text: str, *, keep_stopwords: bool = False) -> list[str]:
    tokens = _TOKEN.findall(text.lower())
    if keep_stopwords:
        return tokens
    return [t for t in tokens if t not in STOPWORDS]


def stem(token: str) -> str:
    """Tiny suffix stripper; enough to match plurals and common verb forms."""
    for suffix in ("ing", "edly", "ed", "ies", "es", "s"):
        if token.endswith(suffix) and len(token) - len(suffix) >= 3:
            base = token[: -len(suffix)]
            return base + "y" if suffix == "ies" else base
    return token


def terms(text: str) -> list[str]:
    return [stem(t) for t in tokenize(text)]


@dataclass(frozen=True)
class Chunk:
    index: int
    text: str
    start: int
    end: int


_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n{2,}")


def chunk_text(text: str, max_chars: int = 800, overlap_sentences: int = 1) -> list[Chunk]:
    """Split on sentence boundaries into chunks of at most ``max_chars``.

    Overlapping one sentence between chunks keeps facts that straddle a
    boundary retrievable from either side.
    """
    spans: list[tuple[int, int]] = []
    pos = 0
    for match in _SENTENCE.finditer(text):
        if match.start() > pos:
            spans.append((pos, match.start()))
        pos = match.end()
    if pos < len(text):
        spans.append((pos, len(text)))
    spans = [(s, e) for s, e in spans if text[s:e].strip()]
    chunks: list[Chunk] = []
    i = 0
    while i < len(spans):
        start = spans[i][0]
        j = i
        while j + 1 < len(spans) and spans[j + 1][1] - start <= max_chars:
            j += 1
        end = spans[j][1]
        if end - start > max_chars:  # single very long sentence: hard split
            end = start + max_chars
            spans[j] = (end, spans[j][1])
            chunks.append(Chunk(len(chunks), text[start:end].strip(), start, end))
            i = j
            continue
        chunks.append(Chunk(len(chunks), text[start:end].strip(), start, end))
        if j + 1 >= len(spans):
            break
        i = max(i + 1, j + 1 - overlap_sentences)
    return chunks
