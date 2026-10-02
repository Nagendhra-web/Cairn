"""Integration tests for MCP: a real subprocess server, client, bridge and pins."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from cairn.core.errors import NotFound, ToolError, ToolTimeout
from cairn.mcp import (
    PROTOCOL_VERSION,
    JSONRPCError,
    MCPClient,
    MCPConnectionError,
    MCPServer,
    MCPServerConfig,
    approve,
    infer_effects,
    load_pins,
    mount_mcp_server,
    pin_report,
    sanitize_text,
    save_pins,
    unmount_mcp_server,
)
from cairn.mcp.jsonrpc import (
    INVALID_REQUEST,
    PARSE_ERROR,
    ErrorResponse,
    Notification,
    PendingRequests,
    Request,
    Response,
    encode_message,
    parse_message,
)
from cairn.security.secrets import SecretVault
from cairn.tools.decorator import tool
from cairn.tools.registry import ToolContext, ToolRegistry
from cairn.tools.spec import OutputTrust

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "tests" / "fixtures" / "mcp_echo_server.py"
SRC = str(ROOT / "src")


def server_env(**extra: str) -> dict[str, str]:
    return {"PYTHONPATH": SRC, **extra}


def make_client(**env: str) -> MCPClient:
    return MCPClient(
        sys.executable,
        [str(SERVER)],
        env=server_env(**env),
        name="fixture",
        request_timeout=10.0,
        startup_timeout=15.0,
    )


def make_config(**overrides: Any) -> MCPServerConfig:
    env = server_env(**overrides.pop("env", {}))
    return MCPServerConfig(
        name="echo", command=sys.executable, args=[str(SERVER)], env=env, **overrides
    )


def ctx() -> ToolContext:
    return ToolContext(run_id="run_test", node_id="n1", secrets=SecretVault().scope(()))


# ---------------------------------------------------------------- JSON-RPC unit


def test_jsonrpc_framing_roundtrip() -> None:
    req = Request(7, "tools/call", {"name": "x\ny"})
    line = encode_message(req)
    assert line.endswith(b"\n") and line.count(b"\n") == 1
    assert parse_message(line) == req
    assert parse_message(encode_message(Notification("ping"))) == Notification("ping")
    assert parse_message(encode_message(Response("a", {"ok": 1}))) == Response("a", {"ok": 1})
    err = parse_message(b'{"jsonrpc":"2.0","id":3,"error":{"code":-1,"message":"no"}}')
    assert isinstance(err, ErrorResponse) and err.error.code == -1


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"not json", PARSE_ERROR),
        (b"[]", INVALID_REQUEST),
        (b'{"jsonrpc":"1.0","id":1,"method":"x"}', INVALID_REQUEST),
        (b'{"jsonrpc":"2.0","id":true,"method":"x"}', INVALID_REQUEST),
        (b'{"jsonrpc":"2.0","id":1}', INVALID_REQUEST),
    ],
)
def test_jsonrpc_rejects_bad_messages(raw: bytes, code: int) -> None:
    with pytest.raises(JSONRPCError) as info:
        parse_message(raw)
    assert info.value.rpc_code == code


async def test_pending_requests_correlate_by_id() -> None:
    pending = PendingRequests()
    first, fut1 = pending.new()
    second, fut2 = pending.new()
    assert first != second
    assert pending.resolve(Response(second, "b"))
    assert not pending.resolve(Response(99, "nobody"))
    pending.fail_all(RuntimeError("gone"))
    assert await fut2 == "b"
    with pytest.raises(RuntimeError):
        await fut1


# ---------------------------------------------------------------- client


async def test_handshake_list_and_call() -> None:
    async with make_client() as client:
        assert client.protocol_version == PROTOCOL_VERSION
        assert client.server_info["name"] == "echo-fixture"
        assert client.server_capabilities.get("tools") is not None
        assert client.instructions == "Test fixture server."
        await client.ping()

        tools = {t.name: t for t in await client.list_tools()}
        assert {"echo", "add", "note", "lookup", "fail", "slow"} <= set(tools)
        assert tools["echo"].input_schema["required"] == ["text"]
        assert tools["echo"].annotations["readOnlyHint"] is True

        # Stray print() in the tool goes to stderr, not into the protocol stream.
        echoed = await client.call_tool("echo", {"text": "hello"})
        assert echoed.text == "hello" and echoed.structured is None

        added = await client.call_tool("add", {"a": 2, "b": 3.5})
        assert added.structured == {"sum": 5.5}
        assert json.loads(added.text) == {"sum": 5.5}

        listed = await client.call_tool("lookup", {"word": "hi"})
        assert listed.structured == {"result": ["hi", "HI"]}


async def test_pagination_follows_cursor() -> None:
    async with make_client(CAIRN_TEST_PAGE_SIZE="2") as paged, make_client() as whole:
        assert [t.name for t in await paged.list_tools()] == [
            t.name for t in await whole.list_tools()
        ]


async def test_error_results_raise_tool_error() -> None:
    async with make_client() as client:
        with pytest.raises(ToolError, match="boom"):
            await client.call_tool("fail", {"message": "boom"})
        with pytest.raises(ToolError, match="invalid arguments"):
            await client.call_tool("add", {"a": "x", "b": 1})
        with pytest.raises(ToolError, match="unknown tool"):
            await client.call_tool("does_not_exist", {})
        # The connection survives errors.
        assert (await client.call_tool("echo", {"text": "still here"})).text == "still here"


async def test_privileged_tools_hidden_by_default() -> None:
    async with make_client() as client:
        names = {t.name for t in await client.list_tools()}
        assert "wipe" not in names
        with pytest.raises(ToolError, match="unknown tool"):
            await client.call_tool("wipe", {"path": "/"})
    async with make_client(CAIRN_TEST_ALLOW_PRIVILEGED="1") as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert tools["wipe"].annotations["destructiveHint"] is True
        assert (await client.call_tool("wipe", {"path": "/tmp/x"})).text == "wiped /tmp/x"


async def test_env_is_minimal_and_secrets_are_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CAIRN_TEST_PARENT_SECRET", "should-not-leak")
    async with make_client(CAIRN_SECRET_API_TOKEN="tok-123456") as client:
        keys = (await client.call_tool("env_keys", {})).structured["result"]
        assert "CAIRN_TEST_PARENT_SECRET" not in keys
        assert "CAIRN_SECRET_API_TOKEN" in keys and "PYTHONPATH" in keys
        assert (await client.call_tool("token_length", {})).text == "10"
        leaked = await client.call_tool("leak_token", {})
        assert "tok-123456" not in leaked.text
        assert "[REDACTED:API_TOKEN]" in leaked.text


async def test_request_timeout_cancels_and_connection_survives() -> None:
    async with make_client() as client:
        with pytest.raises(ToolTimeout):
            await client.call_tool("slow", {"seconds": 5}, timeout_s=0.3)
        assert (await client.call_tool("slow", {"seconds": 0}, timeout_s=5)).text == "done"


async def test_graceful_shutdown_and_use_after_close() -> None:
    client = make_client()
    await client.start()
    assert client.running and client.pid
    await client.close()
    assert client.returncode is not None and not client.running
    with pytest.raises(MCPConnectionError):
        await client.call_tool("echo", {"text": "x"})
    await client.close()  # idempotent


async def test_unresponsive_server_times_out_and_is_killed() -> None:
    client = MCPClient(
        sys.executable,
        [
            "-c",
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)",
        ],
        name="mute",
        startup_timeout=0.5,
        shutdown_timeout=0.3,
    )
    with pytest.raises(ToolTimeout):
        await client.start()
    assert client.returncode is not None  # stdin close and SIGTERM ignored, SIGKILL won


async def test_server_exit_fails_pending_requests() -> None:
    client = MCPClient(
        sys.executable,
        ["-c", "import sys; sys.stdin.readline()"],
        name="quitter",
        startup_timeout=5,
    )
    with pytest.raises(MCPConnectionError):
        await client.start()


async def test_missing_command_raises_connection_error() -> None:
    with pytest.raises(MCPConnectionError):
        await MCPClient("/nonexistent/mcp-server-binary").start()


# ---------------------------------------------------------------- bridge


def test_infer_effects_is_conservative_for_untrusted() -> None:
    assert infer_effects({}, trusted=False) == {"network"}
    assert infer_effects({"readOnlyHint": True}, trusted=False) == {"network", "read"}
    assert infer_effects({"destructiveHint": True}, trusted=False) == {"network", "write"}
    assert infer_effects({"readOnlyHint": True}, trusted=True) == {"read"}
    assert infer_effects({"readOnlyHint": True, "openWorldHint": True}, trusted=True) == {
        "read",
        "network",
    }


def test_sanitize_text_strips_invisible_and_caps() -> None:
    assert sanitize_text("a\x07b​c‮ d") == "abc d"
    capped = sanitize_text("x" * 5000, 1000)
    assert len(capped) == 1000 and capped.endswith("...")


async def test_mount_registers_namespaced_untrusted_tools() -> None:
    registry = ToolRegistry()
    config = make_config(
        allowed_tools=["echo", "add", "lookup", "note", "fail"],
        effects_override={"add": ["read"]},
        sensitive_params={"echo": ["text"]},
    )
    report = await mount_mcp_server(registry, config)
    try:
        assert sorted(report.added) == ["echo.add", "echo.echo", "echo.fail", "echo.lookup",
                                        "echo.note"]
        assert {s.name for s in report.skipped} >= {"echo.slow", "echo.env_keys"}
        assert all(s.reason == "not in allowed_tools" for s in report.skipped)
        assert not report.quarantined and sorted(report.unpinned) == sorted(report.added)

        spec = registry.get("echo.echo")
        assert spec.source == "mcp:echo"
        assert spec.output_trust is OutputTrust.UNTRUSTED
        assert spec.effects == {"network", "read"}  # hint adds, never removes network
        assert spec.sensitive_params == {"text"}
        assert "mcp" in spec.tags
        assert registry.get("echo.add").effects == {"read"}  # operator override wins
        assert registry.get("echo.fail").effects == {"network", "read"}

        # Poisoned description: sanitized, and the injection is surfaced.
        lookup_spec = registry.get("echo.lookup")
        assert "\x07" not in lookup_spec.description
        assert "​" not in lookup_spec.description
        assert {s.kind for s in report.signals["echo.lookup"]} >= {"tool-invocation"}

        assert await registry.invoke(spec, {"text": "via registry"}, ctx()) == "via registry"
        assert await registry.invoke(registry.get("echo.add"), {"a": 1, "b": 2}, ctx()) == {
            "sum": 3
        }
        with pytest.raises(ToolError, match="kaput"):
            await registry.invoke(registry.get("echo.fail"), {"message": "kaput"}, ctx())

        found = await registry.discover("echo text back", k=3)
        assert "echo.echo" in [m.spec.name for m in found]
    finally:
        await unmount_mcp_server(registry, report)
    assert "echo.echo" not in registry
    assert not report.client.running


async def test_trusted_server_keeps_trusted_labels() -> None:
    registry = ToolRegistry()
    report = await mount_mcp_server(registry, make_config(trust="trusted", allowed_tools=["e*"]))
    try:
        spec = registry.get("echo.echo")
        assert spec.output_trust is OutputTrust.TRUSTED
        assert spec.effects == {"read"}
        assert report.signals == {}
    finally:
        await unmount_mcp_server(registry, report)


async def test_rug_pull_quarantines_changed_tool(tmp_path: Path) -> None:
    pin_path = tmp_path / "pins" / "echo.json"
    registry = ToolRegistry()
    first = await mount_mcp_server(registry, make_config(allowed_tools=["note", "echo"]))
    await unmount_mcp_server(registry, first)
    pins = pin_report(pin_path, first)
    assert load_pins(pin_path) == pins == first.fingerprints

    # Same definitions: nothing quarantined.
    registry = ToolRegistry()
    same = await mount_mcp_server(
        registry, make_config(allowed_tools=["note", "echo"], pinned=load_pins(pin_path))
    )
    assert not same.quarantined and not same.unpinned
    await unmount_mcp_server(registry, same)

    # The server silently rewrites the description of `note`.
    registry = ToolRegistry()
    pulled = await mount_mcp_server(
        registry,
        make_config(
            allowed_tools=["note", "echo"],
            pinned=load_pins(pin_path),
            env={"CAIRN_TEST_RUGPULL": "1"},
        ),
    )
    try:
        assert [q.name for q in pulled.quarantined] == ["echo.note"]
        held = pulled.quarantined[0]
        assert held.pinned == pins["echo.note"] and held.current != held.pinned
        assert "echo.note" not in registry
        assert "echo.note" not in {s.name for s in registry.list()}
        with pytest.raises(NotFound, match="quarantined"):
            registry.get("echo.note")
        assert "echo.echo" in registry  # unchanged tools stay usable
        assert pulled.usable == ["echo.echo"]
        assert {s.kind for s in pulled.signals["echo.note"]} >= {"override", "exfiltration"}

        # Pinning without explicit approval keeps the old pin.
        assert pin_report(pin_path, pulled)["echo.note"] == pins["echo.note"]

        # Operator reviews and re-approves.
        new_pin = approve(registry, pulled, "echo.note")
        assert "echo.note" in registry
        save_pins(pin_path, {**load_pins(pin_path), "echo.note": new_pin})
        assert load_pins(pin_path)["echo.note"] == held.current
    finally:
        await unmount_mcp_server(registry, pulled)


async def test_quarantine_unpinned_tools_when_requested() -> None:
    registry = ToolRegistry()
    first = await mount_mcp_server(registry, make_config(allowed_tools=["echo"]))
    await unmount_mcp_server(registry, first)
    registry = ToolRegistry()
    report = await mount_mcp_server(
        registry,
        make_config(
            allowed_tools=["echo", "add"], pinned=first.fingerprints, quarantine_unpinned=True
        ),
    )
    try:
        assert [q.name for q in report.quarantined] == ["echo.add"]
        assert "echo.echo" in registry and "echo.add" not in registry
    finally:
        await unmount_mcp_server(registry, report)


async def test_mount_skips_name_collisions() -> None:
    registry = ToolRegistry()

    @tool(name="echo.echo")
    def local_echo(text: str) -> str:
        """Local tool that owns the name first."""
        return text

    registry.register(local_echo)
    report = await mount_mcp_server(registry, make_config(allowed_tools=["echo"]))
    try:
        assert report.added == []
        skipped = {s.name: s.reason for s in report.skipped}
        assert skipped["echo.echo"].startswith("name collides")
        assert registry.get("echo.echo").source == "local"
    finally:
        await report.client.close()


def test_config_rejects_unknown_effects_and_bad_names() -> None:
    with pytest.raises(ValueError):
        MCPServerConfig(name="x", command="y", effects_override={"t": ["teleport"]})
    with pytest.raises(ValueError):
        MCPServerConfig(name="has.dot", command="y")
    assert MCPServerConfig(name="x", command="y").trust == "untrusted"


def test_pin_file_missing_and_malformed(tmp_path: Path) -> None:
    assert load_pins(tmp_path / "absent.json") == {}
    bad = tmp_path / "bad.json"
    bad.write_text('{"tools": [1, 2]}')
    with pytest.raises(ValueError):
        load_pins(bad)


# ---------------------------------------------------------------- server in-process


async def test_server_in_process_least_privilege() -> None:
    @tool(effects={"read"})
    def read_it(x: int) -> int:
        """Read something."""
        return x

    @tool(effects={"send"})
    def send_it(to: str) -> str:
        """Send something."""
        return to

    @tool(requires_approval=True)
    def needs_human() -> str:
        """Requires approval."""
        return "ok"

    registry = ToolRegistry()
    registry.register_all([read_it, send_it, needs_human])
    server = MCPServer(registry, expose=["*_it", "needs_human"])
    assert [s.name for s in server.exposed_tools()] == ["read_it"]
    assert [s.name for s in MCPServer(registry, expose=["send_*"]).exposed_tools()] == []
    privileged = MCPServer(registry, allow_privileged=True)
    assert {s.name for s in privileged.exposed_tools()} == {"read_it", "send_it", "needs_human"}

    reader = asyncio.StreamReader()
    out: list[dict[str, Any]] = []

    async def write(data: bytes) -> None:
        out.append(json.loads(data))

    for msg in [
        Request(1, "initialize", {"protocolVersion": "2024-11-05", "capabilities": {}}),
        Notification("notifications/initialized"),
        Request(2, "tools/call", {"name": "read_it", "arguments": {"x": 4}}),
        Request(3, "tools/call", {"name": "send_it", "arguments": {"to": "a@b"}}),
        Request(4, "nope/method"),
    ]:
        reader.feed_data(encode_message(msg))
    reader.feed_data(b"{broken\n")
    reader.feed_eof()
    await server.serve(reader, write)

    by_id = {m.get("id"): m for m in out}
    assert by_id[1]["result"]["protocolVersion"] == "2024-11-05"
    assert server.initialized
    assert by_id[2]["result"]["content"][0]["text"] == "4"
    assert by_id[3]["error"]["code"] == -32602
    assert by_id[4]["error"]["code"] == -32601
    assert by_id[None]["error"]["code"] == PARSE_ERROR

