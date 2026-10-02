"""Resource budgets: the guard against runaway loops and cost blowups.

Budgets are enforced by the kernel before each effect, not by asking the model
to be frugal. Usage is reconstructed from the journal on resume, so a run that
is interrupted and resumed cannot reset its own spending.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cairn.core.errors import BudgetExceeded


@dataclass
class Budget:
    max_cost_usd: float | None = None
    max_tokens: int | None = 200_000
    max_model_calls: int | None = 200
    max_tool_calls: int | None = 200
    max_wall_s: float | None = 900.0
    max_nodes: int = 64
    max_depth: int = 3
    max_concurrency: int = 8

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> Budget:
        return cls(**(data or {}))

    def child(self, used: Usage) -> Budget:
        """Budget for a sub-agent: whatever the parent has left, one level shallower."""
        def left(limit: float | None, spent: float) -> Any:
            return None if limit is None else max(0, limit - spent)

        return Budget(
            max_cost_usd=left(self.max_cost_usd, used.cost_usd),
            max_tokens=left(self.max_tokens, used.tokens),
            max_model_calls=left(self.max_model_calls, used.model_calls),
            max_tool_calls=left(self.max_tool_calls, used.tool_calls),
            max_wall_s=self.max_wall_s,
            max_nodes=self.max_nodes,
            max_depth=self.max_depth - 1,
            max_concurrency=self.max_concurrency,
        )


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    unpriced_calls: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    retrieval_calls: int = 0
    memory_ops: int = 0
    replayed_effects: int = 0
    by_model: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def add_model(self, model: str, inp: int, out: int, cost: float | None) -> None:
        self.model_calls += 1
        self.input_tokens += inp
        self.output_tokens += out
        if cost is None:
            self.unpriced_calls += 1
        else:
            self.cost_usd += cost
        entry = self.by_model.setdefault(
            model, {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        )
        entry["calls"] += 1
        entry["input_tokens"] += inp
        entry["output_tokens"] += out
        entry["cost_usd"] += cost or 0.0

    def add_planning(self, usage: dict[str, Any]) -> None:
        calls = int(usage.get("calls", 0))
        inp, out = int(usage.get("input_tokens", 0)), int(usage.get("output_tokens", 0))
        self.model_calls += calls
        self.input_tokens += inp
        self.output_tokens += out
        self.cost_usd += float(usage.get("cost_usd") or 0.0)
        entry = self.by_model.setdefault(
            "planner", {"calls": 0, "input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0}
        )
        entry["calls"] += calls
        entry["input_tokens"] += inp
        entry["output_tokens"] += out
        entry["cost_usd"] += float(usage.get("cost_usd") or 0.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "tokens": self.tokens,
            "cost_usd": round(self.cost_usd, 6),
            "unpriced_calls": self.unpriced_calls,
            "model_calls": self.model_calls,
            "tool_calls": self.tool_calls,
            "retrieval_calls": self.retrieval_calls,
            "memory_ops": self.memory_ops,
            "replayed_effects": self.replayed_effects,
            "by_model": self.by_model,
        }


def check_budget(budget: Budget, usage: Usage, kind: str, elapsed_s: float) -> None:
    if budget.max_wall_s is not None and elapsed_s > budget.max_wall_s:
        raise BudgetExceeded(f"wall-clock budget of {budget.max_wall_s}s exhausted", limit="wall")
    if budget.max_cost_usd is not None and usage.cost_usd >= budget.max_cost_usd:
        raise BudgetExceeded(f"cost budget of ${budget.max_cost_usd} exhausted", limit="cost")
    if kind == "model":
        if budget.max_tokens is not None and usage.tokens >= budget.max_tokens:
            raise BudgetExceeded(f"token budget of {budget.max_tokens} exhausted", limit="tokens")
        if budget.max_model_calls is not None and usage.model_calls >= budget.max_model_calls:
            raise BudgetExceeded(
                f"model call budget of {budget.max_model_calls} exhausted", limit="model_calls"
            )
    if (
        kind == "tool"
        and budget.max_tool_calls is not None
        and usage.tool_calls >= budget.max_tool_calls
    ):
        raise BudgetExceeded(
            f"tool call budget of {budget.max_tool_calls} exhausted", limit="tool_calls"
        )
