"""Serve a Cairn :class:`ToolRegistry` to MCP clients over stdio.

Exposing tools to an outside agent is an act of delegation, so the server
applies least privilege by default:

* only tools matching the ``expose`` globs are listed or callable, and a
  hidden tool is indistinguishable from a nonexistent one;
* tools with privileged effects (``write``, ``send``, ``execute``, ``delete``,
  ``payment``) or that require human approval are withheld unless
  ``allow_privileged=True``, because an MCP client has no channel for Cairn's
  approval flow and its own provenance tracking is unknown;
* each call receives a :class:`SecretScope` holding only the secrets the tool
  declared, and secret values are redacted from anything sent back.

Stdout carries protocol messages only. :meth:`MCPServer.serve_stdio` moves
the original stdout aside and points file descriptor 1 at stderr, so a stray
``print`` in a tool cannot corrupt the JSON-RPC stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import json
import logging
import os
import sys
import threading
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, BinaryIO

from cairn.core.errors import CairnError, NotFound, ToolArgumentError
from cairn.core.ids import new_id, to_jsonable
from cairn.mcp.client import PROTOCOL_VERSION, SUPPORTED_PROTOCOL_VERSIONS
from cairn.mcp.jsonrpc import (
    DEFAULT_MAX_MESSAGE_BYTES,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    ErrorObject,
    ErrorResponse,
    JSONRPCError,
    Notification,
    Request,
    RequestId,
    Response,
    encode_message,
    parse_message,
)
from cairn.provenance.policy import PRIVILEGED_EFFECTS
from cairn.security.secrets import SecretVault
from cairn.tools.registry import ToolContext, ToolRegistry
from cairn.tools.spec import ToolSpec

log = logging.getLogger("cairn.mcp.server")

Writer = Callable[[bytes], Awaitable[None]]
_DESTRUCTIVE = frozenset({"write", "delete", "payment", "execute"})
_OPEN_WORLD = frozenset({"network", "send"})


class MCPServer:
    """An MCP server backed by a :class:`ToolRegistry`."""

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        name: str = "cairn",
        version: str = "0.1.0",
        expose: Sequence[str] = ("*",),
        allow_privileged: bool = False,
        vault: SecretVault | None = None,
        instructions: str | None = None,
        page_size: int = 100,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
    ) -> None:
        if page_size < 1:
            raise ValueError("page_size must be positive")
        self.registry = registry
        self.name = name
        self.version = version
        self.expose = list(expose)
        self.allow_privileged = allow_privileged
        self.vault = vault or SecretVault()
        self.instructions = instructions
        self.page_size = page_size
        self.max_message_bytes = max_message_bytes
        self.session_id = new_id("mcp")
        self.initialized = False
        self.client_info: dict[str, Any] = {}
        self._inflight: dict[RequestId, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------ policy

    def is_exposed(self, spec: ToolSpec) -> bool:
        if not any(fnmatch.fnmatchcase(spec.name, p) for p in self.expose):
            return False
        if self.allow_privileged:
            return True
        return not (spec.effects & PRIVILEGED_EFFECTS) and not spec.requires_approval

    def exposed_tools(self) -> list[ToolSpec]:
        return [spec for spec in self.registry.list() if self.is_exposed(spec)]

    def _resolve(self, name: str) -> ToolSpec:
        try:
            spec = self.registry.get(name)
        except NotFound:
            spec = None
        if spec is None or not self.is_exposed(spec):
            raise JSONRPCError(INVALID_PARAMS, f"unknown tool: {name}")
        return spec

    @staticmethod
    def tool_definition(spec: ToolSpec) -> dict[str, Any]:
        """The MCP ``Tool`` object for a spec, with annotations derived from effects."""
        schema = dict(spec.input_schema) or {"type": "object", "properties": {}}
        schema.setdefault("type", "object")
        effects = spec.effects
        definition: dict[str, Any] = {
            "name": spec.name,
            "description": spec.description,
            "inputSchema": schema,
            "annotations": {
                "readOnlyHint": effects <= {"read"},
                "destructiveHint": bool(effects & _DESTRUCTIVE),
                "idempotentHint": spec.idempotent,
                "openWorldHint": bool(effects & _OPEN_WORLD),
            },
        }
        if spec.output_schema and spec.output_schema.get("type") == "object":
            definition["outputSchema"] = spec.output_schema
        return definition

    # ------------------------------------------------------------------ methods

    def _initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOL_VERSIONS else PROTOCOL_VERSION
        info = params.get("clientInfo")
        self.client_info = info if isinstance(info, dict) else {}
        result: dict[str, Any] = {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": self.name, "version": self.version},
        }
        if self.instructions:
            result["instructions"] = self.instructions
        return result

    def _list_tools(self, params: dict[str, Any]) -> dict[str, Any]:
        tools = self.exposed_tools()
        cursor = params.get("cursor")
        start = 0
        if cursor is not None:
            if not isinstance(cursor, str) or not cursor.isdigit():
                raise JSONRPCError(INVALID_PARAMS, "invalid cursor")
            start = int(cursor)
        page = tools[start : start + self.page_size]
        result: dict[str, Any] = {"tools": [self.tool_definition(s) for s in page]}
        if start + self.page_size < len(tools):
            result["nextCursor"] = str(start + self.page_size)
        return result

    async def _call_tool(self, request_id: RequestId, params: dict[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        if not isinstance(name, str):
            raise JSONRPCError(INVALID_PARAMS, "'name' must be a string")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise JSONRPCError(INVALID_PARAMS, "'arguments' must be an object")
        spec = self._resolve(name)
        ctx = ToolContext(
            run_id=self.session_id,
            node_id=f"call_{request_id}",
            secrets=self.vault.scope(spec.secrets),
        )
        redactor = self.vault.redactor()
        try:
            value = await self.registry.invoke(spec, arguments, ctx)
        except ToolArgumentError as exc:
            return _error_result(redactor.text(exc.message))
        except CairnError as exc:
            return _error_result(redactor.text(exc.message))
        except Exception as exc:  # registry.invoke wraps most failures; belt and braces
            log.exception("tool %s crashed", name)
            return _error_result(redactor.text(f"{type(exc).__name__}: {exc}"))
        return _success_result(redactor.deep(to_jsonable(value)))

    async def handle_request(self, request: Request) -> Any:
        """Dispatch one request and return its result (or raise JSONRPCError)."""
        params = request.params or {}
        method = request.method
        if method == "initialize":
            return self._initialize(params)
        if method == "ping":
            return {}
        if method == "tools/list":
            return self._list_tools(params)
        if method == "tools/call":
            return await self._call_tool(request.id, params)
        raise JSONRPCError(METHOD_NOT_FOUND, f"method not found: {method}")

    def handle_notification(self, note: Notification) -> None:
        if note.method == "notifications/initialized":
            self.initialized = True
        elif note.method == "notifications/cancelled":
            params = note.params or {}
            target = params.get("requestId")
            task = self._inflight.get(target) if isinstance(target, int | str) else None
            if task is not None:
                task.cancel()

    # ------------------------------------------------------------------ transport

    async def serve(self, reader: asyncio.StreamReader, write: Writer) -> None:
        """Process messages from ``reader`` until EOF, writing replies with ``write``."""

        async def respond(message: Response | ErrorResponse) -> None:
            try:
                await write(encode_message(message))
            except (ConnectionError, OSError) as exc:
                log.debug("could not write response: %s", exc)

        async def run(request: Request) -> None:
            try:
                result = await self.handle_request(request)
                reply: Response | ErrorResponse = Response(request.id, result)
            except asyncio.CancelledError:
                return  # cancelled requests get no response
            except JSONRPCError as exc:
                reply = ErrorResponse(request.id, exc.error)
            except Exception as exc:
                log.exception("internal error handling %s", request.method)
                reply = ErrorResponse(request.id, ErrorObject(INTERNAL_ERROR, str(exc)[:500]))
            finally:
                self._inflight.pop(request.id, None)
            await respond(reply)

        while True:
            try:
                line = await reader.readline()
            except ValueError:
                await respond(
                    ErrorResponse(None, ErrorObject(INVALID_REQUEST, "message too large"))
                )
                break
            if not line:
                break
            if not line.strip():
                continue
            try:
                message = parse_message(line, max_bytes=self.max_message_bytes)
            except JSONRPCError as exc:
                await respond(ErrorResponse(_salvage_id(line), exc.error))
                continue
            if isinstance(message, Request):
                if message.id in self._inflight:
                    await respond(
                        ErrorResponse(
                            message.id, ErrorObject(INVALID_REQUEST, "duplicate request id")
                        )
                    )
                    continue
                if message.method == "initialize":
                    # Inline, so the handshake completes before any later request runs.
                    await run(message)
                    continue
                self._inflight[message.id] = asyncio.create_task(run(message))
            elif isinstance(message, Notification):
                self.handle_notification(message)
            # Responses are ignored: this server never sends requests.
        pending = list(self._inflight.values())
        if pending:
            _done, still = await asyncio.wait(pending, timeout=5.0)
            for task in still:
                task.cancel()
            await asyncio.gather(*still, return_exceptions=True)

    async def serve_stdio(self, *, protect_stdout: bool = True) -> None:
        """Serve on this process's stdin/stdout until the client closes stdin."""
        out_fd = sys.stdout.fileno()
        if protect_stdout:
            sys.stdout.flush()
            out_fd = os.dup(out_fd)
            os.dup2(sys.stderr.fileno(), 1)
        out: BinaryIO = os.fdopen(out_fd, "wb", buffering=0)
        lock = asyncio.Lock()

        def write_sync(data: bytes) -> None:
            out.write(data)

        async def write(data: bytes) -> None:
            async with lock:
                await asyncio.to_thread(write_sync, data)

        reader = await _stdin_reader(self.max_message_bytes)
        try:
            await self.serve(reader, write)
        finally:
            with contextlib.suppress(OSError):
                out.close()


