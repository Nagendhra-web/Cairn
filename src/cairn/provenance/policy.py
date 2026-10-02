"""Flow policy engine.

Every tool invocation is checked *before* it executes. The check sees:

* the tool's declared effects (``send``, ``write``, ``execute``, ``network``...),
* which parameters are sensitive sinks (recipients, paths, URLs, commands),
* the label of every argument (data integrity),
* the label of the decision to call the tool at all (control integrity), which
  degrades when a plan was produced after reading untrusted data or when the
  call sits under a branch whose condition was untrusted,
* the capability grants of the calling agent.

This is the core prompt-injection defense. It does not try to recognize
malicious text; it tracks *where data came from* and refuses to let untrusted
data steer privileged actions. A model that has been fully hijacked by an
injected instruction still cannot exfiltrate data, because the argument that
carries the attacker's address is labeled untrusted and the sink requires
trusted input or a human approval.
"""

from __future__ import annotations

import fnmatch
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from cairn.provenance.labels import Label, join_all


class Verdict(StrEnum):
    ALLOW = "allow"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"

    @property
    def severity(self) -> int:
        return {"allow": 0, "require_approval": 1, "deny": 2}[self.value]


#: Effects that change the world or move data out of the process.
PRIVILEGED_EFFECTS = frozenset({"write", "send", "execute", "delete", "payment"})
EGRESS_EFFECTS = frozenset({"send", "network"})


@dataclass(frozen=True)
class FlowRequest:
    tool: str
    effects: frozenset[str]
    args: dict[str, Any]
    arg_labels: dict[str, Label]
    control: Label
    sensitive_params: frozenset[str] = frozenset()
    allowed_secrecy: frozenset[str] = frozenset()
    requires_approval: bool = False
    grants: tuple[str, ...] = ("*",)
    agent: str = "default"

    @property
    def data_label(self) -> Label:
        return join_all(self.arg_labels.values())


@dataclass(frozen=True)
class Decision:
    verdict: Verdict
    rule: str
    reason: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict.value,
            "rule": self.rule,
            "reason": self.reason,
            "details": self.details,
        }


Rule = Callable[[FlowRequest], Decision | None]


def capability_rule(req: FlowRequest) -> Decision | None:
    if any(fnmatch.fnmatchcase(req.tool, pattern) for pattern in req.grants):
        return None
    return Decision(
        Verdict.DENY,
        "capability",
        f"agent '{req.agent}' has no grant for tool '{req.tool}'",
        {"grants": list(req.grants)},
    )


def sensitive_sink_rule(req: FlowRequest) -> Decision | None:
    tainted = sorted(
        name
        for name in req.sensitive_params
        if name in req.arg_labels and not req.arg_labels[name].trusted
    )
    if not tainted:
        return None
    sources = sorted(join_all(req.arg_labels[n] for n in tainted).sources)
    return Decision(
        Verdict.REQUIRE_APPROVAL,
        "untrusted-to-sensitive-sink",
        f"sensitive parameter(s) {tainted} of '{req.tool}' derive from untrusted data",
        {"params": tainted, "sources": sources},
    )


def control_integrity_rule(req: FlowRequest) -> Decision | None:
    privileged = req.effects & PRIVILEGED_EFFECTS
    if not privileged or req.control.trusted:
        return None
    return Decision(
        Verdict.REQUIRE_APPROVAL,
        "untrusted-control-flow",
        f"the decision to call '{req.tool}' ({', '.join(sorted(privileged))}) "
        "was influenced by untrusted data",
        {"sources": sorted(req.control.sources)},
    )


def secrecy_egress_rule(req: FlowRequest) -> Decision | None:
    if not (req.effects & EGRESS_EFFECTS):
        return None
    leaking = sorted(req.data_label.secrecy - req.allowed_secrecy)
    if not leaking:
        return None
    return Decision(
        Verdict.DENY,
        "secret-egress",
        f"'{req.tool}' would send data derived from {leaking} outside the runtime",
        {"secrecy": leaking},
    )


def declared_approval_rule(req: FlowRequest) -> Decision | None:
    if req.requires_approval:
        return Decision(
            Verdict.REQUIRE_APPROVAL,
            "tool-requires-approval",
            f"'{req.tool}' is configured to always require approval",
        )
    return None


DEFAULT_RULES: tuple[Rule, ...] = (
    capability_rule,
    secrecy_egress_rule,
    sensitive_sink_rule,
    control_integrity_rule,
    declared_approval_rule,
)


class PolicyEngine:
    """Evaluates all rules and returns the most severe decision.

    ``strict=True`` upgrades every ``REQUIRE_APPROVAL`` to ``DENY``, which is
    the right setting for unattended batch jobs where nobody can approve.
    """

    def __init__(
        self,
        rules: Iterable[Rule] = DEFAULT_RULES,
        *,
        strict: bool = False,
        enabled: bool = True,
    ) -> None:
        self.rules: list[Rule] = list(rules)
        self.strict = strict
        self.enabled = enabled

    def add_rule(self, rule: Rule) -> None:
        self.rules.append(rule)

    def evaluate(self, req: FlowRequest) -> Decision:
        if not self.enabled:
            # Capability grants are always enforced; disabling the engine only
            # turns off provenance rules (used for baseline benchmarks).
            cap = capability_rule(req)
            return cap or Decision(Verdict.ALLOW, "disabled", "provenance policy disabled")
        worst = Decision(Verdict.ALLOW, "default", "no rule objected")
        for rule in self.rules:
            decision = rule(req)
            if decision is None:
                continue
            if self.strict and decision.verdict is Verdict.REQUIRE_APPROVAL:
                decision = Decision(
                    Verdict.DENY, decision.rule, decision.reason + " (strict mode)", decision.details
                )
            if decision.verdict.severity > worst.verdict.severity:
                worst = decision
        return worst
