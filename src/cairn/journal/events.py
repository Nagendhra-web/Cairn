"""Journal event model.

The journal is the single source of truth for a run. Every state transition
and every nondeterministic effect is an append-only event; run state is a pure
fold over events (see :mod:`cairn.runtime.state`). Events are hash-chained
per run so tampering with an audit record is detectable.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cairn.core.ids import canonical_json


class EventType(StrEnum):
    RUN_CREATED = "run.created"
    RUN_STARTED = "run.started"
    RUN_SUSPENDED = "run.suspended"
    RUN_COMPLETED = "run.completed"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    RUN_FORKED = "run.forked"
    NODE_STARTED = "node.started"
    NODE_COMPLETED = "node.completed"
    NODE_FAILED = "node.failed"
    NODE_RETRYING = "node.retrying"
    NODE_SKIPPED = "node.skipped"
    NODE_WAITING = "node.waiting"
    EFFECT_COMPLETED = "effect.completed"
    EFFECT_FAILED = "effect.failed"
    EFFECT_DIVERGED = "effect.diverged"
    POLICY_DECISION = "policy.decision"
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    VERIFY_RESULT = "verify.result"
    ROUTE_DECISION = "route.decision"
    SUBRUN_LINKED = "subrun.linked"
    NOTE = "note"


TERMINAL_RUN_EVENTS = frozenset(
    {EventType.RUN_COMPLETED, EventType.RUN_FAILED, EventType.RUN_CANCELLED}
)

GENESIS_HASH = "0" * 64


@dataclass(frozen=True, slots=True)
class EventDraft:
    """An event before the store assigns its sequence number and hash."""

    type: str
    data: dict[str, Any] = field(default_factory=dict)
    node_id: str | None = None
    ts: float = 0.0


@dataclass(frozen=True, slots=True)
class Event:
    run_id: str
    seq: int
    type: str
    data: dict[str, Any]
    node_id: str | None
    ts: float
    prev_hash: str
    hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "seq": self.seq,
            "type": self.type,
            "node_id": self.node_id,
            "ts": self.ts,
            "data": self.data,
            "prev_hash": self.prev_hash,
            "hash": self.hash,
        }


def compute_hash(
    run_id: str,
    seq: int,
    type_: str,
    node_id: str | None,
    ts: float,
    data: dict[str, Any],
    prev_hash: str,
) -> str:
    body = canonical_json(
        {"run_id": run_id, "seq": seq, "type": type_, "node_id": node_id, "ts": ts, "data": data}
    )
    return hashlib.sha256((prev_hash + body).encode("utf-8")).hexdigest()


def seal(run_id: str, seq: int, draft: EventDraft, prev_hash: str) -> Event:
    digest = compute_hash(run_id, seq, draft.type, draft.node_id, draft.ts, draft.data, prev_hash)
    return Event(run_id, seq, draft.type, draft.data, draft.node_id, draft.ts, prev_hash, digest)


def verify_chain(events: list[Event]) -> list[str]:
    """Return a list of integrity problems; empty means the chain is intact."""
    problems: list[str] = []
    prev = GENESIS_HASH
    for expected_seq, ev in enumerate(events, start=1):
        if ev.seq != expected_seq:
            problems.append(f"seq {ev.seq}: expected {expected_seq} (gap or reorder)")
        if ev.prev_hash != prev:
            problems.append(f"seq {ev.seq}: prev_hash does not match previous event")
        actual = compute_hash(ev.run_id, ev.seq, ev.type, ev.node_id, ev.ts, ev.data, ev.prev_hash)
        if actual != ev.hash:
            problems.append(f"seq {ev.seq}: content hash mismatch (event was modified)")
        prev = ev.hash
    return problems
