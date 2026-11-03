from __future__ import annotations

from pathlib import Path

import gen_modules as gm
import pytest


def manifest(purpose: str = "Does a thing.", status: str = "live") -> str:
    return f'purpose = "{purpose}"\nstatus = "{status}"\nentrypoints = ["/x", "main.py"]\nowner = "maintainer"\n'


def make_tree(root: Path) -> None:
    modules = root / "backend" / "app" / "modules"
    (modules / "alpha").mkdir(parents=True)
    (modules / "alpha" / "module.toml").write_text(manifest("Alpha | pipes"), encoding="utf-8")
    (modules / "alpha" / "a.py").write_text("one\ntwo\n", encoding="utf-8")
    (modules / "alpha" / "inner").mkdir()
    (modules / "alpha" / "inner" / "module.toml").write_text(manifest(status="in-development"), encoding="utf-8")
    (modules / "alpha" / "inner" / "b.py").write_text("x\n" * 200, encoding="utf-8")
    (modules / "alpha" / "__pycache__").mkdir()
    (modules / "alpha" / "__pycache__" / "junk.py").write_text("x\n" * 99, encoding="utf-8")
    (modules / "beta").mkdir()
    (modules / "beta" / "module.toml").write_text(manifest(status="backlog-candidate"), encoding="utf-8")


def test_committed_modules_table_is_current() -> None:
    assert gm.OUTPUT.read_text(encoding="utf-8") == gm.render()


def test_every_module_directory_of_the_repository_has_a_manifest() -> None:
    assert gm.missing_manifests() == []


def test_table_has_status_purpose_entrypoints_and_loc_excluding_nested_modules(tmp_path: Path) -> None:
    make_tree(tmp_path)
    text = gm.render(tmp_path)
    assert "| `backend/app/modules/alpha` | live | Alpha \\| pipes | `/x`, `main.py` | <100 |" in text
    assert "| `backend/app/modules/alpha/inner` | in-development | Does a thing. | `/x`, `main.py` | 200 |" in text
    assert "| `backend/app/modules/beta` | backlog-candidate |" in text
    assert text.endswith("Totals: 1 live, 1 in-development, 1 backlog-candidate.\n")


def test_loc_is_rounded_to_the_nearest_hundred() -> None:
    labels = [gm.loc_label(n) for n in (0, 99, 100, 149, 151, 3549, 3551)]
    assert labels == ["<100", "<100", "100", "100", "200", "3500", "3600"]


def test_output_is_deterministic(tmp_path: Path) -> None:
    make_tree(tmp_path)
    assert gm.render(tmp_path) == gm.render(tmp_path)


def test_module_directory_without_manifest_is_an_error(tmp_path: Path) -> None:
    make_tree(tmp_path)
    (tmp_path / "backend" / "app" / "modules" / "gamma").mkdir()
    with pytest.raises(ValueError, match="backend/app/modules/gamma"):
        gm.render(tmp_path)


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('status = "live"\nentrypoints = []\nowner = "m"\n', "missing purpose"),
        (manifest(status="done"), "status must be one of"),
        ('purpose = "x"\nstatus = "live"\nentrypoints = "main.py"\nowner = "m"\n', "entrypoints must be a list"),
        (manifest(purpose="  "), "purpose is empty"),
    ],
)
def test_invalid_manifest_is_an_error(tmp_path: Path, text: str, message: str) -> None:
    make_tree(tmp_path)
    (tmp_path / "backend" / "app" / "modules" / "beta" / "module.toml").write_text(text, encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        gm.render(tmp_path)
