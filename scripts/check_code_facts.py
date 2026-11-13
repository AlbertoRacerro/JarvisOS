#!/usr/bin/env python3
"""Check that ready specs state their code facts as references that still resolve (spec 270).

For every `ready` row of docs/specs/STATUS.md whose spec number is 270 or higher, the linked spec file must have a
section whose heading starts with `## Code facts`, containing at least one backticked reference `path::Symbol` or
`path::Class.method`. The path must exist relative to the repository root; in a `.py` file every dotted part must be
a class, function or assignment target found through `ast`; in any other file the last part must occur as a word.

    python scripts/check_code_facts.py
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from check_spec_status import REGISTRY, parse_registry

ROOT = Path(__file__).resolve().parents[1]
FIRST_CHECKED = 270
_REFERENCE = re.compile(r"`([^`\s]+)::([A-Za-z_][\w.]*)`")
_LINK = re.compile(r"\]\(([^)\s]+\.md)\)")
_NUMBER = re.compile(r"^(\d+)")


def _names(body: list[ast.stmt]) -> dict[str, ast.AST]:
    found: dict[str, ast.AST] = {}
    for node in body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            found[node.target.id] = node
    return found


def resolve(root: Path, path: str, symbol: str) -> str | None:
    """None when the reference resolves, else the reason it does not."""
    target = Path(path)
    if target.is_absolute() or path.startswith(("/", "\\")) or bool(target.drive):
        return "path is not repository-relative"
    file = root / target
    try:
        resolved_root = root.resolve()
        resolved = file.resolve()
        if not resolved.is_relative_to(resolved_root):
            return "path escapes repository root"
    except (OSError, RuntimeError, ValueError):
        return "file does not exist"
    if not file.is_file():
        return "file does not exist"
    text = file.read_text(encoding="utf-8")
    if file.suffix != ".py":
        last = symbol.split(".")[-1]
        return None if re.search(rf"(?<![\w]){re.escape(last)}(?![\w])", text) else f"`{last}` does not occur"
    try:
        scope: list[ast.stmt] = ast.parse(text).body
    except SyntaxError as error:
        return f"cannot parse: {error}"
    for part in symbol.split("."):
        node = _names(scope).get(part)
        if node is None:
            return f"`{part}` is not defined"
        scope = node.body if isinstance(node, ast.ClassDef) else []
    return None


def facts_section(lines: list[str]) -> list[tuple[int, str]] | None:
    """(line number, text) of the `## Code facts` section, or None when there is none."""
    section: list[tuple[int, str]] | None = None
    for number, line in enumerate(lines, start=1):
        if line.startswith("## "):
            if section is not None:
                break
            if line.startswith("## Code facts"):
                section = []
            continue
        if section is not None:
            section.append((number, line))
    return section


def check(root: Path = ROOT) -> tuple[list[str], int, int]:
    """(errors, references checked, specs checked)."""
    registry = root / REGISTRY
    rows = parse_registry(registry.read_text(encoding="utf-8"))
    errors: list[str] = []
    references = specs = 0
    for spec_id, row in sorted(rows.items()):
        number = _NUMBER.match(spec_id)
        if row.get("status") != "ready" or number is None or int(number.group(1)) < FIRST_CHECKED:
            continue
        link = _LINK.search(str(row.get("description", "")))
        if link is None:
            errors.append(f"{REGISTRY}: spec {spec_id}: no link to a spec file")
            continue
        spec = (registry.parent / link.group(1)).resolve()
        name = spec.relative_to(root.resolve()).as_posix() if spec.is_relative_to(root.resolve()) else str(spec)
        if not spec.is_file():
            errors.append(f"{name}: spec file does not exist")
            continue
        specs += 1
        section = facts_section(spec.read_text(encoding="utf-8").splitlines())
        if section is None:
            errors.append(f"{name}: no `## Code facts` section")
            continue
        found = 0
        for line_number, line in section:
            for path, symbol in _REFERENCE.findall(line):
                found += 1
                reason = resolve(root, path, symbol)
                if reason is not None:
                    errors.append(f"{name}:{line_number}: `{path}::{symbol}`: {reason}")
        if not found:
            errors.append(f"{name}: `## Code facts` has no `path::Symbol` reference")
        references += found
    return errors, references, specs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    try:
        errors, references, specs = check(args.root)
    except (OSError, ValueError) as error:
        print(f"code-facts: FAIL: {error}", file=sys.stderr)
        return 1
    for error in errors:
        print(error, file=sys.stderr)
    if errors:
        print(f"code-facts: {len(errors)} problem(s)", file=sys.stderr)
        return 1
    print(f"code-facts: OK ({references} references in {specs} specs)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
