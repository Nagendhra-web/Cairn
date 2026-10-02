"""Tool specifications.

A tool is a *capability* with a contract, not just a function: it declares its
input schema, its side effects, which parameters are security-sensitive sinks,
how trustworthy its output is, and which secrets it needs. The runtime uses
these declarations for policy checks, approval prompts, discovery and replay.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cairn.core.ids import stable_hash


class Effect(StrEnum):
    READ = "read"
    WRITE = "write"
    DELETE = "delete"
    NETWORK = "network"
    SEND = "send"
    EXECUTE = "execute"
    PAYMENT = "payment"


class OutputTrust(StrEnum):
    INHERIT = "inherit"  # pure function of its inputs (calculator, formatter)
    TRUSTED = "trusted"  # authoritative internal source (config, own database)
    UNTRUSTED = "untrusted"  # attacker-influenceable content (web, email, MCP, files)


ToolFn = Callable[..., Any] | Callable[..., Awaitable[Any]]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    fn: ToolFn
    effects: frozenset[str] = frozenset()
    sensitive_params: frozenset[str] = frozenset()
    output_trust: OutputTrust = OutputTrust.UNTRUSTED
    output_secrecy: frozenset[str] = frozenset()
    allowed_secrecy: frozenset[str] = frozenset()
    secrets: frozenset[str] = frozenset()
    requires_approval: bool = False
    timeout_s: float = 60.0
    idempotent: bool = False
    tags: frozenset[str] = frozenset()
    version: str = "1"
    source: str = "local"
    examples: list[dict[str, Any]] = field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    wants_context: bool = False

    @property
    def fingerprint(self) -> str:
        """Hash of the security-relevant contract, used to pin tool definitions.

        If an MCP server silently changes a tool's description or schema (a
        "rug pull"), the fingerprint changes and the tool is quarantined until
        an operator re-approves it.
        """
        return stable_hash(
            {
                "name": self.name,
                "description": self.description,
                "schema": self.input_schema,
                "effects": sorted(self.effects),
                "version": self.version,
            },
            length=16,
        )

    def catalog_entry(self) -> dict[str, Any]:
        """Compact description shown to planners."""
        props = self.input_schema.get("properties", {})
        return {
            "name": self.name,
            "description": self.description,
            "params": {
                k: {kk: vv for kk, vv in v.items() if kk in ("type", "description", "enum", "items")}
                for k, v in props.items()
            },
            "required": self.input_schema.get("required", []),
            "effects": sorted(self.effects),
            "returns": self.output_schema.get("type") if self.output_schema else None,
        }

    def search_text(self) -> str:
        params = " ".join(
            f"{k} {v.get('description', '')}"
            for k, v in self.input_schema.get("properties", {}).items()
        )
        return f"{self.name.replace('.', ' ').replace('_', ' ')} {self.description} {params} {' '.join(self.tags)}"
