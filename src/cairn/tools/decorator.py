"""``@tool`` decorator: derive a :class:`ToolSpec` from a typed Python function."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Iterable
from typing import Any, get_type_hints

from pydantic import TypeAdapter, create_model

from cairn.tools.spec import OutputTrust, ToolFn, ToolSpec

_PARAM_DOC = re.compile(r"^\s*(\w+)\s*(?:\([^)]*\))?\s*:\s*(.+)$")


def _param_docs(doc: str) -> dict[str, str]:
    out: dict[str, str] = {}
    in_args = False
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped.lower() in ("args:", "arguments:", "parameters:"):
            in_args = True
            continue
        if in_args:
            if not stripped:
                continue
            if stripped.endswith(":") and " " not in stripped:
                break
            match = _PARAM_DOC.match(line)
            if match:
                out[match.group(1)] = match.group(2).strip()
    return out


def schema_from_function(fn: Callable[..., Any]) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
    """Build a JSON schema for ``fn``'s parameters using pydantic.

    A parameter named ``ctx`` is injected by the runtime and excluded from the
    schema. Returns ``(input_schema, output_schema, wants_context)``.
    """
    sig = inspect.signature(fn)
    hints = get_type_hints(fn)
    docs = _param_docs(inspect.getdoc(fn) or "")
    fields: dict[str, Any] = {}
    wants_context = False
    for name, param in sig.parameters.items():
        if name == "ctx":
            wants_context = True
            continue
        if param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        annotation = hints.get(name, Any)
        default = ... if param.default is inspect.Parameter.empty else param.default
        fields[name] = (annotation, default)
    model = create_model(f"{fn.__name__}_args", **fields)
    schema = model.model_json_schema()
    schema.pop("title", None)
    for name, prop in schema.get("properties", {}).items():
        prop.pop("title", None)
        if name in docs:
            prop.setdefault("description", docs[name])
    output_schema = None
    if "return" in hints and hints["return"] is not type(None):
        try:
            output_schema = TypeAdapter(hints["return"]).json_schema()
        except Exception:  # unsupported return annotations simply omit the schema
            output_schema = None
    return schema, output_schema, wants_context


def tool(
    fn: ToolFn | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    effects: Iterable[str] = (),
    sensitive: Iterable[str] = (),
    output_trust: OutputTrust | str = OutputTrust.INHERIT,
    output_secrecy: Iterable[str] = (),
    allowed_secrecy: Iterable[str] = (),
    secrets: Iterable[str] = (),
    requires_approval: bool = False,
    timeout_s: float = 60.0,
    idempotent: bool = False,
    tags: Iterable[str] = (),
    version: str = "1",
) -> Any:
    """Turn a typed function into a tool.

    Example::

        @tool(effects={"send"}, sensitive={"to"})
        async def send_email(to: str, subject: str, body: str) -> str:
            '''Send an email.

            Args:
                to: recipient address
            '''
    """

    def wrap(f: ToolFn) -> ToolSpec:
        input_schema, output_schema, wants_context = schema_from_function(f)
        doc = inspect.getdoc(f) or ""
        summary = description or doc.split("\n\n")[0].strip() or f.__name__
        unknown = set(sensitive) - set(input_schema.get("properties", {}))
        if unknown:
            raise ValueError(f"sensitive params {sorted(unknown)} are not parameters of {f.__name__}")
        return ToolSpec(
            name=name or f.__name__,
            description=summary,
            input_schema=input_schema,
            output_schema=output_schema,
            fn=f,
            effects=frozenset(str(e) for e in effects),
            sensitive_params=frozenset(sensitive),
            output_trust=OutputTrust(output_trust),
            output_secrecy=frozenset(output_secrecy),
            allowed_secrecy=frozenset(allowed_secrecy),
            secrets=frozenset(secrets),
            requires_approval=requires_approval,
            timeout_s=timeout_s,
            idempotent=idempotent,
            tags=frozenset(tags),
            version=version,
            wants_context=wants_context,
        )

    if fn is not None:
        return wrap(fn)
    return wrap
