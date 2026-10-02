"""Model Context Protocol support: client, registry bridge and server.

* :class:`MCPClient` talks to an MCP server subprocess over stdio.
* :func:`mount_mcp_server` registers a server's tools into a
  :class:`~cairn.tools.registry.ToolRegistry` with conservative effects,
  untrusted output labels, description sanitizing and rug-pull pinning.
* :class:`MCPServer` exposes a registry to other MCP clients with least
  privilege.

No MCP SDK is required: JSON-RPC 2.0 framing lives in :mod:`cairn.mcp.jsonrpc`.
"""

from cairn.mcp.bridge import (
    MCPServerConfig,
    MountReport,
    QuarantinedTool,
    SkippedTool,
    approve,
    build_tool_spec,
    infer_effects,
    load_pins,
    mount_mcp_server,
    pin_report,
    sanitize_text,
    save_pins,
    unmount_mcp_server,
)
from cairn.mcp.client import (
    PROTOCOL_VERSION,
    MCPClient,
    MCPConnectionError,
    MCPProtocolError,
    MCPToolInfo,
    MCPToolResult,
)
from cairn.mcp.jsonrpc import JSONRPCError
from cairn.mcp.server import MCPServer, run_stdio_server

__all__ = [
    "PROTOCOL_VERSION",
    "JSONRPCError",
    "MCPClient",
    "MCPConnectionError",
    "MCPProtocolError",
    "MCPServer",
    "MCPServerConfig",
    "MCPToolInfo",
    "MCPToolResult",
    "MountReport",
    "QuarantinedTool",
    "SkippedTool",
    "approve",
    "build_tool_spec",
    "infer_effects",
    "load_pins",
    "mount_mcp_server",
    "pin_report",
    "run_stdio_server",
    "sanitize_text",
    "save_pins",
    "unmount_mcp_server",
]
