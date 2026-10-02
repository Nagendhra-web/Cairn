"""Labeled values: data paired with its provenance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from cairn.provenance.labels import BOTTOM, Label


@dataclass(frozen=True, slots=True)
class Labeled:
    value: Any
    label: Label = BOTTOM

    def to_dict(self) -> dict[str, Any]:
        return {"value": self.value, "label": self.label.to_dict()}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Labeled:
        return cls(value=data.get("value"), label=Label.from_dict(data.get("label")))


def resolve_path(value: Any, path: str) -> Any:
    """Walk ``a.b[0].c`` style paths through dicts, lists and attributes.

    Raises ``KeyError`` with the failing segment so plan validation can report
    precise errors when a planner references a field that does not exist.
    """
    if not path:
        return value
    current = value
    for raw in _split_path(path):
        if isinstance(raw, int):
            if not isinstance(current, list | tuple) or raw >= len(current) or raw < -len(current):
                raise KeyError(f"index [{raw}] out of range")
            current = current[raw]
        elif isinstance(current, dict):
            if raw not in current:
                raise KeyError(f"missing key '{raw}'")
            current = current[raw]
        elif hasattr(current, raw) and not raw.startswith("_"):
            current = getattr(current, raw)
        else:
            raise KeyError(f"cannot read '{raw}' from {type(current).__name__}")
    return current


def _split_path(path: str) -> list[str | int]:
    out: list[str | int] = []
    for part in path.split("."):
        if not part:
            continue
        while "[" in part:
            head, _, rest = part.partition("[")
            if head:
                out.append(head)
            index, _, part = rest.partition("]")
            out.append(int(index))
        if part:
            out.append(part)
    return out
