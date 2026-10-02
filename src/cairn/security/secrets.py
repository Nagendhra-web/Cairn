"""Secret isolation.

Secrets never enter prompts, plans or the journal. Tools declare which secrets
they need; the runtime hands a tool only the secrets it declared, at call
time, through :class:`SecretScope`. All tool output and model input passes
through :class:`Redactor`, which replaces any secret value that leaks into
text with ``[REDACTED:NAME]`` before it is journaled or shown to a model.
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from typing import Any

from cairn.core.errors import PolicyViolation


class SecretVault:
    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values: dict[str, str] = dict(values or {})

    @classmethod
    def from_env(cls, prefix: str = "CAIRN_SECRET_") -> SecretVault:
        return cls({k[len(prefix):]: v for k, v in os.environ.items() if k.startswith(prefix) and v})

    def set(self, name: str, value: str) -> None:
        self._values[name] = value

    def names(self) -> list[str]:
        return sorted(self._values)

    def scope(self, allowed: Iterable[str]) -> SecretScope:
        return SecretScope(self, frozenset(allowed))

    def _get(self, name: str) -> str | None:
        return self._values.get(name)

    def redactor(self) -> Redactor:
        return Redactor({n: v for n, v in self._values.items() if len(v) >= 4})


class SecretScope:
    """The view of the vault a single tool invocation receives."""

    def __init__(self, vault: SecretVault, allowed: frozenset[str]) -> None:
        self._vault = vault
        self.allowed = allowed
        self.accessed: list[str] = []

    def get(self, name: str) -> str:
        if name not in self.allowed:
            raise PolicyViolation(
                f"tool did not declare secret '{name}'", secret=name, declared=sorted(self.allowed)
            )
        value = self._vault._get(name)
        if value is None:
            raise PolicyViolation(f"secret '{name}' is not configured", secret=name)
        self.accessed.append(name)
        return value


class Redactor:
    def __init__(self, secrets: Mapping[str, str]) -> None:
        # Longest first so overlapping secrets redact cleanly.
        self._pairs = sorted(secrets.items(), key=lambda kv: -len(kv[1]))

    def text(self, value: str) -> str:
        for name, secret in self._pairs:
            if secret in value:
                value = value.replace(secret, f"[REDACTED:{name}]")
        return value

    def deep(self, value: Any) -> Any:
        if not self._pairs:
            return value
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.deep(v) for v in value]
        if isinstance(value, dict):
            return {k: self.deep(v) for k, v in value.items()}
        return value

    def found(self, value: Any) -> list[str]:
        text = str(value)
        return [name for name, secret in self._pairs if secret in text]
