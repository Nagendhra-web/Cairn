"""Provenance labels.

A :class:`Label` answers two questions about a value:

* **Integrity**: could an attacker have influenced it? Values derived from web
  pages, retrieved documents, MCP servers or emails are ``UNTRUSTED``. Integrity
  is a two-point lattice and joins with ``min``: anything derived from
  untrusted input is untrusted.
* **Secrecy**: what confidential material is it derived from? Secrecy tags
  (for example ``secret:GITHUB_TOKEN`` or ``pii``) join by union and restrict
  where a value may flow (egress tools).

``sources`` records *where* the value came from (``user``, ``tool:http.fetch``,
``retrieval:docs``) for audit and explanation; it is not used for decisions.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any


class Integrity(IntEnum):
    UNTRUSTED = 0
    TRUSTED = 1


@dataclass(frozen=True, slots=True)
class Label:
    integrity: Integrity = Integrity.TRUSTED
    sources: frozenset[str] = field(default_factory=frozenset)
    secrecy: frozenset[str] = field(default_factory=frozenset)

    @property
    def trusted(self) -> bool:
        return self.integrity is Integrity.TRUSTED

    def join(self, other: Label) -> Label:
        """Least upper bound: the label of a value derived from both inputs."""
        return Label(
            integrity=Integrity(min(self.integrity, other.integrity)),
            sources=self.sources | other.sources,
            secrecy=self.secrecy | other.secrecy,
        )

    def with_source(self, source: str) -> Label:
        return Label(self.integrity, self.sources | {source}, self.secrecy)

    def with_secrecy(self, *tags: str) -> Label:
        return Label(self.integrity, self.sources, self.secrecy | set(tags))

    def taint(self, source: str) -> Label:
        return Label(Integrity.UNTRUSTED, self.sources | {source}, self.secrecy)

    def to_dict(self) -> dict[str, Any]:
        return {
            "integrity": self.integrity.name.lower(),
            "sources": sorted(self.sources),
            "secrecy": sorted(self.secrecy),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Label:
        if not data:
            return BOTTOM
        return cls(
            integrity=Integrity[str(data.get("integrity", "trusted")).upper()],
            sources=frozenset(data.get("sources", ())),
            secrecy=frozenset(data.get("secrecy", ())),
        )

    def describe(self) -> str:
        parts = [self.integrity.name.lower()]
        if self.sources:
            parts.append("from " + ",".join(sorted(self.sources)))
        if self.secrecy:
            parts.append("secret " + ",".join(sorted(self.secrecy)))
        return " ".join(parts)


#: Identity element for ``join``: trusted, no sources, no secrecy.
BOTTOM = Label()
#: Values typed by the operator who started the run.
USER = Label(Integrity.TRUSTED, frozenset({"user"}))
#: Literals authored by a planner that only saw trusted input.
SYSTEM = Label(Integrity.TRUSTED, frozenset({"system"}))


def untrusted(source: str) -> Label:
    return Label(Integrity.UNTRUSTED, frozenset({source}))


def join_all(labels: Iterable[Label]) -> Label:
    result = BOTTOM
    for label in labels:
        result = result.join(label)
    return result
