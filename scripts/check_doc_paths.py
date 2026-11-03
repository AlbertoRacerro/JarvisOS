#!/usr/bin/env python3
"""Check that the canonical agent documents only cite repository paths that exist (and stay short).

Checked files: AGENTS.md, CLAUDE.md, docs/PROCESS.md, docs/ARCHITECTURE.md, docs/README.md.
A backticked span (or a token of a fenced command) counts as a path when it contains a `/`, or when it is a bare
`*.md` name. Spans with whitespace, URLs, globs, placeholders (`<x>`, `NNN`, `{x}`), `~`, `$VAR`, a leading `/`
(HTTP routes) or a build/environment directory (`.venv`, `node_modules`, `dist`) are ignored. A path resolves relative
to the repository root or to the directory of the document that cites it.

    python scripts/check_doc_paths.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ("AGENTS.md", "CLAUDE.md", "docs/PROCESS.md", "docs/ARCHITECTURE.md", "docs/README.md")
MAX_LINES = {"AGENTS.md": 120, "CLAUDE.md": 20}
IGNORED_PARTS = {".venv", "node_modules", "dist"}
_SPAN = re.compile(r"`([^`\n]+)`")
_TOKEN_SPLIT = re.compile(r"[\s();,]+")
_PLACEHOLDER = re.compile(r"[<>*?\[\]{}$|]|NNN|^~|^/|://|^-|^N/A$")


def candidate(text: str) -> str | None:
    """The repository path a token names, or None when it is not a checkable path."""
    text = text.strip().rstrip(".,:;")
    text = re.sub(r":\d+(-\d+)?$", "", text)
    text = text.split("#", 1)[0]
    if not text or _PLACEHOLDER.search(text):
        return None
    if "/" not in text and not text.endswith(".md"):
        return None
    if any(part in IGNORED_PARTS for part in text.split("/")):
        return None
    return text


def resolves(path: str, document: Path, root: Path) -> bool:
    return (root / path).exists() or (document.parent / path).exists()


def tokens(line: str, in_fence: bool) -> list[str]:
    if in_fence:
        return [item for item in _TOKEN_SPLIT.split(line) if item]
    spans = [match.group(1) for match in _SPAN.finditer(line)]
    # A span with whitespace is a command or a phrase: check the path-like words inside it.
    return [word for span in spans for word in (_TOKEN_SPLIT.split(span) if " " in span else [span]) if word]


def check(root: Path = ROOT, docs: tuple[str, ...] = DOCS) -> list[str]:
    errors: list[str] = []
    for name in docs:
        document = root / name
        if not document.is_file():
            errors.append(f"{name}: file is missing")
            continue
        lines = document.read_text(encoding="utf-8").splitlines()
        limit = MAX_LINES.get(name)
        if limit is not None and len(lines) > limit:
            errors.append(f"{name}: {len(lines)} lines, limit is {limit}")
        if name == "CLAUDE.md" and (not lines or lines[0].strip() != "@AGENTS.md"):
            errors.append("CLAUDE.md:1: must start with @AGENTS.md")
        in_fence = False
        for number, line in enumerate(lines, start=1):
            if line.lstrip().startswith("```"):
                in_fence = not in_fence
                continue
            words = tokens(line, in_fence)
            if name == "CLAUDE.md" and line.startswith("@"):
                words.append(line[1:].strip())
            for word in words:
                path = candidate(word)
                if path is not None and not resolves(path, document, root):
                    errors.append(f"{name}:{number}: `{path}` does not exist")
    return errors


def main() -> int:
    errors = check()
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"doc-paths: {len(errors)} problem(s)", file=sys.stderr)
        return 1
    print("doc-paths: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
