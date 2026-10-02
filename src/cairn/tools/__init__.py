"""Tools as capabilities: typed contracts, discovery, validated invocation."""

from cairn.tools.decorator import schema_from_function, tool
from cairn.tools.registry import ToolContext, ToolMatch, ToolRegistry
from cairn.tools.spec import Effect, OutputTrust, ToolSpec

__all__ = [
    "Effect",
    "OutputTrust",
    "ToolContext",
    "ToolMatch",
    "ToolRegistry",
    "ToolSpec",
    "schema_from_function",
    "tool",
]
