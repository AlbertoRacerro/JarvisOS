from __future__ import annotations

from pathlib import Path

import check_doc_paths as cdp
import pytest


def make_repo(root: Path, **docs: str) -> None:
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "scripts").mkdir(exist_ok=True)
    (root / "scripts" / "tool.py").write_text("", encoding="utf-8")
    (root / "docs" / "PROCESS.md").write_text("ok\n", encoding="utf-8")
    for name, text in docs.items():
        target = root / name.replace("__", "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


DOCS = ("AGENTS.md",)


def test_repository_documents_cite_only_existing_paths() -> None:
    assert cdp.check() == []


def test_missing_path_is_reported_with_file_and_line(tmp_path: Path) -> None:
    make_repo(tmp_path, **{"AGENTS.md": "fine `scripts/tool.py`\nbroken `scripts/gone.py`\n"})
    assert cdp.check(tmp_path, DOCS) == ["AGENTS.md:2: `scripts/gone.py` does not exist"]


@pytest.mark.parametrize(
    "span",
    [
        "scripts/*.py",
        "https://example.com/a/b.md",
        "/workspaces/{id}/actions",
        "docs/specs/NNN-title.md",
        "<evidence-dir>/summary.json",
        "~/.config/tool/settings.toml",
        "backend/.venv/bin/python",
        "frontend/node_modules/x/y.js",
        "N/A",
        "JARVISOS_PACK_PATH",
        "jarvis_pack.toml",
        "route_class=auto",
    ],
)
def test_globs_urls_routes_placeholders_and_environment_paths_are_ignored(tmp_path: Path, span: str) -> None:
    make_repo(tmp_path, **{"AGENTS.md": f"see `{span}` here\n"})
    assert cdp.check(tmp_path, DOCS) == []


def test_bare_markdown_names_resolve_next_to_the_document(tmp_path: Path) -> None:
    make_repo(tmp_path, **{"docs__README.md": "see `PROCESS.md` and `GONE.md`\n"})
    assert cdp.check(tmp_path, ("docs/README.md",)) == ["docs/README.md:1: `GONE.md` does not exist"]


def test_paths_inside_fenced_commands_are_checked(tmp_path: Path) -> None:
    text = "```bash\n(cd backend && python scripts/tool.py --x scripts/gone.sh)\n```\n"
    make_repo(tmp_path, **{"AGENTS.md": text})
    assert cdp.check(tmp_path, DOCS) == ["AGENTS.md:2: `scripts/gone.sh` does not exist"]


def test_words_inside_a_backticked_command_are_checked(tmp_path: Path) -> None:
    make_repo(tmp_path, **{"AGENTS.md": "run `python3 scripts/gone.py --help`\n"})
    assert cdp.check(tmp_path, DOCS) == ["AGENTS.md:1: `scripts/gone.py` does not exist"]


def test_agents_md_line_budget(tmp_path: Path) -> None:
    make_repo(tmp_path, **{"AGENTS.md": "line\n" * 121})
    assert cdp.check(tmp_path, DOCS) == ["AGENTS.md: 121 lines, limit is 120"]
    make_repo(tmp_path, **{"AGENTS.md": "line\n" * 120})
    assert cdp.check(tmp_path, DOCS) == []


def test_claude_md_must_import_agents_md(tmp_path: Path) -> None:
    make_repo(tmp_path, **{"AGENTS.md": "x\n", "CLAUDE.md": "# Claude\n"})
    assert cdp.check(tmp_path, ("CLAUDE.md",)) == ["CLAUDE.md:1: must start with @AGENTS.md"]
    make_repo(tmp_path, **{"CLAUDE.md": "@AGENTS.md\n\nnotes\n"})
    assert cdp.check(tmp_path, ("CLAUDE.md",)) == []


def test_missing_document_is_reported(tmp_path: Path) -> None:
    make_repo(tmp_path)
    assert cdp.check(tmp_path, ("AGENTS.md",)) == ["AGENTS.md: file is missing"]
