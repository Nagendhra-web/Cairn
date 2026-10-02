#!/usr/bin/env python3
"""Run every offline example and fail if any exits non-zero.

Examples that need an API key or a local model server exit 0 with an
explanation when it is absent, so they are safe to include here.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    examples = sorted((ROOT / "examples").glob("[0-9][0-9]_*.py"))
    if not examples:
        print("no examples found")
        return 1
    failed = []
    for script in examples:
        started = time.perf_counter()
        proc = subprocess.run([sys.executable, str(script)], cwd=ROOT, capture_output=True, text=True,
                              timeout=300)
        took = time.perf_counter() - started
        status = "ok" if proc.returncode == 0 else f"FAILED ({proc.returncode})"
        print(f"{status:<12} {script.name:<32} {took:6.2f}s")
        if proc.returncode != 0:
            failed.append(script.name)
            print(proc.stdout[-2000:])
            print(proc.stderr[-2000:])
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
