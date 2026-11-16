"""Offline and local proof tests for the headless engineering bench command (spec 282)."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.modules.bluecad.models import BuildResult
from app.modules.bluecad.result_adapters import (
    CHECK_BREP_VALID,
    CHECK_ELEMENT_QUALITY,
    CHECK_FORCE_EQUILIBRIUM,
    CHECK_SOLVER_COMPLETION,
    CHECK_VOLUME_CONSERVATION,
    CHECK_VOLUME_ELEMENTS,
    CHECK_WATERTIGHT,
)
from app.modules.engineering.bench import MAX_VIEW_CHARS, main
from app.modules.engineering.evaluator_contracts import (
    ResultCheck,
)

FIXTURES_DIR = Path(__file__).parent / "bluecad" / "fixtures"


def _make_absent_tools_registry(tmp_path: Path) -> Path:
    reg_path = tmp_path / "absent_tools.yaml"
    fake_bin = tmp_path / "missing_binary"
    fake_hash = "a" * 64

    tools = [
        {
            "id": "build123d",
            "kind": "cad_kernel",
            "integration_mode": "in_process",
            "version_pin": "0.11.1",
            "license": {"spdx": "Apache-2.0", "boundary": "A", "verified_date": "2026-07-03"},
            "enabled": False,
            "entrypoint": "build123d",
            "capabilities": ["brep"],
            "health_check": None,
        },
        {
            "id": "gmsh",
            "kind": "mesher",
            "integration_mode": "subprocess",
            "version_pin": "4.12.1",
            "license": {"spdx": "GPL-2.0-or-later", "boundary": "C", "verified_date": "2026-10-24"},
            "enabled": True,
            "entrypoint": str(fake_bin),
            "binary_sha256": fake_hash,
            "provenance_url": "https://example.com/gmsh",
            "capabilities": ["tet_mesh"],
            "health_check": None,
        },
        {
            "id": "calculix",
            "kind": "fem_solver",
            "integration_mode": "subprocess",
            "version_pin": "2.22",
            "license": {"spdx": "GPL-2.0", "boundary": "C", "verified_date": "2026-07-03"},
            "enabled": True,
            "entrypoint": str(fake_bin),
            "binary_sha256": fake_hash,
            "provenance_url": "https://example.com/calculix",
            "capabilities": ["static"],
            "health_check": None,
        },
    ]
    reg_path.write_text(yaml.safe_dump({"registry_version": "bluecad_tool_registry_v0_1", "tools": tools}), encoding="utf-8")
    return reg_path


def _make_working_tools_registry(tmp_path: Path) -> Path:
    reg_path = tmp_path / "tools.yaml"
    gmsh_bin = shutil.which("gmsh") or ""
    ccx_bin = shutil.which("ccx") or ""

    gmsh_hash = hashlib.sha256(Path(gmsh_bin).read_bytes()).hexdigest() if gmsh_bin and Path(gmsh_bin).is_file() else "0" * 64
    ccx_hash = hashlib.sha256(Path(ccx_bin).read_bytes()).hexdigest() if ccx_bin and Path(ccx_bin).is_file() else "0" * 64

    tools = [
        {
            "id": "build123d",
            "kind": "cad_kernel",
            "integration_mode": "in_process",
            "version_pin": "0.11.1",
            "license": {"spdx": "Apache-2.0", "boundary": "A", "verified_date": "2026-07-03"},
            "enabled": True,
            "entrypoint": "build123d",
            "capabilities": ["brep", "sweep", "boolean", "fillet", "step_export", "stl_export"],
            "health_check": f'{sys.executable} -c "import build123d"',
        },
        {
            "id": "gmsh",
            "kind": "mesher",
            "integration_mode": "subprocess",
            "version_pin": "4.12.1",
            "license": {"spdx": "GPL-2.0-or-later", "boundary": "C", "verified_date": "2026-10-24"},
            "enabled": bool(gmsh_bin),
            "entrypoint": gmsh_bin,
            "binary_sha256": gmsh_hash,
            "provenance_url": "https://packages.ubuntu.com/noble/gmsh",
            "capabilities": ["step_import", "tet_mesh", "physical_groups", "inp_export"],
            "health_check": f"{gmsh_bin} -version" if gmsh_bin else None,
        },
        {
            "id": "calculix",
            "kind": "fem_solver",
            "integration_mode": "subprocess",
            "version_pin": "2.22",
            "license": {"spdx": "GPL-2.0", "boundary": "C", "verified_date": "2026-07-03"},
            "enabled": bool(ccx_bin),
            "entrypoint": ccx_bin,
            "binary_sha256": ccx_hash,
            "provenance_url": "https://packages.ubuntu.com/noble/calculix-ccx",
            "capabilities": ["static", "modal", "thermal"],
            "health_check": None,
        },
    ]
    reg_path.write_text(yaml.safe_dump({"registry_version": "bluecad_tool_registry_v0_1", "tools": tools}), encoding="utf-8")
    return reg_path


def _valid_pipe_pressure_drop_request(ws_id: str = "main") -> dict[str, Any]:
    now = datetime.now(UTC)
    return {
        "schema_version": "engineering_evaluator.v1",
        "request_ref": {
            "authority_owner": "process_stack",
            "object_type": "evaluation_request",
            "object_id": "req-pipe-1",
            "workspace_id": ws_id,
            "revision": "1",
        },
        "evaluator_id": "fluids.pipe_pressure_drop",
        "subject_ref": {
            "authority_owner": "process_stack",
            "object_type": "material_state",
            "object_id": "water-25C",
            "workspace_id": ws_id,
            "revision": "1",
        },
        "inputs": [
            {"name": "density", "value": {"value": 997.0, "unit": "kg/m3"}},
            {"name": "dynamic_viscosity", "value": {"value": 0.00089, "unit": "Pa*s"}},
            {"name": "velocity", "value": {"value": 1.0, "unit": "m/s"}},
            {"name": "diameter", "value": {"value": 50.0, "unit": "mm"}},
            {"name": "length", "value": {"value": 10.0, "unit": "m"}},
            {"name": "roughness", "value": {"value": 0.0, "unit": "m"}},
        ],
        "backend_options": {},
        "requested_at": (now - timedelta(seconds=2)).isoformat(),
        "deadline_at": (now + timedelta(minutes=5)).isoformat(),
    }


def test_list_absent_tool_registry(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    absent_reg = _make_absent_tools_registry(tmp_path)
    exit_code = main(["list", "--tool-registry", str(absent_reg), "--root", str(tmp_path)])
    assert exit_code == 0
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert isinstance(data, list)
    assert len(data) == 9

    by_id = {row["evaluator_id"]: row for row in data}
    # Verify the six registry evaluators are present
    assert "fluids.pipe_pressure_drop" in by_id
    assert "ht.internal_convection" in by_id
    assert "coolprop.pure_fluid_properties" in by_id
    assert "dwsim.flowsheet" in by_id
    assert "openfoam_channel_v1" in by_id
    assert "photobioreactor.day_night" in by_id

    # Verify three BLUECAD evaluators are present and marked unavailable with reason codes
    solid_row = by_id["bluecad.solid"]
    mesh_row = by_id["bluecad.mesh"]
    fem_row = by_id["bluecad.fem"]

    assert solid_row["state"] in {"disabled", "not_installed"}
    assert solid_row["reason_code"] in {"TOOL_DISABLED", "BUILD123D_NOT_INSTALLED"}
    assert solid_row["request_kind"] == "geometry_spec"
    assert CHECK_BREP_VALID in solid_row["required_checks"]
    assert CHECK_WATERTIGHT in solid_row["required_checks"]
    assert CHECK_VOLUME_CONSERVATION in solid_row["advisory_checks"]

    assert mesh_row["state"] in {"not_installed", "unhealthy"}
    assert mesh_row["reason_code"] == "TOOL_BINARY_MISSING"
    assert mesh_row["request_kind"] == "analysis_spec"
    assert CHECK_VOLUME_ELEMENTS in mesh_row["required_checks"]
    assert CHECK_ELEMENT_QUALITY in mesh_row["required_checks"]

    assert fem_row["state"] in {"not_installed", "unhealthy"}
    assert fem_row["reason_code"] == "TOOL_BINARY_MISSING"
    assert fem_row["request_kind"] == "analysis_spec"
    assert CHECK_SOLVER_COMPLETION in fem_row["required_checks"]
    assert CHECK_FORCE_EQUILIBRIUM in fem_row["required_checks"]


def test_registry_evaluator_valid_and_invalid_run(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    req_data = _valid_pipe_pressure_drop_request()
    req_file = tmp_path / "request.json"
    req_file.write_text(json.dumps(req_data), encoding="utf-8")
    out_dir = tmp_path / "out_valid"

    # Valid run: exit code 1 because registry evaluators declare no required checks (verdict is 'unchecked')
    code = main(["run", "fluids.pipe_pressure_drop", str(req_file), "--out", str(out_dir), "--root", str(tmp_path)])
    assert code == 1
    captured = capsys.readouterr()
    view = json.loads(captured.out)
    assert view["evaluator_id"] == "fluids.pipe_pressure_drop"
    assert view["status"] == "succeeded"
    assert view["verdict"] == "unchecked"
    assert len(captured.out) <= MAX_VIEW_CHARS

    result_file = out_dir / "result.json"
    assert result_file.is_file()
    saved_res = json.loads(result_file.read_text(encoding="utf-8"))
    assert saved_res["verdict"] == "unchecked"
    assert saved_res["status"] == "succeeded"
    outputs = {o["name"]: o["value"]["value"] for o in saved_res["outputs"]}
    assert "pressure_drop" in outputs
    assert outputs["pressure_drop"] > 0

    # Invalid request: invalid field in EvaluationRequest
    invalid_req = copy.deepcopy(req_data)
    del invalid_req["request_ref"]["workspace_id"]  # missing required field
    invalid_file = tmp_path / "invalid_request.json"
    invalid_file.write_text(json.dumps(invalid_req), encoding="utf-8")
    out_invalid = tmp_path / "out_invalid"

    code_err = main(["run", "fluids.pipe_pressure_drop", str(invalid_file), "--out", str(out_invalid), "--root", str(tmp_path)])
    assert code_err == 2
    captured_err = capsys.readouterr()
    err_doc = json.loads(captured_err.out)
    assert err_doc["code"] == "INVALID_REQUEST"
    assert "workspace_id" in err_doc["hint"]
    assert not out_invalid.exists()  # does not run engine


def test_bluecad_chain_with_monkeypatched_adapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # 1. Monkeypatch solid build_geometry_spec_file
    tube_fixture = FIXTURES_DIR / "minimal_single_tube.json"
    assert tube_fixture.is_file()
    spec_json = tmp_path / "geometry_spec.json"
    spec_json.write_text(tube_fixture.read_text(encoding="utf-8"), encoding="utf-8")

    solid_dir = tmp_path / "solid_out"

    def mock_build_geometry_spec_file(path: Any, out_d: Any, *, timeout_s: float = 30.0) -> BuildResult:
        out = Path(out_d)
        out.mkdir(parents=True, exist_ok=True)
        manifest = {
            "assembly": {
                "total_volume_mm3": 12566.37,
                "bbox_mm": {"min": [0.0, 0.0, 0.0], "max": [10.0, 10.0, 50.0]},
                "kernel_checks": {"tube": {"brep_valid": True, "manifold": True}},
            }
        }
        (out / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
        (out / "validation_report.json").write_text(
            json.dumps(
                {
                    "checks": [
                        {"id": "T1_BREP_VALID", "status": "pass"},
                        {"id": "T1_WATERTIGHT", "status": "pass"},
                        {
                            "id": "T1_VOLUME_DECL",
                            "status": "pass",
                            "detail": {"rel_error": 0.0001, "rel_tol": 0.01},
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )
        (out / "model.step").write_text("ISO-10303-21;\nEND-ISO-10303-21;\n", encoding="utf-8")
        return BuildResult(
            spec_id="sha256:" + "1" * 64,
            out_dir=out,
            manifest_path=out / "manifest.json",
            report_path=out / "validation_report.json",
            manifest=manifest,
            report={
                "checks": [
                    {"id": "T1_BREP_VALID", "status": "pass"},
                    {"id": "T1_WATERTIGHT", "status": "pass"},
                    {
                        "id": "T1_VOLUME_DECL",
                        "status": "pass",
                        "detail": {"rel_error": 0.0001, "rel_tol": 0.01},
                    },
                ]
            },
            verdict="pass",
            errors=[],
        )

    import app.modules.engineering.bench as bench_module

    monkeypatch.setattr(bench_module, "build_geometry_spec_file", mock_build_geometry_spec_file)

    code_solid = main(["run", "bluecad.solid", str(spec_json), "--out", str(solid_dir), "--root", str(tmp_path)])
    assert code_solid == 0
    captured = capsys.readouterr()
    view_solid = json.loads(captured.out)
    assert view_solid["verdict"] == "usable"
    assert (solid_dir / "result.json").is_file()

    # 2. Monkeypatch mesh_analysis_spec
    mesh_spec_data = {
        "schema_version": "bluecad_analysis_spec_v0_1",
        "analysis_id": "test-mesh",
        "geometry": {
            "step_path": "model.step",  # relative to request file
            "manifest_path": "manifest.json",
        },
        "mesh": {"target_size": 10.0},
    }
    mesh_req_file = solid_dir / "mesh_spec.json"
    mesh_req_file.write_text(json.dumps(mesh_spec_data), encoding="utf-8")
    mesh_dir = tmp_path / "mesh_out"

    def mock_mesh_analysis_spec(analysis_spec: Any, out_d: Any, **kwargs: Any) -> dict[str, Any]:
        out = Path(out_d)
        out.mkdir(parents=True, exist_ok=True)
        inp = out / "mesh.inp"
        inp.write_text("*NODE\n1, 0, 0, 0\n*ELEMENT\n1, 1\n", encoding="utf-8")
        inp_hash = hashlib.sha256(inp.read_bytes()).hexdigest()
        log_file = out / "gmsh.log"
        log_file.write_text("Info: worst = 0.45 / average = 0.72\n", encoding="utf-8")
        return {
            "schema_version": "bluecad_mesh_result_v0_1",
            "verdict": "pass",
            "attempts": [
                {
                    "counts": {"elements_total": 500, "nodes_total": 120},
                    "artifacts": {"gmsh_log": {"path": str(log_file), "sha256": "sha256:" + "2" * 64}},
                }
            ],
            "artifacts": {
                "mesh_inp": {"path": str(inp), "sha256": f"sha256:{inp_hash}"},
                "gmsh_log": {"path": str(log_file), "sha256": "sha256:" + "2" * 64},
            },
        }

    monkeypatch.setattr(bench_module, "mesh_analysis_spec", mock_mesh_analysis_spec)

    code_mesh = main(["run", "bluecad.mesh", str(mesh_req_file), "--out", str(mesh_dir), "--root", str(tmp_path)])
    assert code_mesh == 0
    captured = capsys.readouterr()
    view_mesh = json.loads(captured.out)
    assert view_mesh["verdict"] == "usable"
    assert (mesh_dir / "result.json").is_file()
    assert (mesh_dir / "mesh_result.json").is_file()

    # 3. Monkeypatch solve_static_analysis
    fem_spec_data = {
        "schema_version": "bluecad_analysis_spec_v0_1",
        "analysis_id": "test-fem",
        "bcs": [{"port_label": "p1", "kind": "fixed"}],
        "loads": [{"type": "force_total", "force": [0.0, 0.0, -100.0]}],
    }
    fem_req_file = tmp_path / "fem_spec.json"
    fem_req_file.write_text(json.dumps(fem_spec_data), encoding="utf-8")
    fem_dir = tmp_path / "fem_out"

    def mock_solve_static_analysis(spec: Any, mesh_res: Any, out_d: Any, **kwargs: Any) -> dict[str, Any]:
        out = Path(out_d)
        out.mkdir(parents=True, exist_ok=True)
        return {
            "verdict": "pass",
            "max_displacement": {"value": 0.025, "unit": "mm"},
            "max_von_mises": {"value": 45.2, "unit": "MPa"},
            "reactions": [{"force": [0.0, 0.0, 100.0]}],
            "reaction_resultant": [0.0, 0.0, 100.0],
            "artifacts": {},
        }

    monkeypatch.setattr(bench_module, "solve_static_analysis", mock_solve_static_analysis)

    code_fem = main(["run", "bluecad.fem", str(fem_req_file), "--out", str(fem_dir), "--from", str(mesh_dir), "--root", str(tmp_path)])
    assert code_fem == 0
    captured = capsys.readouterr()
    view_fem = json.loads(captured.out)
    assert view_fem["verdict"] == "usable"
    assert (fem_dir / "result.json").is_file()
    assert (fem_dir / "fem_result.json").is_file()


def test_path_boundary_and_out_refusals(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    outside_root = tmp_path.parent / "outside_root.json"
    outside_root.write_text("{}", encoding="utf-8")
    out_dir = tmp_path / "out"

    # Request outside root
    code1 = main(["run", "fluids.pipe_pressure_drop", str(outside_root), "--out", str(out_dir), "--root", str(tmp_path)])
    assert code1 == 2
    err1 = json.loads(capsys.readouterr().out)
    assert err1["code"] == "PATH_OUTSIDE_ROOT"

    # --out outside root
    inside_req = tmp_path / "req.json"
    inside_req.write_text("{}", encoding="utf-8")
    outside_out = tmp_path.parent / "outside_out"
    code2 = main(["run", "fluids.pipe_pressure_drop", str(inside_req), "--out", str(outside_out), "--root", str(tmp_path)])
    assert code2 == 2
    err2 = json.loads(capsys.readouterr().out)
    assert err2["code"] == "PATH_OUTSIDE_ROOT"

    # Non-empty --out
    existing_out = tmp_path / "existing_out"
    existing_out.mkdir()
    (existing_out / "some_file.txt").write_text("existing", encoding="utf-8")
    code3 = main(["run", "fluids.pipe_pressure_drop", str(inside_req), "--out", str(existing_out), "--root", str(tmp_path)])
    assert code3 == 2
    err3 = json.loads(capsys.readouterr().out)
    assert err3["code"] == "OUT_DIR_NOT_EMPTY"

    # bluecad.fem missing --from
    fem_req = tmp_path / "fem.json"
    fem_req.write_text("{}", encoding="utf-8")
    out_fresh = tmp_path / "out_fresh"
    code4 = main(["run", "bluecad.fem", str(fem_req), "--out", str(out_fresh), "--root", str(tmp_path)])
    assert code4 == 2
    err4 = json.loads(capsys.readouterr().out)
    assert err4["code"] == "MISSING_FROM_DIR"


def test_truncation_bound(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    # Create a result with a large number of checks and outputs exceeding 8000 chars
    out_dir = tmp_path / "out_huge"
    out_dir.mkdir()
    req_ref = {
        "authority_owner": "test",
        "object_type": "evaluation_request",
        "object_id": "r1",
        "workspace_id": "ws",
        "revision": "1",
    }
    checks = []
    # 1 fail check (must be preserved)
    checks.append(
        ResultCheck(
            id="FAIL_CHECK",
            family="input",
            status="fail",
            hint="Critical failure detected in input boundary",
            source="physical_limit",
        ).model_dump(mode="json")
    )
    # 1 warn check (must be preserved)
    checks.append(
        ResultCheck(
            id="WARN_CHECK",
            family="numerical",
            status="warn",
            hint="Warning residual threshold approached",
            source="policy:solver_completion",
        ).model_dump(mode="json")
    )
    # 200 passing checks (should be cut on truncation)
    for i in range(200):
        checks.append(
            ResultCheck(
                id=f"PASS_CHECK_{i}",
                family="plausibility",
                status="pass",
                hint=f"Check {i} satisfied with generous safety factor and strict boundary adherence",
                source="policy:solver_completion",
            ).model_dump(mode="json")
        )

    res_doc = {
        "schema_version": "engineering_evaluator.v1",
        "result_ref": {
            "authority_owner": "test",
            "object_type": "evaluation_result",
            "object_id": "res-1",
            "workspace_id": "ws",
            "revision": "1",
        },
        "request_ref": req_ref,
        "evaluator_id": "test.evaluator",
        "backend_version": "1.0",
        "status": "succeeded",
        "verdict": "not_usable",
        "fidelity": "screening",
        "checks": checks,
        "outputs": [{"name": f"output_{i}", "value": {"value": float(i), "unit": "m"}} for i in range(50)],
        "output_artifacts": [],
        "completed_at": datetime.now(UTC).isoformat(),
    }
    (out_dir / "result.json").write_text(json.dumps(res_doc, indent=2), encoding="utf-8")

    from app.modules.engineering.bench import format_bounded_view

    view_str = format_bounded_view(
        evaluator_id="test.evaluator",
        status="succeeded",
        verdict="not_usable",
        failure=None,
        checks=checks,
        outputs=res_doc["outputs"],
        artifacts=[],
        result_path=out_dir / "result.json",
    )

    assert len(view_str) <= MAX_VIEW_CHARS
    doc = json.loads(view_str)
    assert doc["truncated"] is True
    assert doc["evaluator_id"] == "test.evaluator"
    assert doc["status"] == "succeeded"
    assert doc["verdict"] == "not_usable"
    # Preserved failed and warning checks
    assert any(c["id"] == "FAIL_CHECK" for c in doc["failed_checks"])
    assert any(c["id"] == "WARN_CHECK" for c in doc["warning_checks"])
    assert str(out_dir / "result.json") == doc["result_path"]


def test_compare_matching_mismatched_and_missing_outputs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dir_a = tmp_path / "dir_a"
    dir_b = tmp_path / "dir_b"
    dir_a.mkdir()
    dir_b.mkdir()

    res_a = {
        "evaluator_id": "eval.one",
        "status": "succeeded",
        "verdict": "usable",
        "outputs": [
            {"name": "match_metric", "value": {"value": 10.0, "unit": "mm"}},
            {"name": "mismatch_metric", "value": {"value": 5.0, "unit": "mm"}},
            {"name": "a_only_metric", "value": {"value": 42.0, "unit": "Pa"}},
        ],
        "checks": [
            {"id": "CHECK_1", "status": "pass", "actual": 1.0},
            {"id": "CHECK_ONLY_A", "status": "pass", "actual": 2.0},
        ],
    }
    res_b = {
        "evaluator_id": "eval.one",
        "status": "succeeded",
        "verdict": "usable_with_warnings",
        "outputs": [
            {"name": "match_metric", "value": {"value": 12.0, "unit": "mm"}},
            {"name": "mismatch_metric", "value": {"value": 5.0, "unit": "m"}},  # unit mismatch
            {"name": "b_only_metric", "value": {"value": 99.0, "unit": "J"}},
        ],
        "checks": [
            {"id": "CHECK_1", "status": "warn", "actual": 1.5},
            {"id": "CHECK_ONLY_B", "status": "fail", "actual": 9.0},
        ],
    }

    (dir_a / "result.json").write_text(json.dumps(res_a), encoding="utf-8")
    (dir_b / "result.json").write_text(json.dumps(res_b), encoding="utf-8")

    code = main(["compare", str(dir_a), str(dir_b), "--root", str(tmp_path)])
    assert code == 0
    captured = capsys.readouterr()
    diff = json.loads(captured.out)

    assert diff["evaluator_a"] == "eval.one"
    assert diff["verdict_a"] == "usable"
    assert diff["verdict_b"] == "usable_with_warnings"

    outs = diff["outputs"]
    # Matching
    assert outs["match_metric"]["mark"] == "match"
    assert outs["match_metric"]["unit"] == "mm"
    assert outs["match_metric"]["absolute_difference"] == pytest.approx(2.0)
    assert outs["match_metric"]["relative_difference"] == pytest.approx(0.2)

    # Unit mismatch
    assert outs["mismatch_metric"]["mark"] == "unit_mismatch"
    assert outs["mismatch_metric"]["absolute_difference"] is None

    # Missing
    assert outs["a_only_metric"]["mark"] == "missing"
    assert outs["b_only_metric"]["mark"] == "missing"

    # Checks
    checks = diff["checks"]
    assert checks["CHECK_1"]["status_a"] == "pass"
    assert checks["CHECK_1"]["status_b"] == "warn"
    assert checks["CHECK_ONLY_A"]["status_b"] is None
    assert checks["CHECK_ONLY_B"]["status_a"] is None

    # Error reading compare dir
    non_existent = tmp_path / "non_existent"
    code_err = main(["compare", str(dir_a), str(non_existent), "--root", str(tmp_path)])
    assert code_err == 2
    err_doc = json.loads(capsys.readouterr().out)
    assert err_doc["code"] == "INPUT_NOT_FOUND"


def test_real_tools_engine_proof_criterion_7(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Acceptance criterion 7: Real solid -> mesh -> FEM chain with installed tools."""
    pytest.importorskip("build123d", reason="build123d is not installed; solid engine path not proved")
    if not shutil.which("gmsh") or not shutil.which("ccx"):
        pytest.skip("gmsh or ccx is not installed; mesh and FEM engine paths not proved")
    tool_reg = _make_working_tools_registry(tmp_path)
    tube_fixture = FIXTURES_DIR / "minimal_single_tube.json"
    assert tube_fixture.is_file(), "minimal_single_tube.json fixture required"

    spec_json = tmp_path / "tube_geom.json"
    spec_json.write_text(tube_fixture.read_text(encoding="utf-8"), encoding="utf-8")

    # 1. Run solid
    solid_out = tmp_path / "solid_real"
    c_solid = main(["run", "bluecad.solid", str(spec_json), "--out", str(solid_out), "--root", str(tmp_path), "--tool-registry", str(tool_reg)])
    assert c_solid == 0
    res_solid = json.loads((solid_out / "result.json").read_text(encoding="utf-8"))
    assert res_solid["status"] == "succeeded"
    assert res_solid["verdict"] == "usable"

    # 2. Run mesh
    mesh_spec = {
        "schema_version": "bluecad_analysis_spec_v0_1",
        "analysis_id": "single-tube-real-bench",
        "analysis_type": "static",
        "geometry": {
            "step_path": str(solid_out / "model.step"),
            "manifest_path": str(solid_out / "manifest.json"),
        },
        "material": {"name": "steel", "E": 200000.0, "nu": 0.3, "rho": 7.8e-9, "yield_strength": 250.0},
        "bcs": [{"port_label": "tube1.port_a", "kind": "fixed"}],
        "loads": [{"port_label": "tube1.port_b", "type": "force_total", "force": [100.0, 0.0, 0.0]}],
        "mesh": {"target_size": 50.0},
        "pass_criteria": [{"metric": "max_displacement", "op": "<=", "value": 100.0}],
        "timeout_s": 60.0,
    }
    mesh_spec_file = tmp_path / "mesh_spec.json"
    mesh_spec_file.write_text(json.dumps(mesh_spec), encoding="utf-8")

    mesh_out = tmp_path / "mesh_real"
    c_mesh = main(["run", "bluecad.mesh", str(mesh_spec_file), "--out", str(mesh_out), "--root", str(tmp_path), "--tool-registry", str(tool_reg)])
    assert c_mesh == 0
    res_mesh = json.loads((mesh_out / "result.json").read_text(encoding="utf-8"))
    assert res_mesh["status"] == "succeeded"
    assert res_mesh["verdict"] == "usable"

    # 3. Run FEM
    fem_out = tmp_path / "fem_real"
    c_fem = main(["run", "bluecad.fem", str(mesh_spec_file), "--out", str(fem_out), "--from", str(mesh_out), "--root", str(tmp_path), "--tool-registry", str(tool_reg)])
    assert c_fem == 0
    res_fem = json.loads((fem_out / "result.json").read_text(encoding="utf-8"))
    assert res_fem["status"] == "succeeded"
    assert res_fem["verdict"] == "usable"


