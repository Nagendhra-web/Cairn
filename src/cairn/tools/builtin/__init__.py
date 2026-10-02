"""Built-in tools. Each declares its effects, sensitive sinks and output trust."""

from cairn.tools.builtin.comms import send_email
from cairn.tools.builtin.compute import calculate, run_python, safe_eval
from cairn.tools.builtin.fs import list_dir, read_file, write_file
from cairn.tools.builtin.web import fetch, html_to_text, post_json
from cairn.tools.spec import ToolSpec

ALL = [calculate, run_python, read_file, list_dir, write_file, fetch, post_json, send_email]


def builtin_tools(include: tuple[str, ...] = ("*",)) -> list[ToolSpec]:
    import fnmatch

    return [t for t in ALL if any(fnmatch.fnmatchcase(t.name, p) for p in include)]


__all__ = [
    "ALL",
    "builtin_tools",
    "calculate",
    "fetch",
    "html_to_text",
    "list_dir",
    "post_json",
    "read_file",
    "run_python",
    "safe_eval",
    "send_email",
    "write_file",
]
