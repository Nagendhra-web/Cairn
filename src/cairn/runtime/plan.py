"""Plan IR: the typed execution graph that the runtime executes.

Plans are data, not code. A planner model emits one as JSON, a developer
builds one in Python with :class:`PlanBuilder`, or a workflow file defines one.
Because the plan is data the runtime can validate it before running anything
(unknown tools, missing grants, cycles, bad references), journal it, diff it,
patch it when forking, and reason about information flow through it.

Argument values inside nodes are JSON with two special forms:

* ``{"$ref": "node_id.path.to[0].field"}`` - the (labeled) output of a node.
* ``{"$tmpl": "Summary of {{fetch.text}}"}`` - string interpolation of refs.

Special ref roots: ``$item`` and ``$index`` inside ``map`` bodies, ``$last``
and ``$iteration`` inside ``loop`` bodies, ``$feedback`` when a verifier
re-runs a node, and ``$input`` for run inputs.
"""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from cairn.models.types import Tier

REF_RE = re.compile(r"\{\{\s*([$A-Za-z_][\w$.\[\]-]*)\s*\}\}")
NODE_ID_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
SPECIAL_ROOTS = frozenset({"$item", "$index", "$last", "$iteration", "$feedback", "$input"})


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


def ref(path: str) -> dict[str, str]:
    return {"$ref": path}


def tmpl(text: str) -> dict[str, str]:
    return {"$tmpl": text}


class Condition(_Strict):
    """A side-effect-free predicate over node outputs.

    Deliberately not an expression language with ``eval``: planners can emit
    it as JSON, and it can never execute code.
    """

    op: Literal[
        "eq", "ne", "gt", "gte", "lt", "lte", "contains", "not_contains", "truthy", "falsy",
        "len_gt", "len_lt", "matches", "and", "or", "not",
    ]
    left: Any = None
    right: Any = None
    args: list[Condition] = Field(default_factory=list)


class RetryPolicy(_Strict):
    max_attempts: int = Field(default=1, ge=1, le=10)
    backoff_s: float = Field(default=0.5, ge=0)
    multiplier: float = Field(default=2.0, ge=1)
    max_backoff_s: float = Field(default=30.0, ge=0)
    escalate_tier: bool = False


class Fallback(_Strict):
    """An alternative strategy tried after the primary one is exhausted."""

    tool: str | None = None
    args: dict[str, Any] | None = None
    tier: Tier | None = None
    model: str | None = None


class NodeBase(_Strict):
    id: str
    deps: list[str] = Field(default_factory=list)
    description: str = ""
    when: Condition | None = None
    join: Literal["all", "any"] = "all"
    retry: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout_s: float | None = Field(default=None, gt=0)
    fallbacks: list[Fallback] = Field(default_factory=list)
    on_error: Literal["fail", "skip", "default"] = "fail"
    default: Any = None


class ToolNode(NodeBase):
    kind: Literal["tool"] = "tool"
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)


class LLMNode(NodeBase):
    kind: Literal["llm"] = "llm"
    prompt: str
    system: str | None = None
    output_schema: dict[str, Any] | None = None
    tier: Tier = Tier.BALANCED
    model: str | None = None
    max_tokens: int = Field(default=2048, ge=1)
    images: list[Any] = Field(default_factory=list)


class RetrieveNode(NodeBase):
    kind: Literal["retrieve"] = "retrieve"
    query: Any
    collection: str = "default"
    k: int = Field(default=5, ge=1, le=100)
    mode: Literal["hybrid", "lexical", "dense", "adaptive"] = "hybrid"


class MemoryNode(NodeBase):
    kind: Literal["memory"] = "memory"
    op: Literal["recall", "remember"]
    query: Any = None
    text: Any = None
    memory_kind: Literal["semantic", "episodic", "procedural"] = "semantic"
    k: int = Field(default=5, ge=1, le=50)
    importance: float = Field(default=0.5, ge=0, le=1)


class AgentNode(NodeBase):
    """Delegate a sub-goal to a child agent run (dynamic task creation).

    The child gets an attenuated capability set: ``tools`` must be a subset of
    the parent's grants. Its plan inherits this node's control label, so a
    sub-goal derived from untrusted data cannot launder that taint.
    """

    kind: Literal["agent"] = "agent"
    goal: Any
    tools: list[str] = Field(default_factory=lambda: ["*"])
    tier: Tier = Tier.BALANCED
    instructions: str | None = None


class ApprovalNode(NodeBase):
    kind: Literal["approval"] = "approval"
    message: Any
    show: dict[str, Any] = Field(default_factory=dict)


class Check(_Strict):
    type: Literal[
        "not_empty", "regex", "contains", "not_contains", "json_schema", "max_length",
        "min_length", "equals", "condition",
    ]
    value: Any = None
    condition: Condition | None = None
    path: str = ""


class VerifyNode(NodeBase):
    """Validate another node's output; optionally re-run it with feedback.

    Every other dependent of ``target`` is automatically made to depend on the
    verifier, so nothing consumes an unverified value.
    """

    kind: Literal["verify"] = "verify"
    target: str
    checks: list[Check] = Field(default_factory=list)
    critic: str | None = None
    critic_tier: Tier = Tier.BALANCED
    max_rounds: int = Field(default=2, ge=1, le=5)
    on_fail: Literal["retry_target", "fail", "warn"] = "retry_target"


