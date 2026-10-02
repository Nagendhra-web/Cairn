"""Static plan validation and normalization.

Run before any effect executes. A planner that hallucinates a tool, references
a node that does not exist, creates a cycle, or exceeds the node budget is
caught here, and the precise problem list is fed back to the planner for a
repair attempt. Nothing is half-executed.
"""

from __future__ import annotations

import fnmatch
from collections import deque
from collections.abc import Iterable
from typing import Any

from cairn.core.errors import PlanValidationError
from cairn.runtime.plan import (
    NODE_ID_RE,
    SPECIAL_ROOTS,
    AgentNode,
    LoopNode,
    MapNode,
    Plan,
    ToolNode,
    VerifyNode,
    collect_refs,
    node_refs,
)
from cairn.tools.registry import ToolRegistry


def validate_plan(
    plan: Plan,
    tools: ToolRegistry | None = None,
    *,
    grants: Iterable[str] = ("*",),
    max_nodes: int = 64,
) -> Plan:
    """Return a normalized copy of ``plan`` or raise :class:`PlanValidationError`."""
    problems: list[str] = []
    grants = list(grants)
    plan = plan.model_copy(deep=True)
    ids = [n.id for n in plan.nodes]
    id_set = set(ids)

    if not plan.nodes:
        problems.append("plan has no nodes")
    if len(plan.nodes) > max_nodes:
        problems.append(f"plan has {len(plan.nodes)} nodes; the budget allows {max_nodes}")
    seen: set[str] = set()
    for node_id in ids:
        if not NODE_ID_RE.match(node_id):
            problems.append(f"node id '{node_id}' must match {NODE_ID_RE.pattern}")
        if node_id in seen:
            problems.append(f"duplicate node id '{node_id}'")
        seen.add(node_id)

    for node in plan.nodes:
        allowed_special = {"$input"}
        if isinstance(node, MapNode):
            allowed_special |= {"$item", "$index"}
        if isinstance(node, LoopNode):
            allowed_special |= {"$last", "$iteration"}
        refs = node_refs(node)
        for root in sorted(refs):
            if root in SPECIAL_ROOTS:
                if root not in allowed_special and root != "$feedback":
                    problems.append(f"node '{node.id}' uses {root} outside a map/loop body")
                continue
            if root == node.id:
                problems.append(f"node '{node.id}' references itself")
            elif root not in id_set:
                problems.append(f"node '{node.id}' references unknown node '{root}'")
            elif root not in node.deps:
                node.deps.append(root)
        for dep in node.deps:
            if dep not in id_set:
                problems.append(f"node '{node.id}' depends on unknown node '{dep}'")
        node.deps = sorted(set(node.deps))

        for tool_name, tool_args, where in _tools_used(node):
            if tools is not None and tool_name not in tools:
                problems.append(f"node '{node.id}' uses unknown tool '{tool_name}'{where}")
                continue
            if not any(fnmatch.fnmatchcase(tool_name, g) for g in grants):
                problems.append(f"node '{node.id}' uses tool '{tool_name}' which is not granted")
                continue
            if tools is not None:
                problems.extend(_check_tool_args(node.id, tools, tool_name, tool_args))
        if isinstance(node, AgentNode):
            for pattern in node.tools:
                if not _attenuates(pattern, grants):
                    problems.append(
                        f"agent node '{node.id}' requests tools '{pattern}' beyond parent grants"
                    )

    # Verification gates: dependents of a verified node must wait for the verifier.
    for node in plan.nodes:
        if isinstance(node, VerifyNode):
            if node.target not in id_set:
                problems.append(f"verify node '{node.id}' targets unknown node '{node.target}'")
                continue
            if node.target not in node.deps:
                node.deps.append(node.target)
            for other in plan.nodes:
                if other.id in (node.id, node.target) or isinstance(other, VerifyNode):
                    continue
                if node.target in other.deps and node.id not in other.deps:
                    other.deps.append(node.id)
                    other.deps.sort()

    for root in sorted(collect_refs(plan.output)):
        if root not in id_set:
            problems.append(f"plan output references unknown node '{root}'")

    if not problems:
        cycle = _find_cycle(plan)
        if cycle:
            problems.append("dependency cycle: " + " -> ".join(cycle))

    if problems:
        raise PlanValidationError(f"plan failed validation with {len(problems)} problem(s)", problems)
    return plan


def topological_order(plan: Plan) -> list[str]:
    indegree = {n.id: len(n.deps) for n in plan.nodes}
    children: dict[str, list[str]] = {n.id: [] for n in plan.nodes}
    for n in plan.nodes:
        for d in n.deps:
            children[d].append(n.id)
    queue = deque(sorted(i for i, d in indegree.items() if d == 0))
    order: list[str] = []
    while queue:
        current = queue.popleft()
        order.append(current)
        for child in sorted(children[current]):
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    return order


def _find_cycle(plan: Plan) -> list[str] | None:
    deps = {n.id: list(n.deps) for n in plan.nodes}
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(node_id: str) -> list[str] | None:
        state[node_id] = 1
        path.append(node_id)
        for dep in deps.get(node_id, []):
            if state.get(dep) == 1:
                return [*path[path.index(dep):], dep]
            if state.get(dep) is None:
                found = visit(dep)
                if found:
                    return found
        path.pop()
        state[node_id] = 2
        return None

    for node_id in deps:
        if state.get(node_id) is None:
            found = visit(node_id)
            if found:
                return found
    return None


def _tools_used(node: Any) -> list[tuple[str, dict[str, Any], str]]:
    """(tool, args, location) for every tool a node may call, fallbacks included."""
    out: list[tuple[str, dict[str, Any], str]] = []
    primary_args: dict[str, Any] = {}
    if isinstance(node, ToolNode):
        primary_args = node.args
        out.append((node.tool, node.args, ""))
    if isinstance(node, MapNode | LoopNode) and isinstance(node.body, ToolNode):
        out.append((node.body.tool, node.body.args, " (in body)"))
    for fb in getattr(node, "fallbacks", []):
        if fb.tool:
            out.append((fb.tool, fb.args if fb.args is not None else primary_args, " (fallback)"))
    return out


def _check_tool_args(
    node_id: str, tools: ToolRegistry, tool_name: str, args: dict[str, Any]
) -> list[str]:
    spec = tools.get(tool_name)
    props = spec.input_schema.get("properties", {})
    problems = []
    for required in spec.input_schema.get("required", []):
        if required not in args:
            problems.append(f"node '{node_id}': tool '{tool_name}' requires argument '{required}'")
    for name in args:
        if name not in props:
            problems.append(f"node '{node_id}': tool '{tool_name}' has no parameter '{name}'")
    return problems


def _attenuates(pattern: str, grants: list[str]) -> bool:
    """True if every tool matched by ``pattern`` is matched by some parent grant."""
    return any(g == "*" or fnmatch.fnmatchcase(pattern, g) for g in grants)