def _success_result(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return {"content": [{"type": "text", "text": value}], "isError": False}
    text = json.dumps(value, ensure_ascii=False, default=str)
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": False}
    if isinstance(value, dict):
        result["structuredContent"] = value
    elif isinstance(value, list):
        result["structuredContent"] = {"result": value}
    return result


def _error_result(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "isError": True}


def _salvage_id(line: bytes) -> RequestId | None:
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    if isinstance(obj, dict):
        value = obj.get("id")
        if isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool)):
            return value
    return None


async def _stdin_reader(limit: int) -> asyncio.StreamReader:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=limit)
    try:
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        return reader
    except (ValueError, OSError):
        pass  # stdin is a regular file or otherwise not pollable: read in a thread

    def pump() -> None:
        stream = sys.stdin.buffer
        while True:
            chunk = stream.readline()
            if not chunk:
                loop.call_soon_threadsafe(reader.feed_eof)
                return
            loop.call_soon_threadsafe(reader.feed_data, chunk)

    threading.Thread(target=pump, name="mcp-stdin", daemon=True).start()
    return reader


def run_stdio_server(registry: ToolRegistry, **options: Any) -> None:
    """Blocking convenience entry point: ``run_stdio_server(registry, expose=["fs.*"])``."""
    logging.basicConfig(
        level=os.environ.get("CAIRN_MCP_LOG_LEVEL", "WARNING"),
        stream=sys.stderr,
        format="%(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(MCPServer(registry, **options).serve_stdio())