def test_view_with_many_failed_checks_stays_valid_json() -> None:
    from app.modules.engineering.bench import format_bounded_view

    checks = [
        ResultCheck(id=f"FAIL_{i}", family="input", status="fail", actual=float(i), limit=1.0, unit="m",
                    hint="x" * 200, source="physical_limit").model_dump(mode="json")
        for i in range(64)
    ]
    view = format_bounded_view(evaluator_id="e", status="succeeded", verdict="not_usable", failure=None,
                               checks=checks, outputs=[], artifacts=[], result_path=Path("result.json"))
    assert len(view) <= MAX_VIEW_CHARS
    doc = json.loads(view)
    assert doc["truncated"] is True
    assert [c["id"] for c in doc["failed_checks"]] == [f"FAIL_{i}" for i in range(64)]
    assert doc["failed_checks"][3]["actual"] == 3.0


def test_fem_refuses_mesh_artifact_outside_root(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    import app.modules.engineering.bench as bench_module

    root = tmp_path / "root"
    mesh_dir = root / "mesh"
    mesh_dir.mkdir(parents=True)
    outside = tmp_path / "elsewhere.inp"
    outside.write_text("*NODE\n", encoding="utf-8")
    (mesh_dir / "mesh_result.json").write_text(
        json.dumps({"artifacts": {"mesh_inp": {"path": str(outside)}}}), encoding="utf-8")
    spec = root / "spec.json"
    spec.write_text(json.dumps({"analysis_type": "static"}), encoding="utf-8")
    called: list[bool] = []
    monkeypatch.setattr(bench_module, "solve_static_analysis", lambda *a, **k: called.append(True))

    code = main(["run", "bluecad.fem", str(spec), "--out", str(root / "fem"), "--from", str(mesh_dir),
                 "--root", str(root)])

    assert code == 2
    assert json.loads(capsys.readouterr().out)["code"] == "PATH_OUTSIDE_ROOT"
    assert called == []


def test_engine_exception_is_json_error(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    import app.modules.engineering.bench as bench_module

    spec = tmp_path / "geom.json"
    spec.write_text("{}", encoding="utf-8")

    def boom(*args: object, **kwargs: object) -> None:
        raise KeyError("geometry")

    monkeypatch.setattr(bench_module, "build_geometry_spec_file", boom)
    code = main(["run", "bluecad.solid", str(spec), "--out", str(tmp_path / "out"), "--root", str(tmp_path)])

    assert code == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["code"] == "ENGINE_ERROR_KEYERROR"
    assert "\n" not in doc["hint"]
