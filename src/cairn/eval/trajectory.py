"""Trajectory extraction and matchers.

Final-answer metrics cannot tell an agent that got the right answer by
calling the right tools from one that guessed, or one that got there after
also emailing a stranger. The journal records every step, so a trajectory is
just a projection of a run's events into a typed, compact summary that tests
and evaluations can assert on.

Effect events do not all carry the same payload: a live tool call records its
request, but a failed or replayed one records only its key. The extractor
therefore remembers the tool named by the most recent policy decision of each
node, which the executor always emits immediately before the effect.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from cairn.journal.events import Event, EventType
from cairn.journal.store import JournalStore


@dataclass
class ToolCall:
    node_id: str | None
    key: str
    tool: str | None
    args: dict[str, Any] | None
    status: str  # completed | failed
    replayed: bool = False
    label: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    latency_ms: float | None = None
    injection_signals: list[str] = field(default_factory=list)


@dataclass
class ModelCall:
    node_id: str | None
    key: str
    model: str | None
    provider: str | None
    tier: str | None
    status: str
    replayed: bool = False
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: float | None = None
    error: dict[str, Any] | None = None


@dataclass
class PolicyRecord:
    node_id: str | None
    tool: str | None
    verdict: str
    rule: str
    reason: str


@dataclass
class ApprovalRecord:
    request_id: str
    node_id: str | None
    reason: str
    status: str = "pending"  # pending | approved | rejected
    decided_by: str | None = None
    copied: bool = False


@dataclass
class RetryRecord:
    node_id: str | None
    attempt: int
    reason: str | None
    delay_s: float | None


@dataclass
class VerifyRecord:
    node_id: str | None
    target: str | None
    round: int
    passed: bool
    issues: list[str]
    score: float | None


@dataclass
class Trajectory:
    run_id: str
    goal: str = ""
    status: str = "created"
    output: Any = None
    error: dict[str, Any] | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    model_calls: list[ModelCall] = field(default_factory=list)
    policy: list[PolicyRecord] = field(default_factory=list)
    approvals: list[ApprovalRecord] = field(default_factory=list)
    retries: list[RetryRecord] = field(default_factory=list)
    verifications: list[VerifyRecord] = field(default_factory=list)
    node_status: dict[str, str] = field(default_factory=dict)
    fallback_attempts: list[str] = field(default_factory=list)
    started_at: float | None = None
    ended_at: float | None = None
    event_count: int = 0

    @property
    def tools(self) -> list[str]:
        """Names of tools whose invocation was attempted, in journal order."""
        return [c.tool for c in self.tool_calls if c.tool is not None]

    @property
    def executed_tools(self) -> list[str]:
        """Names of tools that completed live (not replayed from a recording)."""
        return [
            c.tool for c in self.tool_calls
            if c.tool is not None and c.status == "completed" and not c.replayed
        ]

    @property
    def duration_s(self) -> float | None:
        if self.started_at is None or self.ended_at is None:
            return None
        return self.ended_at - self.started_at

    def calls_to(self, tool: str) -> list[ToolCall]:
        return [c for c in self.tool_calls if c.tool == tool]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["duration_s"] = self.duration_s
        return data

    def summary(self) -> dict[str, Any]:
        """A compact form suitable for embedding in result files."""
        return {
            "run_id": self.run_id,
            "status": self.status,
            "tools": self.tools,
            "models": [m.model for m in self.model_calls],
            "policy": [f"{p.tool}:{p.verdict}:{p.rule}" for p in self.policy],
            "approvals": [f"{a.request_id}:{a.status}" for a in self.approvals],
            "retries": len(self.retries),
            "fallbacks": len(self.fallback_attempts),
            "nodes": dict(self.node_status),
        }


def extract_trajectory(events: Sequence[Event], run_id: str | None = None) -> Trajectory:
    """Project a run's events (``journal.read(run_id)``) into a :class:`Trajectory`."""
    traj = Trajectory(run_id=run_id or (events[0].run_id if events else ""))
    last_tool: dict[str | None, str] = {}
    approvals: dict[str, ApprovalRecord] = {}
    for ev in events:
        d = ev.data
        t = ev.type
        traj.event_count += 1
        if t == EventType.RUN_CREATED:
            traj.goal = str(d.get("goal", ""))
            for node in (d.get("plan") or {}).get("nodes", []):
                traj.node_status.setdefault(str(node.get("id")), "pending")
        elif t == EventType.RUN_STARTED:
            if traj.started_at is None:
                traj.started_at = ev.ts
            traj.status = "running"
        elif t == EventType.NODE_STARTED:
            traj.node_status[ev.node_id or ""] = "running"
            if d.get("strategy", "primary") != "primary":
                traj.fallback_attempts.append(ev.node_id or "")
        elif t == EventType.NODE_COMPLETED:
            traj.node_status[ev.node_id or ""] = "completed"
        elif t == EventType.NODE_FAILED and d.get("final", True):
            traj.node_status[ev.node_id or ""] = "failed"
        elif t == EventType.NODE_SKIPPED:
            traj.node_status[ev.node_id or ""] = "skipped"
        elif t == EventType.NODE_WAITING:
            traj.node_status[ev.node_id or ""] = "waiting"
        elif t == EventType.NODE_RETRYING:
            traj.retries.append(
                RetryRecord(ev.node_id, int(d.get("attempt", 0)), d.get("reason"), d.get("delay_s"))
            )
        elif t == EventType.POLICY_DECISION:
            tool = d.get("tool")
            last_tool[ev.node_id] = str(tool) if tool is not None else ""
            traj.policy.append(
                PolicyRecord(ev.node_id, tool, str(d.get("verdict")), str(d.get("rule")),
                             str(d.get("reason", "")))
            )
        elif t in (EventType.EFFECT_COMPLETED, EventType.EFFECT_FAILED):
            _effect(traj, ev, last_tool)
        elif t == EventType.APPROVAL_REQUESTED:
            rec = ApprovalRecord(
                request_id=str(d.get("request_id")), node_id=ev.node_id,
                reason=str(d.get("reason", "")), copied=bool(d.get("copied_from")),
            )
            approvals.setdefault(rec.request_id, rec)
        elif t == EventType.APPROVAL_DECIDED:
            rid = str(d.get("request_id"))
            rec = approvals.setdefault(rid, ApprovalRecord(rid, ev.node_id, ""))
            rec.status = "approved" if d.get("approved") else "rejected"
            rec.decided_by = d.get("by")
        elif t == EventType.VERIFY_RESULT:
            traj.verifications.append(
                VerifyRecord(ev.node_id, d.get("target"), int(d.get("round", 1)),
                             bool(d.get("passed")), list(d.get("issues") or []), d.get("score"))
            )
        elif t == EventType.RUN_SUSPENDED:
            traj.status = "suspended"
        elif t == EventType.RUN_COMPLETED:
            traj.status = "completed"
            traj.output = d.get("output")
            traj.ended_at = ev.ts
        elif t == EventType.RUN_FAILED:
            traj.status = "failed"
            traj.error = d.get("error")
            traj.ended_at = ev.ts
        elif t == EventType.RUN_CANCELLED:
            traj.status = "cancelled"
            traj.ended_at = ev.ts
    traj.approvals = list(approvals.values())
    return traj


