"""Computation tools: a safe arithmetic evaluator and a resource-limited Python runner."""

from __future__ import annotations

import ast
import asyncio
import math
import operator
import sys
import tempfile
from typing import Any

from cairn.core.errors import ToolError
from cairn.security.sandbox import safe_env
from cairn.tools.decorator import tool

_BINOPS: dict[type, Any] = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}
_UNARY: dict[type, Any] = {ast.UAdd: operator.pos, ast.USub: operator.neg}
_FUNCS: dict[str, Any] = {
    "sqrt": math.sqrt, "log": math.log, "exp": math.exp, "abs": abs, "round": round,
    "min": min, "max": max, "sin": math.sin, "cos": math.cos, "floor": math.floor,
    "ceil": math.ceil,
}
_CONSTS = {"pi": math.pi, "e": math.e}


def safe_eval(expression: str) -> float | int:
    """Evaluate arithmetic without ``eval``: only numbers, operators and math functions."""
    tree = ast.parse(expression, mode="eval")

    def ev(node: ast.AST) -> Any:
        if isinstance(node, ast.Expression):
            return ev(node.body)
        if isinstance(node, ast.Constant) and isinstance(node.value, int | float):
            return node.value
        if isinstance(node, ast.BinOp) and type(node.op) in _BINOPS:
            left, right = ev(node.left), ev(node.right)
            if isinstance(node.op, ast.Pow) and abs(right) > 1000:
                raise ToolError("exponent too large")
            return _BINOPS[type(node.op)](left, right)
        if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY:
            return _UNARY[type(node.op)](ev(node.operand))
        if isinstance(node, ast.Name) and node.id in _CONSTS:
            return _CONSTS[node.id]
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in _FUNCS and not node.keywords):
            return _FUNCS[node.func.id](*[ev(a) for a in node.args])
        raise ToolError(f"unsupported expression element: {ast.dump(node)[:80]}")

    result = ev(tree)
    if not isinstance(result, int | float):
        raise ToolError("expression did not produce a number")
    return result


@tool(name="math.calculate", output_trust="inherit", idempotent=True,
      tags={"arithmetic", "compute", "number"})
def calculate(expression: str) -> float | int:
    """Evaluate an arithmetic expression such as '(3 + 4) * sqrt(16)'.

    Args:
        expression: arithmetic using + - * / // % ** and sqrt, log, exp, min, max, round
    """
    return safe_eval(expression)


def _limits(cpu_s: int, memory_mb: int) -> Any:
    def apply() -> None:  # pragma: no cover - runs in the child process
        import resource

        resource.setrlimit(resource.RLIMIT_CPU, (cpu_s, cpu_s))
        mem = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))

    return apply if sys.platform != "win32" else None


@tool(
    name="code.python",
    effects={"execute"},
    sensitive={"code"},
    output_trust="untrusted",
    timeout_s=60.0,
    tags={"python", "script", "program", "compute"},
)
async def run_python(code: str, timeout_s: float = 10.0, memory_mb: int = 512) -> dict[str, Any]:
    """Run Python code in an isolated subprocess and return stdout/stderr.

    Isolation: separate interpreter in isolated mode (-I), empty temporary
    working directory, scrubbed environment (no inherited credentials), CPU,
    memory, file-size and file-descriptor limits, and a wall-clock timeout.
    This is process-level isolation, not a VM: it does not block network
    access. Run Cairn inside a container without network for stronger
    guarantees (see docs/security.md).

    Args:
        code: Python source to execute
        timeout_s: wall-clock limit in seconds
        memory_mb: address-space limit in megabytes
    """
    with tempfile.TemporaryDirectory(prefix="cairn-py-") as workdir:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", "-c", code,
            cwd=workdir,
            env=safe_env(),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            preexec_fn=_limits(int(timeout_s) + 1, memory_mb),
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return {"exit_code": None, "stdout": "", "stderr": "timed out", "timed_out": True}
    return {
        "exit_code": proc.returncode,
        "stdout": out.decode("utf-8", "replace")[-20_000:],
        "stderr": err.decode("utf-8", "replace")[-5_000:],
        "timed_out": False,
    }
