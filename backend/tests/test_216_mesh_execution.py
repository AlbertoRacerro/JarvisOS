"""Mesh preparation never advances to a solver without a checked mesh."""

import re
from pathlib import Path

from app.modules.bluecad.cfd_pipe import mesh_execution
from app.modules.bluecad.registry import ToolRunResult
from tests.test_216_cfd_pipe_case import FIXTURES, laminar


def test_checked_mesh_runs_pinned_tools_in_order(monkeypatch, tmp_path: Path) -> None:
    contract = laminar()
    seen: list[str] = []
    monkeypatch.setattr(mesh_execution, "resolve_tool", lambda tool_id, registry_path: {"version_pin": "v2412"})

    def run(tool_id, args, cwd, timeout, registry_path, *, cancel):
        seen.append(tool_id)
        assert cwd == tmp_path / "case"
        assert args[:2] == ["-case", str(cwd)]
        # The checked-in real log has a different mesh level. This mocked tool
        # output uses its measured quality values and this contract's cell count.
        output = (re.sub(r"(?m)^\s*cells:\s+\d+\s*$", f"    cells: {mesh_execution.mesh_plan(contract).cell_count}",
                         (FIXTURES / "checkMesh_ok.log").read_text())
                  if tool_id == "openfoam_checkmesh" else "mesh built")
        return ToolRunResult(0, output, "")

    monkeypatch.setattr(mesh_execution, "run_tool", run)
    result = mesh_execution.prepare_checked_mesh(contract, tmp_path / "case")
    assert seen == ["openfoam_blockmesh", "openfoam_checkmesh"]
    assert result.status == "accepted"
    assert result.check.accepted
    assert result.case_manifest is not None
    assert (tmp_path / "case" / "checkMesh.log").is_file()


def test_mesh_refusal_stops_before_solver_or_checkmesh(monkeypatch, tmp_path: Path) -> None:
    contract = laminar()
    monkeypatch.setattr(mesh_execution, "resolve_tool", lambda tool_id, registry_path: {"version_pin": "v2412"})
    seen: list[str] = []

    def run(tool_id, args, cwd, timeout, registry_path, *, cancel):
        seen.append(tool_id)
        return ToolRunResult(124, "partial", "", timed_out=True, code="TIMEOUT")

    monkeypatch.setattr(mesh_execution, "run_tool", run)
    result = mesh_execution.prepare_checked_mesh(contract, tmp_path / "case")
    assert seen == ["openfoam_blockmesh"]
    assert result.status == "refused"
    assert result.reason == "openfoam_blockmesh: TIMEOUT"
    assert not result.check.accepted


def test_mesh_plan_refusal_writes_nothing(monkeypatch, tmp_path: Path) -> None:
    contract = laminar(mesh_level=4)
    monkeypatch.setattr(mesh_execution, "plan_refusals", lambda contract, plan: ["cell budget exceeded"])
    monkeypatch.setattr(mesh_execution, "resolve_tool", lambda *args: (_ for _ in ()).throw(AssertionError("tool called")))
    result = mesh_execution.prepare_checked_mesh(contract, tmp_path / "case")
    assert result.status == "refused"
    assert "cell budget" in result.reason
    assert not (tmp_path / "case").exists()


def test_different_tool_pins_refuse_before_case_write(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(mesh_execution, "resolve_tool", lambda tool_id, registry_path: {
        "version_pin": "v2412" if tool_id == "openfoam_blockmesh" else "v2506"})
    result = mesh_execution.prepare_checked_mesh(laminar(), tmp_path / "case")
    assert result.status == "refused"
    assert result.reason == "mesh tools have different OpenFOAM version pins"
    assert not (tmp_path / "case").exists()


def test_parallel_case_manifest_records_processors_and_decompose_dict(monkeypatch, tmp_path: Path) -> None:
    contract = laminar()
    monkeypatch.setattr(mesh_execution, "resolve_tool", lambda tool_id, registry_path: {"version_pin": "v2412"})
    output = re.sub(r"(?m)^\s*cells:\s+\d+\s*$", f"    cells: {mesh_execution.mesh_plan(contract).cell_count}",
                    (FIXTURES / "checkMesh_ok.log").read_text())
    monkeypatch.setattr(mesh_execution, "run_tool", lambda tool_id, *a, **k: ToolRunResult(
        0, output if tool_id == "openfoam_checkmesh" else "mesh built", ""))
    result = mesh_execution.prepare_checked_mesh(contract, tmp_path / "case", processors=4)
    assert result.status == "accepted" and result.case_manifest["processors"] == 4
    assert "system/decomposeParDict" in result.case_manifest["files"]
    assert (tmp_path / "case" / "system" / "decomposeParDict").is_file()
    serial = mesh_execution.prepare_checked_mesh(contract, tmp_path / "serial")
    assert "processors" not in serial.case_manifest
    assert serial.case_manifest["contract_digest"] == result.case_manifest["contract_digest"]
