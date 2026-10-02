"""Resolve plan arguments, templates and conditions to labeled values.

Every resolution returns both the value and the join of the labels of every
node output it touched. This is where provenance propagates: if a template
interpolates a web page, the resulting string carries the web page's label.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from cairn.provenance.labels import BOTTOM, Label
from cairn.provenance.values import Labeled, resolve_path
from cairn.runtime.plan import REF_RE, Condition, split_ref
from cairn.security.injection import quarantine


class Scope:
    """Name -> labeled value lookup used during resolution."""

    def __init__(self, values: Mapping[str, Labeled], plan_label: Label = BOTTOM) -> None:
        self.values = dict(values)
        self.plan_label = plan_label

    def child(self, **extra: Labeled) -> Scope:
        scope = Scope(self.values, self.plan_label)
        scope.values.update(extra)
        return scope

    def lookup(self, path: str) -> Labeled:
        root, rest = split_ref(path)
        if root not in self.values:
            raise KeyError(f"'{root}' has no output (not executed, skipped or unknown)")
        base = self.values[root]
        try:
            value = resolve_path(base.value, rest)
        except KeyError as exc:
            raise KeyError(f"reference '{path}': {exc.args[0]}") from None
        return Labeled(value, base.label)


def resolve(value: Any, scope: Scope) -> Labeled:
    """Resolve refs and templates recursively. Literals carry the plan's label."""
    label = scope.plan_label

    def walk(v: Any) -> Any:
        nonlocal label
        if isinstance(v, dict):
            if set(v) == {"$ref"} and isinstance(v["$ref"], str):
                found = scope.lookup(v["$ref"])
                label = label.join(found.label)
                return found.value
            if set(v) == {"$tmpl"} and isinstance(v["$tmpl"], str):
                rendered = render(v["$tmpl"], scope, quarantine_untrusted=False)
                label = label.join(rendered.label)
                return rendered.value
            return {k: walk(item) for k, item in v.items()}
        if isinstance(v, list):
            return [walk(item) for item in v]
        return v

    resolved = walk(value)
    return Labeled(resolved, label)


def render(template: str, scope: Scope, *, quarantine_untrusted: bool = True) -> Labeled:
    """Interpolate ``{{ref}}`` placeholders.

    With ``quarantine_untrusted`` (used for prompts), untrusted values are
    wrapped in explicit data delimiters so the model is told they are content,
    not instructions. Labels still propagate either way.
    """
    label = scope.plan_label

    def substitute(match: re.Match[str]) -> str:
        nonlocal label
        found = scope.lookup(match.group(1))
        label = label.join(found.label)
        text = to_text(found.value)
        if quarantine_untrusted and not found.label.trusted:
            source = ",".join(sorted(found.label.sources)) or "untrusted source"
            return quarantine(text, source)
        return text

    return Labeled(REF_RE.sub(substitute, template), label)


def to_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    return json.dumps(value, indent=2, ensure_ascii=False, default=str)


def evaluate(cond: Condition, scope: Scope) -> Labeled:
    """Evaluate a condition; the result label is the join of everything it read."""
    if cond.op in ("and", "or"):
        results = [evaluate(c, scope) for c in cond.args]
        label = scope.plan_label
        for r in results:
            label = label.join(r.label)
        values = [bool(r.value) for r in results]
        return Labeled(all(values) if cond.op == "and" else any(values), label)
    if cond.op == "not":
        if len(cond.args) != 1:
            raise ValueError("'not' takes exactly one argument")
        inner = evaluate(cond.args[0], scope)
        return Labeled(not inner.value, inner.label)
    left = resolve(cond.left, scope)
    right = resolve(cond.right, scope)
    label = left.label.join(right.label)
    lv, rv = left.value, right.value
    op = cond.op
    if op == "eq":
        result = lv == rv
    elif op == "ne":
        result = lv != rv
    elif op in ("gt", "gte", "lt", "lte"):
        try:
            a, b = float(lv), float(rv)
        except (TypeError, ValueError):
            result = False
        else:
            result = {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
    elif op == "contains":
        result = _contains(lv, rv)
    elif op == "not_contains":
        result = not _contains(lv, rv)
    elif op == "truthy":
        result = bool(lv)
    elif op == "falsy":
        result = not lv
    elif op == "len_gt":
        result = _len(lv) > int(rv)
    elif op == "len_lt":
        result = _len(lv) < int(rv)
    elif op == "matches":
        result = re.search(str(rv), to_text(lv)) is not None
    else:  # pragma: no cover - guarded by the Literal type
        raise ValueError(f"unknown condition op {op}")
    return Labeled(bool(result), label)


def _contains(container: Any, item: Any) -> bool:
    if isinstance(container, str):
        return str(item).lower() in container.lower()
    if isinstance(container, list | tuple | dict | set):
        return item in container
    return False


def _len(value: Any) -> int:
    try:
        return len(value)
    except TypeError:
        return 0
