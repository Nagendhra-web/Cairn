"""Provenance lattice, observability, configuration, built-in tools and the decorator."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cairn.config import load_config
from cairn.core.errors import ConfigError, SandboxViolation, ToolError
from cairn.observability import build_trace, render_html, render_text, run_report, to_otlp
from cairn.provenance import BOTTOM, USER, Integrity, Label, join_all, untrusted
from cairn.runtime import Plan, PlanBuilder, ToolNode, ref
from cairn.security import PathSandbox
from cairn.security.secrets import SecretVault
from cairn.tools import ToolContext, tool
from cairn.tools.builtin import calculate, read_file, run_python, safe_eval, write_file


def test_label_lattice_laws():
    a, b, c = USER, untrusted("web"), Label().with_secrecy("pii")
    assert a.join(b) == b.join(a)
    assert a.join(b).join(c) == a.join(b.join(c))
    assert a.join(a) == a
    assert BOTTOM.join(a) == a
    assert a.join(b).integrity is Integrity.UNTRUSTED
    assert join_all([a, b, c]).secrecy == {"pii"}


def test_tool_decorator_builds_schema_and_validates_sensitive():
    @tool(effects={"send"}, sensitive={"to"})
    def notify(to: str, count: int = 1) -> dict[str, int]:
        """Notify someone.

        Args:
            to: recipient address
            count: how many times
        """
        return {"n": count}

    assert notify.input_schema["required"] == ["to"]
    assert notify.input_schema["properties"]["to"]["description"] == "recipient address"
    assert notify.output_schema is not None
    with pytest.raises(ValueError):
        tool(sensitive={"nope"})(lambda x: x)


def test_safe_calculator():
    assert calculate.fn("2 * (3 + 4)") == 14
    assert safe_eval("sqrt(16) + pi") == pytest.approx(7.14159, rel=1e-4)
    for evil in ("__import__('os')", "open('x')", "2 ** 100000", "[1,2]"):
        with pytest.raises(ToolError):
            safe_eval(evil)


async def test_python_runner_isolation():
    out = await run_python.fn("import os; print(len(os.environ), os.getcwd().startswith('/tmp'))")
    assert out["exit_code"] == 0
    count, in_tmp = out["stdout"].split()
    assert int(count) <= 4 and in_tmp == "True"  # scrubbed environment, temp workdir
    slow = await run_python.fn("while True: pass", timeout_s=0.5)
    assert slow["timed_out"] or slow["exit_code"] != 0


def test_fs_tools_respect_sandbox(tmp_path: Path):
    ctx = ToolContext("r", "n", SecretVault().scope(()), sandbox=PathSandbox([tmp_path]))
    write_file.fn("notes/a.txt", "hello", ctx)
    assert read_file.fn("notes/a.txt", ctx) == "hello"
    with pytest.raises(SandboxViolation):
        read_file.fn("../../etc/passwd", ctx)


async def test_report_trace_and_renderers(runtime, scripted):
    scripted.on("Summarize", "short")
    b = PlanBuilder("observe")
    b.tool("page", "web.get", url="u")
    b.llm("sum", "Summarize {{page}}")
    b.tool("bad", "broken", deps=["sum"]).__class__  # noqa: B018
    plan = b.build()
    plan.nodes[-1].on_error = "skip"
    result = await runtime.run(plan)
    events = await runtime.journal.read(result.run_id)
    report = run_report(result.run_id, events)
    assert report["nodes"]["sum"]["model_calls"] == 1
    assert report["nodes"]["sum"]["label"].startswith("untrusted")
    assert report["usage"]["tool_calls"] == 1
    assert report["injection_signals"]
    text = render_text(report)
    assert "page" in text and "untrusted" in text
    html = render_html(report)
    assert html.startswith("<!doctype html>") and "<script" not in html.lower()
    otlp = to_otlp(build_trace(result.run_id, events))
    spans = otlp["resourceSpans"][0]["scopeSpans"][0]["spans"]
    assert len({s["traceId"] for s in spans}) == 1
    assert any("parentSpanId" in s for s in spans)


def test_config_loading_and_errors(tmp_path: Path):
    cfg = tmp_path / "cairn.toml"
    cfg.write_text('[cairn]\ndata_dir = "d"\n[cairn.security]\nnetwork_allow = ["a.com"]\n')
    config = load_config(cfg, env={})
    assert config.data_dir == "d" and config.security.network_allow == ["a.com"]
    assert config.models == []
    auto = load_config(cfg, env={"ANTHROPIC_API_KEY": "k"})
    assert {m.tier.value for m in auto.models} == {"fast", "balanced", "frontier"}
    cfg.write_text('[cairn]\nbudget = {max_nodes = "lots"}\n')
    with pytest.raises(ConfigError, match=r"budget\.max_nodes"):
        load_config(cfg, env={})
    cfg.write_text('[[cairn.models]]\nname = "x"\nprovider = "anthropic"\napi_key_env = "NOPE"\n')
    with pytest.raises(ConfigError, match="NOPE"):
        load_config(cfg, env={})
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml", env={})


async def test_tool_discovery_ranks_by_task(runtime):
    matches = await runtime.services.tools.discover("send a message to someone", k=3)
    assert matches[0].spec.name == "mail.send"
    matches = await runtime.services.tools.discover("sum two numbers", k=3, grants=["add", "echo"])
    assert matches[0].spec.name == "add"
    assert all(m.spec.name in ("add", "echo") for m in matches)


async def test_plan_json_roundtrip(runtime):
    plan = Plan(goal="x", nodes=[ToolNode(id="a", tool="add", args={"a": 1, "b": ref("$input.n")})],
                output=ref("a"))
    again = Plan.model_validate_json(json.dumps(plan.model_dump(mode="json")))
    result = await runtime.run(again, inputs={"n": 41})
    assert result.output == 42


def test_mcp_config_errors_surface_at_load(tmp_path: Path):
    cfg = tmp_path / "cairn.toml"
    cfg.write_text('[[cairn.mcp_servers]]\nname = "x"\ncommand = "y"\npinnned = {}\n')
    with pytest.raises(ConfigError, match="mcp_servers"):
        load_config(cfg, env={})
