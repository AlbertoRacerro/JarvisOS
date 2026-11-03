from __future__ import annotations

import hashlib
import json
import re
import stat
import sys
from pathlib import Path

import pytest

from app.modules.bluecad.models import BluecadCandidateCreate
from app.modules.bluecad.registry import (
    DEFAULT_REGISTRY_PATH,
    REGISTRY_ENV_VAR,
    ToolRegistryError,
    check_registry,
    load_registry,
    resolve_registry_path,
    resolve_tool,
    run_tool,
    validate_license_boundaries,
)

CI_FORBIDDEN_IMPORT_RE = re.compile(
    r"^\s*(?:import\s+(?:gmsh|FreeCAD|calculix)\b|from\s+(?:gmsh|FreeCAD|calculix)\b)"
)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_tool(path: Path, body: str = "") -> Path:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, time\n"
        f"{body}\n"
        "print('fake-tool:' + ','.join(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _registry(tmp_path: Path, tools: list[dict]) -> Path:
    path = tmp_path / "registry.yaml"
    path.write_text(json.dumps({"registry_version": "bluecad_tool_registry_v0_1", "tools": tools}), encoding="utf-8")
    return path


def _subprocess_tool(entrypoint: Path, **overrides: object) -> dict:
    tool = {
        "id": "fake",
        "kind": "mesher",
        "integration_mode": "subprocess",
        "version_pin": "1.2.3",
        "license": {"spdx": "MIT", "boundary": "B", "verified_date": "2026-07-03"},
        "enabled": True,
        "entrypoint": str(entrypoint),
        "binary_sha256": _hash(entrypoint) if entrypoint.exists() else "0" * 64,
        "provenance_url": "https://example.invalid/fake",
        "capabilities": ["echo"],
        "health_check": f"{entrypoint} health",
    }
    tool.update(overrides)
    return tool


def test_load_registry_rejects_duplicate_ids_unknown_fields_and_missing_spdx(tmp_path: Path) -> None:
    exe = _write_tool(tmp_path / "fake.py")
    valid = _subprocess_tool(exe)
    with pytest.raises(ToolRegistryError, match="DUPLICATE_TOOL_ID"):
        load_registry(_registry(tmp_path, [valid, valid]))

    with pytest.raises(ToolRegistryError, match="UNKNOWN_FIELD"):
        load_registry(_registry(tmp_path, [valid | {"surprise": True}]))

    missing_spdx = valid.copy()
    missing_spdx["id"] = "missing-spdx"
    missing_spdx["license"] = {"boundary": "B", "verified_date": "2026-07-03"}
    with pytest.raises(ToolRegistryError) as exc_info:
        load_registry(_registry(tmp_path, [missing_spdx]))
    assert exc_info.value.code == "SPDX_REQUIRED"


def test_operator_registry_env_override_and_explicit_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_exe = _write_tool(tmp_path / "env.py")
    explicit_exe = _write_tool(tmp_path / "explicit.py")
    env_dir = tmp_path / "env-registry"
    env_dir.mkdir()
    explicit_dir = tmp_path / "explicit-registry"
    explicit_dir.mkdir()
    env_registry = _registry(env_dir, [_subprocess_tool(env_exe)])
    explicit_registry = _registry(explicit_dir, [_subprocess_tool(explicit_exe)])

    monkeypatch.setenv(REGISTRY_ENV_VAR, str(env_registry))
    assert resolve_registry_path() == env_registry
    assert resolve_tool("fake")["entrypoint"] == str(env_exe)
    assert resolve_registry_path(explicit_registry) == explicit_registry
    assert resolve_tool("fake", explicit_registry)["entrypoint"] == str(explicit_exe)

    monkeypatch.setenv(REGISTRY_ENV_VAR, "   ")
    assert resolve_registry_path() == DEFAULT_REGISTRY_PATH
    with pytest.raises(ToolRegistryError) as exc_info:
        resolve_registry_path("   ")
    assert exc_info.value.code == "REGISTRY_PATH_INVALID"


def test_request_shaped_data_cannot_select_tool_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = _write_tool(tmp_path / "fake.py")
    registry = _registry(tmp_path, [_subprocess_tool(exe)])
    monkeypatch.setenv(REGISTRY_ENV_VAR, str(registry))

    payload = BluecadCandidateCreate.model_validate(
        {"brief_text": "single tube", "registry_path": "/request/controlled/tools.yaml"}
    )
    assert "registry_path" not in payload.model_dump()
    assert resolve_tool("fake")["entrypoint"] == str(exe)


def test_resolve_tool_fail_closed_errors_are_distinct(tmp_path: Path) -> None:
    exe = _write_tool(tmp_path / "fake.py")
    registry = _registry(tmp_path, [_subprocess_tool(exe, enabled=False)])

    with pytest.raises(ToolRegistryError) as unknown:
        resolve_tool("missing", registry)
    assert unknown.value.code == "TOOL_UNKNOWN"

    with pytest.raises(ToolRegistryError) as disabled:
        resolve_tool("fake", registry)
    assert disabled.value.code == "TOOL_DISABLED"

    missing_registry = _registry(tmp_path, [_subprocess_tool(tmp_path / "missing.py")])
    with pytest.raises(ToolRegistryError) as missing:
        resolve_tool("fake", missing_registry)
    assert missing.value.code == "TOOL_BINARY_MISSING"

    unhashed_registry = _registry(tmp_path, [_subprocess_tool(exe, binary_sha256=None)])
    with pytest.raises(ToolRegistryError) as unhashed:
        load_registry(unhashed_registry)
    assert unhashed.value.code == "UNHASHED_SUBPROCESS_TOOL"


