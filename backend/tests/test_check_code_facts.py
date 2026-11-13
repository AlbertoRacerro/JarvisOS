from __future__ import annotations

from pathlib import Path

import check_code_facts as ccf
import pytest

HEADER = """# Spec status

## Registry
| Spec | Status | Implementation PR | Name | Depends on | Description |
| --- | --- | --- | --- | --- | --- |
"""

MODULE = '''VALUE = 1
LIMIT: int = 2


def helper():
    pass


class Owner:
    NAME = "x"

    def act(self):
        pass
'''


def make_repo(root: Path, rows: list[tuple[str, str]], specs: dict[str, str]) -> None:
    (root / "docs" / "specs").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "scripts" / "tool.py").write_text(MODULE, encoding="utf-8")
    (root / "scripts" / "run.sh").write_text("start_service --fast\n", encoding="utf-8")
    lines = [f"| {spec} | {status} | — | NAME-{spec} | — | [Spec]({spec}-x.md) text |" for spec, status in rows]
    (root / "docs" / "specs" / "STATUS.md").write_text(HEADER + "\n".join(lines) + "\n", encoding="utf-8")
    for spec, text in specs.items():
        (root / "docs" / "specs" / f"{spec}-x.md").write_text(text, encoding="utf-8")


def spec_text(*references: str) -> str:
    facts = "\n".join(f"- fact `{reference}`." for reference in references)
    return f"# Spec\n\n## Goal\n\nx\n\n## Code facts checked at `abc`\n\n{facts}\n\n## Scope\n\n- `scripts/nope.py::gone`\n"


GOOD = (
    "scripts/tool.py::helper",
    "scripts/tool.py::Owner",
    "scripts/tool.py::Owner.act",
    "scripts/tool.py::Owner.NAME",
    "scripts/tool.py::VALUE",
    "scripts/tool.py::LIMIT",
    "scripts/run.sh::start_service",
)


def test_resolving_references_pass_and_scope_section_is_ignored(tmp_path: Path) -> None:
    make_repo(tmp_path, [("270", "ready")], {"270": spec_text(*GOOD)})
    errors, references, specs = ccf.check(tmp_path)
    assert errors == []
    assert (references, specs) == (len(GOOD), 1)


@pytest.mark.parametrize(
    ("reference", "reason"),
    [
        ("scripts/missing.py::helper", "file does not exist"),
        ("scripts/tool.py::absent", "`absent` is not defined"),
        ("scripts/tool.py::Owner.absent", "`absent` is not defined"),
        ("scripts/tool.py::helper.inner", "`inner` is not defined"),
        ("scripts/run.sh::stop_service", "`stop_service` does not occur"),
        ("scripts/run.sh::start", "`start` does not occur"),
    ],
)
def test_unresolved_reference_fails_naming_spec_and_reference(tmp_path: Path, reference: str, reason: str) -> None:
    make_repo(tmp_path, [("271", "ready")], {"271": spec_text("scripts/tool.py::helper", reference)})
    errors, _, _ = ccf.check(tmp_path)
    assert len(errors) == 1
    assert errors[0].startswith("docs/specs/271-x.md:")
    assert f"`{reference}`: {reason}" in errors[0]


def test_missing_section_and_empty_section_fail(tmp_path: Path) -> None:
    make_repo(
        tmp_path,
        [("272", "ready"), ("273", "ready")],
        {"272": "# Spec\n\n## Goal\n\nx\n", "273": spec_text()},
    )
    errors, _, specs = ccf.check(tmp_path)
    assert specs == 2
    assert "docs/specs/272-x.md: no `## Code facts` section" in errors
    assert "docs/specs/273-x.md: `## Code facts` has no `path::Symbol` reference" in errors


def test_other_statuses_and_older_specs_are_not_checked(tmp_path: Path) -> None:
    bad = spec_text("scripts/missing.py::helper")
    make_repo(
        tmp_path,
        [("269", "ready"), ("274", "planned"), ("275", "blocked")],
        {"269": bad, "274": bad, "275": "# no section\n"},
    )
    assert ccf.check(tmp_path) == ([], 0, 0)


def test_main_exit_codes(tmp_path: Path, capsys) -> None:
    make_repo(tmp_path, [("270", "ready")], {"270": spec_text("scripts/tool.py::absent")})
    assert ccf.main(["--root", str(tmp_path)]) == 1
    assert "code-facts: 1 problem(s)" in capsys.readouterr().err


def test_repository_ready_specs_pass() -> None:
    errors, _, _ = ccf.check()
    assert errors == []
