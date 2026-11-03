from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_architecture_enforcement.py"


def _run(root: Path) -> subprocess.CompletedProcess[str]:
    config = root / "configs" / "architecture_enforcement.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"exceptions": []}), encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(root), "--config", str(config)],
        check=False,
        capture_output=True,
        text=True,
    )


def test_ae002_nested_function_inherits_enclosing_import_alias(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/nested_alias.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def outer():\n"
        "    import httpx as transport\n"
        "    def inner():\n"
        "        transport.post('https://example.invalid')\n"
        "    return inner\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "AE002 backend/app/nested_alias.py::inner" in result.stdout


def test_ae002_nested_local_import_still_shadows_enclosing_alias(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/nested_shadow.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def outer():\n"
        "    import httpx as transport\n"
        "    def inner():\n"
        "        import unrelated as transport\n"
        "        transport.post('not-network')\n"
        "    return inner\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr


def test_ae002_nested_function_sees_enclosing_import_executed_after_definition(tmp_path: Path) -> None:
    source = tmp_path / "backend/app/nested_late_outer_import.py"
    source.parent.mkdir(parents=True)
    source.write_text(
        "def outer():\n"
        "    def inner():\n"
        "        transport.post('https://example.invalid')\n"
        "    import httpx as transport\n"
        "    return inner\n",
        encoding="utf-8",
    )
    result = _run(tmp_path)
    assert result.returncode == 1
    assert "AE002 backend/app/nested_late_outer_import.py::inner" in result.stdout
