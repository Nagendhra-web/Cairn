"""Structured output extraction and validation.

Native JSON-schema modes are used where providers support them, but the
runtime never trusts them blindly: every structured response is parsed and
validated here, and validation errors are fed back to the model for a bounded
number of repair attempts.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_json(text: str) -> Any:
    """Parse the first JSON value in ``text``, tolerating code fences and prose."""
    candidates: list[str] = [text.strip()]
    candidates.extend(m.group(1).strip() for m in _FENCE.finditer(text))
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = text.find(opener), text.rfind(closer)
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])
    last_error: Exception | None = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
    raise ValueError(f"no valid JSON found in model output ({last_error})")


def validate_against(schema: dict[str, Any] | type[BaseModel], value: Any) -> Any:
    """Validate ``value`` against a pydantic model or a JSON schema subset.

    Returns the validated value (a model instance for pydantic schemas). Raises
    ``ValueError`` with a readable message suitable for a repair prompt.
    """
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        try:
            return schema.model_validate(value)
        except ValidationError as exc:
            raise ValueError(_compact_errors(exc)) from exc
    problems = list(check_schema(schema, value, "$"))
    if problems:
        raise ValueError("; ".join(problems[:10]))
    return value


_TYPES: dict[str, tuple[type, ...]] = {
    "object": (dict,),
    "array": (list,),
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "null": (type(None),),
}


def check_schema(schema: dict[str, Any], value: Any, path: str) -> list[str]:
    """Validate the commonly used JSON-schema keywords without extra dependencies."""
    problems: list[str] = []
    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        ok = any(
            isinstance(value, _TYPES.get(t, (object,)))
            and not (t in ("integer", "number") and isinstance(value, bool))
            for t in types
        )
        if not ok:
            return [f"{path}: expected {expected}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        problems.append(f"{path}: {value!r} not in {schema['enum']}")
    if isinstance(value, dict):
        for key in schema.get("required", []):
            if key not in value:
                problems.append(f"{path}: missing required '{key}'")
        props = schema.get("properties", {})
        for key, sub in props.items():
            if key in value:
                problems.extend(check_schema(sub, value[key], f"{path}.{key}"))
        if schema.get("additionalProperties") is False:
            extra = set(value) - set(props)
            if extra:
                problems.append(f"{path}: unexpected keys {sorted(extra)}")
    if isinstance(value, list):
        if "items" in schema:
            for i, item in enumerate(value):
                problems.extend(check_schema(schema["items"], item, f"{path}[{i}]"))
        if "minItems" in schema and len(value) < schema["minItems"]:
            problems.append(f"{path}: fewer than {schema['minItems']} items")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            problems.append(f"{path}: more than {schema['maxItems']} items")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            problems.append(f"{path}: shorter than {schema['minLength']}")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            problems.append(f"{path}: longer than {schema['maxLength']}")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            problems.append(f"{path}: does not match /{schema['pattern']}/")
    if isinstance(value, int | float) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            problems.append(f"{path}: below minimum {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            problems.append(f"{path}: above maximum {schema['maximum']}")
    return problems


def schema_of(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()


def _compact_errors(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors()[:10]:
        loc = ".".join(str(p) for p in err["loc"]) or "$"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)
