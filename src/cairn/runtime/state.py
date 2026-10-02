"""Run state as a pure fold over journal events (event sourcing).

Nothing about a run lives only in memory. The executor, the CLI inspector, the
HTTP API and the trace viewer all reconstruct state with :func:`fold`, which
is why a crashed run can resume exactly where it stopped and why inspection
never disagrees with execution.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cairn.journal.events import Event, EventType
from cairn.provenance.labels import BOTTOM, Label
from cairn.provenance.values import Labeled
from cairn.runtime.budget import Budget, Usage
from cairn.runtime.plan import Plan


@dataclass
class EffectRecord:
    key: str
    kind: str
    fingerprint: str
    result: Any = None
    label: Label = BOTTOM
    error: dict[str, Any] | None = None
    seq: int = 0


@dataclass
class NodeState:
    status: str = "pending"  # pending|running|completed|failed|skipped|waiting
    attempts: int = 0
    output: Labeled | None = None
    error: dict[str, Any] | None = None
    started_at: float | None = None
    ended_at: float | None = None
    waiting_on: str | None = None
    control: Label = BOTTOM


@dataclass
class ApprovalState:
    request_id: str
    node_id: str | None
    status: str = "pending"  # pending|approved|rejected
    reason: str = ""
    preview: dict[str, Any] = field(default_factory=dict)
    decided_by: str | None = None
    note: str | None = None


@dataclass
class RunState:
    run_id: str
    plan: Plan | None = None
    goal: str = ""
    status: str = "created"
    label: Label = BOTTOM
    grants: list[str] = field(default_factory=lambda: ["*"])
    budget: Budget = field(default_factory=Budget)
    inputs: dict[str, Any] = field(default_factory=dict)
    depth: int = 0
    agent: str | None = None
    parent_run_id: str | None = None
    nodes: dict[str, NodeState] = field(default_factory=dict)
    effects: dict[str, EffectRecord] = field(default_factory=dict)
    approvals: dict[str, ApprovalState] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)
    output: Labeled | None = None
    error: dict[str, Any] | None = None
    started_at: float | None = None
    ended_at: float | None = None
    last_seq: int = 0
    children: list[str] = field(default_factory=list)

    def node(self, node_id: str) -> NodeState:
        return self.nodes.setdefault(node_id, NodeState())

    @property
    def pending_approvals(self) -> list[ApprovalState]:
        return [a for a in self.approvals.values() if a.status == "pending"]


def fold(run_id: str, events: list[Event], state: RunState | None = None) -> RunState:
    st = state or RunState(run_id=run_id)
    for ev in events:
        apply(st, ev)
    return st


def apply(st: RunState, ev: Event) -> None:  # noqa: C901 - a flat dispatch is clearest here
    d = ev.data
    t = ev.type
    st.last_seq = ev.seq
    if t == EventType.RUN_CREATED:
        st.plan = Plan.model_validate(d["plan"])
        st.goal = d.get("goal", st.plan.goal)
        st.label = Label.from_dict(d.get("label"))
        st.grants = list(d.get("grants", ["*"]))
        st.budget = Budget.from_dict(d.get("budget"))
        st.inputs = d.get("inputs", {})
        st.depth = int(d.get("depth", 0))
        st.agent = d.get("agent")
        st.parent_run_id = d.get("parent_run_id")
        for node in st.plan.nodes:
            st.nodes.setdefault(node.id, NodeState())
    elif t == EventType.RUN_STARTED:
        st.status = "running"
        if st.started_at is None:
            st.started_at = ev.ts
    elif t == EventType.NODE_STARTED:
        ns = st.node(ev.node_id or "")
        ns.status = "running"
        ns.attempts = max(ns.attempts, int(d.get("attempt", 1)))
        ns.started_at = ns.started_at or ev.ts
        ns.waiting_on = None
    elif t == EventType.NODE_COMPLETED:
        ns = st.node(ev.node_id or "")
        ns.status = "completed"
        ns.output = Labeled(d.get("output"), Label.from_dict(d.get("label")))
        ns.error = None
        ns.ended_at = ev.ts
        if "control" in d:
            ns.control = Label.from_dict(d["control"])
    elif t == EventType.NODE_FAILED:
        ns = st.node(ev.node_id or "")
        ns.error = d.get("error")
        if d.get("final", True):
            ns.status = "failed"
            ns.ended_at = ev.ts
    elif t == EventType.NODE_SKIPPED:
        ns = st.node(ev.node_id or "")
        ns.status = "skipped"
        ns.ended_at = ev.ts
        if "control" in d:
            ns.control = Label.from_dict(d["control"])
    elif t == EventType.NODE_WAITING:
        ns = st.node(ev.node_id or "")
        ns.status = "waiting"
        ns.waiting_on = d.get("request_id")
    elif t in (EventType.EFFECT_COMPLETED, EventType.EFFECT_FAILED):
        rec = EffectRecord(
            key=d["key"],
            kind=d["kind"],
            fingerprint=d["fingerprint"],
            result=d.get("result"),
            label=Label.from_dict(d.get("label")),
            error=d.get("error"),
            seq=ev.seq,
        )
        st.effects[rec.key] = rec
        if d.get("replayed"):
            st.usage.replayed_effects += 1
        if t == EventType.EFFECT_COMPLETED:
            _account(st.usage, rec.kind, d)
    elif t == EventType.APPROVAL_REQUESTED:
        st.approvals.setdefault(
            d["request_id"],
            ApprovalState(
                request_id=d["request_id"],
                node_id=ev.node_id,
                reason=d.get("reason", ""),
                preview=d.get("preview", {}),
            ),
        )
    elif t == EventType.APPROVAL_DECIDED:
        ap = st.approvals.setdefault(
            d["request_id"], ApprovalState(request_id=d["request_id"], node_id=ev.node_id)
        )
        ap.status = "approved" if d.get("approved") else "rejected"
        ap.decided_by = d.get("by")
        ap.note = d.get("note")
    elif t == EventType.SUBRUN_LINKED:
        st.children.append(d["child_run_id"])
    elif t == EventType.RUN_SUSPENDED:
        st.status = "suspended"
    elif t == EventType.RUN_COMPLETED:
        st.status = "completed"
        st.output = Labeled(d.get("output"), Label.from_dict(d.get("label")))
        st.ended_at = ev.ts
    elif t == EventType.RUN_FAILED:
        st.status = "failed"
        st.error = d.get("error")
        st.ended_at = ev.ts
    elif t == EventType.RUN_CANCELLED:
        st.status = "cancelled"
        st.ended_at = ev.ts


def _account(usage: Usage, kind: str, d: dict[str, Any]) -> None:
    if d.get("replayed"):
        return
    if kind == "model":
        u = d.get("usage") or {}
        usage.add_model(
            str((d.get("result") or {}).get("model", "unknown")),
            int(u.get("input_tokens", 0)),
            int(u.get("output_tokens", 0)),
            d.get("cost_usd"),
        )
    elif kind == "tool":
        usage.tool_calls += 1
    elif kind == "retrieval":
        usage.retrieval_calls += 1
    elif kind.startswith("memory"):
        usage.memory_ops += 1
