"""Exception hierarchy.

Every error carries a stable ``code`` so that journals, the HTTP API and the
CLI can report failures uniformly, and so retry policies can match on error
kinds instead of fragile message text.
"""

from __future__ import annotations

from typing import Any


class CairnError(Exception):
    code = "cairn_error"
    retryable = False

    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ConfigError(CairnError):
    code = "config_error"


class NotFound(CairnError):
    code = "not_found"


class PlanValidationError(CairnError):
    code = "plan_invalid"

    def __init__(self, message: str, problems: list[str] | None = None) -> None:
        super().__init__(message, problems=problems or [])
        self.problems = problems or []


class PolicyViolation(CairnError):
    """A provenance or capability policy denied an action outright."""

    code = "policy_violation"


class ApprovalRequired(CairnError):
    """Raised inside a node when a human decision is needed before continuing."""

    code = "approval_required"

    def __init__(self, message: str, request_id: str, **details: Any) -> None:
        super().__init__(message, request_id=request_id, **details)
        self.request_id = request_id


class BudgetExceeded(CairnError):
    code = "budget_exceeded"


class ReplayDivergence(CairnError):
    """Strict replay found an effect whose request differs from the recording."""

    code = "replay_divergence"


class RunCancelled(CairnError):
    code = "cancelled"


class SandboxViolation(CairnError):
    code = "sandbox_violation"


class ToolError(CairnError):
    code = "tool_error"
    retryable = True


class ToolArgumentError(ToolError):
    code = "tool_arguments_invalid"
    retryable = False


class ToolTimeout(ToolError):
    code = "tool_timeout"


class ModelError(CairnError):
    code = "model_error"
    retryable = True


class RateLimited(ModelError):
    code = "rate_limited"


class VerificationFailed(CairnError):
    code = "verification_failed"
    retryable = True


def error_payload(exc: BaseException) -> dict[str, Any]:
    """Uniform, JSON-safe description of any exception for the journal."""
    if isinstance(exc, CairnError):
        payload = exc.to_dict()
        payload["retryable"] = exc.retryable
        return payload
    return {
        "code": type(exc).__name__,
        "message": str(exc) or type(exc).__name__,
        "details": {},
        "retryable": isinstance(exc, TimeoutError | ConnectionError),
    }
