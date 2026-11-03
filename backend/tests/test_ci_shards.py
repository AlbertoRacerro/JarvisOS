"""Execute the workflow's real selectors: every test module has exactly one owner."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/ci.yml"


def selector(job: str) -> tuple[list[str], str]:
    config = yaml.safe_load(WORKFLOW.read_text())["jobs"][job]
    groups = config["strategy"]["matrix"]["group"]
    step = next(step for step in config["steps"] if step.get("name", "").endswith("test shard"))
    source = step["run"].split("python - <<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    return groups, source


def select(source: str, group: str, cwd: Path, *, index: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", source], cwd=cwd,
                          env={**os.environ, "TEST_GROUP": group, "BLUECAD_SHARD": str(index)}, capture_output=True, text=True, check=False)


def test_workflow_shards_partition_real_modules_once_including_conformance() -> None:
    backend = WORKFLOW.parents[2] / "backend"
    all_paths = sorted(path.relative_to(backend).as_posix() for path in (backend / "tests").rglob("test_*.py"))
    assigned = []
    for job in ("backend-core-tests", "backend-bluecad-tests"):
        groups, source = selector(job)
        for index, group in enumerate(groups):
            result = select(source, group, backend, index=index)
            assert result.returncode == 0, result.stderr
            paths = [Path(path).as_posix() for path in result.stdout.splitlines()]
            assert paths, group
            assigned.extend(paths)
    canary = yaml.safe_load(WORKFLOW.read_text())["jobs"]["bluecad-geometry-canary"]
    command = next(step["run"] for step in canary["steps"] if step.get("id") == "property")
    # The three pinned canary files run under their original profile and time gate.
    assigned.extend(word for word in command.split() if word.startswith("tests/") and word.endswith(".py"))
    assert sorted(assigned) == all_paths
    assert len(assigned) == len(set(assigned))


def test_core_selector_automatically_includes_new_and_nested_modules(tmp_path: Path) -> None:
    groups, source = selector("backend-core-tests")
    expected = {f"tests/{name}/test_case_{index}.py" for index, name in enumerate(["nested"] * 19)}
    for relative in expected | {"tests/bluecad/test_specialist.py"}:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    selected = []
    for group in groups:
        result = select(source, group, tmp_path)
        assert result.returncode == 0, result.stderr
        selected.extend(Path(path).as_posix() for path in result.stdout.splitlines())
    assert set(selected) == expected and len(selected) == len(expected)


@pytest.mark.parametrize("group", ["core-invalid", ""])
def test_core_selector_rejects_unknown_shard(tmp_path: Path, group: str) -> None:
    _, source = selector("backend-core-tests")
    assert select(source, group, tmp_path).returncode != 0


def test_core_selector_refuses_empty_shards(tmp_path: Path) -> None:
    groups, source = selector("backend-core-tests")
    path = tmp_path / "tests/test_only.py"
    path.parent.mkdir()
    path.touch()
    assert select(source, groups[0], tmp_path).returncode != 0
