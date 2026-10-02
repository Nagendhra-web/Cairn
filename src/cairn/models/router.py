"""Cost-aware model router with fallback, circuit breaking and rate limits.

Selection rule: among registered endpoints that satisfy the request's
capability needs (vision, audio, json...), prefer the requested tier, then
tiers *above* it, and only then lower tiers as a last resort; within a tier,
cheaper and local endpoints come first. A downgrade is never silent: the
journaled route decision records every candidate tried. If the chosen endpoint fails with a retryable error or its
circuit is open, the router falls through to the next candidate. Every
decision is returned as a :class:`RouteDecision` so it can be journaled and
inspected.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from cairn.core.errors import ConfigError, ModelError
from cairn.models.base import Provider, StreamingProvider
from cairn.models.types import ModelInfo, ModelRequest, ModelResponse, Tier
from cairn.security.ratelimit import CircuitBreaker, TokenBucket


@dataclass
class Endpoint:
    info: ModelInfo
    provider: Provider
    breaker: CircuitBreaker = field(default_factory=CircuitBreaker)
    limiter: TokenBucket | None = None


@dataclass
class RouteDecision:
    requested_tier: str
    needs: list[str]
    chosen: str | None = None
    attempts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "requested_tier": self.requested_tier,
            "needs": self.needs,
            "chosen": self.chosen,
            "attempts": self.attempts,
        }


class ModelRouter:
    def __init__(self) -> None:
        self._endpoints: dict[str, Endpoint] = {}

    def register(
        self,
        provider: Provider,
        info: ModelInfo,
        *,
        requests_per_second: float | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> ModelRouter:
        key = f"{info.provider}/{info.name}"
        self._endpoints[key] = Endpoint(
            info=info,
            provider=provider,
            breaker=breaker or CircuitBreaker(),
            limiter=TokenBucket(requests_per_second) if requests_per_second else None,
        )
        return self

    @property
    def models(self) -> list[ModelInfo]:
        return [e.info for e in self._endpoints.values()]

    def candidates(
        self, tier: Tier | str = Tier.BALANCED, needs: set[str] | None = None, model: str | None = None
    ) -> list[Endpoint]:
        tier = Tier(tier)
        needs = needs or set()
        pool = [e for e in self._endpoints.values() if needs <= e.info.capabilities]
        if model is not None:
            exact = [e for e in pool if e.info.name == model or f"{e.info.provider}/{e.info.name}" == model]
            if not exact:
                raise ConfigError(f"model '{model}' is not registered or lacks {sorted(needs)}")
            rest = [e for e in pool if e not in exact]
            return exact + sorted(rest, key=lambda e: self._rank(e, tier))
        if not pool:
            raise ConfigError(
                f"no registered model satisfies capabilities {sorted(needs)}",
                registered=[e.info.name for e in self._endpoints.values()],
            )
        return sorted(pool, key=lambda e: self._rank(e, tier))

    @staticmethod
    def _rank(e: Endpoint, tier: Tier) -> tuple[int, float, int]:
        distance = e.info.tier.rank - tier.rank
        # Same tier first, then tiers above (escalation), then tiers below.
        tier_key = distance if distance >= 0 else 10 - distance
        price = (e.info.input_price_per_mtok or 0.0) + (e.info.output_price_per_mtok or 0.0)
        return (tier_key, price, 0 if e.info.local else 1)

    async def complete(
        self,
        request: ModelRequest,
        *,
        tier: Tier | str = Tier.BALANCED,
        model: str | None = None,
        on_token: Callable[[str], None] | None = None,
    ) -> tuple[ModelResponse, RouteDecision]:
        needs = request.required_capabilities()
        decision = RouteDecision(requested_tier=str(Tier(tier)), needs=sorted(needs))
        last_error: Exception | None = None
        for endpoint in self.candidates(tier, needs, model):
            name = f"{endpoint.info.provider}/{endpoint.info.name}"
            if endpoint.breaker.open:
                decision.attempts.append({"model": name, "skipped": "circuit_open"})
                continue
            if endpoint.limiter is not None:
                await endpoint.limiter.acquire()
            started = time.perf_counter()
            try:
                response = await self._call(endpoint, request, on_token)
            except ModelError as exc:
                endpoint.breaker.record_failure()
                decision.attempts.append(
                    {"model": name, "error": exc.code, "message": exc.message[:200]}
                )
                last_error = exc
                if not exc.retryable:
                    break
                continue
            endpoint.breaker.record_success()
            response.latency_ms = response.latency_ms or (time.perf_counter() - started) * 1000
            response.cost_usd = endpoint.info.cost(response.usage)
            decision.chosen = name
            decision.attempts.append({"model": name, "ok": True})
            return response, decision
        err = ModelError(
            "all candidate models failed",
            route=decision.to_dict(),
            last_error=str(last_error) if last_error else None,
        )
        err.retryable = last_error is not None and getattr(last_error, "retryable", False)
        raise err

    async def _call(
        self, endpoint: Endpoint, request: ModelRequest, on_token: Callable[[str], None] | None
    ) -> ModelResponse:
        provider = endpoint.provider
        if on_token is not None and isinstance(provider, StreamingProvider):
            final: ModelResponse | None = None
            stream: AsyncIterator[str | ModelResponse] = provider.stream(endpoint.info.name, request)
            async for item in stream:
                if isinstance(item, ModelResponse):
                    final = item
                else:
                    on_token(item)
            if final is None:
                raise ModelError("stream ended without a final response")
            return final
        return await provider.complete(endpoint.info.name, request)
