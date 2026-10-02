"""Dependency container and the narrow interfaces the executor depends on.

The executor never imports concrete memory stores, corpora or agent classes;
it talks to these protocols. Swapping SQLite memory for a vector database, or
the built-in planner for your own, means providing a different object here.
"""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from cairn.core.clock import Clock, SystemClock
from cairn.journal.store import InMemoryJournal, JournalStore
from cairn.models.router import ModelRouter
from cairn.provenance.labels import Label
from cairn.provenance.policy import PolicyEngine
from cairn.security.sandbox import NetworkPolicy, PathSandbox
from cairn.security.secrets import SecretVault
from cairn.tools.registry import ToolRegistry


class MemoryService(Protocol):
    async def recall(self, query: str, kind: str, k: int) -> list[dict[str, Any]]:
        """Return memories as dicts that include a ``label`` entry (Label.to_dict())."""
        ...

    async def remember(
        self, text: str, kind: str, label: Label, importance: float, source_run: str
    ) -> dict[str, Any]: ...


class Corpus(Protocol):
    name: str
    trusted: bool

    async def search(self, query: str, k: int, mode: str) -> list[dict[str, Any]]: ...


@dataclass
class ApprovalRequest:
    run_id: str
    request_id: str
    node_id: str | None
    reason: str
    rule: str
    subject: dict[str, Any]


@dataclass
class ApprovalDecision:
    approved: bool
    by: str = "handler"
    note: str | None = None


ApprovalHandler = Callable[[ApprovalRequest], Awaitable[ApprovalDecision | None]]


@dataclass
class SubagentRequest:
    run_id: str
    parent_run_id: str
    goal: str
    grants: list[str]
    tier: str
    instructions: str | None
    depth: int
    label: Label
    budget: dict[str, Any]


class SubagentRunner(Protocol):
    async def __call__(self, request: SubagentRequest) -> Any:
        """Run (or resume) the child run and return a RunResult."""
        ...


class LiveBus:
    """Ephemeral per-run channel for token streams and progress (not journaled)."""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)

    def publish(self, run_id: str, message: dict[str, Any]) -> None:
        for q in list(self._subs.get(run_id, ())):
            q.put_nowait(message)

    def has_subscribers(self, run_id: str) -> bool:
        return bool(self._subs.get(run_id))

    async def subscribe(self, run_id: str) -> AsyncIterator[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subs[run_id].add(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subs[run_id].discard(q)


@dataclass
class Services:
    journal: JournalStore = field(default_factory=InMemoryJournal)
    router: ModelRouter = field(default_factory=ModelRouter)
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    policy: PolicyEngine = field(default_factory=PolicyEngine)
    clock: Clock = field(default_factory=SystemClock)
    vault: SecretVault = field(default_factory=SecretVault)
    sandbox: PathSandbox | None = None
    network: NetworkPolicy | None = None
    memory: MemoryService | None = None
    corpora: dict[str, Corpus] = field(default_factory=dict)
    approval_handler: ApprovalHandler | None = None
    subagents: SubagentRunner | None = None
    live: LiveBus = field(default_factory=LiveBus)
    tool_services: dict[str, Any] = field(default_factory=dict)
    replay_sleeps: bool = False
