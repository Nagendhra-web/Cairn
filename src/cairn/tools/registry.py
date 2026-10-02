"""Tool registry with capability-aware discovery and validated invocation."""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from cairn.core.errors import NotFound, ToolArgumentError, ToolError, ToolTimeout
from cairn.models.structured import check_schema
from cairn.retrieval.index import HybridIndex
from cairn.security.sandbox import NetworkPolicy, PathSandbox
from cairn.security.secrets import SecretScope
from cairn.tools.spec import ToolSpec

log = logging.getLogger("cairn.tools")


@dataclass
class ToolContext:
    """What a tool may see of the runtime. Passed when the tool accepts ``ctx``."""

    run_id: str
    node_id: str
    secrets: SecretScope
    sandbox: PathSandbox | None = None
    network: NetworkPolicy | None = None
    services: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        self.notes.append(message)


@dataclass
class ToolMatch:
    spec: ToolSpec
    score: float
    reason: dict[str, float]


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, ToolSpec] = {}
        self._quarantined: dict[str, str] = {}
        self._index = HybridIndex()
        self._indexed: set[str] = set()

    def register(self, spec: ToolSpec, *, replace: bool = False) -> ToolSpec:
        if spec.name in self._tools and not replace:
            raise ValueError(f"tool '{spec.name}' already registered")
        self._tools[spec.name] = spec
        self._indexed.discard(spec.name)
        return spec

    def register_all(self, specs: Iterable[ToolSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)
        self._indexed.discard(name)

    def quarantine(self, name: str, reason: str) -> None:
        self._quarantined[name] = reason

    def release(self, name: str) -> None:
        self._quarantined.pop(name, None)

    def quarantined(self) -> dict[str, str]:
        return dict(self._quarantined)

    def get(self, name: str) -> ToolSpec:
        if name in self._quarantined:
            raise NotFound(f"tool '{name}' is quarantined: {self._quarantined[name]}", tool=name)
        try:
            return self._tools[name]
        except KeyError:
            close = sorted(n for n in self._tools if n.split(".")[-1] in name or name in n)
            raise NotFound(f"unknown tool '{name}'", tool=name, did_you_mean=close[:3]) from None

    def peek(self, name: str) -> ToolSpec | None:
        """Return a tool even if quarantined (for inspection and re-approval flows)."""
        return self._tools.get(name)

    def __contains__(self, name: str) -> bool:
        return name in self._tools and name not in self._quarantined

    def list(self, grants: Iterable[str] = ("*",)) -> list[ToolSpec]:
        patterns = list(grants)
        return [
            spec
            for name, spec in sorted(self._tools.items())
            if name not in self._quarantined
            and any(fnmatch.fnmatchcase(name, p) for p in patterns)
        ]

    async def _ensure_index(self) -> None:
        pending = [s for s in self._tools.values() if s.name not in self._indexed]
        stale = self._indexed - set(self._tools)
        for name in stale:
            await self._index.remove(name)
            self._indexed.discard(name)
        if pending:
            await self._index.add_many([(s.name, s.search_text(), None) for s in pending])
            self._indexed.update(s.name for s in pending)

    async def discover(
        self, query: str, k: int = 8, grants: Iterable[str] = ("*",)
    ) -> list[ToolMatch]:
        """Rank granted tools by relevance to a task description.

        Planners see only the top matches instead of the full catalog, which
        keeps prompts small when hundreds of tools (for example from several
        MCP servers) are registered.
        """
        await self._ensure_index()
        allowed = {s.name for s in self.list(grants)}
        hits = await self._index.search(query, k=max(k * 3, 20))
        out = [
            ToolMatch(self._tools[h.id], h.score, h.parts) for h in hits if h.id in allowed
        ]
        return out[:k]

    def validate_args(self, spec: ToolSpec, args: dict[str, Any]) -> list[str]:
        return check_schema(spec.input_schema, args, spec.name)

    async def invoke(self, spec: ToolSpec, args: dict[str, Any], ctx: ToolContext) -> Any:
        problems = self.validate_args(spec, args)
        if problems:
            raise ToolArgumentError(
                f"invalid arguments for '{spec.name}': {'; '.join(problems)}", tool=spec.name
            )
        kwargs = dict(args)
        if spec.wants_context:
            kwargs["ctx"] = ctx
        try:
            if inspect.iscoroutinefunction(spec.fn):
                coro = spec.fn(**kwargs)
            else:
                coro = asyncio.to_thread(spec.fn, **kwargs)
            return await asyncio.wait_for(coro, timeout=spec.timeout_s)
        except TimeoutError as exc:
            raise ToolTimeout(f"tool '{spec.name}' timed out after {spec.timeout_s}s",
                              tool=spec.name) from exc
        except ToolError:
            raise
        except Exception as exc:
            if getattr(exc, "code", None):  # CairnError subclasses keep their identity
                raise
            raise ToolError(f"tool '{spec.name}' failed: {type(exc).__name__}: {exc}",
                            tool=spec.name) from exc
