"""MCP client over the stdio transport.

An MCP server is third-party code running as a child process. The client is
built around that threat model:

* The child gets a minimal environment (:func:`cairn.security.sandbox.safe_env`)
  plus only the variables the operator passed explicitly, so credentials in
  the parent's environment do not leak into a server that never needed them.
* Every request has a timeout, and a timed out or cancelled request is
  announced to the server with ``notifications/cancelled``.
* Lines from the server are size limited and schema checked; a misbehaving
  server can fail its own requests but cannot wedge the runtime.
* Shutdown follows the MCP stdio guidance: close stdin, wait, ``SIGTERM``,
  wait, ``SIGKILL``. Stderr is drained continuously (a full stderr pipe would
  otherwise block the server) and forwarded to logging, never to a model.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Any

from cairn.core.errors import ToolError, ToolTimeout
from cairn.mcp.jsonrpc import (
    DEFAULT_MAX_MESSAGE_BYTES,
    METHOD_NOT_FOUND,
    ErrorObject,
    ErrorResponse,
    JSONRPCError,
    Notification,
    PendingRequests,
    Request,
    Response,
    encode_message,
    parse_message,
)
from cairn.security.sandbox import safe_env

log = logging.getLogger("cairn.mcp.client")

PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS = frozenset({"2025-06-18", "2025-03-26", "2024-11-05"})
CLIENT_INFO: dict[str, str] = {"name": "cairn", "version": "0.1.0"}
MAX_LIST_PAGES = 100
_STDERR_LINE_CAP = 2000

NotificationHandler = Callable[[str, dict[str, Any] | None], Awaitable[None] | None]


class MCPConnectionError(ToolError):
    """The server process is not running or the transport broke."""

    code = "mcp_connection_error"


class MCPProtocolError(ToolError):
    """The server violated the protocol (bad handshake, malformed result)."""

    code = "mcp_protocol_error"
    retryable = False


@dataclass(frozen=True)
class MCPToolInfo:
    """A tool definition as advertised by a server (untrusted until vetted)."""

    name: str
    description: str
    input_schema: dict[str, Any]
    title: str | None = None
    annotations: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: Any) -> MCPToolInfo:
        if not isinstance(raw, dict) or not isinstance(raw.get("name"), str):
            raise MCPProtocolError(f"malformed tool definition: {str(raw)[:200]}")
        schema = raw.get("inputSchema")
        if not isinstance(schema, dict):
            schema = {"type": "object", "properties": {}}
        annotations = raw.get("annotations")
        output_schema = raw.get("outputSchema")
        title = raw.get("title")
        description = raw.get("description")
        return cls(
            name=raw["name"],
            description=description if isinstance(description, str) else "",
            input_schema=schema,
            title=title if isinstance(title, str) else None,
            annotations=annotations if isinstance(annotations, dict) else {},
            output_schema=output_schema if isinstance(output_schema, dict) else None,
            raw=raw,
        )


@dataclass(frozen=True)
class MCPToolResult:
    """Outcome of ``tools/call``: concatenated text plus optional structured data."""

    text: str
    structured: Any = None
    content: list[dict[str, Any]] = field(default_factory=list)
    is_error: bool = False

    @property
    def value(self) -> Any:
        """Structured content when the server provided it, otherwise the text."""
        return self.structured if self.structured is not None else self.text

    @classmethod
    def from_dict(cls, raw: Any) -> MCPToolResult:
        if not isinstance(raw, dict):
            raise MCPProtocolError("tools/call result must be an object")
        content = raw.get("content")
        parts = [p for p in content if isinstance(p, dict)] if isinstance(content, list) else []
        texts: list[str] = []
        for part in parts:
            kind = part.get("type")
            if kind == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif kind == "resource" and isinstance(part.get("resource"), dict):
                res_text = part["resource"].get("text")
                if isinstance(res_text, str):
                    texts.append(res_text)
            elif kind in ("image", "audio"):
                texts.append(f"[{kind} content: {part.get('mimeType', 'unknown type')}]")
            elif kind == "resource_link":
                texts.append(f"[resource: {part.get('uri', '')}]")
        return cls(
            text="\n".join(texts),
            structured=raw.get("structuredContent"),
            content=parts,
            is_error=raw.get("isError") is True,
        )


class MCPClient:
    """Speaks MCP to one server subprocess over stdin/stdout.

    Use as an async context manager::

        async with MCPClient(sys.executable, ["server.py"]) as client:
            tools = await client.list_tools()
            result = await client.call_tool("echo", {"text": "hi"})
    """

    def __init__(
        self,
        command: str,
        args: Sequence[str] = (),
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | Path | None = None,
        name: str | None = None,
        request_timeout: float = 30.0,
        startup_timeout: float = 10.0,
        shutdown_timeout: float = 2.0,
        client_info: Mapping[str, str] | None = None,
        max_message_bytes: int = DEFAULT_MAX_MESSAGE_BYTES,
        on_notification: NotificationHandler | None = None,
    ) -> None:
        self.command = command
        self.args = list(args)
        self.env = dict(env or {})
        self.cwd = str(cwd) if cwd is not None else None
        self.name = name or Path(command).name
        self.request_timeout = request_timeout
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        self.client_info = dict(client_info or CLIENT_INFO)
        self.max_message_bytes = max_message_bytes
        self.on_notification = on_notification
        self.server_info: dict[str, Any] = {}
        self.server_capabilities: dict[str, Any] = {}
        self.protocol_version: str | None = None
        self.instructions: str | None = None
        self.tools_changed = asyncio.Event()
        self._proc: asyncio.subprocess.Process | None = None
        self._pending = PendingRequests()
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._closed = False
        self._broken: BaseException | None = None
        self._initialized = False
        self._stderr_log = logging.getLogger(f"cairn.mcp.server.{self.name}")

    # ------------------------------------------------------------------ lifecycle

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and not self._closed

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc else None

    @property
    def returncode(self) -> int | None:
        return self._proc.returncode if self._proc else None

    async def start(self) -> dict[str, Any]:
        """Spawn the server and perform the ``initialize`` handshake."""
        if self._proc is not None:
            raise MCPConnectionError(f"MCP client '{self.name}' already started")
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.command,
                *self.args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=safe_env(self.env),
                cwd=self.cwd,
                limit=self.max_message_bytes,
            )
        except OSError as exc:
            raise MCPConnectionError(
                f"cannot start MCP server '{self.name}': {exc}", server=self.name
            ) from exc
        self._reader_task = asyncio.create_task(self._read_loop(), name=f"mcp-read-{self.name}")
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name=f"mcp-stderr-{self.name}"
        )
        try:
            result = await self.request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": self.client_info,
                },
                timeout_s=self.startup_timeout,
            )
            if not isinstance(result, dict):
                raise MCPProtocolError("initialize result must be an object", server=self.name)
            version = result.get("protocolVersion")
            if version not in SUPPORTED_PROTOCOL_VERSIONS:
                raise MCPProtocolError(
                    f"server '{self.name}' speaks unsupported protocol version {version!r}",
                    server=self.name,
                    supported=sorted(SUPPORTED_PROTOCOL_VERSIONS),
                )
            self.protocol_version = str(version)
            caps = result.get("capabilities")
            info = result.get("serverInfo")
            instructions = result.get("instructions")
            self.server_capabilities = caps if isinstance(caps, dict) else {}
            self.server_info = info if isinstance(info, dict) else {}
            self.instructions = instructions if isinstance(instructions, str) else None
            await self.notify("notifications/initialized")
        except BaseException:
            await self.close()
            raise
        self._initialized = True
        log.info("connected to MCP server %s (%s)", self.name, self.server_info.get("name"))
        return result

    async def close(self) -> None:
        """Shut the server down: close stdin, wait, terminate, then kill."""
        if self._closed:
            return
        self._closed = True
        proc = self._proc
        if proc is not None:
            if proc.stdin is not None and not proc.stdin.is_closing():
                with contextlib.suppress(Exception):
                    proc.stdin.close()
            for step in ("wait", "terminate", "kill"):
                if proc.returncode is not None:
                    break
                if step == "terminate":
                    with contextlib.suppress(ProcessLookupError):
                        proc.terminate()
                elif step == "kill":
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=self.shutdown_timeout)
                except TimeoutError:
                    log.warning("MCP server %s did not exit after %s", self.name, step)
            if proc.returncode is None:  # pragma: no cover - kill should always win
                await proc.wait()
        self._pending.fail_all(MCPConnectionError(f"MCP client '{self.name}' closed"))
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def __aenter__(self) -> MCPClient:
        if self._proc is None:
            await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.close()

    # ------------------------------------------------------------------ messaging

    async def _send(self, payload: bytes) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or self._closed or self._broken is not None:
            raise MCPConnectionError(
                f"MCP server '{self.name}' is not connected", server=self.name
            ) from self._broken
        async with self._write_lock:
            try:
                proc.stdin.write(payload)
                await proc.stdin.drain()
            except (ConnectionError, RuntimeError) as exc:
                raise MCPConnectionError(
                    f"lost connection to MCP server '{self.name}': {exc}", server=self.name
                ) from exc

    async def request(
        self, method: str, params: dict[str, Any] | None = None, *, timeout_s: float | None = None
    ) -> Any:
        """Send a request and wait for its result.

        Raises :class:`ToolTimeout` after ``timeout_s`` (default
        ``request_timeout``) and :class:`JSONRPCError` for error responses.
        A timed out or cancelled request is cancelled on the server too.
        """
        request_id, future = self._pending.new()
        limit = self.request_timeout if timeout_s is None else timeout_s
        try:
            await self._send(encode_message(Request(request_id, method, params)))
            return await asyncio.wait_for(asyncio.shield(future), timeout=limit)
        except TimeoutError:
            self._cancel_remote(request_id, "timeout")
            raise ToolTimeout(
                f"MCP request '{method}' to '{self.name}' timed out after {limit}s",
                server=self.name,
                method=method,
            ) from None
        except asyncio.CancelledError:
            self._cancel_remote(request_id, "cancelled by client")
            raise
        finally:
            self._pending.discard(request_id)
            if not future.done():
                future.cancel()
            elif not future.cancelled():
                future.exception()  # mark retrieved so asyncio does not warn

    def _cancel_remote(self, request_id: int, reason: str) -> None:
        if not self.running or self._broken is not None:
            return
        note = Notification(
            "notifications/cancelled", {"requestId": request_id, "reason": reason}
        )
        task = asyncio.get_running_loop().create_task(self._send_quietly(encode_message(note)))
        task.add_done_callback(lambda t: t.cancelled() or t.exception())

    async def _send_quietly(self, payload: bytes) -> None:
        with contextlib.suppress(MCPConnectionError):
            await self._send(payload)

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._send(encode_message(Notification(method, params)))

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        stdout = self._proc.stdout
        reason: BaseException = MCPConnectionError(
            f"MCP server '{self.name}' closed its output", server=self.name
        )
        try:
            while True:
                try:
                    line = await stdout.readline()
                except ValueError:
                    reason = MCPProtocolError(
                        f"MCP server '{self.name}' sent a message over "
                        f"{self.max_message_bytes} bytes",
                        server=self.name,
                    )
                    break
                if not line:
                    break
                if not line.strip():
                    continue
                try:
                    message = parse_message(line, max_bytes=self.max_message_bytes)
                except JSONRPCError as exc:
                    log.warning("ignoring invalid message from %s: %s", self.name, exc)
                    continue
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive
            reason = MCPConnectionError(f"reader for '{self.name}' failed: {exc}")
        self._broken = reason
        self._pending.fail_all(reason)

    async def _dispatch(self, message: Request | Notification | Response | ErrorResponse) -> None:
        if isinstance(message, Response | ErrorResponse):
            if not self._pending.resolve(message):
                log.debug("response for unknown request id %r from %s", message.id, self.name)
            return
        if isinstance(message, Request):
            # Servers may ping; everything else (sampling, roots, elicitation)
            # is a capability this client never advertised, so refuse it.
            if message.method == "ping":
                reply: Response | ErrorResponse = Response(message.id, {})
            else:
                reply = ErrorResponse(
                    message.id, ErrorObject(METHOD_NOT_FOUND, f"unsupported: {message.method}")
                )
            await self._send_quietly(encode_message(reply))
            return
        if message.method == "notifications/tools/list_changed":
            self.tools_changed.set()
        if self.on_notification is not None:
            try:
                outcome = self.on_notification(message.method, message.params)
                if outcome is not None:
                    await outcome
            except Exception:
                log.exception("notification handler for %s failed", self.name)

    async def _drain_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        stderr = self._proc.stderr
        while True:
            try:
                line = await stderr.readline()
            except ValueError:
                continue  # overlong line: the reader already discarded it
            if not line:
                return
            text = line.decode("utf-8", "replace").rstrip()
            if text:
                self._stderr_log.info("%s", text[:_STDERR_LINE_CAP])

    # ------------------------------------------------------------------ MCP API

    def _require_ready(self) -> None:
        if not self._initialized or not self.running:
            raise MCPConnectionError(f"MCP client '{self.name}' is not connected")

    async def ping(self, *, timeout_s: float | None = None) -> None:
        await self.request("ping", timeout_s=timeout_s)

    async def list_tools(self, *, timeout_s: float | None = None) -> list[MCPToolInfo]:
        """Fetch every tool, following ``nextCursor`` pagination."""
        self._require_ready()
        tools: list[MCPToolInfo] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(MAX_LIST_PAGES):
            params = {"cursor": cursor} if cursor is not None else None
            result = await self.request("tools/list", params, timeout_s=timeout_s)
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                raise MCPProtocolError(f"malformed tools/list result from '{self.name}'")
            tools.extend(MCPToolInfo.from_dict(t) for t in result["tools"])
            next_cursor = result.get("nextCursor")
            if not isinstance(next_cursor, str) or not next_cursor:
                self.tools_changed.clear()
                return tools
            if next_cursor in seen:
                raise MCPProtocolError(f"server '{self.name}' repeated pagination cursor")
            seen.add(next_cursor)
            cursor = next_cursor
        raise MCPProtocolError(f"server '{self.name}' returned more than {MAX_LIST_PAGES} pages")

    async def call_tool(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> MCPToolResult:
        """Call a tool. A result flagged ``isError`` raises :class:`ToolError`."""
        self._require_ready()
        try:
            params = {"name": name, "arguments": dict(arguments or {})}
            raw = await self.request("tools/call", params, timeout_s=timeout_s)
        except JSONRPCError as exc:
            raise ToolError(
                f"MCP tool '{self.name}.{name}' failed: {exc.message}",
                tool=f"{self.name}.{name}",
                rpc_code=exc.rpc_code,
            ) from exc
        result = MCPToolResult.from_dict(raw)
        if result.is_error:
            raise ToolError(
                f"MCP tool '{self.name}.{name}' reported an error: {result.text[:2000]}",
                tool=f"{self.name}.{name}",
                content=result.text[:2000],
            )
        return result
