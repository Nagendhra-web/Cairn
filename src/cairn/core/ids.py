"""Identifier generation and canonical hashing.

Canonical JSON is the foundation of replay: effect fingerprints, event hash
chains and approval bindings all hash the canonical form, so two semantically
identical requests always produce the same digest regardless of dict order.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from pydantic import BaseModel


def new_id(prefix: str) -> str:
    """Return a short, collision-resistant identifier such as ``run_3f9a...``."""
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


def to_jsonable(value: Any) -> Any:
    """Convert pydantic models, sets, tuples and bytes into JSON-compatible data."""
    if isinstance(value, BaseModel):
        return to_jsonable(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [to_jsonable(v) for v in value]
    if isinstance(value, set | frozenset):
        return sorted(to_jsonable(v) for v in value)
    if isinstance(value, bytes):
        return {"$bytes": value.hex()}
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    if hasattr(value, "to_dict"):
        return to_jsonable(value.to_dict())
    return repr(value)


def canonical_json(value: Any) -> str:
    """Serialize deterministically: sorted keys, no whitespace, UTF-8 preserved."""
    return json.dumps(to_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def stable_hash(value: Any, length: int = 64) -> str:
    """SHA-256 of the canonical JSON form, truncated to ``length`` hex chars."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()[:length]