def test_resolve_tool_detects_hash_mismatch_at_resolve_time(tmp_path: Path) -> None:
    exe = _write_tool(tmp_path / "fake.py")
    registry = _registry(tmp_path, [_subprocess_tool(exe)])
    exe.write_text(exe.read_text(encoding="utf-8") + "\n# changed\n", encoding="utf-8")

    with pytest.raises(ToolRegistryError) as exc_info:
        resolve_tool("fake", registry)
    assert exc_info.value.code == "TOOL_HASH_MISMATCH"


def test_run_tool_captures_output_cwd_and_timeout(tmp_path: Path) -> None:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    exe = _write_tool(tmp_path / "fake.py", "import pathlib\nprint('cwd=' + pathlib.Path.cwd().name)")
    registry = _registry(tmp_path, [_subprocess_tool(exe)])

    result = run_tool("fake", ["one", "two"], cwd, timeout=5, registry_path=registry)
    assert result.returncode == 0
    assert "cwd=cwd" in result.stdout
    assert "fake-tool:one,two" in result.stdout
    assert result.stderr == ""

    slow = _write_tool(tmp_path / "slow.py", "time.sleep(2)")
    slow_registry = _registry(tmp_path, [_subprocess_tool(slow)])
    timed_out = run_tool("fake", [], cwd, timeout=0.1, registry_path=slow_registry)
    assert timed_out.timed_out is True
    assert timed_out.code == "TIMEOUT"
    assert timed_out.returncode == 124


def test_run_tool_streams_before_exit_and_still_captures_output(tmp_path: Path) -> None:
    exe = _write_tool(tmp_path / "stream.py", "print('first', flush=True)\ntime.sleep(0.2)\nprint('second', flush=True)")
    registry = _registry(tmp_path, [_subprocess_tool(exe)])
    chunks: list[str] = []
    result = run_tool("fake", [], tmp_path, 2, registry, on_stdout=chunks.append)
    assert result.returncode == 0
    assert chunks[:2] == ["first\n", "second\n"]
    assert "".join(chunks) == result.stdout


def test_run_tool_stream_callback_can_cancel_before_process_exits(tmp_path: Path) -> None:
    from threading import Event

    exe = _write_tool(tmp_path / "stream.py", "print('first', flush=True)\ntime.sleep(2)")
    registry = _registry(tmp_path, [_subprocess_tool(exe)])
    cancel = Event()

    def on_stdout(chunk: str) -> None:
        assert chunk == "first\n"
        cancel.set()

    result = run_tool("fake", [], tmp_path, 1, registry, cancel=cancel, on_stdout=on_stdout)
    assert result.code == "CANCELLED"
    assert result.stdout == "first\n"


def test_boundary_consistency_negative_and_shipped_yaml() -> None:
    bad = _subprocess_tool(Path(sys.executable), integration_mode="in_process")
    bad["license"] = {"spdx": "GPL-2.0", "boundary": "C", "verified_date": "2026-07-03"}

    with pytest.raises(ToolRegistryError) as exc_info:
        validate_license_boundaries({"registry_version": "bluecad_tool_registry_v0_1", "tools": [bad]})
    assert exc_info.value.code == "LICENSE_BOUNDARY_VIOLATION"

    shipped = load_registry(Path(__file__).resolve().parents[3] / "configs" / "bluecad_tools.yaml")
    for tool in shipped["tools"]:
        if tool["license"]["boundary"] in {"C", "D"}:
            assert tool["integration_mode"] != "in_process"


def test_ci_forbidden_import_regex_matches_imports_not_comments() -> None:
    assert CI_FORBIDDEN_IMPORT_RE.search("import gmsh")
    assert CI_FORBIDDEN_IMPORT_RE.search("from gmsh import model")
    assert CI_FORBIDDEN_IMPORT_RE.search("import FreeCAD")
    assert CI_FORBIDDEN_IMPORT_RE.search("import calculix")
    assert not CI_FORBIDDEN_IMPORT_RE.search("# gmsh is GPL")
    assert not CI_FORBIDDEN_IMPORT_RE.search("print('import gmsh')")


def test_registry_check_cli_exit_codes(tmp_path: Path) -> None:
    exe = _write_tool(tmp_path / "fake.py")
    passing_registry = _registry(tmp_path, [_subprocess_tool(exe)])
    exit_code, output = check_registry(passing_registry)
    assert exit_code == 0
    assert f"registry\t{passing_registry}" in output
    assert "fake\tTrue\tok\tok" in output

    failing_registry = _registry(tmp_path, [_subprocess_tool(tmp_path / "missing.py")])
    exit_code, output = check_registry(failing_registry)
    assert exit_code == 1
    assert "TOOL_BINARY_MISSING" in output
