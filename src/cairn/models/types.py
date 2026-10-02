"""Provider-neutral model request and response types."""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field


class Tier(StrEnum):
    """Coarse capability tiers used by the router.

    Plans and agents ask for a tier rather than a model name, so the same plan
    runs against local models, hosted models or a mix, and so the router can
    escalate (``fast`` -> ``balanced`` -> ``frontier``) when verification fails.
    """

    FAST = "fast"
    BALANCED = "balanced"
    FRONTIER = "frontier"

    @property
    def rank(self) -> int:
        return _TIER_ORDER.index(self)

    def escalate(self) -> Tier:
        return _TIER_ORDER[min(self.rank + 1, len(_TIER_ORDER) - 1)]


_TIER_ORDER = [Tier.FAST, Tier.BALANCED, Tier.FRONTIER]


class ContentPart(BaseModel):
    type: Literal["text", "image", "audio", "document"]
    text: str | None = None
    url: str | None = None
    data: str | None = None  # base64 payload
    media_type: str | None = None


class Message(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str | list[ContentPart]

    def text(self) -> str:
        if isinstance(self.content, str):
            return self.content
        return "\n".join(p.text or "" for p in self.content if p.type == "text")

    def modalities(self) -> set[str]:
        if isinstance(self.content, str):
            return {"text"}
        return {p.type for p in self.content}


class ModelRequest(BaseModel):
    messages: list[Message]
    system: str | None = None
    max_tokens: int = 4096
    temperature: float | None = None
    response_schema: dict[str, Any] | None = None
    stop: list[str] | None = None
    effort: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    def required_capabilities(self) -> set[str]:
        caps: set[str] = set()
        for msg in self.messages:
            mods = msg.modalities()
            if "image" in mods:
                caps.add("vision")
            if "audio" in mods:
                caps.add("audio")
            if "document" in mods:
                caps.add("documents")
        if self.response_schema is not None:
            caps.add("json")
        return caps


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens


class ModelResponse(BaseModel):
    text: str
    model: str
    provider: str
    usage: Usage = Field(default_factory=Usage)
    finish_reason: str | None = None
    latency_ms: float = 0.0
    ttft_ms: float | None = None
    cost_usd: float | None = None
    parsed: Any = None


class ModelInfo(BaseModel):
    """Static description of a model endpoint used for routing and costing.

    Prices are per million tokens and are configuration, not facts baked into
    the code: when a price is unknown the cost is reported as ``None`` rather
    than guessed.
    """

    name: str
    provider: str
    tier: Tier = Tier.BALANCED
    capabilities: set[str] = Field(default_factory=lambda: {"text", "json"})
    context_window: int = 128_000
    input_price_per_mtok: float | None = None
    output_price_per_mtok: float | None = None
    local: bool = False

    def cost(self, usage: Usage) -> float | None:
        if self.input_price_per_mtok is None or self.output_price_per_mtok is None:
            return None
        return (
            usage.input_tokens * self.input_price_per_mtok
            + usage.output_tokens * self.output_price_per_mtok
        ) / 1_000_000


def estimate_tokens(text: str) -> int:
    """Rough token estimate (about 4 characters per token) for budgeting only."""
    return max(1, (len(text) + 3) // 4)


def estimate_request_tokens(req: ModelRequest) -> int:
    total = estimate_tokens(req.system or "")
    for msg in req.messages:
        total += estimate_tokens(msg.text()) + 4
    return total
