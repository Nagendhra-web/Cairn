"""Knowledge graph extracted from documents, used for multi-hop retrieval.

Vector and lexical search find passages that look like the query. They miss
passages that are only *connected* to it ("which database does the service
that handles billing use?"). The graph links entities across chunks so
retrieval can follow relations one or two hops away from the entities the
query names.

Extraction is pattern-based by default (deterministic, offline). An
LLM-backed extractor can be supplied for higher recall; triples from it carry
the same chunk provenance.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

_ENTITY = r"([A-Z][\w.+-]*(?:\s+[A-Z][\w.+-]*){0,3})"
_RELATIONS: list[tuple[str, re.Pattern[str]]] = [
    (rel, re.compile(_ENTITY + r"\s+" + pat + r"\s+(?:the\s+|a\s+|an\s+)?" + _ENTITY))
    for rel, pat in [
        ("uses", r"(?:uses|use|relies on|is built on|runs on)"),
        ("depends_on", r"(?:depends on|requires)"),
        ("part_of", r"(?:is part of|belongs to|is a component of)"),
        ("owned_by", r"(?:is owned by|is maintained by|is managed by)"),
        ("created_by", r"(?:was created by|was founded by|was developed by|was written by)"),
        ("stores_in", r"(?:stores (?:its )?data in|persists to|writes to)"),
        ("replaced_by", r"(?:was replaced by|is superseded by)"),
        ("is_a", r"(?:is a|is an)"),
        ("located_in", r"(?:is located in|is based in|is headquartered in)"),
    ]
]


@dataclass(frozen=True)
class Triple:
    subject: str
    relation: str
    object: str
    chunk_id: str


Extractor = Callable[[str], Awaitable[list[tuple[str, str, str]]]]


def extract_triples(text: str) -> list[tuple[str, str, str]]:
    out: list[tuple[str, str, str]] = []
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        for relation, pattern in _RELATIONS:
            for match in pattern.finditer(sentence):
                subj, obj = match.group(1).strip(), match.group(2).strip()
                if subj.lower() != obj.lower() and subj.split()[0] not in {"The", "A", "An", "It", "This"}:
                    out.append((subj, relation, obj))
    return out


class KnowledgeGraph:
    def __init__(self, extractor: Extractor | None = None) -> None:
        self.extractor = extractor
        self.triples: list[Triple] = []
        self._by_entity: dict[str, list[int]] = defaultdict(list)
        self._names: dict[str, str] = {}

    @staticmethod
    def norm(name: str) -> str:
        return re.sub(r"\s+", " ", name.strip().lower())

    async def add_chunk(self, chunk_id: str, text: str) -> int:
        found = await self.extractor(text) if self.extractor else extract_triples(text)
        for subj, rel, obj in found:
            self.add(Triple(subj, rel, obj, chunk_id))
        return len(found)

    def add(self, triple: Triple) -> None:
        index = len(self.triples)
        self.triples.append(triple)
        for name in (triple.subject, triple.object):
            key = self.norm(name)
            self._names.setdefault(key, name)
            self._by_entity[key].append(index)

    def remove_chunks(self, chunk_ids: Iterable[str]) -> None:
        drop = set(chunk_ids)
        kept = [t for t in self.triples if t.chunk_id not in drop]
        self.triples, self._by_entity, self._names = [], defaultdict(list), {}
        for t in kept:
            self.add(t)

    @property
    def entities(self) -> list[str]:
        return sorted(self._names.values())

    def mentioned(self, text: str) -> list[str]:
        lowered = f" {self.norm(text)} "
        return [key for key in self._names if f" {key} " in lowered or f" {key}?" in lowered
                or f" {key}," in lowered or f" {key}." in lowered]

    def neighbors(self, entity: str, hops: int = 1) -> list[Triple]:
        frontier = {self.norm(entity)}
        seen_entities = set(frontier)
        result: list[Triple] = []
        seen_triples: set[int] = set()
        for _ in range(hops):
            nxt: set[str] = set()
            for key in frontier:
                for idx in self._by_entity.get(key, []):
                    if idx in seen_triples:
                        continue
                    seen_triples.add(idx)
                    t = self.triples[idx]
                    result.append(t)
                    for other in (self.norm(t.subject), self.norm(t.object)):
                        if other not in seen_entities:
                            seen_entities.add(other)
                            nxt.add(other)
            frontier = nxt
        return result

    def related_chunks(self, query: str, hops: int = 2) -> list[tuple[str, float]]:
        """Chunk ids connected to entities named in the query, closer hops scoring higher."""
        scores: dict[str, float] = defaultdict(float)
        for entity in self.mentioned(query):
            for hop in range(1, hops + 1):
                for t in self.neighbors(entity, hop):
                    scores[t.chunk_id] = max(scores[t.chunk_id], 1.0 / hop)
        return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))

    def to_dict(self) -> dict[str, Any]:
        return {
            "entities": len(self._names),
            "triples": [t.__dict__ for t in self.triples],
        }
