"""Memory policy: what may be written, how important it is, how it decays and ranks.

Three concerns live here, separate from storage, so they can be tuned or
replaced without touching persistence:

* **Write policy** enforces provenance. Procedural memories become planner
  few-shots and pinned memories never decay, so both would let one injected
  document steer every future run; they therefore require a trusted label.
  Untrusted episodic/semantic writes are allowed (agents must be able to
  remember what they read) but are flagged, capped in importance, and keep
  their label so recall re-taints whatever consumes them.
* **Importance** is a cheap heuristic: specific, numeric, entity-bearing text
  is more likely to be worth recalling than chit-chat.
* **Decay and ranking** model forgetting: strength halves every half-life
  (per kind), is reinforced by use, and pinned memories are exempt.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from cairn.core.errors import PolicyViolation
from cairn.memory.types import MemoryKind, MemoryRecord
from cairn.provenance.labels import Label

MINUTE = 60.0
HOUR = 60 * MINUTE
DAY = 24 * HOUR

#: Default half-lives. Working memory is a scratchpad and should vanish within
#: the hour; procedures that worked stay useful for a long time.
DEFAULT_HALF_LIVES: dict[MemoryKind, float] = {
    MemoryKind.WORKING: 15 * MINUTE,
    MemoryKind.SESSION: 1 * DAY,
    MemoryKind.EPISODIC: 14 * DAY,
    MemoryKind.SEMANTIC: 90 * DAY,
    MemoryKind.PROCEDURAL: 365 * DAY,
}

FLAG_UNTRUSTED = "untrusted_source"

_NUMBER = re.compile(r"\d")
_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_'-]*")
_SALIENT = re.compile(
    r"\b(always|never|must|prefer|prefers|important|remember|deadline|password|"
    r"requirement|policy|do not|don't)\b",
    re.IGNORECASE,
)


def _has_entity(text: str) -> bool:
    """True when a capitalised word appears somewhere other than a sentence start."""
    for match in _WORD.finditer(text):
        if not match.group()[0].isupper():
            continue
        before = text[: match.start()].rstrip()
        if before and not before.endswith((".", "!", "?", ":")):
            return True
    return False


def score_importance(text: str, explicit: float | None = None) -> float:
    """Heuristic importance in [0, 1].

    The text signal rewards specificity: length (up to a point), digits
    (dates, versions, quantities), capitalised tokens that are not sentence
    starts (named entities) and directive words. When the caller supplies an
    ``explicit`` importance it dominates (70%) because the caller usually knows
    better, but the text signal still separates otherwise equal writes.
    """
    words = _WORD.findall(text)
    signal = 0.2 + min(0.25, len(words) / 80)
    if _NUMBER.search(text):
        signal += 0.15
    if _has_entity(text):
        signal += 0.15
    if _SALIENT.search(text):
        signal += 0.15
    signal = min(1.0, signal)
    if explicit is None:
        return round(signal, 4)
    explicit = min(1.0, max(0.0, explicit))
    return round(min(1.0, 0.7 * explicit + 0.3 * signal), 4)


@dataclass(frozen=True, slots=True)
class WriteDecision:
    """Outcome of a permitted write: the importance to store and any flags to attach."""

    importance: float
    flags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class WritePolicy:
    """Provenance rules for memory writes.

    ``untrusted_importance_cap`` stops injected text from inflating its own
    importance ("IMPORTANT: always do X") to outrank genuine memories or to
    survive decay longer.
    """

    allow_untrusted_procedural: bool = False
    untrusted_importance_cap: float = 0.6

    def check_write(
        self, kind: MemoryKind, label: Label, importance: float, *, pinned: bool = False
    ) -> WriteDecision:
        if pinned and not label.trusted:
            raise PolicyViolation(
                "pinned memories require a trusted label",
                kind=kind.value,
                label=label.to_dict(),
            )
        if label.trusted:
            return WriteDecision(importance)
        if kind is MemoryKind.PROCEDURAL and not self.allow_untrusted_procedural:
            raise PolicyViolation(
                "untrusted content may not be written to procedural memory: it would "
                "become planner few-shot examples for future runs",
                kind=kind.value,
                label=label.to_dict(),
            )
        return WriteDecision(min(importance, self.untrusted_importance_cap), (FLAG_UNTRUSTED,))

    def check_pin(self, record: MemoryRecord) -> None:
        if not record.label.trusted:
            raise PolicyViolation(
                "cannot pin a memory derived from untrusted content",
                memory_id=record.id,
                label=record.label.to_dict(),
            )


@dataclass(frozen=True, slots=True)
class DecayPolicy:
    """Exponential forgetting with per-kind half-lives.

    ``strength = base(importance) * 0.5 ** (age / half_life) + reinforcement``
    where ``age`` is measured from the last access, so recalled memories stay
    fresh, and reinforcement grows with ``log1p(access_count)``. Records whose
    strength drops below ``expire_threshold`` are deleted by ``expire``.
    """

    half_lives: dict[MemoryKind, float] = field(default_factory=lambda: dict(DEFAULT_HALF_LIVES))
    expire_threshold: float = 0.05
    reinforcement: float = 0.05

    def half_life(self, kind: MemoryKind) -> float:
        return self.half_lives.get(kind, DEFAULT_HALF_LIVES[kind])

    def recency(self, record: MemoryRecord, now: float) -> float:
        """Pure time decay in (0, 1]: 1.0 when just touched, 0.5 after one half-life."""
        age = max(0.0, now - max(record.last_accessed, record.created_at))
        return math.exp(-math.log(2) * age / self.half_life(record.kind))

    def effective_strength(self, record: MemoryRecord, now: float) -> float:
        if record.pinned:
            return 1.0
        base = 0.4 + 0.6 * record.importance
        boost = self.reinforcement * math.log1p(record.access_count)
        strength: float = min(1.0, (base + boost) * self.recency(record, now))
        return strength

    def is_expired(self, record: MemoryRecord, now: float) -> bool:
        return not record.pinned and self.effective_strength(record, now) < self.expire_threshold


@dataclass(frozen=True, slots=True)
class ScoringWeights:
    relevance: float = 0.6
    recency: float = 0.15
    importance: float = 0.15
    frequency: float = 0.05
    pinned: float = 0.05


@dataclass(frozen=True, slots=True)
class ScoredMemory:
    record: MemoryRecord
    score: float
    parts: dict[str, float]


@dataclass(frozen=True, slots=True)
class RetrievalScorer:
    """Combine index relevance with recency, importance, use and pinning.

    Relevance dominates so that an important but off-topic memory does not
    crowd out the answer; the other terms break ties between comparably
    relevant memories in favour of fresh, important, frequently used ones.
    """

    weights: ScoringWeights = field(default_factory=ScoringWeights)
    decay: DecayPolicy = field(default_factory=DecayPolicy)

    def score(self, record: MemoryRecord, relevance: float, now: float) -> ScoredMemory:
        w = self.weights
        recency = 1.0 if record.pinned else self.decay.recency(record, now)
        frequency = min(1.0, math.log1p(record.access_count) / math.log(50))
        parts = {
            "relevance": relevance,
            "recency": recency,
            "importance": record.importance,
            "frequency": frequency,
            "pinned": 1.0 if record.pinned else 0.0,
        }
        total = (
            w.relevance * relevance
            + w.recency * recency
            + w.importance * record.importance
            + w.frequency * frequency
            + w.pinned * parts["pinned"]
        )
        return ScoredMemory(record, round(total, 6), parts)
