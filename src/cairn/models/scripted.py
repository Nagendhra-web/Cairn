"""Deterministic scripted provider.

Used for tests, offline examples, reproducible evaluations and adversarial
simulations (for example a "compromised" model that obeys injected
instructions). Every response is a pure function of the request, so runs that
use it are bit-for-bit reproducible.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from cairn.core.errors import ModelError
from cairn.models.types import (
    ModelRequest,
    ModelResponse,
    Usage,
    estimate_request_tokens,
    estimate_tokens,
)

Responder = Callable[[ModelRequest], str | dict[str, Any] | list[Any]]


@dataclass
class ScriptRule:
    """Respond with ``response`` when ``match`` matches the request.

    ``match`` is a substring, a compiled regex, or a predicate over the
    request. It is tested against the system prompt plus all message text.
    ``fail_times`` makes the first N matching calls raise a retryable
    :class:`ModelError`, which is how failure recovery is tested.
    """

    match: str | re.Pattern[str] | Callable[[ModelRequest], bool]
    response: str | dict[str, Any] | list[Any] | Responder
    fail_times: int = 0
    model: str | None = None
    calls: int = field(default=0, init=False)

    def matches(self, model: str, req: ModelRequest, haystack: str) -> bool:
        if self.model is not None and self.model != model:
            return False
        if isinstance(self.match, str):
            return self.match in haystack
        if isinstance(self.match, re.Pattern):
            return self.match.search(haystack) is not None
        return bool(self.match(req))


class ScriptedProvider:
    name = "scripted"

    def __init__(
        self,
        rules: list[ScriptRule] | None = None,
        default: str | Responder | None = None,
        latency_s: float = 0.0,
    ) -> None:
        self.rules = list(rules or [])
        self.default = default
        self.latency_s = latency_s
        self.requests: list[tuple[str, ModelRequest]] = []

    def on(
        self,
        match: str | re.Pattern[str] | Callable[[ModelRequest], bool],
        response: str | dict[str, Any] | list[Any] | Responder,
        *,
        fail_times: int = 0,
        model: str | None = None,
    ) -> ScriptedProvider:
        self.rules.append(ScriptRule(match, response, fail_times, model))
        return self

    def _render(self, model: str, req: ModelRequest) -> str:
        haystack = "\n".join([req.system or "", *(m.text() for m in req.messages)])
        for rule in self.rules:
            if not rule.matches(model, req, haystack):
                continue
            rule.calls += 1
            if rule.calls <= rule.fail_times:
                raise ModelError(
                    f"scripted transient failure {rule.calls}/{rule.fail_times}", model=model
                )
            out = rule.response(req) if callable(rule.response) else rule.response
            return out if isinstance(out, str) else json.dumps(out)
        if self.default is None:
            preview = haystack[-200:].replace("\n", " ")
            raise ModelError(f"no scripted rule matched request: ...{preview}", model=model)
        out = self.default(req) if callable(self.default) else self.default
        return out if isinstance(out, str) else json.dumps(out)

    async def complete(self, model: str, request: ModelRequest) -> ModelResponse:
        self.requests.append((model, request))
        if self.latency_s:
            await asyncio.sleep(self.latency_s)
        text = self._render(model, request)
        usage = Usage(
            input_tokens=estimate_request_tokens(request),
            output_tokens=estimate_tokens(text),
            estimated=True,
        )
        return ModelResponse(
            text=text, model=model, provider=self.name, usage=usage, finish_reason="stop"
        )

    async def stream(self, model: str, request: ModelRequest) -> AsyncIterator[str | ModelResponse]:
        final = await self.complete(model, request)
        for i in range(0, len(final.text), 16):
            yield final.text[i : i + 16]
        yield final
