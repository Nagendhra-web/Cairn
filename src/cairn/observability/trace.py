"""Traces and run reports derived from the journal.

Because the journal already records every decision and effect with
timestamps, observability needs no separate instrumentation path: spans,
per-node usage, policy decisions, retries and approvals are all projections
of the same events the executor writes. What you debug is exactly what ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cairn.journal.events import Event, EventType
from cairn.provenance.labels import Label
from cairn.runtime.state import fold


@dataclass
class Span:
    span_id: str
    name: str
    kind: str
    start: float
    end: float | None = None
    parent_id: str | None = None
    status: str = "ok"
    attributes: dict[str, Any] = field(default_factory=dict)
    children: list[Span] = field(default_factory=list)

    @property
    def duration_ms(self) -> float | None:
        return None if self.end is None else round((self.end - self.start) * 1000, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "name": self.name,
            "kind": self.kind,
            "start": self.start,
            "end": self.end,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attributes": self.attributes,
            "children": [c.to_dict() for c in self.children],
        }


def build_trace(run_id: str, events: list[Event]) -> Span:
    """Run span -> node attempt spans -> effect spans."""
    start = events[0].ts if events else 0.0
    root = Span(run_id, run_id, "run", start)
    attempt_spans: dict[str, Span] = {}
    for ev in events:
        d = ev.data
        if ev.type == EventType.RUN_CREATED:
            root.attributes["goal"] = d.get("goal")
            root.attributes["agent"] = d.get("agent")
        elif ev.type == EventType.NODE_STARTED and ev.node_id:
            previous = attempt_spans.get(ev.node_id)
            if previous is not None and previous.end is None:
                previous.end = ev.ts
                previous.status = "interrupted"  # the process died during this attempt
            span = Span(f"{ev.node_id}@{d.get('attempt')}", ev.node_id, "node", ev.ts,
                        parent_id=run_id, attributes={"attempt": d.get("attempt"),
                                                      "strategy": d.get("strategy")})
            attempt_spans[ev.node_id] = span
            root.children.append(span)
        elif ev.type in (EventType.EFFECT_COMPLETED, EventType.EFFECT_FAILED) and ev.node_id:
            latency = float(d.get("latency_ms") or 0.0) / 1000
            parent = attempt_spans.get(ev.node_id)
            attrs: dict[str, Any] = {k: d[k] for k in ("kind", "key", "usage", "cost_usd", "ttft_ms", "tier",
                                        "replayed", "injection_signals") if k in d}
            if d.get("kind") == "model" and isinstance(d.get("result"), dict):
                attrs["model"] = d["result"].get("model")
            if d.get("kind") == "tool" and isinstance(d.get("request"), dict):
                attrs["tool"] = d["request"].get("tool")
            effect = Span(d["key"], _effect_name(d), "effect", ev.ts - latency, ev.ts,
                        parent_id=parent.span_id if parent else run_id,
                        status="error" if ev.type == EventType.EFFECT_FAILED else "ok",
                        attributes=attrs)
            (parent.children if parent else root.children).append(effect)
        elif ev.type in (EventType.NODE_COMPLETED, EventType.NODE_FAILED, EventType.NODE_WAITING,
                         EventType.NODE_SKIPPED) and ev.node_id:
            current = attempt_spans.get(ev.node_id)
            if current is not None and current.end is None:
                current.end = ev.ts
                current.status = {
                    EventType.NODE_COMPLETED: "ok", EventType.NODE_FAILED: "error",
                    EventType.NODE_WAITING: "waiting", EventType.NODE_SKIPPED: "skipped",
                }[EventType(ev.type)]
                if ev.type == EventType.NODE_FAILED:
                    current.attributes["error"] = d.get("error")
        elif ev.type in (EventType.RUN_COMPLETED, EventType.RUN_FAILED, EventType.RUN_CANCELLED,
                         EventType.RUN_SUSPENDED):
            root.end = ev.ts
            root.status = ev.type.split(".")[1]
    return root


def _effect_name(d: dict[str, Any]) -> str:
    kind = d.get("kind", "effect")
    if kind == "tool" and isinstance(d.get("request"), dict):
        return f"tool {d['request'].get('tool')}"
    if kind == "model" and isinstance(d.get("result"), dict):
        return f"model {d['result'].get('model')}"
    return str(kind)


def run_report(run_id: str, events: list[Event]) -> dict[str, Any]:
    """Everything an engineer needs to debug a run, as one JSON document."""
    state = fold(run_id, events)
    per_node: dict[str, dict[str, Any]] = {}
    for node_id, ns in state.nodes.items():
        per_node[node_id] = {
            "status": ns.status,
            "attempts": ns.attempts,
            "duration_ms": round((ns.ended_at - ns.started_at) * 1000, 3)
            if ns.started_at and ns.ended_at else None,
            "label": ns.output.label.describe() if ns.output else None,
            "control": ns.control.describe(),
            "error": ns.error,
            "tokens": 0,
            "cost_usd": 0.0,
            "model_calls": 0,
            "tool_calls": 0,
        }
    policy, approvals, retries, verifications, divergences, signals = [], [], [], [], [], []
    for ev in events:
        d = ev.data
        node = per_node.get(ev.node_id or "")
        if ev.type == EventType.EFFECT_COMPLETED and node is not None and not d.get("replayed"):
            if d.get("kind") == "model":
                usage = d.get("usage") or {}
                node["tokens"] += int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
                node["cost_usd"] += d.get("cost_usd") or 0.0
                node["model_calls"] += 1
            elif d.get("kind") == "tool":
                node["tool_calls"] += 1
            if d.get("injection_signals"):
                signals.append({"node_id": ev.node_id, "signals": d["injection_signals"]})
        elif ev.type == EventType.POLICY_DECISION and d.get("verdict") != "allow":
            policy.append({"node_id": ev.node_id, **{k: d.get(k) for k in ("tool", "verdict", "rule", "reason")}})
        elif ev.type == EventType.APPROVAL_REQUESTED:
            approvals.append({"node_id": ev.node_id, "request_id": d.get("request_id"),
                              "reason": d.get("reason"), "status": state.approvals[d["request_id"]].status
                              if d.get("request_id") in state.approvals else "pending"})
        elif ev.type == EventType.NODE_RETRYING:
            retries.append({"node_id": ev.node_id, "attempt": d.get("attempt"), "reason": d.get("reason"),
                            "delay_s": d.get("delay_s")})
        elif ev.type == EventType.VERIFY_RESULT:
            fields = ("target", "round", "passed", "issues")
            verifications.append({"node_id": ev.node_id, **{k: d.get(k) for k in fields}})
        elif ev.type == EventType.EFFECT_DIVERGED:
            divergences.append({"node_id": ev.node_id, "key": d.get("key")})
    duration = (state.ended_at - state.started_at) if state.started_at and state.ended_at else None
    created = next((e for e in events if e.type == EventType.RUN_CREATED), None)
    return {
        "run_id": run_id,
        "goal": state.goal,
        "status": state.status,
        "agent": state.agent,
        "parent_run_id": state.parent_run_id,
        "children": state.children,
        "plan_label": state.label.describe(),
        "output": state.output.value if state.output else None,
        "output_label": state.output.label.describe() if state.output else None,
        "output_trusted": state.output.label.trusted if state.output else None,
        "error": state.error,
        "duration_s": round(duration, 4) if duration is not None else None,
        "usage": state.usage.to_dict(),
        "planning": (created.data.get("planning") if created else None),
        "forked_from": (created.data.get("forked_from") if created else None),
        "nodes": per_node,
        "policy_decisions": policy,
        "approvals": approvals,
        "retries": retries,
        "verifications": verifications,
        "divergences": divergences,
        "injection_signals": signals,
        "events": len(events),
        "trace": build_trace(run_id, events).to_dict(),
    }


def label_badge(label: dict[str, Any] | None) -> str:
    if not label:
        return "?"
    return "trusted" if Label.from_dict(label).trusted else "UNTRUSTED"
