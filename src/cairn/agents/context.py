"""Context engineering for planners: budgeted, prioritized, provenance-aware.

The planner is the privileged component: its output decides which tools run.
So the context it sees is assembled deliberately:

* **Routing**: each section has a priority; when the token budget is tight,
  low-priority sections are truncated first, then dropped (compression).
* **Isolation**: every section carries a provenance label. By default
  untrusted sections (recalled memories that came from web pages, for
  example) are excluded from the planner context, so the plan stays trusted.
  If an operator opts in, they are included and the plan's control label
  becomes untrusted, which the policy engine then enforces at every
  privileged tool call. Either way the trade-off is explicit and journaled.
* **Caching**: sections are rendered in a stable order (static instructions
  first, volatile content last) so provider-side prompt caches hit, and the
  rendered tool catalog is memoized per tool set.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cairn.core.ids import stable_hash
from cairn.models.types import estimate_tokens
from cairn.provenance.labels import BOTTOM, Label, join_all

_MARKER = "\n[... truncated to fit context budget]"


@dataclass
class Section:
    name: str
    text: str
    priority: int  # higher survives longer under budget pressure
    label: Label = BOTTOM
    min_tokens: int = 0  # never truncated below this; dropped instead

    @property
    def tokens(self) -> int:
        return estimate_tokens(self.text)


@dataclass
class BuiltContext:
    text: str
    label: Label
    included: list[str]
    dropped: list[str]
    truncated: list[str]
    excluded_untrusted: list[str]
    tokens: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label.to_dict(),
            "included": self.included,
            "dropped": self.dropped,
            "truncated": self.truncated,
            "excluded_untrusted": self.excluded_untrusted,
            "tokens": self.tokens,
        }


@dataclass
class ContextBuilder:
    budget_tokens: int = 6000
    isolate_untrusted: bool = True
    sections: list[Section] = field(default_factory=list)

    def add(self, name: str, text: str, priority: int, label: Label = BOTTOM, min_tokens: int = 0) -> None:
        if text.strip():
            self.sections.append(Section(name, text.strip(), priority, label, min_tokens))

    def build(self) -> BuiltContext:
        excluded = []
        candidates = []
        for s in self.sections:
            if self.isolate_untrusted and not s.label.trusted:
                excluded.append(s.name)
            else:
                candidates.append(s)
        order = {id(s): i for i, s in enumerate(candidates)}
        kept = list(candidates)
        dropped: list[str] = []
        truncated: list[str] = []

        def total() -> int:
            return sum(s.tokens for s in kept)

        # Compress lowest-priority sections first: truncate, then drop.
        for victim in sorted(candidates, key=lambda s: (s.priority, -order[id(s)])):
            if total() <= self.budget_tokens:
                break
            overflow = total() - self.budget_tokens
            room = victim.tokens - overflow - estimate_tokens(_MARKER)
            if room > max(victim.min_tokens, 8):
                cut = victim.text[: room * 4]
                if "\n" in cut:
                    cut = cut.rsplit("\n", 1)[0]
                victim.text = cut + _MARKER
                truncated.append(victim.name)
            else:
                kept.remove(victim)
                dropped.append(victim.name)
        kept.sort(key=lambda s: order[id(s)])
        text = "\n\n".join(f"## {s.name}\n{s.text}" for s in kept)
        return BuiltContext(
            text=text,
            label=join_all(s.label for s in kept),
            included=[s.name for s in kept],
            dropped=dropped,
            truncated=truncated,
            excluded_untrusted=excluded,
            tokens=estimate_tokens(text),
        )


_CATALOG_CACHE: dict[str, str] = {}


def render_catalog(entries: list[dict[str, Any]]) -> str:
    """Render tool catalog entries; memoized by content hash (context caching)."""
    key = stable_hash(entries, length=16)
    cached = _CATALOG_CACHE.get(key)
    if cached is not None:
        return cached
    lines = []
    for e in entries:
        params = ", ".join(
            f"{name}{'' if name in e['required'] else '?'}: {spec.get('type', 'any')}"
            + (f" ({spec['description']})" if spec.get("description") else "")
            for name, spec in e["params"].items()
        )
        effects = f" [effects: {', '.join(e['effects'])}]" if e["effects"] else ""
        lines.append(f"- {e['name']}({params}){effects}: {e['description']}")
    rendered = "\n".join(lines)
    if len(_CATALOG_CACHE) > 256:
        _CATALOG_CACHE.clear()
    _CATALOG_CACHE[key] = rendered
    return rendered
