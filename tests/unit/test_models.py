"""Model layer: routing, fallback, circuit breaking, costing, structured output, providers."""

from __future__ import annotations

import json

import httpx
import pytest

from cairn.core.errors import ConfigError, ModelError
from cairn.models import (
    Message,
    ModelInfo,
    ModelRequest,
    ModelRouter,
    OpenAICompatibleProvider,
    ScriptedProvider,
    Tier,
    extract_json,
    validate_against,
)
from cairn.models.types import ContentPart, Usage
from cairn.security import CircuitBreaker


def req(text: str = "hi", **kw: object) -> ModelRequest:
    return ModelRequest(messages=[Message(role="user", content=text)], **kw)  # type: ignore[arg-type]


def info(name: str, tier: Tier, price: float = 1.0, caps: set[str] | None = None, local: bool = False) -> ModelInfo:
    return ModelInfo(name=name, provider="scripted", tier=tier, input_price_per_mtok=price,
                     output_price_per_mtok=price, capabilities=caps or {"text", "json"}, local=local)


async def test_router_prefers_requested_tier_then_escalates():
    p = ScriptedProvider(default="x")
    router = ModelRouter()
    router.register(p, info("big", Tier.FRONTIER, 10))
    router.register(p, info("mid", Tier.BALANCED, 3))
    router.register(p, info("cheap-mid", Tier.BALANCED, 1))
    assert [e.info.name for e in router.candidates(Tier.BALANCED)][:2] == ["cheap-mid", "mid"]
    assert router.candidates(Tier.FRONTIER)[0].info.name == "big"
    # Never silently downgrade: a frontier request falls back upward-first, then down.
    assert [e.info.name for e in router.candidates(Tier.FRONTIER)] == ["big", "cheap-mid", "mid"]


async def test_router_filters_by_capability():
    p = ScriptedProvider(default="x")
    router = ModelRouter().register(p, info("text-only", Tier.FAST))
    router.register(p, info("vision", Tier.FRONTIER, caps={"text", "json", "vision"}))
    image = ModelRequest(messages=[Message(role="user", content=[
        ContentPart(type="text", text="what is this"), ContentPart(type="image", url="https://x/y.png")])])
    _, decision = await router.complete(image, tier=Tier.FAST)
    assert decision.chosen == "scripted/vision"
    router2 = ModelRouter().register(p, info("text-only", Tier.FAST))
    with pytest.raises(ConfigError):
        await router2.complete(image)


async def test_router_falls_back_and_costs():
    flaky = ScriptedProvider().on("hi", "from flaky", fail_times=5)
    good = ScriptedProvider(default="from good")
    router = ModelRouter()
    router.register(flaky, info("a", Tier.FAST, 0.5))
    router.register(good, ModelInfo(name="b", provider="other", tier=Tier.FAST, input_price_per_mtok=2.0,
                                    output_price_per_mtok=4.0))
    resp, decision = await router.complete(req(), tier=Tier.FAST)
    assert resp.text == "from good"
    assert decision.attempts[0]["error"] == "model_error"
    assert resp.cost_usd == pytest.approx((resp.usage.input_tokens * 2 + resp.usage.output_tokens * 4) / 1e6)


async def test_unknown_price_reports_none_not_zero():
    router = ModelRouter().register(ScriptedProvider(default="x"), ModelInfo(name="m", provider="p"))
    resp, _ = await router.complete(req())
    assert resp.cost_usd is None


def test_circuit_breaker_opens_and_half_opens():
    now = [0.0]
    cb = CircuitBreaker(threshold=2, cooldown_s=10, clock=lambda: now[0])
    cb.record_failure()
    assert not cb.open
    cb.record_failure()
    assert cb.open
    now[0] = 11
    assert not cb.open  # half-open trial allowed
    cb.record_failure()
    assert cb.open


def test_tier_escalation():
    assert Tier.FAST.escalate() is Tier.BALANCED
    assert Tier.FRONTIER.escalate() is Tier.FRONTIER


def test_extract_json_tolerates_fences_and_prose():
    assert extract_json('Sure! ```json\n{"a": 1}\n``` done') == {"a": 1}
    assert extract_json('noise [1, 2] noise') == [1, 2]
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_schema_validation_messages():
    schema = {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer", "minimum": 1}},
              "additionalProperties": False}
    assert validate_against(schema, {"n": 3}) == {"n": 3}
    with pytest.raises(ValueError, match="below minimum"):
        validate_against(schema, {"n": 0})
    with pytest.raises(ValueError, match="unexpected keys"):
        validate_against(schema, {"n": 2, "x": 1})
    with pytest.raises(ValueError, match="expected integer"):
        validate_against(schema, {"n": True})


def test_model_info_cost():
    m = ModelInfo(name="m", provider="p", input_price_per_mtok=3, output_price_per_mtok=15)
    assert m.cost(Usage(input_tokens=1_000_000, output_tokens=100_000)) == pytest.approx(4.5)


async def test_openai_compatible_provider_wire_format():
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.update(body)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={
            "model": body["model"], "choices": [{"message": {"content": '{"ok": true}'}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 3},
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = OpenAICompatibleProvider("http://local/v1", "k", client=client)
    resp = await provider.complete("llama", req("q", system="sys", response_schema={"type": "object"}))
    assert resp.text == '{"ok": true}' and resp.usage.input_tokens == 12
    assert seen["auth"] == "Bearer k"
    assert seen["messages"][0] == {"role": "system", "content": "sys"}  # type: ignore[index]
    assert seen["response_format"]["type"] == "json_schema"  # type: ignore[index]


async def test_openai_compatible_streaming_and_errors():
    chunks = [
        'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"},"finish_reason":"stop"}]}',
        'data: {"choices":[],"usage":{"prompt_tokens":5,"completion_tokens":2}}',
        "data: [DONE]",
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if json.loads(request.content).get("model") == "bad":
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, text="\n\n".join(chunks))

    provider = OpenAICompatibleProvider("http://local/v1", client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    parts = [item async for item in provider.stream("m", req())]
    assert parts[:2] == ["Hel", "lo"]
    final = parts[-1]
    assert final.text == "Hello" and final.usage.output_tokens == 2 and final.ttft_ms is not None
    with pytest.raises(ModelError) as info_:
        await provider.complete("bad", req())
    assert info_.value.code == "rate_limited"


async def test_anthropic_provider_maps_requests_and_refusals():
    from types import SimpleNamespace

    from cairn.models.anthropic import AnthropicProvider

    captured: dict[str, object] = {}

    class Messages:
        async def create(self, **params: object) -> object:
            captured.update(params)
            if params["model"] == "refuse":
                return SimpleNamespace(stop_reason="refusal", stop_details=SimpleNamespace(category="cyber"),
                                       content=[], usage=SimpleNamespace(input_tokens=1, output_tokens=0))
            return SimpleNamespace(stop_reason="end_turn", model=params["model"],
                                   content=[SimpleNamespace(type="text", text="hello")],
                                   usage=SimpleNamespace(input_tokens=7, output_tokens=2))

    client = SimpleNamespace(beta=SimpleNamespace(messages=Messages()))
    provider = AnthropicProvider(client=client)
    resp = await provider.complete("claude-opus-5-5", req("q", system="s", response_schema={"type": "object"}))
    assert resp.text == "hello" and resp.usage.input_tokens == 7
    assert captured["system"] == "s"
    assert captured["output_config"] == {"format": {"type": "json_schema", "schema": {"type": "object"}}}
    assert captured["fallbacks"] == "default"
    with pytest.raises(ModelError) as err:
        await provider.complete("refuse", req())
    assert err.value.retryable is False
