"""Working memory: a bounded, per-session scratchpad rendered under a token budget.

Working memory is the context an agent carries between steps of one session
(observations, intermediate results, the user's latest constraints). It is
process-local on purpose: it changes every step and is cheap to rebuild, so
persisting it would only add write load. What does not fit the model's
context window is compressed by priority: low-priority items are dropped or
truncated first, so the budget is spent on what matters most.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

from cairn.core.clock import Clock, SystemClock
from cairn.core.ids import new_id
from cairn.provenance.labels import BOTTOM, Label, join_all

TokenEstimator = Callable[[str], int]


def estimate_tokens(text: str) -> int:
    """Rough token count (about four characters per token for English text)."""
    return max(1, math.ceil(len(text) / 4)) if text else 0


@dataclass(slots=True)
class WorkingItem:
    key: str
    text: str
    priority: float
    label: Label
    added_at: float
    seq: int


@dataclass(slots=True)
class RenderedContext:
    """The result of :meth:`WorkingMemory.render`.

    ``label`` joins only the items that made it into ``text``: content that was
    dropped cannot influence the model, so it should not taint the output.
    """

    text: str
    tokens: int
    label: Label
    included: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)


class WorkingMemory:
    """Bounded scratchpad. When full, the lowest-priority (then oldest) item is evicted."""

    def __init__(
        self,
        max_items: int = 64,
        *,
        clock: Clock | None = None,
        estimator: TokenEstimator = estimate_tokens,
        min_truncated_tokens: int = 8,
    ) -> None:
        if max_items < 1:
            raise ValueError("max_items must be at least 1")
        self.max_items = max_items
        self.clock: Clock = clock or SystemClock()
        self.estimator = estimator
        self.min_truncated_tokens = min_truncated_tokens
        self._items: dict[str, WorkingItem] = {}
        self._seq = 0

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, key: object) -> bool:
        return key in self._items

    def add(
        self,
        text: str,
        priority: float = 0.5,
        label: Label = BOTTOM,
        *,
        key: str | None = None,
    ) -> WorkingItem:
        """Add or replace (by ``key``) an item, evicting the weakest item if over capacity."""
        self._seq += 1
        item = WorkingItem(
            key=key or new_id("wm"),
            text=text,
            priority=min(1.0, max(0.0, priority)),
            label=label,
            added_at=self.clock.now(),
            seq=self._seq,
        )
        self._items.pop(item.key, None)
        self._items[item.key] = item
        while len(self._items) > self.max_items:
            weakest = min(self._items.values(), key=lambda i: (i.priority, i.seq))
            del self._items[weakest.key]
        return item

    def get(self, key: str) -> WorkingItem | None:
        return self._items.get(key)

    def remove(self, key: str) -> bool:
        return self._items.pop(key, None) is not None

    def clear(self) -> None:
        self._items.clear()

    def items(self) -> list[WorkingItem]:
        """Items in insertion order."""
        return sorted(self._items.values(), key=lambda i: i.seq)

    @property
    def label(self) -> Label:
        return join_all(i.label for i in self._items.values())

    def render(self, budget_tokens: int, *, separator: str = "\n") -> RenderedContext:
        """Render the highest-priority items that fit in ``budget_tokens``.

        Items are chosen greedily by priority (newest first on ties). An item
        that does not fit is truncated if at least ``min_truncated_tokens``
        remain, otherwise dropped. Chosen items are emitted in insertion order
        so the rendered context still reads chronologically.
        """
        ranked = sorted(self._items.values(), key=lambda i: (-i.priority, -i.seq))
        sep_cost = self.estimator(separator) if separator else 0
        remaining = max(0, budget_tokens)
        chosen: dict[str, str] = {}
        truncated: list[str] = []
        dropped: list[str] = []
        for item in ranked:
            cost = self.estimator(item.text) + (sep_cost if chosen else 0)
            if cost <= remaining:
                chosen[item.key] = item.text
                remaining -= cost
                continue
            room = remaining - (sep_cost if chosen else 0)
            if room >= self.min_truncated_tokens:
                text = self._truncate(item.text, room)
                if text:
                    chosen[item.key] = text
                    truncated.append(item.key)
                    remaining -= self.estimator(text) + (sep_cost if len(chosen) > 1 else 0)
                    continue
            dropped.append(item.key)
        ordered = [i for i in self.items() if i.key in chosen]
        text = separator.join(chosen[i.key] for i in ordered)
        return RenderedContext(
            text=text,
            tokens=self.estimator(text),
            label=join_all(i.label for i in ordered),
            included=[i.key for i in ordered],
            truncated=truncated,
            dropped=dropped,
        )

    def _truncate(self, text: str, tokens: int) -> str:
        """Longest prefix (cut at a word boundary when possible) within ``tokens``."""
        marker = " ..."
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.estimator(text[:mid] + marker) <= tokens:
                lo = mid
            else:
                hi = mid - 1
        if lo == 0:
            return ""
        cut = text[:lo]
        space = cut.rfind(" ")
        if space > lo // 2:
            cut = cut[:space]
        return cut.rstrip() + marker
