"""Provider protocol.

A provider is anything that can turn a :class:`ModelRequest` into a
:class:`ModelResponse`. Streaming and embeddings are optional capabilities.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

from cairn.models.types import ModelRequest, ModelResponse


@runtime_checkable
class Provider(Protocol):
    name: str

    async def complete(self, model: str, request: ModelRequest) -> ModelResponse: ...


@runtime_checkable
class StreamingProvider(Provider, Protocol):
    def stream(self, model: str, request: ModelRequest) -> AsyncIterator[str | ModelResponse]:
        """Yield text deltas, then exactly one final :class:`ModelResponse`."""
        ...


@runtime_checkable
class EmbeddingProvider(Protocol):
    name: str

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]: ...
