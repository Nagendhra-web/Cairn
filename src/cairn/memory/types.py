"""Memory record types.

A :class:`MemoryRecord` is the unit every layer stores. It always carries the
provenance :class:`Label` of the text it holds, because memory is a channel
across runs: if a record lost its label, injected content written during one
run could resurface as trusted context in a later run (memory poisoning).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from cairn.provenance.labels import Label


class MemoryKind(StrEnum):
    """The memory layers, ordered roughly from shortest to longest lived.

    * ``working``: the scratchpad of an in-flight step; decays in minutes.
    * ``session``: facts relevant to one conversation or run family; hours.
    * ``episodic``: what happened in a past run (goal, outcome, status); weeks.
    * ``semantic``: distilled facts, often consolidated from episodes; months.
    * ``procedural``: plans that worked, reused as planner few-shots; longest.
    """

    WORKING = "working"
    SESSION = "session"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"

    @classmethod
    def parse(cls, value: str | MemoryKind) -> MemoryKind:
        """Accept enum members or their string values (case-insensitive)."""
        if isinstance(value, MemoryKind):
            return value
        try:
            return cls(value.strip().lower())
        except ValueError:
            allowed = ", ".join(k.value for k in cls)
            raise ValueError(
                f"unknown memory kind '{value}' (expected one of: {allowed})"
            ) from None


@dataclass(slots=True)
class MemoryRecord:
    """One stored memory.

    ``pinned`` means a trusted principal explicitly approved keeping this
    record: it is exempt from decay and expiry. ``superseded_by`` points at the
    consolidated record that replaced it; superseded records are kept for
    audit but excluded from recall.
    """

    id: str
    kind: MemoryKind
    text: str
    label: Label
    importance: float = 0.5
    created_at: float = 0.0
    last_accessed: float = 0.0
    access_count: int = 0
    pinned: bool = False
    source_run: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    embedding: list[float] | None = None
    superseded_by: str | None = None

    def __post_init__(self) -> None:
        self.kind = MemoryKind.parse(self.kind)
        self.importance = min(1.0, max(0.0, float(self.importance)))
        if not self.last_accessed:
            self.last_accessed = self.created_at

    @property
    def trusted(self) -> bool:
        trusted: bool = self.label.trusted
        return trusted

    @property
    def active(self) -> bool:
        """False once the record has been folded into a consolidated memory."""
        return self.superseded_by is None

    def copy(self, **changes: Any) -> MemoryRecord:
        """Return a modified copy; stores hand out copies so callers cannot mutate state."""
        clone = replace(self, **changes)
        clone.metadata = dict(clone.metadata)
        if clone.embedding is not None:
            clone.embedding = list(clone.embedding)
        return clone

    def to_dict(self, *, include_embedding: bool = False) -> dict[str, Any]:
        """JSON-safe view. ``label`` is always present: the executor joins it into results."""
        data: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind.value,
            "text": self.text,
            "label": self.label.to_dict(),
            "trusted": self.label.trusted,
            "importance": self.importance,
            "created_at": self.created_at,
            "last_accessed": self.last_accessed,
            "access_count": self.access_count,
            "pinned": self.pinned,
            "source_run": self.source_run,
            "metadata": dict(self.metadata),
            "superseded_by": self.superseded_by,
        }
        if include_embedding:
            data["embedding"] = list(self.embedding) if self.embedding is not None else None
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> MemoryRecord:
        embedding = data.get("embedding")
        return cls(
            id=str(data["id"]),
            kind=MemoryKind.parse(str(data["kind"])),
            text=str(data["text"]),
            label=Label.from_dict(data.get("label")),
            importance=float(data.get("importance", 0.5)),
            created_at=float(data.get("created_at", 0.0)),
            last_accessed=float(data.get("last_accessed", 0.0)),
            access_count=int(data.get("access_count", 0)),
            pinned=bool(data.get("pinned", False)),
            source_run=data.get("source_run"),
            metadata=dict(data.get("metadata") or {}),
            embedding=[float(v) for v in embedding] if embedding is not None else None,
            superseded_by=data.get("superseded_by"),
        )
