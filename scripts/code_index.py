#!/usr/bin/env python3
"""Print a generated index of Python files: each class, function and method with its line and docstring summary.

An on-demand reader (spec 270), never committed: read the index of a file or directory, then open exactly the
functions you need. Standard library `ast` only; output is deterministic.

    python scripts/code_index.py scripts/check_doc_paths.py backend/app/core
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIPPED_DIRS = {"__pycache__", "node_modules", ".venv", "dist", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def python_files(path: Path) -> list[Path]:
    if path.is_file():
        return [path] if path.suffix == ".py" else []
    found: list[Path] = []
    for child in sorted(path.iterdir()):
        if child.is_dir():
            if child.name not in SKIPPED_DIRS and not child.name.startswith("."):
                found.extend(python_files(child))
        elif child.suffix == ".py":
            found.append(child)
    return found


def _kind(node: ast.AST) -> str:
    if isinstance(node, ast.ClassDef):
        return "class"
    return "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"


def _summary(node: ast.AST) -> str:
    doc = ast.get_docstring(node) if isinstance(node, _DEFS) else None
    first = doc.strip().splitlines()[0].strip() if doc and doc.strip() else ""
    return f" - {first}" if first else ""


def symbols(tree: ast.Module) -> list[tuple[int, str, str, str]]:
    """(line, kind, qualified name, summary) for module-level defs and the methods of module-level classes."""
    rows: list[tuple[int, str, str, str]] = []
    for node in tree.body:
        if not isinstance(node, _DEFS):
            continue
        rows.append((node.lineno, _kind(node), node.name, _summary(node)))
        if isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(member, _DEFS):
                    rows.append((member.lineno, _kind(member), f"{node.name}.{member.name}", _summary(member)))
    return sorted(rows, key=lambda row: row[0])


def _display(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def index(paths: list[Path], root: Path = ROOT) -> tuple[list[str], list[str]]:
    """Index lines and error messages for the given files or directories."""
    lines: list[str] = []
    errors: list[str] = []
    for given in paths:
        if not given.exists():
            errors.append(f"{given}: does not exist")
            continue
        for file in python_files(given):
            name = _display(file, root)
            try:
                tree = ast.parse(file.read_text(encoding="utf-8"), filename=name)
            except (SyntaxError, UnicodeDecodeError) as error:
                errors.append(f"{name}: cannot parse: {error}")
                continue
            lines.extend(f"{name}:{line} {kind} {qualified}{summary}" for line, kind, qualified, summary in symbols(tree))
    return lines, errors


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not args or args[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if args else 2
    lines, errors = index([Path(arg) for arg in args])
    for line in lines:
        print(line)
    for error in errors:
        print(error, file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
