from __future__ import annotations

from pathlib import Path

import code_index as ci

PACKAGE = '''"""Module docstring."""


def plain():
    return 1


async def fetch():
    """Fetch something.

    More detail.
    """


class Thing:
    """A thing."""

    def method(self):
        """Do it."""

    async def later(self):
        pass
'''


def make_package(root: Path) -> Path:
    package = root / "pkg"
    package.mkdir()
    (package / "a.py").write_text(PACKAGE, encoding="utf-8")
    cache = package / "__pycache__"
    cache.mkdir()
    (cache / "cached.py").write_text("def hidden():\n    pass\n", encoding="utf-8")
    (package / "notes.txt").write_text("def ignored(): pass\n", encoding="utf-8")
    return package


def test_index_lists_functions_classes_and_methods_in_source_order(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    lines, errors = ci.index([package], root=tmp_path)
    assert errors == []
    assert lines == [
        "pkg/a.py:4 def plain",
        "pkg/a.py:8 async def fetch - Fetch something.",
        "pkg/a.py:15 class Thing - A thing.",
        "pkg/a.py:18 def Thing.method - Do it.",
        "pkg/a.py:21 async def Thing.later",
    ]


def test_syntax_error_is_reported_and_other_files_still_indexed(tmp_path: Path, capsys) -> None:
    package = make_package(tmp_path)
    (package / "b.py").write_text("def broken(:\n", encoding="utf-8")
    (package / "c.py").write_text("def after():\n    pass\n", encoding="utf-8")
    lines, errors = ci.index([package], root=tmp_path)
    assert "pkg/c.py:1 def after" in lines
    assert len(errors) == 1 and errors[0].startswith("pkg/b.py: cannot parse")

    assert ci.main([str(package)]) == 1
    captured = capsys.readouterr()
    assert "def after" in captured.out
    assert "cannot parse" in captured.err


def test_repository_script_index_has_correct_lines() -> None:
    target = ci.ROOT / "scripts" / "check_doc_paths.py"
    lines, errors = ci.index([target])
    assert errors == []
    source = target.read_text(encoding="utf-8").splitlines()
    names = [line.split(" ")[2] for line in lines]
    assert names == ["candidate", "resolves", "tokens", "check", "main"]
    for line in lines:
        number = int(line.split(" ")[0].rsplit(":", 1)[1])
        assert source[number - 1].startswith(f"def {line.split(' ')[2]}(")


def test_missing_path_is_an_error(tmp_path: Path) -> None:
    lines, errors = ci.index([tmp_path / "absent"], root=tmp_path)
    assert lines == [] and errors and "does not exist" in errors[0]
