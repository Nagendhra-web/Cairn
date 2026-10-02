#!/usr/bin/env python3
"""Fail if any tracked text file contains an em dash (U+2014).

Repository style rule: use commas, colons, semicolons, parentheses or plain
hyphens instead. Runs in CI and as a pre-commit hook.

Usage: python scripts/check_no_em_dash.py [paths...]
With no paths, checks every file tracked by git (falls back to walking the tree).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

FORBIDDEN = chr(0x2014)  # built from its code point so this file never contains it
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".ruff_cache",
             ".pytest_cache", "dist", "build", ".cairn"}
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".db", ".sqlite", ".whl",
                   ".gz", ".zip", ".pyc", ".woff", ".woff2"}


def candidate_files(args: list[str]) -> list[Path]:
    if args:
        return [Path(a) for a in args]
    try:
        out = subprocess.run(["git", "ls-files", "-co", "--exclude-standard"], capture_output=True,
                             text=True, check=True).stdout
        return [Path(line) for line in out.splitlines() if line]
    except (OSError, subprocess.CalledProcessError):
        return [p for p in Path(".").rglob("*")
                if p.is_file() and not (set(p.parts) & SKIP_DIRS)]


def main(argv: list[str]) -> int:
    failures = 0
    for path in candidate_files(argv):
        if path.suffix.lower() in BINARY_SUFFIXES or not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            col = line.find(FORBIDDEN)
            if col != -1:
                failures += 1
                print(f"{path}:{lineno}:{col + 1}: em dash (U+2014) is not allowed; "
                      "use a comma, colon, semicolon, parentheses or a hyphen")
    if failures:
        print(f"\n{failures} em dash occurrence(s) found.")
        return 1
    print("no em dashes found")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