BodyNode = Annotated[ToolNode | LLMNode, Field(discriminator="kind")]


class MapNode(NodeBase):
    """Fan out ``body`` over each element of ``over`` concurrently."""

    kind: Literal["map"] = "map"
    over: Any
    body: BodyNode
    max_parallel: int = Field(default=4, ge=1, le=64)
    max_items: int = Field(default=100, ge=1)


class LoopNode(NodeBase):
    """Repeat ``body`` until ``until`` holds (evaluated with ``$last``)."""

    kind: Literal["loop"] = "loop"
    body: BodyNode
    until: Condition
    max_iterations: int = Field(default=5, ge=1, le=50)


Node = Annotated[
    ToolNode | LLMNode | RetrieveNode | MemoryNode | AgentNode | ApprovalNode | VerifyNode
    | MapNode | LoopNode,
    Field(discriminator="kind"),
]


class Plan(_Strict):
    goal: str
    nodes: list[Node]
    output: Any = None
    version: int = 1
    metadata: dict[str, Any] = Field(default_factory=dict)

    def node(self, node_id: str) -> Node:
        for n in self.nodes:
            if n.id == node_id:
                return n
        raise KeyError(node_id)

    @property
    def ids(self) -> list[str]:
        return [n.id for n in self.nodes]

    def descendants(self, node_id: str) -> set[str]:
        children: dict[str, set[str]] = {n.id: set() for n in self.nodes}
        for n in self.nodes:
            for d in n.deps:
                children.setdefault(d, set()).add(n.id)
        out: set[str] = set()
        stack = [node_id]
        while stack:
            for child in children.get(stack.pop(), ()):
                if child not in out:
                    out.add(child)
                    stack.append(child)
        return out


def collect_refs(value: Any) -> set[str]:
    """Return the root node ids referenced anywhere inside ``value``."""
    roots: set[str] = set()

    def walk(v: Any) -> None:
        if isinstance(v, dict):
            if set(v) == {"$ref"} and isinstance(v["$ref"], str):
                roots.add(_root(v["$ref"]))
            elif set(v) == {"$tmpl"} and isinstance(v["$tmpl"], str):
                roots.update(_root(m) for m in REF_RE.findall(v["$tmpl"]))
            else:
                for item in v.values():
                    walk(item)
        elif isinstance(v, list):
            for item in v:
                walk(item)
        elif isinstance(v, BaseModel):
            walk(v.model_dump())

    walk(value)
    return roots


def node_refs(node: BaseModel) -> set[str]:
    """All roots referenced by a node, including inside prompts and conditions."""
    data = node.model_dump(exclude={"id", "deps", "description", "retry", "fallbacks"})
    roots = collect_refs(data)
    if isinstance(node, LLMNode):
        roots.update(_root(m) for m in REF_RE.findall(node.prompt))
        if node.system:
            roots.update(_root(m) for m in REF_RE.findall(node.system))
    if isinstance(node, MapNode | LoopNode):
        roots.update(node_refs(node.body))
    return roots


def _root(path: str) -> str:
    return re.split(r"[.\[]", path, maxsplit=1)[0]


def split_ref(path: str) -> tuple[str, str]:
    root = _root(path)
    rest = path[len(root):]
    return root, rest[1:] if rest.startswith(".") else rest


class PlanBuilder:
    """Fluent Python API for authoring plans.

    Example::

        b = PlanBuilder("Summarize a page")
        page = b.tool("fetch", "http.fetch", url="https://example.com")
        b.llm("summary", "Summarize:\\n{{fetch.text}}", tier="fast")
        plan = b.build(output=ref("summary"))
    """

    def __init__(self, goal: str) -> None:
        self.goal = goal
        self.nodes: list[Any] = []

    def add(self, node: Any) -> str:
        self.nodes.append(node)
        return str(node.id)

    def tool(self, node_id: str, tool: str, deps: list[str] | None = None, **args: Any) -> str:
        return self.add(ToolNode(id=node_id, tool=tool, args=args, deps=deps or []))

    def llm(self, node_id: str, prompt: str, **kw: Any) -> str:
        return self.add(LLMNode(id=node_id, prompt=prompt, **kw))

    def retrieve(self, node_id: str, query: Any, **kw: Any) -> str:
        return self.add(RetrieveNode(id=node_id, query=query, **kw))

    def verify(self, node_id: str, target: str, **kw: Any) -> str:
        return self.add(VerifyNode(id=node_id, target=target, **kw))

    def approval(self, node_id: str, message: Any, **kw: Any) -> str:
        return self.add(ApprovalNode(id=node_id, message=message, **kw))

    def agent(self, node_id: str, goal: Any, **kw: Any) -> str:
        return self.add(AgentNode(id=node_id, goal=goal, **kw))

    def build(self, output: Any = None, **metadata: Any) -> Plan:
        return Plan(goal=self.goal, nodes=self.nodes, output=output, metadata=metadata)
