"""CLI end to end in a subprocess: init, demo, exec, inspect, verify, replay, fork, memory."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e

SRC = str(Path(__file__).resolve().parents[2] / "src")


def cairn(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONPATH": SRC, "NO_COLOR": "1"}
    env.pop("ANTHROPIC_API_KEY", None)
    proc = subprocess.run([sys.executable, "-m", "cairn.cli.main", *args], cwd=cwd, env=env,
                          capture_output=True, text=True, timeout=120)
    if check and proc.returncode != 0:
        raise AssertionError(f"cairn {' '.join(args)} failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return proc


PLAN = {"goal": "compute and save", "nodes": [
    {"id": "x", "kind": "tool", "tool": "math.calculate", "args": {"expression": "2**10"}},
    {"id": "save", "kind": "tool", "tool": "fs.write",
     "args": {"path": "out.txt", "content": {"$tmpl": "result={{x}}"}}},
], "output": {"$ref": "x"}}


def test_cli_workflow(tmp_path: Path):
    assert "wrote cairn.toml" in cairn(tmp_path, "init").stdout
    assert "config: valid" in cairn(tmp_path, "doctor").stdout
    (tmp_path / "plan.json").write_text(json.dumps(PLAN))
    out = cairn(tmp_path, "exec", "plan.json").stdout
    assert "[completed]" in out and "1024" in out
    assert (tmp_path / "workspace" / "out.txt").read_text() == "result=1024"

    runs = json.loads(cairn(tmp_path, "runs", "--json").stdout)
    run_id = runs[0]["run_id"]
    assert "journal intact" in cairn(tmp_path, "verify", run_id).stdout
    assert "matched=True" in cairn(tmp_path, "replay", run_id).stdout
    report = json.loads(cairn(tmp_path, "show", run_id, "--json").stdout)
    assert report["output"] == 1024 and report["output_trusted"] is True
    cairn(tmp_path, "show", run_id, "--html", "r.html")
    assert (tmp_path / "r.html").read_text().startswith("<!doctype html>")
    forked = cairn(tmp_path, "fork", run_id, "--set", 'x.args={"expression": "3**4"}').stdout
    assert "81" in forked
    assert "policy.decision" in cairn(tmp_path, "events", run_id, "--type", "policy").stdout
    assert "math.calculate" in cairn(tmp_path, "tools", "--search", "arithmetic").stdout
    assert '"total": 0' in cairn(tmp_path, "memory", "stats").stdout


def test_cli_errors_are_actionable(tmp_path: Path):
    proc = cairn(tmp_path, "show", "run_does_not_exist", check=False)
    assert proc.returncode == 2 and "not_found" in proc.stderr
    proc = cairn(tmp_path, "run", "do something", check=False)
    assert proc.returncode == 2 and "no models configured" in proc.stderr


def test_cli_offline_demos(tmp_path: Path):
    out = cairn(tmp_path, "demo", "injection").stdout
    assert "WITHOUT provenance policy" in out and "exfil@attacker.example" in out
    assert "emails actually sent: none" in out
    out = cairn(tmp_path, "demo", "durability").stdout
    assert "charge calls=1 (not repeated)" in out and "matched=True" in out
