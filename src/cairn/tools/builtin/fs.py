"""Sandboxed filesystem tools. All paths resolve through :class:`PathSandbox`."""

from __future__ import annotations

from typing import Any

from cairn.core.errors import SandboxViolation
from cairn.tools.decorator import tool
from cairn.tools.registry import ToolContext


def _sandbox(ctx: ToolContext) -> Any:
    if ctx.sandbox is None:
        raise SandboxViolation("filesystem tools require a configured sandbox root")
    return ctx.sandbox


@tool(name="fs.read", effects={"read"}, output_trust="untrusted", tags={"file", "filesystem"})
def read_file(path: str, ctx: ToolContext, max_bytes: int = 200_000) -> str:
    """Read a UTF-8 text file inside the sandbox.

    File contents are labeled untrusted: anyone who could write the file could
    have planted instructions in it.

    Args:
        path: file path relative to the sandbox root
        max_bytes: maximum number of bytes to return
    """
    resolved = _sandbox(ctx).resolve(path)
    data: bytes = resolved.read_bytes()[:max_bytes]
    return data.decode("utf-8", errors="replace")


@tool(name="fs.list", effects={"read"}, output_trust="untrusted", tags={"file", "directory"})
def list_dir(ctx: ToolContext, path: str = ".") -> list[str]:
    """List entries of a directory inside the sandbox (directories end with '/').

    Args:
        path: directory path relative to the sandbox root
    """
    resolved = _sandbox(ctx).resolve(path)
    return sorted(p.name + ("/" if p.is_dir() else "") for p in resolved.iterdir())


@tool(
    name="fs.write",
    effects={"write"},
    sensitive={"path", "content"},
    output_trust="trusted",
    tags={"file", "save"},
)
def write_file(path: str, content: str, ctx: ToolContext) -> dict[str, Any]:
    """Write a UTF-8 text file inside the sandbox, creating parent directories.

    Args:
        path: file path relative to the sandbox root
        content: text to write
    """
    resolved = _sandbox(ctx).resolve(path, write=True)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")
    return {"path": str(resolved), "bytes": len(content.encode("utf-8"))}
