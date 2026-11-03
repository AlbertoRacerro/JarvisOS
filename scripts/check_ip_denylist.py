#!/usr/bin/env python3
"""Fail if a tracked file (or file name) matches a private deny-list term.

The terms live in the Actions secret IP_DENYLIST (environment variable of the same name), one case-insensitive regular
expression per line; blank lines and lines starting with `#` are ignored. They are never stored in the repository,
because a public deny-list would disclose the terms. Output is `path:line` only: a matched term is never printed.

    IP_DENYLIST=... python scripts/check_ip_denylist.py            # skip with a notice when the variable is unset
    IP_DENYLIST=... python scripts/check_ip_denylist.py --require  # fail when it is unset (pushes to the main branches)

Exit codes: 0 clean or skipped, 1 matches found or the secret is required but missing, 2 invalid term.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAX_BYTES = 5 * 1024 * 1024
SKIPPED_DIRS = {".git", "node_modules", ".venv", "__pycache__", "dist"}


def parse_terms(text: str) -> list[re.Pattern[str]]:
    patterns: list[re.Pattern[str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        term = line.strip()
        if not term or term.startswith("#"):
            continue
        try:
            patterns.append(re.compile(term, re.IGNORECASE))
        except re.error as exc:
            raise ValueError(f"deny-list line {number} is not a valid regular expression ({exc.msg})") from None
    return patterns


def tracked_files(root: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True
        ).stdout.decode("utf-8", "surrogateescape")
        return sorted(item for item in out.split("\0") if item)
    except (OSError, subprocess.CalledProcessError):
        found: list[str] = []
        for directory, names, files in os.walk(root):
            names[:] = [name for name in names if name not in SKIPPED_DIRS]
            found += [(Path(directory) / name).relative_to(root).as_posix() for name in files]
        return sorted(found)


def scan(root: Path, patterns: list[re.Pattern[str]], files: list[str] | None = None) -> list[str]:
    """`path` for a file name that matches, `path:line` for a content match. Never the matched text."""
    hits: list[str] = []
    for rel in files if files is not None else tracked_files(root):
        if any(pattern.search(rel) for pattern in patterns):
            hits.append(rel)
        path = root / rel
        try:
            if not path.is_file() or path.is_symlink() or path.stat().st_size > MAX_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        if b"\0" in data[:8192]:
            continue
        for number, line in enumerate(data.decode("utf-8", "replace").splitlines(), start=1):
            if any(pattern.search(line) for pattern in patterns):
                hits.append(f"{rel}:{number}")
    return hits


def main(argv: list[str]) -> int:
    raw = os.environ.get("IP_DENYLIST", "")
    if not raw.strip():
        if "--require" in argv:
            print("leak-guard: IP_DENYLIST secret is required here but is not set", file=sys.stderr)
            return 1
        print("::notice::leak-guard: IP_DENYLIST is not available (fork pull request?); deny-list check skipped")
        return 0
    try:
        patterns = parse_terms(raw)
    except ValueError as exc:
        print(f"leak-guard: {exc}", file=sys.stderr)
        return 2
    if not patterns:
        print("leak-guard: IP_DENYLIST holds no terms", file=sys.stderr)
        return 1 if "--require" in argv else 0
    hits = scan(ROOT, patterns)
    for hit in hits:
        print(f"deny-list match: {hit}", file=sys.stderr)
    if hits:
        print(f"leak-guard: {len(hits)} match(es) (terms are not printed)", file=sys.stderr)
        return 1
    print(f"leak-guard: deny-list clean ({len(patterns)} term(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
