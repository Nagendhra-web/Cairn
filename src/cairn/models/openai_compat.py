"""OpenAI-compatible chat completions provider over raw HTTP.

One implementation covers every server that speaks the ``/v1/chat/completions``
protocol: hosted APIs, vLLM, llama.cpp server, LM Studio, Ollama
(``http://localhost:11434/v1``), TGI and most gateways. Local models are
therefore first-class without extra code.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from typing import Any

import httpx

from cairn.core.errors import ModelError, RateLimited
from cairn.models.types import (
    Message,
    ModelRequest,
    ModelResponse,
    Usage,
    estimate_request_tokens,
    estimate_tokens,
)


class OpenAICompatibleProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        name: str = "openai-compatible",
        timeout_s: float = 120.0,
        native_json_schema: bool = True,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.name = name
        self.base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self.native_json_schema = native_json_schema
        self._client = client or httpx.AsyncClient(timeout=timeout_s)

    def _payload(self, model: str, req: ModelRequest, stream: bool) -> dict[str, Any]:
        messages: list[dict[str, Any]] = []
        if req.system:
            messages.append({"role": "system", "content": req.system})
        messages.extend(_convert(m) for m in req.messages)
        body: dict[str, Any] = {"model": model, "messages": messages, "max_tokens": req.max_tokens}
        if req.temperature is not None:
            body["temperature"] = req.temperature
        if req.stop:
            body["stop"] = req.stop
        if req.response_schema is not None:
            if self.native_json_schema:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {"name": "output", "schema": req.response_schema},
                }
            else:
                body["response_format"] = {"type": "json_object"}
        if stream:
            body["stream"] = True
            body["stream_options"] = {"include_usage": True}
        return body

    async def complete(self, model: str, request: ModelRequest) -> ModelResponse:
        started = time.perf_counter()
        resp = await self._post("/chat/completions", self._payload(model, request, False))
        data = resp.json()
        try:
            choice = data["choices"][0]
            text = choice["message"].get("content") or ""
        except (KeyError, IndexError) as exc:
            raise ModelError(f"malformed response from {self.name}", body=data) from exc
        return ModelResponse(
            text=text,
            model=data.get("model", model),
            provider=self.name,
            usage=_usage(data.get("usage"), request, text),
            finish_reason=choice.get("finish_reason"),
            latency_ms=(time.perf_counter() - started) * 1000,
        )

    async def stream(self, model: str, request: ModelRequest) -> AsyncIterator[str | ModelResponse]:
        started = time.perf_counter()
        ttft: float | None = None
        chunks: list[str] = []
        usage_raw: dict[str, Any] | None = None
        finish: str | None = None
        url = self.base_url + "/chat/completions"
        async with self._client.stream(
            "POST", url, json=self._payload(model, request, True), headers=self._headers
        ) as resp:
            if resp.status_code >= 400:
                await resp.aread()
                _raise_for(resp, self.name)
            async for line in resp.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                event = json.loads(payload)
                usage_raw = event.get("usage") or usage_raw
                for choice in event.get("choices", []):
                    delta = (choice.get("delta") or {}).get("content")
                    finish = choice.get("finish_reason") or finish
                    if delta:
                        if ttft is None:
                            ttft = (time.perf_counter() - started) * 1000
                        chunks.append(delta)
                        yield delta
        text = "".join(chunks)
        yield ModelResponse(
            text=text,
            model=model,
            provider=self.name,
            usage=_usage(usage_raw, request, text),
            finish_reason=finish,
            latency_ms=(time.perf_counter() - started) * 1000,
            ttft_ms=ttft,
        )

    async def embed(self, model: str, texts: list[str]) -> list[list[float]]:
        resp = await self._post("/embeddings", {"model": model, "input": texts})
        rows = sorted(resp.json()["data"], key=lambda r: r["index"])
        return [list(map(float, r["embedding"])) for r in rows]

    async def _post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        try:
            resp = await self._client.post(self.base_url + path, json=body, headers=self._headers)
        except httpx.TransportError as exc:
            raise ModelError(f"{self.name} transport error: {exc}") from exc
        if resp.status_code >= 400:
            _raise_for(resp, self.name)
        return resp

    async def aclose(self) -> None:
        await self._client.aclose()


def _convert(msg: Message) -> dict[str, Any]:
    if isinstance(msg.content, str):
        return {"role": msg.role, "content": msg.content}
    parts: list[dict[str, Any]] = []
    for p in msg.content:
        if p.type == "text":
            parts.append({"type": "text", "text": p.text or ""})
        elif p.type == "image":
            url = p.url or f"data:{p.media_type or 'image/png'};base64,{p.data}"
            parts.append({"type": "image_url", "image_url": {"url": url}})
        elif p.type == "audio":
            fmt = (p.media_type or "audio/wav").split("/")[-1]
            parts.append({"type": "input_audio", "input_audio": {"data": p.data, "format": fmt}})
        else:
            raise ModelError("document parts are not supported by the OpenAI-compatible protocol")
    return {"role": msg.role, "content": parts}


def _usage(raw: dict[str, Any] | None, req: ModelRequest, text: str) -> Usage:
    if raw and "prompt_tokens" in raw:
        return Usage(
            input_tokens=int(raw.get("prompt_tokens", 0)),
            output_tokens=int(raw.get("completion_tokens", 0)),
        )
    return Usage(
        input_tokens=estimate_request_tokens(req), output_tokens=estimate_tokens(text), estimated=True
    )


def _raise_for(resp: httpx.Response, name: str) -> None:
    detail = resp.text[:500]
    if resp.status_code == 429:
        raise RateLimited(f"{name} rate limited", status=429, body=detail)
    err = ModelError(f"{name} returned HTTP {resp.status_code}", status=resp.status_code, body=detail)
    err.retryable = resp.status_code >= 500 or resp.status_code in (408, 409)
    raise err
