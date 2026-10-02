"""JSON-RPC 2.0 messages and newline-delimited framing for the MCP stdio transport.

MCP's stdio transport sends one JSON-RPC message per line, UTF-8 encoded, with
no embedded newlines. This module is deliberately small and dependency free:
it parses and validates individual messages, encodes them back to bytes, and
correlates request ids with the futures waiting on their responses.

Everything read from a peer is treated as hostile input. Parsing never trusts
the shape of a message, oversized lines are refused instead of buffered
without bound, and batches (removed from MCP in protocol 2025-06-18) are
rejected rather than half-supported.
"""

from __future__ import annotations

import asyncio
import itertools
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, TypeAlias

from cairn.core.errors import CairnError

JSONRPC_VERSION = "2.0"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
#: MCP convention for "request cancelled" style server-defined failures.
REQUEST_CANCELLED = -32800

#: Default ceiling for a single framed message (16 MiB).
DEFAULT_MAX_MESSAGE_BYTES = 16 * 1024 * 1024

RequestId: TypeAlias = int | str


@dataclass(frozen=True)
class ErrorObject:
    """The ``error`` member of a JSON-RPC error response."""

    code: int
    message: str
    data: Any = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            out["data"] = self.data
        return out

    @classmethod
    def from_dict(cls, raw: Any) -> ErrorObject:
        if not isinstance(raw, Mapping):
            return cls(INTERNAL_ERROR, f"malformed error object: {raw!r}"[:200])
        code = raw.get("code")
        message = raw.get("message")
        return cls(
            code if isinstance(code, int) and not isinstance(code, bool) else INTERNAL_ERROR,
            message if isinstance(message, str) else "unknown error",
            raw.get("data"),
        )


class JSONRPCError(CairnError):
    """A JSON-RPC level failure, raised locally or received from the peer."""

    code = "jsonrpc_error"

    def __init__(self, rpc_code: int, message: str, data: Any = None) -> None:
        super().__init__(message, rpc_code=rpc_code, data=data)
        self.rpc_code = rpc_code
        self.data = data

    @property
    def error(self) -> ErrorObject:
        return ErrorObject(self.rpc_code, self.message, self.data)


@dataclass(frozen=True)
class Request:
    id: RequestId
    method: str
    params: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "id": self.id, "method": self.method}
        if self.params is not None:
            out["params"] = self.params
        return out


@dataclass(frozen=True)
class Notification:
    method: str
    params: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"jsonrpc": JSONRPC_VERSION, "method": self.method}
        if self.params is not None:
            out["params"] = self.params
        return out


@dataclass(frozen=True)
class Response:
    id: RequestId
    result: Any

    def to_dict(self) -> dict[str, Any]:
        return {"jsonrpc": JSONRPC_VERSION, "id": self.id, "result": self.result}


@dataclass(frozen=True)
class ErrorResponse:
    #: ``None`` when the request id could not be determined (parse errors).
    id: RequestId | None
    error: ErrorObject

    def to_dict(self) -> dict[str, Any]:
        return {"jsonrpc": JSONRPC_VERSION, "id": self.id, "error": self.error.to_dict()}


Message: TypeAlias = Request | Notification | Response | ErrorResponse


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def message_from_obj(obj: Any) -> Message:
    """Validate a decoded JSON value and turn it into a typed message."""
    if isinstance(obj, list):
        raise JSONRPCError(INVALID_REQUEST, "JSON-RPC batches are not supported")
    if not isinstance(obj, dict):
        raise JSONRPCError(INVALID_REQUEST, "message must be a JSON object")
    if obj.get("jsonrpc") != JSONRPC_VERSION:
        raise JSONRPCError(INVALID_REQUEST, "missing or wrong 'jsonrpc' version")
    params = obj.get("params")
    if "method" in obj:
        method = obj["method"]
        if not isinstance(method, str) or not method:
            raise JSONRPCError(INVALID_REQUEST, "'method' must be a non-empty string")
        if params is not None and not isinstance(params, dict):
            raise JSONRPCError(INVALID_PARAMS, "'params' must be an object")
        if "id" in obj:
            if not _valid_id(obj["id"]):
                raise JSONRPCError(INVALID_REQUEST, "'id' must be a string or integer")
            return Request(obj["id"], method, params)
        return Notification(method, params)
    if "id" not in obj:
        raise JSONRPCError(INVALID_REQUEST, "message is neither request, notification nor response")
    msg_id = obj["id"]
    if msg_id is not None and not _valid_id(msg_id):
        raise JSONRPCError(INVALID_REQUEST, "'id' must be a string or integer")
    if "error" in obj:
        return ErrorResponse(msg_id, ErrorObject.from_dict(obj["error"]))
    if "result" in obj and msg_id is not None:
        return Response(msg_id, obj["result"])
    raise JSONRPCError(INVALID_REQUEST, "response needs 'result' or 'error'")


def parse_message(line: bytes | str, *, max_bytes: int = DEFAULT_MAX_MESSAGE_BYTES) -> Message:
    """Decode one framed line into a message, raising :class:`JSONRPCError` on bad input."""
    if len(line) > max_bytes:
        raise JSONRPCError(INVALID_REQUEST, f"message exceeds {max_bytes} bytes")
    try:
        obj = json.loads(line)
    except (ValueError, UnicodeDecodeError) as exc:
        raise JSONRPCError(PARSE_ERROR, f"invalid JSON: {exc}") from None
    return message_from_obj(obj)


def encode_message(message: Message | Mapping[str, Any]) -> bytes:
    """Serialize a message as one line terminated by ``\\n``.

    ``json.dumps`` escapes newlines inside strings, so the output never
    contains a raw newline other than the terminator.
    """
    obj = message if isinstance(message, Mapping) else message.to_dict()
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8") + b"\n"


@dataclass
class PendingRequests:
    """Allocates request ids and correlates responses with waiting futures."""

    _counter: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    _waiting: dict[RequestId, asyncio.Future[Any]] = field(default_factory=dict)

    def new(self) -> tuple[int, asyncio.Future[Any]]:
        request_id = next(self._counter)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._waiting[request_id] = future
        return request_id, future

    def discard(self, request_id: RequestId) -> None:
        self._waiting.pop(request_id, None)

    def resolve(self, message: Response | ErrorResponse) -> bool:
        """Complete the matching future. Returns ``False`` for unknown ids."""
        if message.id is None:
            return False
        future = self._waiting.pop(message.id, None)
        if future is None or future.done():
            return False
        if isinstance(message, Response):
            future.set_result(message.result)
        else:
            err = message.error
            future.set_exception(JSONRPCError(err.code, err.message, err.data))
        return True

    def fail_all(self, exc: BaseException) -> None:
        waiting, self._waiting = self._waiting, {}
        for future in waiting.values():
            if not future.done():
                future.set_exception(exc)

    def __len__(self) -> int:
        return len(self._waiting)
