"""Shared helpers for benchmark scripts.

Benchmarks are run as plain scripts (``python benchmarks/<name>.py``), so this
module also makes ``src/`` importable when the package is not installed.
Every result file goes through :func:`write_results`, which stamps it with the
environment (git commit, Python, platform) and refuses to write text that
contains an em dash, keeping generated files consistent with the repo style.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
BENCH_DIR = ROOT / "benchmarks"
DATASETS = BENCH_DIR / "datasets"
RESULTS = BENCH_DIR / "results"

if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from cairn.eval.runner import environment_info, utc_now  # noqa: E402

EM_DASH = chr(0x2014)  # the character this module refuses to write into results


def stamp(name: str, config: dict[str, Any]) -> dict[str, Any]:
    """Header recorded at the top of every benchmark result file."""
    return {
        "benchmark": name,
        "created_at": utc_now(),
        "environment": environment_info(ROOT),
        "config": config,
    }


def write_results(name: str, payload: dict[str, Any], markdown: str) -> tuple[Path, Path]:
    """Write ``results/<name>.json`` and ``results/<name>.md`` and return their paths."""
    RESULTS.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n"
    for blob in (text, markdown):
        if EM_DASH in blob:
            raise ValueError("refusing to write results containing an em dash")
    json_path = RESULTS / f"{name}.json"
    md_path = RESULTS / f"{name}.md"
    json_path.write_text(text, encoding="utf-8")
    md_path.write_text(markdown, encoding="utf-8")
    return json_path, md_path


def env_markdown(header: dict[str, Any]) -> list[str]:
    env = header["environment"]
    return [
        f"- Generated: {header['created_at']} by `benchmarks/{header['benchmark']}.py`",
        f"- Git commit: `{env.get('git_commit')}` (uncommitted changes: {env.get('git_dirty')})",
        f"- Python {env.get('python')} ({env.get('implementation')}) on {env.get('platform')}, "
        f"{env.get('cpu_count')} CPUs",
    ]


def fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}g}"
    return str(value)


def pct(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"
