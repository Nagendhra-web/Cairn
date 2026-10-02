"""Versioned JSONL evaluation datasets.

A result is only meaningful next to the exact data that produced it. Every
dataset therefore declares a name and version (a header record on the first
line, or a ``<file>.meta.json`` sidecar), and loading computes a SHA-256 over
the canonical form of its rows. Results record that hash, so two experiments
can be compared only when they ran on identical cases, and silent edits to a
dataset ("just fixed one label") are visible as a hash change.

Header record example (first non-blank line)::

    {"dataset": "qa-smoke", "version": "1.0.0", "description": "..."}

The hash covers rows only, in file order, not the header. Bumping a version
string or rewording a description does not change what was measured; editing,
adding, removing or reordering a case does.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from cairn.core.errors import CairnError
from cairn.core.ids import canonical_json


class DatasetError(CairnError):
    code = "dataset_invalid"


class Expectations(BaseModel):
    """What a case's run is expected to do. Every field is optional.

    Expectations are declarative so the same dataset can be scored by
    different runners, and so a reader can audit what "pass" means without
    reading scorer code.
    """

    model_config = ConfigDict(extra="forbid")

    status: str | None = "completed"
    output_equals: Any = None
    output_contains: list[str] = Field(default_factory=list)
    reference: str | None = None
    min_f1: float | None = None
    expected_tools: list[str] | None = None
    tool_sequence: list[str] | None = None
    forbidden_tools: list[str] = Field(default_factory=list)
    max_model_calls: int | None = None
    max_tool_calls: int | None = None
    evidence: list[str] = Field(default_factory=list)
    min_groundedness: float | None = None
    judge: bool = False
    custom: dict[str, Any] = Field(default_factory=dict)


class EvalCase(BaseModel):
    """One evaluation case: a goal or a full plan, inputs, expectations and tags.

    ``plan`` is Plan IR JSON (see :mod:`cairn.runtime.plan`); runners that
    plan dynamically can instead use ``goal`` and ignore ``plan``.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    goal: str = ""
    plan: dict[str, Any] | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    expectations: Expectations = Field(default_factory=Expectations)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


def content_hash(rows: Iterable[Any]) -> str:
    """SHA-256 over canonical JSON of each row, newline-joined, in order.

    Canonical JSON (sorted keys, no whitespace) makes the hash independent
    of formatting and key order in the file, so re-serializing a dataset with
    another tool does not change its identity.
    """
    digest = hashlib.sha256()
    for i, row in enumerate(rows):
        if i:
            digest.update(b"\n")
        data = row.model_dump(mode="json") if isinstance(row, BaseModel) else row
        digest.update(canonical_json(data).encode("utf-8"))
    return digest.hexdigest()


@dataclass
class RawDataset:
    """A versioned JSONL file whose rows are left as plain dicts."""

    name: str
    version: str
    rows: list[dict[str, Any]]
    hash: str
    path: str | None = None
    header: dict[str, Any] = field(default_factory=dict)

    def ref(self) -> dict[str, Any]:
        """The identity recorded next to results."""
        return {
            "name": self.name,
            "version": self.version,
            "hash": self.hash,
            "rows": len(self.rows),
            "path": self.path,
        }


@dataclass
class Dataset:
    """A versioned list of :class:`EvalCase`."""

    name: str
    version: str
    cases: list[EvalCase]
    hash: str
    path: str | None = None
    header: dict[str, Any] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.cases)

    def __iter__(self) -> Iterator[EvalCase]:
        return iter(self.cases)

    def ref(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "hash": self.hash,
            "rows": len(self.cases),
            "path": self.path,
        }

    def filter(self, tags: Iterable[str]) -> Dataset:
        """Subset by tag. The subset gets its own hash because it is different data."""
        wanted = set(tags)
        cases = [c for c in self.cases if wanted & set(c.tags)]
        return Dataset(self.name, self.version, cases, content_hash(cases), self.path,
                       dict(self.header, filtered_by=sorted(wanted)))

    @classmethod
    def from_cases(
        cls, name: str, version: str, cases: Sequence[EvalCase | dict[str, Any]]
    ) -> Dataset:
        parsed = [c if isinstance(c, EvalCase) else EvalCase.model_validate(c) for c in cases]
        _check_unique([c.id for c in parsed], name)
        return cls(name, version, parsed, content_hash(parsed))


def _check_unique(ids: Sequence[str], name: str) -> None:
    seen: set[str] = set()
    dupes: set[str] = set()
    for case_id in ids:
        if case_id in seen:
            dupes.add(case_id)
        seen.add(case_id)
    if dupes:
        raise DatasetError(f"dataset '{name}' has duplicate case ids: {sorted(dupes)}")


def sidecar_path(path: Path) -> Path:
    return path.with_name(path.stem + ".meta.json")


def load_jsonl(path: str | Path) -> RawDataset:
    """Load a versioned JSONL file without interpreting its rows.

    Raises :class:`DatasetError` if the file has neither a header record nor
    a sidecar, or if a line is not valid JSON (with the line number).
    """
    p = Path(path)
    header: dict[str, Any] | None = None
    rows: list[dict[str, Any]] = []
    with p.open(encoding="utf-8") as fh:
        for lineno, line in enumerate(fh, start=1):
            text = line.strip()
            if not text:
                continue
            try:
                record = json.loads(text)
            except json.JSONDecodeError as exc:
                raise DatasetError(f"{p}:{lineno}: invalid JSON: {exc.msg}") from exc
            if not isinstance(record, dict):
                raise DatasetError(f"{p}:{lineno}: each line must be a JSON object")
            if header is None and not rows and "dataset" in record and "id" not in record:
                header = record
                continue
            rows.append(record)
    if header is None:
        side = sidecar_path(p)
        if side.exists():
            header = json.loads(side.read_text(encoding="utf-8"))
        else:
            raise DatasetError(
                f"{p} has no header record and no sidecar {side.name}; "
                "datasets must declare a name and version"
            )
    assert header is not None
    if "version" not in header:
        raise DatasetError(f"{p}: dataset header must include 'version'")
    name = str(header.get("dataset") or p.stem)
    return RawDataset(name, str(header["version"]), rows, content_hash(rows), str(p), header)


def load_dataset(path: str | Path) -> Dataset:
    """Load a versioned JSONL file of :class:`EvalCase` rows."""
    raw = load_jsonl(path)
    cases: list[EvalCase] = []
    for i, row in enumerate(raw.rows, start=1):
        try:
            cases.append(EvalCase.model_validate(row))
        except Exception as exc:
            raise DatasetError(
                f"{raw.path}: row {i} ({row.get('id')!r}) is invalid: {exc}"
            ) from exc
    _check_unique([c.id for c in cases], raw.name)
    # Hash the validated rows so defaults filled in by the model are covered too.
    return Dataset(raw.name, raw.version, cases, content_hash(cases), raw.path, raw.header)


def write_jsonl(
    path: str | Path,
    name: str,
    version: str,
    rows: Sequence[dict[str, Any] | BaseModel],
    **header: Any,
) -> str:
    """Write a dataset with a header record; returns the content hash."""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    data = [r.model_dump(mode="json") if isinstance(r, BaseModel) else r for r in rows]
    lines = [json.dumps({"dataset": name, "version": version, **header}, ensure_ascii=False)]
    lines += [json.dumps(r, ensure_ascii=False, sort_keys=True) for r in data]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return content_hash(data)