def _effect(traj: Trajectory, ev: Event, last_tool: dict[str | None, str]) -> None:
    d = ev.data
    kind = d.get("kind")
    status = "completed" if ev.type == EventType.EFFECT_COMPLETED else "failed"
    replayed = bool(d.get("replayed"))
    key = str(d.get("key", ""))
    if kind == "tool":
        request = d.get("request") if isinstance(d.get("request"), dict) else None
        tool = (request or {}).get("tool") or last_tool.get(ev.node_id) or None
        args = (request or {}).get("args")
        traj.tool_calls.append(
            ToolCall(
                node_id=ev.node_id, key=key, tool=tool,
                args=args if isinstance(args, dict) else None,
                status=status, replayed=replayed, label=d.get("label"), error=d.get("error"),
                latency_ms=d.get("latency_ms"),
                injection_signals=[str(s.get("kind")) for s in d.get("injection_signals") or []],
            )
        )
    elif kind == "model":
        result = d.get("result") if isinstance(d.get("result"), dict) else {}
        usage = d.get("usage") or {}
        traj.model_calls.append(
            ModelCall(
                node_id=ev.node_id, key=key, model=(result or {}).get("model"),
                provider=(result or {}).get("provider"), tier=d.get("tier"), status=status,
                replayed=replayed, input_tokens=int(usage.get("input_tokens", 0)),
                output_tokens=int(usage.get("output_tokens", 0)), cost_usd=d.get("cost_usd"),
                latency_ms=d.get("model_latency_ms", d.get("latency_ms")), error=d.get("error"),
            )
        )


async def load_trajectory(journal: JournalStore, run_id: str) -> Trajectory:
    """Read a run's events from ``journal`` and extract its trajectory."""
    return extract_trajectory(await journal.read(run_id), run_id)


# ---------------------------------------------------------------- matchers


def is_subsequence(expected: Sequence[str], actual: Sequence[str]) -> bool:
    """True when ``expected`` appears in ``actual`` in order, gaps allowed.

    Subsequence rather than equality because extra read-only calls (a second
    search, a retry) are usually fine, while skipping or reordering required
    steps is not.
    """
    it = iter(actual)
    return all(any(item == got for got in it) for item in expected)


def check_trajectory(
    traj: Trajectory,
    *,
    tool_sequence: Sequence[str] | None = None,
    required_tools: Iterable[str] = (),
    forbidden_tools: Iterable[str] = (),
    max_model_calls: int | None = None,
    max_tool_calls: int | None = None,
    status: str | None = None,
    executed_only: bool = False,
) -> list[str]:
    """Return human-readable violations; an empty list means the trajectory passes.

    ``executed_only`` restricts tool checks to calls that actually completed
    live, which is the right view for safety checks ("was the payment
    *made*?") as opposed to behavior checks ("did the agent *try*?").
    """
    tools = traj.executed_tools if executed_only else traj.tools
    problems: list[str] = []
    if status is not None and traj.status != status:
        problems.append(f"status is '{traj.status}', expected '{status}'")
    if tool_sequence is not None and not is_subsequence(list(tool_sequence), tools):
        problems.append(f"tool sequence {list(tool_sequence)} is not a subsequence of {tools}")
    for name in required_tools:
        if name not in tools:
            problems.append(f"required tool '{name}' was not called")
    for name in forbidden_tools:
        if name in tools:
            problems.append(f"forbidden tool '{name}' was called")
    live_models = [m for m in traj.model_calls if not m.replayed]
    if max_model_calls is not None and len(live_models) > max_model_calls:
        problems.append(f"{len(live_models)} model calls exceed the limit of {max_model_calls}")
    if max_tool_calls is not None and len(tools) > max_tool_calls:
        problems.append(f"{len(tools)} tool calls exceed the limit of {max_tool_calls}")
    return problems


def assert_trajectory(traj: Trajectory, **expectations: Any) -> None:
    """Raise ``AssertionError`` listing every violation found by :func:`check_trajectory`."""
    problems = check_trajectory(traj, **expectations)
    if problems:
        raise AssertionError("trajectory check failed:\n- " + "\n- ".join(problems))
