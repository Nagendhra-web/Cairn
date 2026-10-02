"""Claude provider built on the official ``anthropic`` SDK (optional extra).

Install with ``pip install 'cairn-runtime[anthropic]'``. Structured output uses
``output_config.format`` (JSON schema). Server-side refusal fallbacks are on by
default (``fallbacks="default"``); pass ``server_fallback=False`` to disable.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from typing import Any

from cairn.core.errors import ConfigError, ModelError, RateLimited
from cairn.models.types import Message, ModelRequest, ModelResponse, Usage

DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
_FALLBACK_BETA = "server-side-fallback-2026-07-01"


class AnthropicProvider:
    name = "anthropic"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        base_url: str | None = None,
        server_fallback: bool = True,
        timeout_s: float = 600.0,
        client: Any = None,
    ) -> None:
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on environment
                raise ConfigError(
                    "the Anthropic provider needs the 'anthropic' package: "
                    "pip install 'cairn-runtime[anthropic]'"
                ) from exc
            kwargs: dict[str, Any] = {"timeout": timeout_s}
            if api_key:
                kwargs["api_key"] = api_key
            if base_url:
                kwargs["base_url"] = base_url
            client = anthropic.AsyncAnthropic(**kwargs)
        self._client = client
        self.server_fallback = server_fallback

    def _params(self, model: str, req: ModelRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": model,
            "max_tokens": req.max_tokens,
            "messages": [_convert(m) for m in req.messages if m.role != "system"],
        }
        system = "\n\n".join(
            [s for s in [req.system, *(m.text() for m in req.messages if m.role == "system")] if s]
        )
        if system:
            params["system"] = system
        if req.stop:
            params["stop_sequences"] = req.stop
        output_config: dict[str, Any] = {}
        if req.effort:
            output_config["effort"] = req.effort
        if req.response_schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": req.response_schema}
        if output_config:
            params["output_config"] = output_config
        if self.server_fallback:
            params["betas"] = [_FALLBACK_BETA]
            params["fallbacks"] = "default"
        return params

    async def complete(self, model: str, request: ModelRequest) -> ModelResponse:
        started = time.perf_counter()
        try:
            msg = await self._client.beta.messages.create(**self._params(model, request))
        except Exception as exc:
            raise _map_error(exc) from exc
        return _to_response(msg, model, (time.perf_counter() - started) * 1000, None)

    async def stream(self, model: str, request: ModelRequest) -> AsyncIterator[str | ModelResponse]:
        started = time.perf_counter()
        ttft: float | None = None
        try:
            async with self._client.beta.messages.stream(**self._params(model, request)) as stream:
                async for delta in stream.text_stream:
                    if ttft is None:
                        ttft = (time.perf_counter() - started) * 1000
                    yield delta
                final = await stream.get_final_message()
        except Exception as exc:
            raise _map_error(exc) from exc
        yield _to_response(final, model, (time.perf_counter() - started) * 1000, ttft)


def _to_response(msg: Any, model: str, latency_ms: float, ttft: float | None) -> ModelResponse:
    if getattr(msg, "stop_reason", None) == "refusal":
        details = getattr(msg, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        err = ModelError("model declined the request", category=category)
        err.retryable = False
        raise err
    text = "".join(b.text for b in msg.content if getattr(b, "type", None) == "text")
    usage = Usage(
        input_tokens=int(getattr(msg.usage, "input_tokens", 0) or 0),
        output_tokens=int(getattr(msg.usage, "output_tokens", 0) or 0),
    )
    return ModelResponse(
        text=text,
        model=getattr(msg, "model", model),
        provider="anthropic",
        usage=usage,
        finish_reason=getattr(msg, "stop_reason", None),
        latency_ms=latency_ms,
        ttft_ms=ttft,
    )


def _convert(msg: Message) -> dict[str, Any]:
    if isinstance(msg.content, str):
        return {"role": msg.role, "content": msg.content}
    blocks: list[dict[str, Any]] = []
    for p in msg.content:
        if p.type == "text":
            blocks.append({"type": "text", "text": p.text or ""})
        elif p.type in ("image", "document"):
            if p.url:
                source: dict[str, Any] = {"type": "url", "url": p.url}
            else:
                default = "image/png" if p.type == "image" else "application/pdf"
                source = {"type": "base64", "media_type": p.media_type or default, "data": p.data}
            blocks.append({"type": p.type, "source": source})
        else:
            raise ModelError("audio input is not supported by the Anthropic provider")
    return {"role": msg.role, "content": blocks}


def _map_error(exc: Exception) -> ModelError:
    if isinstance(exc, ModelError):
        return exc
    status = getattr(exc, "status_code", None)
    if status == 429:
        return RateLimited(f"anthropic rate limited: {exc}", status=429)
    err = ModelError(f"anthropic error: {exc}", status=status)
    err.retryable = status is None or status >= 500 or status in (408, 409)
    return err
