"""Offline tests for BLUECAD solid, mesh, and static FEM EvaluationResult adapters (spec 281)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from app.modules.bluecad.export import sha256_file
from app.modules.bluecad.models import BuildResult
from app.modules.bluecad.result_adapters import (
    CHECK_BBOX_DECL,
    CHECK_BREP_VALID,
    CHECK_ELEMENT_QUALITY,
    CHECK_FORCE_EQUILIBRIUM,
    CHECK_MAX_EXTENT,
    CHECK_SOLVER_COMPLETION,
    CHECK_TOPOLOGY,
    CHECK_VOLUME_CONSERVATION,
    CHECK_VOLUME_ELEMENTS,
    CHECK_VOLUME_MAX,
    CHECK_VOLUME_MIN,
    CHECK_WATERTIGHT,
    FEM_EVALUATOR_ID,
    MESH_EVALUATOR_ID,
    MESH_QUALITY_POLICY_LIMIT,
    MESH_QUALITY_POLICY_SOURCE,
    SOLID_BBOX_SOURCE,
    SOLID_CODE_EVALUATOR_ID,
    SOLID_EVALUATOR_ID,
    SOLID_NATIVE_VALIDATION_SOURCE,
    build_solid_code_manifest,
    fem_evaluator_descriptor,
    fem_result_to_evaluation_result,
    mesh_evaluator_descriptor,
    mesh_result_to_evaluation_result,
    solid_build_result_to_evaluation_result,
    solid_code_evaluator_descriptor,
    solid_code_run_record_to_evaluation_result,
    solid_evaluator_descriptor,
)
from app.modules.engineering.evaluator_contracts import (
    EvaluationResult,
    result_verdict,
)
from app.modules.engineering.operator_service import evaluator_registry
from app.modules.studio.geometry import GeometryCase
from app.modules.studio_runtime.runtime import AppliedLimits, Artifact, RunRecord


def _mock_build_result(
    tmp_path: Path,
    *,
    total_volume: float = 1000.0,
    bbox_min: tuple[float, float, float] = (0.0, 0.0, 0.0),
    bbox_max: tuple[float, float, float] = (10.0, 10.0, 10.0),
    brep_valid: bool = True,
    manifold: bool = True,
    declared_volume_rel_error: float | None = None,
    declared_volume_rel_tol: float = 0.001,
    declared_bbox_max_abs_error: float | None = None,
    declared_bbox_abs_tol: float = 0.1,
    extra_checks: list[dict[str, Any]] | None = None,
    verdict: str = "pass",
    errors: list[dict[str, Any]] | None = None,
) -> BuildResult:
    out_dir = tmp_path / "build_out"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = out_dir / "manifest.json"
    report_path = out_dir / "validation_report.json"
    step_path = out_dir / "model.step"
    step_path.write_text("ISO-10303-21;", encoding="utf-8")

    manifest = {
        "assembly": {
            "total_volume_mm3": total_volume,
            "bbox_mm": {"min": list(bbox_min), "max": list(bbox_max)},
            "kernel_checks": {
                "part1": {"brep_valid": brep_valid, "manifold": manifold},
            },
        },
        "parts": {},
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    checks: list[dict[str, Any]] = [
        {"id": "T1_BREP_VALID", "status": "pass" if brep_valid else "fail"},
        {"id": "T1_WATERTIGHT", "status": "pass" if manifold else "fail"},
    ]
    if declared_volume_rel_error is not None:
        checks.append(
            {
                "id": "T1_VOLUME_DECL",
                "status": "pass" if declared_volume_rel_error <= declared_volume_rel_tol else "fail",
                "detail": {
                    "actual": total_volume,
                    "rel_error": declared_volume_rel_error,
                    "rel_tol": declared_volume_rel_tol,
                },
            }
        )
    if declared_bbox_max_abs_error is not None:
        checks.append(
            {
                "id": "T1_BBOX_DECL",
                "tier": 1,
                "status": "pass" if declared_bbox_max_abs_error <= declared_bbox_abs_tol else "fail",
                "detail": {
                    "actual": {"min": list(bbox_min), "max": list(bbox_max)},
                    "declared": {"min": list(bbox_min), "max": [x + declared_bbox_max_abs_error for x in bbox_max]},
                    "abs_tol": declared_bbox_abs_tol,
                    "max_abs_error": declared_bbox_max_abs_error,
                },
                "hint": f"actual bbox max absolute error {declared_bbox_max_abs_error:.6g}; tolerance {declared_bbox_abs_tol:.6g}",
            }
        )
    if extra_checks:
        checks.extend(extra_checks)

    report = {
        "report_version": "bluecad_validation_report_v0_1",
        "spec_id": "sha256:testspec",
        "verdict": verdict,
        "checks": checks,
        "errors": errors or [],
    }
    report_path.write_text(json.dumps(report), encoding="utf-8")

    return BuildResult(
        spec_id="testspec",
        out_dir=out_dir,
        manifest_path=manifest_path,
        report_path=report_path,
        manifest=manifest if not errors or manifest else None,
        report=report,
        verdict=verdict,  # type: ignore[arg-type]
        errors=errors or [],
    )


# ---------------------------------------------------------------------------
# Solids tests
# ---------------------------------------------------------------------------


def test_solid_adapter_valid_solid_absent_declared_volume_yields_usable_with_warnings(
    tmp_path: Path,
) -> None:
    build_result = _mock_build_result(tmp_path, total_volume=125000.0)

    res = solid_build_result_to_evaluation_result(build_result)
    desc = solid_evaluator_descriptor()

    assert res.evaluator_id == SOLID_EVALUATOR_ID
    assert res.status == "succeeded"
    assert res.fidelity == "screening"

    # Outputs contain measured volume and bounding box
    outputs_by_name = {o.name: o.value for o in res.outputs}
    assert outputs_by_name["total_volume"].value == 125000.0
    assert outputs_by_name["total_volume"].unit == "mm**3"
    assert outputs_by_name["bbox_min_x"].value == 0.0
    assert outputs_by_name["bbox_max_x"].value == 10.0
    assert outputs_by_name["bbox_dx"].value == 10.0

    # Checks
    checks_by_id = {c.id: c for c in res.checks}
    assert checks_by_id[CHECK_BREP_VALID].status == "pass"
    assert checks_by_id[CHECK_WATERTIGHT].status == "pass"
    assert checks_by_id[CHECK_VOLUME_CONSERVATION].status == "not_evaluated"
    assert checks_by_id[CHECK_VOLUME_CONSERVATION].actual is None
    assert checks_by_id[CHECK_VOLUME_CONSERVATION].limit is None

    # Verdict derivation
    verdict = result_verdict(desc, res)
    assert verdict == "usable_with_warnings"

    # Preserves artifacts
    artifact_names = {a.object_id for a in res.output_artifacts}
    assert "manifest.json" in artifact_names
    assert "validation_report.json" in artifact_names
    assert "model.step" in artifact_names

    # Round trip
    dumped = res.model_dump(mode="json")
    loaded = EvaluationResult.model_validate(dumped)
    assert loaded == res


def test_solid_adapter_valid_declared_volume_yields_usable(tmp_path: Path) -> None:
    build_result = _mock_build_result(
        tmp_path,
        total_volume=1000.0,
        declared_volume_rel_error=0.0002,
        declared_volume_rel_tol=0.001,
    )

    res = solid_build_result_to_evaluation_result(build_result)
    desc = solid_evaluator_descriptor()

    checks_by_id = {c.id: c for c in res.checks}
    assert checks_by_id[CHECK_BREP_VALID].status == "pass"
    assert checks_by_id[CHECK_WATERTIGHT].status == "pass"
    assert checks_by_id[CHECK_VOLUME_CONSERVATION].status == "pass"
    assert checks_by_id[CHECK_VOLUME_CONSERVATION].actual == 0.0002
    assert checks_by_id[CHECK_VOLUME_CONSERVATION].limit == 0.001

    assert result_verdict(desc, res) == "usable"


def test_solid_adapter_false_declared_volume_fails_without_changing_measured_volume(
    tmp_path: Path,
) -> None:
    build_result = _mock_build_result(
        tmp_path,
        total_volume=1000.0,
        declared_volume_rel_error=0.15,
        declared_volume_rel_tol=0.001,
    )

    res = solid_build_result_to_evaluation_result(build_result)
    desc = solid_evaluator_descriptor()

    # Measured volume is unchanged
    outputs_by_name = {o.name: o.value for o in res.outputs}
    assert outputs_by_name["total_volume"].value == 1000.0

    checks_by_id = {c.id: c for c in res.checks}
    assert checks_by_id[CHECK_VOLUME_CONSERVATION].status == "fail"
    assert checks_by_id[CHECK_VOLUME_CONSERVATION].actual == 0.15

    # Result verdict is not_usable
    assert result_verdict(desc, res) == "not_usable"


def test_solid_adapter_missing_evidence_yields_unchecked_and_omits_missing_outputs(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "missing_evidence"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"assembly": {}, "parts": {}}
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = {
        "report_version": "bluecad_validation_report_v0_1",
        "spec_id": "sha256:missing",
        "verdict": "pass",
        "checks": [],
        "errors": [],
    }
    build_result = BuildResult(
        spec_id="missing",
        out_dir=out_dir,
        manifest_path=manifest_path,
        report_path=None,
        manifest=manifest,
        report=report,
        verdict="pass",
        errors=[],
    )

    res = solid_build_result_to_evaluation_result(build_result)
    desc = solid_evaluator_descriptor()

    # When manifest lacks total_volume_mm3 or bbox_mm, those outputs are omitted (not 0.0)
    assert res.outputs == ()

    # When report lacks T1_BREP_VALID/T1_WATERTIGHT and no kernel_checks, emit not_evaluated
    checks_by_id = {c.id: c for c in res.checks}
    assert checks_by_id[CHECK_BREP_VALID].status == "not_evaluated"
    assert checks_by_id[CHECK_WATERTIGHT].status == "not_evaluated"
    assert checks_by_id[CHECK_VOLUME_CONSERVATION].status == "not_evaluated"

    # Required checks not_evaluated yields unchecked
    assert result_verdict(desc, res) == "unchecked"


def test_solid_adapter_non_finite_or_missing_rel_error_yields_not_evaluated(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "bad_rel_error"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "assembly": {
            "total_volume_mm3": 1000.0,
            "kernel_checks": {"p1": {"brep_valid": True, "manifold": True}},
        },
        "parts": {},
    }
    report = {
        "report_version": "bluecad_validation_report_v0_1",
        "spec_id": "sha256:bad_rel_err",
        "verdict": "pass",
        "checks": [
            {"id": "T1_BREP_VALID", "status": "pass"},
            {"id": "T1_WATERTIGHT", "status": "pass"},
            {"id": "T1_VOLUME_DECL", "status": "pass", "detail": {"rel_error": None, "rel_tol": 0.001}},
        ],
        "errors": [],
    }
    build_result = BuildResult(
        spec_id="bad_rel_err",
        out_dir=out_dir,
        manifest_path=None,
        report_path=None,
        manifest=manifest,
        report=report,
        verdict="pass",
        errors=[],
    )

    res = solid_build_result_to_evaluation_result(build_result)
    checks_by_id = {c.id: c for c in res.checks}
    assert checks_by_id[CHECK_VOLUME_CONSERVATION].status == "not_evaluated"


def test_solid_adapter_bad_brep_or_open_solid_yields_not_usable(tmp_path: Path) -> None:
    desc = solid_evaluator_descriptor()

    # Bad B-rep
    bad_brep_res = _mock_build_result(tmp_path / "brep", brep_valid=False)
    res1 = solid_build_result_to_evaluation_result(bad_brep_res)
    assert {c.id: c.status for c in res1.checks}[CHECK_BREP_VALID] == "fail"
    assert result_verdict(desc, res1) == "not_usable"

    # Open solid (non-manifold)
    open_res = _mock_build_result(tmp_path / "open", manifold=False)
    res2 = solid_build_result_to_evaluation_result(open_res)
    assert {c.id: c.status for c in res2.checks}[CHECK_WATERTIGHT] == "fail"
    assert result_verdict(desc, res2) == "not_usable"


def test_solid_adapter_failure_paths_preserve_native_codes(tmp_path: Path) -> None:
    desc = solid_evaluator_descriptor()

    # Timeout
    timeout_build = BuildResult(
        spec_id="timeout-spec",
        out_dir=tmp_path / "timeout",
        manifest_path=None,
        report_path=None,
        manifest=None,
        report={"verdict": "error", "errors": [{"code": "TIMEOUT", "detail": {"timeout_s": 30}}]},
        verdict="error",
        errors=[{"code": "TIMEOUT", "detail": {"timeout_s": 30}}],
    )
    res_timeout = solid_build_result_to_evaluation_result(timeout_build)
    assert res_timeout.status == "deadline_exceeded"
    assert res_timeout.failure is not None
    assert res_timeout.failure.category == "timeout"
    assert res_timeout.failure.backend_code == "TIMEOUT"
    assert res_timeout.outputs == ()
    assert result_verdict(desc, res_timeout) == "not_usable"

    # Kernel error
    kernel_build = BuildResult(
        spec_id="kernel-spec",
        out_dir=tmp_path / "kernel",
        manifest_path=None,
        report_path=None,
        manifest=None,
        report={"verdict": "error", "errors": [{"code": "KERNEL_ERROR", "detail": {"message": "crash"}}]},
        verdict="error",
        errors=[{"code": "KERNEL_ERROR", "detail": {"message": "crash"}}],
    )
    res_kernel = solid_build_result_to_evaluation_result(kernel_build)
    assert res_kernel.status == "failed"
    assert res_kernel.failure is not None
    assert res_kernel.failure.category == "solver_crash"
    assert res_kernel.failure.backend_code == "KERNEL_ERROR"
    assert result_verdict(desc, res_kernel) == "not_usable"

    # Spec invalid (refusal)
    spec_inv_build = BuildResult(
        spec_id="inv-spec",
        out_dir=tmp_path / "inv",
        manifest_path=None,
        report_path=None,
        manifest=None,
        report={"verdict": "error", "errors": [{"code": "SPEC_INVALID", "detail": {"message": "bad spec"}}]},
        verdict="error",
        errors=[{"code": "SPEC_INVALID", "detail": {"message": "bad spec"}}],
    )
    res_inv = solid_build_result_to_evaluation_result(spec_inv_build)
    assert res_inv.status == "refused"
    assert res_inv.failure is not None
    assert res_inv.failure.category == "invalid_input"
    assert res_inv.failure.backend_code == "SPEC_INVALID"
    assert result_verdict(desc, res_inv) == "not_usable"


# ---------------------------------------------------------------------------
# Spec 284: native solid validation verdict repair tests
# ---------------------------------------------------------------------------


def test_solid_adapter_declared_bbox_failure_yields_not_usable_and_preserves_outputs(
    tmp_path: Path,
) -> None:
    build_result = _mock_build_result(
        tmp_path,
        total_volume=1000.0,
        bbox_min=(0.0, 0.0, 0.0),
        bbox_max=(10.0, 10.0, 10.0),
        declared_bbox_max_abs_error=2.5,
        declared_bbox_abs_tol=0.1,
        verdict="fail",
    )

    res = solid_build_result_to_evaluation_result(build_result)
    desc = solid_evaluator_descriptor()

    # Status remains succeeded (built shape exists)
    assert res.status == "succeeded"
    assert res.failure is None

    # Measured outputs are preserved
    outputs_by_name = {o.name: o.value for o in res.outputs}
    assert outputs_by_name["total_volume"].value == 1000.0
    assert outputs_by_name["bbox_min_x"].value == 0.0
    assert outputs_by_name["bbox_max_x"].value == 10.0
    assert outputs_by_name["bbox_dx"].value == 10.0

    # Checks expose failed bounding box check with actual, limit, unit="mm"
    checks_by_id = {c.id: c for c in res.checks}
    assert CHECK_BBOX_DECL in checks_by_id
    bbox_check = checks_by_id[CHECK_BBOX_DECL]
    assert bbox_check.status == "fail"
    assert bbox_check.actual == 2.5
    assert bbox_check.limit == 0.1
    assert bbox_check.unit == "mm"
    assert bbox_check.source == SOLID_BBOX_SOURCE

    # Derived verdict is not_usable
    assert result_verdict(desc, res) == "not_usable"

    # Artifacts (validation report, manifest, step) are preserved
    artifact_names = {a.object_id for a in res.output_artifacts}
    assert "manifest.json" in artifact_names
    assert "validation_report.json" in artifact_names
    assert "model.step" in artifact_names


def test_solid_adapter_unrecognized_native_failure_yields_not_usable_and_names_check(
    tmp_path: Path,
) -> None:
    extra = [
        {
            "id": "CUSTOM_SURFACE_CURVATURE",
            "tier": 2,
            "status": "fail",
            "hint": "minimum radius of curvature 0.5 mm below threshold 1.0 mm",
            "detail": {"actual": 0.5, "limit": 1.0, "unit": "mm"},
        }
    ]
    build_result = _mock_build_result(
        tmp_path,
        total_volume=500.0,
        extra_checks=extra,
        verdict="fail",
    )

    res = solid_build_result_to_evaluation_result(build_result)
    desc = solid_evaluator_descriptor()

    assert res.status == "succeeded"
    outputs_by_name = {o.name: o.value for o in res.outputs}
    assert outputs_by_name["total_volume"].value == 500.0

    checks_by_id = {c.id: c for c in res.checks}
    assert "CUSTOM_SURFACE_CURVATURE" in checks_by_id
    custom_check = checks_by_id["CUSTOM_SURFACE_CURVATURE"]
    assert custom_check.status == "fail"
    assert "CUSTOM_SURFACE_CURVATURE" in custom_check.hint or "radius" in custom_check.hint
    assert custom_check.source == SOLID_NATIVE_VALIDATION_SOURCE

    # Result verdict is not_usable
    assert result_verdict(desc, res) == "not_usable"


def test_solid_adapter_native_verdict_fail_without_explicit_check_fails_safe(
    tmp_path: Path,
) -> None:
    out_dir = tmp_path / "empty_fail"
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "assembly": {
            "total_volume_mm3": 100.0,
            "kernel_checks": {"part1": {"brep_valid": True, "manifold": True}},
        },
        "parts": {},
    }
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = {
        "report_version": "bluecad_validation_report_v0_1",
        "spec_id": "sha256:empty_fail",
        "verdict": "fail",
        "checks": [],
        "errors": [],
    }
    report_path = out_dir / "validation_report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    build_result = BuildResult(
        spec_id="empty_fail",
        out_dir=out_dir,
        manifest_path=manifest_path,
        report_path=report_path,
        manifest=manifest,
        report=report,
        verdict="fail",
        errors=[],
    )

    res = solid_build_result_to_evaluation_result(build_result)
    desc = solid_evaluator_descriptor()

    assert res.status == "succeeded"
    assert any(c.status == "fail" for c in res.checks)
    assert result_verdict(desc, res) == "not_usable"


def test_solid_adapter_passing_native_report_with_passing_bbox_preserves_verdict_and_outputs(
    tmp_path: Path,
) -> None:
    build_result = _mock_build_result(
        tmp_path,
        total_volume=1000.0,
        declared_bbox_max_abs_error=0.01,
        declared_bbox_abs_tol=0.1,
        verdict="pass",
    )

    res = solid_build_result_to_evaluation_result(build_result)
    desc = solid_evaluator_descriptor()

    assert res.status == "succeeded"
    outputs_by_name = {o.name: o.value for o in res.outputs}
    assert outputs_by_name["total_volume"].value == 1000.0

    # No failed checks; volume wasn't declared so VOLUME_CONSERVATION is not_evaluated
    assert not any(c.status == "fail" for c in res.checks)
    assert result_verdict(desc, res) == "usable_with_warnings"


def test_solid_adapter_declared_bbox_failure_without_finite_numbers_fails_safe(
    tmp_path: Path,
) -> None:
    extra = [
        {
            "id": "T1_BBOX_DECL",
            "tier": 1,
            "status": "fail",
            "detail": {"max_abs_error": None, "abs_tol": None},
            "hint": "declared bounding box comparison failed",
        }
    ]
    build_result = _mock_build_result(
        tmp_path,
        total_volume=800.0,
        extra_checks=extra,
        verdict="fail",
    )

    res = solid_build_result_to_evaluation_result(build_result)
    desc = solid_evaluator_descriptor()

    assert res.status == "succeeded"
    checks_by_id = {c.id: c for c in res.checks}
    assert CHECK_BBOX_DECL in checks_by_id
    c = checks_by_id[CHECK_BBOX_DECL]
    assert c.status == "fail"
    assert c.actual is None
    assert c.limit is None
    assert c.unit is None
    assert result_verdict(desc, res) == "not_usable"


def test_solid_adapter_declared_volume_failure_without_numbers_names_native_check(
    tmp_path: Path,
) -> None:
    build_result = _mock_build_result(
        tmp_path,
        total_volume=800.0,
        extra_checks=[{"id": "T1_VOLUME_DECL", "status": "fail", "detail": {}}],
        verdict="fail",
    )

    res = solid_build_result_to_evaluation_result(build_result)

    assert {check.id: check.status for check in res.checks}["T1_VOLUME_DECL"] == "fail"
    assert result_verdict(solid_evaluator_descriptor(), res) == "not_usable"


def test_solid_adapter_disambiguates_duplicate_native_check_ids(
    tmp_path: Path,
) -> None:
    extra = [
        {"id": "CUSTOM_CHECK", "status": "fail", "hint": "first fail"},
        {"id": "CUSTOM_CHECK", "status": "fail", "hint": "second fail"},
    ]
    build_result = _mock_build_result(
        tmp_path,
        total_volume=500.0,
        extra_checks=extra,
        verdict="fail",
    )

    res = solid_build_result_to_evaluation_result(build_result)
    check_ids = [c.id for c in res.checks]
    assert len(set(check_ids)) == len(check_ids)
    assert "CUSTOM_CHECK" in check_ids
    assert "CUSTOM_CHECK_2" in check_ids


# ---------------------------------------------------------------------------
# Mesh tests
# ---------------------------------------------------------------------------


def _mock_mesh_result(
    tmp_path: Path,
    *,
    elements_total: int = 150,
    nodes_total: int = 40,
    log_quality: str | None = None,
    verdict: str = "pass",
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    mesh_dir = tmp_path / "mesh_out"
    mesh_dir.mkdir(parents=True, exist_ok=True)
    log_path = mesh_dir / "gmsh.log"
    inp_path = mesh_dir / "mesh.inp"
    msh_path = mesh_dir / "mesh.msh"
    geo_path = mesh_dir / "mesh.geo"

    inp_path.write_text("*Heading\n", encoding="utf-8")
    msh_path.write_text("$MeshFormat\n2.2 0 8\n$EndMeshFormat\n", encoding="utf-8")
    geo_path.write_text("// geo\n", encoding="utf-8")
    log_path.write_text(log_quality or "Info: Gmsh run complete\n", encoding="utf-8")

    artifacts = {
        "bluecad_geo": {"path": str(geo_path), "sha256": sha256_file(geo_path), "bytes": 10},
        "mesh_inp": {"path": str(inp_path), "sha256": sha256_file(inp_path), "bytes": 10},
        "mesh_msh": {"path": str(msh_path), "sha256": sha256_file(msh_path), "bytes": 10},
        "gmsh_log": {"path": str(log_path), "sha256": sha256_file(log_path), "bytes": len(log_quality or "")},
    }

    return {
        "schema_version": "bluecad_mesh_result_v0_1",
        "verdict": verdict,
        "errors": errors or [],
        "attempts": [
            {
                "attempt_no": 1,
                "target_size": 25.0,
                "gmsh_returncode": 0 if verdict == "pass" else 1,
                "counts": {
                    "elements_total": elements_total,
                    "nodes_total": nodes_total,
                    "volume_element_types": {"C3D4": elements_total},
                    "physical_groups": {"BODY": elements_total},
                },
                "warnings": [],
                "errors": errors or [],
                "artifacts": artifacts,
            }
        ],
        "artifacts": artifacts,
    }


def test_mesh_adapter_valid_quality_mesh_yields_usable(tmp_path: Path) -> None:
    good_gmsh_log = """
Info    : Meshing 3D...
Info    : Optimizing mesh...
Info    : Optimization starts (volume = 1) with worst = 0.803848 / average = 0.869232:
Info    : 0.00 < quality < 0.10 :         0 elements
Info    : 0.10 < quality < 0.20 :         0 elements
Info    : 0.20 < quality < 0.30 :         0 elements
Info    : 0.80 < quality < 0.90 :        16 elements
Info    : Done writing 'mesh.msh'
"""
    mesh_res = _mock_mesh_result(tmp_path, log_quality=good_gmsh_log)
    res = mesh_result_to_evaluation_result(mesh_res)
    desc = mesh_evaluator_descriptor()

    assert res.evaluator_id == MESH_EVALUATOR_ID
    assert res.status == "succeeded"

    outputs_by_name = {o.name: o.value for o in res.outputs}
    assert outputs_by_name["elements_total"].value == 150.0
    assert outputs_by_name["nodes_total"].value == 40.0
    assert outputs_by_name["mesh_quality_min"].value == 0.803848
    assert outputs_by_name["mesh_quality_mean"].value == 0.869232
    assert outputs_by_name["mesh_quality_below_limit"].value == 0.0

    checks_by_id = {c.id: c for c in res.checks}
    assert checks_by_id[CHECK_VOLUME_ELEMENTS].status == "pass"
    assert checks_by_id[CHECK_ELEMENT_QUALITY].status == "pass"
    assert checks_by_id[CHECK_ELEMENT_QUALITY].actual == 0.803848
    assert checks_by_id[CHECK_ELEMENT_QUALITY].limit == MESH_QUALITY_POLICY_LIMIT
    assert checks_by_id[CHECK_ELEMENT_QUALITY].source == MESH_QUALITY_POLICY_SOURCE
    assert "gamma" in checks_by_id[CHECK_ELEMENT_QUALITY].hint

    assert result_verdict(desc, res) == "usable"


def test_mesh_adapter_no_histogram_omits_below_limit_output(tmp_path: Path) -> None:
    log_no_hist = """
Info    : Meshing 3D...
Info    : Optimizing mesh...
Info    : Optimization starts (volume = 1) with worst = 0.050000 / average = 0.400000:
Info    : Done writing 'mesh.msh'
"""
    mesh_res = _mock_mesh_result(tmp_path, log_quality=log_no_hist)
    res = mesh_result_to_evaluation_result(mesh_res)
    desc = mesh_evaluator_descriptor()

    assert res.status == "succeeded"
    outputs_by_name = {o.name: o.value for o in res.outputs}
    assert "mesh_quality_min" in outputs_by_name
    assert "mesh_quality_mean" in outputs_by_name
    # When no histogram is found, below-limit count is not invented and output is omitted
    assert "mesh_quality_below_limit" not in outputs_by_name

    checks_by_id = {c.id: c for c in res.checks}
    assert checks_by_id[CHECK_ELEMENT_QUALITY].status == "fail"
    assert checks_by_id[CHECK_ELEMENT_QUALITY].actual == 0.05
    assert checks_by_id[CHECK_ELEMENT_QUALITY].source == MESH_QUALITY_POLICY_SOURCE
    assert "gamma" in checks_by_id[CHECK_ELEMENT_QUALITY].hint
    assert result_verdict(desc, res) == "not_usable"


def test_mesh_adapter_degraded_quality_mesh_fails_documented_policy_check(
    tmp_path: Path,
) -> None:
    # Degraded mesh log with worst quality 0.038 < limit 0.10, 5 elements < 0.10
    degraded_gmsh_log = """
Info    : Meshing 3D...
Info    : Optimizing mesh...
Info    : Optimization starts (volume = 1) with worst = 0.038100 / average = 0.450000:
Info    : 0.00 < quality < 0.10 :         5 elements
Info    : 0.10 < quality < 0.20 :        10 elements
Info    : 0.20 < quality < 0.30 :        20 elements
Info    : Done writing 'mesh.msh'
"""
    mesh_res = _mock_mesh_result(tmp_path, log_quality=degraded_gmsh_log)
    res = mesh_result_to_evaluation_result(mesh_res)
    desc = mesh_evaluator_descriptor()

    assert res.status == "succeeded"

    checks_by_id = {c.id: c for c in res.checks}
    assert checks_by_id[CHECK_VOLUME_ELEMENTS].status == "pass"
    assert checks_by_id[CHECK_ELEMENT_QUALITY].status == "fail"
    assert checks_by_id[CHECK_ELEMENT_QUALITY].actual == 0.038100
    assert checks_by_id[CHECK_ELEMENT_QUALITY].limit == MESH_QUALITY_POLICY_LIMIT

    # Separates good from degraded: degraded is not_usable
    assert result_verdict(desc, res) == "not_usable"


def test_mesh_adapter_missing_quality_is_unchecked_when_gmsh_exits_successfully(
    tmp_path: Path,
) -> None:
    # Gmsh log with no quality histogram or worst/average numbers
    no_quality_log = "Info: Gmsh 4.15.2 exited successfully with 0 errors\n"
    mesh_res = _mock_mesh_result(tmp_path, log_quality=no_quality_log)
    res = mesh_result_to_evaluation_result(mesh_res)
    desc = mesh_evaluator_descriptor()

    assert res.status == "succeeded"

    checks_by_id = {c.id: c for c in res.checks}
    assert checks_by_id[CHECK_VOLUME_ELEMENTS].status == "pass"
    # Does NOT invent a value; reports not_evaluated
    assert checks_by_id[CHECK_ELEMENT_QUALITY].status == "not_evaluated"
    assert checks_by_id[CHECK_ELEMENT_QUALITY].actual is None
    assert checks_by_id[CHECK_ELEMENT_QUALITY].limit is None

    # Missing quality on required check derives unchecked
    assert result_verdict(desc, res) == "unchecked"


def test_mesh_adapter_failures_and_errors_preserved(tmp_path: Path) -> None:
    desc = mesh_evaluator_descriptor()

    # MESH_FAIL
    fail_res = _mock_mesh_result(
        tmp_path / "fail",
        verdict="fail",
        errors=[{"code": "MESH_FAIL", "detail": {"returncode": 1}}],
    )
    res_fail = mesh_result_to_evaluation_result(fail_res)
    assert res_fail.status == "failed"
    assert res_fail.failure is not None
    assert res_fail.failure.backend_code == "MESH_FAIL"
    assert res_fail.failure.category == "solver_crash"
    checks_fail = {c.id: c for c in res_fail.checks}
    assert checks_fail[CHECK_VOLUME_ELEMENTS].status == "not_evaluated"
    assert checks_fail[CHECK_VOLUME_ELEMENTS].actual is None
    assert checks_fail[CHECK_ELEMENT_QUALITY].status == "not_evaluated"
    assert result_verdict(desc, res_fail) == "not_usable"

    # MESH_GROUP_EMPTY
    empty_res = _mock_mesh_result(
        tmp_path / "empty",
        verdict="fail",
        errors=[{"code": "MESH_GROUP_EMPTY", "detail": {"group": "BC_fixed"}}],
    )
    res_empty = mesh_result_to_evaluation_result(empty_res)
    assert res_empty.status == "refused"
    assert res_empty.failure is not None
    assert res_empty.failure.category == "invalid_input"
    assert res_empty.failure.backend_code == "MESH_GROUP_EMPTY"
    assert result_verdict(desc, res_empty) == "not_usable"

    # MESH_HIGH_ORDER_INVALID
    ho_res = _mock_mesh_result(
        tmp_path / "ho",
        verdict="fail",
        errors=[{"code": "MESH_HIGH_ORDER_INVALID", "detail": {"diagnostics": ["jac < 0"]}}],
    )
    res_ho = mesh_result_to_evaluation_result(ho_res)
    assert res_ho.status == "refused"
    assert res_ho.failure is not None
    assert res_ho.failure.category == "invalid_input"
    assert res_ho.failure.backend_code == "MESH_HIGH_ORDER_INVALID"
    assert result_verdict(desc, res_ho) == "not_usable"


# ---------------------------------------------------------------------------
# Static FEM tests
# ---------------------------------------------------------------------------


def _mock_fem_result(
    tmp_path: Path,
    *,
    displacement: float = 0.045,
    von_mises: float = 120.5,
    reaction_force: list[float] | None = None,
    verdict: str = "pass",
    errors: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    fem_dir = tmp_path / "fem_out"
    fem_dir.mkdir(parents=True, exist_ok=True)

    inp_path = fem_dir / "analysis.inp"
    log_path = fem_dir / "analysis.log"
    frd_path = fem_dir / "analysis.frd"
    dat_path = fem_dir / "analysis.dat"

    inp_path.write_text("*STEP\n", encoding="utf-8")
    log_path.write_text("Job finished\n", encoding="utf-8")
    frd_path.write_text("frd data\n", encoding="utf-8")
    dat_path.write_text("dat data\n", encoding="utf-8")

    rf = reaction_force or [-100.0, 0.0, 0.0]
    reactions = [{"node_id": 1, "force": rf}]

    artifacts = {
        "inp": {"path": str(inp_path), "sha256": sha256_file(inp_path)},
        "log": {"path": str(log_path), "sha256": sha256_file(log_path)},
        "frd": {"path": str(frd_path), "sha256": sha256_file(frd_path)},
        "dat": {"path": str(dat_path), "sha256": sha256_file(dat_path)},
    }

    return {
        "schema_version": "bluecad_result_summary_v0_1",
        "verdict": verdict,
        "errors": errors or [],
        "solver": {"tool_id": "calculix", "version": "2.22", "returncode": 0 if verdict == "pass" else 1},
        "analysis_type": "static",
        "max_displacement": {"node_id": 10, "value": displacement},
        "max_von_mises": {"element_id": 1, "node_id": 10, "value": von_mises},
        "reactions": reactions,
        "reaction_resultant": rf,
        "artifacts": artifacts,
    }


def test_fem_adapter_force_loaded_single_support_reports_equilibrium_in_newtons(
    tmp_path: Path,
) -> None:
    analysis_spec = {
        "analysis_id": "test-fem-equilibrium",
        "analysis_type": "static",
        "bcs": [{"port_label": "fixed_port", "kind": "fixed"}],
        "loads": [{"port_label": "load_port", "type": "force_total", "force": [100.0, 0.0, 0.0]}],
    }
    # Applied is [100, 0, 0], Reaction is [-100, 0, 0] -> Residual is 0.0 N
    fem_res = _mock_fem_result(tmp_path, reaction_force=[-100.0, 0.0, 0.0])

    res = fem_result_to_evaluation_result(fem_res, analysis_spec)
    desc = fem_evaluator_descriptor()

    assert res.evaluator_id == FEM_EVALUATOR_ID
    assert res.status == "succeeded"

    # Outputs
    outputs_by_name = {o.name: o.value for o in res.outputs}
    assert outputs_by_name["max_displacement"].value == 0.045
    assert outputs_by_name["max_displacement"].unit == "mm"
    assert outputs_by_name["max_von_mises"].value == 120.5
    assert outputs_by_name["max_von_mises"].unit == "MPa"
    assert outputs_by_name["reaction_resultant_x"].value == -100.0
    assert outputs_by_name["reaction_resultant_x"].unit == "N"

    # Checks
    checks_by_id = {c.id: c for c in res.checks}
    assert checks_by_id[CHECK_SOLVER_COMPLETION].status == "pass"
    eq_check = checks_by_id[CHECK_FORCE_EQUILIBRIUM]
    assert eq_check.status == "pass"
    assert eq_check.actual == 0.0
    assert eq_check.unit == "N"
    assert eq_check.limit is not None and eq_check.limit > 0.0

    assert result_verdict(desc, res) == "usable"


def test_fem_adapter_deliberately_wrong_reaction_set_cannot_be_usable(
    tmp_path: Path,
) -> None:
    analysis_spec = {
        "analysis_id": "test-wrong-reactions",
        "analysis_type": "static",
        "bcs": [{"port_label": "fixed_port", "kind": "fixed"}],
        "loads": [{"port_label": "load_port", "type": "force_total", "force": [100.0, 0.0, 0.0]}],
    }
    # Applied is [100, 0, 0], but reaction reports [-50, 0, 0] -> Residual is 50.0 N
    fem_res = _mock_fem_result(tmp_path, reaction_force=[-50.0, 0.0, 0.0])

    res = fem_result_to_evaluation_result(fem_res, analysis_spec)
    desc = fem_evaluator_descriptor()

    checks_by_id = {c.id: c for c in res.checks}
    assert checks_by_id[CHECK_SOLVER_COMPLETION].status == "pass"
    eq_check = checks_by_id[CHECK_FORCE_EQUILIBRIUM]
    assert eq_check.status == "fail"
    assert eq_check.actual == 50.0
    assert eq_check.unit == "N"

    # Cannot be usable
    assert result_verdict(desc, res) == "not_usable"


def test_fem_adapter_multiple_supports_or_pressure_without_resultant_is_unchecked(
    tmp_path: Path,
) -> None:
    desc = fem_evaluator_descriptor()

    # Multiple fixed supports (first-fixed-set reaction output cannot be treated as full reaction)
    multi_bc_spec = {
        "analysis_id": "multi-support",
        "analysis_type": "static",
        "bcs": [
            {"port_label": "support_1", "kind": "fixed"},
            {"port_label": "support_2", "kind": "fixed"},
        ],
        "loads": [{"port_label": "load_1", "type": "force_total", "force": [100.0, 0.0, 0.0]}],
    }
    fem_res1 = _mock_fem_result(tmp_path / "multi", reaction_force=[-100.0, 0.0, 0.0])
    res_multi = fem_result_to_evaluation_result(fem_res1, multi_bc_spec)

    checks1 = {c.id: c for c in res_multi.checks}
    assert checks1[CHECK_SOLVER_COMPLETION].status == "pass"
    assert checks1[CHECK_FORCE_EQUILIBRIUM].status == "not_evaluated"
    assert checks1[CHECK_FORCE_EQUILIBRIUM].actual is None
    # Multiple supports case without complete resultant is unchecked, not passed
    assert result_verdict(desc, res_multi) == "unchecked"

    # Pressure load case (even with caller-supplied claimed resultant force, must be not_evaluated)
    pressure_spec = {
        "analysis_id": "pressure-case",
        "analysis_type": "static",
        "bcs": [{"port_label": "support_1", "kind": "fixed"}],
        "loads": [
            {
                "port_label": "bore",
                "type": "pressure",
                "pressure": 10.0,
                "resultant_force": [100.0, 0.0, 0.0],
            }
        ],
    }
    fem_res2 = _mock_fem_result(tmp_path / "pressure", reaction_force=[-100.0, 0.0, 0.0])
    res_press = fem_result_to_evaluation_result(fem_res2, pressure_spec)

    checks2 = {c.id: c for c in res_press.checks}
    assert checks2[CHECK_SOLVER_COMPLETION].status == "pass"
    assert checks2[CHECK_FORCE_EQUILIBRIUM].status == "not_evaluated"
    assert result_verdict(desc, res_press) == "unchecked"


def test_fem_adapter_bc_without_kind_treated_as_fixed_passes_equilibrium(
    tmp_path: Path,
) -> None:
    # Ordinary spec where BC lacks kind key and load lacks type key (defaults to force_total and vector_n)
    analysis_spec = {
        "analysis_id": "test-no-kind-bc",
        "analysis_type": "static",
        "bcs": [{"port_label": "fixed_port"}],
        "loads": [{"port_label": "load_port", "vector_n": [100.0, 0.0, 0.0]}],
    }
    fem_res = _mock_fem_result(tmp_path, reaction_force=[-100.0, 0.0, 0.0])

    res = fem_result_to_evaluation_result(fem_res, analysis_spec)
    desc = fem_evaluator_descriptor()

    assert res.status == "succeeded"
    checks = {c.id: c for c in res.checks}
    assert checks[CHECK_SOLVER_COMPLETION].status == "pass"
    assert checks[CHECK_FORCE_EQUILIBRIUM].status == "pass"
    assert checks[CHECK_FORCE_EQUILIBRIUM].actual == 0.0
    assert result_verdict(desc, res) == "usable"


def test_fem_adapter_equilibrium_ignores_caller_overridden_tolerance(
    tmp_path: Path,
) -> None:
    # Caller supplies very large equilibrium_tolerance_n, but it must be ignored;
    # limit comes only from module policy constants.
    analysis_spec = {
        "analysis_id": "test-ignore-tolerance",
        "analysis_type": "static",
        "bcs": [{"port_label": "fixed_port", "kind": "fixed"}],
        "loads": [{"port_label": "load_port", "type": "force_total", "force": [100.0, 0.0, 0.0]}],
        "equilibrium_tolerance_n": 5000.0,
    }
    fem_res = _mock_fem_result(tmp_path, reaction_force=[-100.0, 0.0, 0.0])

    res = fem_result_to_evaluation_result(fem_res, analysis_spec)
    eq_check = {c.id: c for c in res.checks}[CHECK_FORCE_EQUILIBRIUM]
    # Limit must NOT be 5000.0; it should be max(0.01 * 100.0, 1e-3) = 1.0 N
    assert eq_check.limit == 1.0


def test_fem_adapter_failures_and_errors_preserved(tmp_path: Path) -> None:
    desc = fem_evaluator_descriptor()

    # Diverged solve
    diverged_res = _mock_fem_result(
        tmp_path / "diverged",
        verdict="fail",
        errors=[{"code": "SOLVE_DIVERGED", "detail": {"returncode": 1}}],
    )
    res_div = fem_result_to_evaluation_result(diverged_res)
    assert res_div.status == "failed"
    assert res_div.failure is not None
    assert res_div.failure.category == "did_not_converge"
    assert res_div.failure.backend_code == "SOLVE_DIVERGED"
    assert res_div.numerical.converged is False
    assert result_verdict(desc, res_div) == "not_usable"

    # Solver crash
    crash_res = _mock_fem_result(
        tmp_path / "crash",
        verdict="fail",
        errors=[{"code": "SOLVE_ERROR", "detail": {"returncode": 2}}],
    )
    res_crash = fem_result_to_evaluation_result(crash_res)
    assert res_crash.status == "failed"
    assert res_crash.failure is not None
    assert res_crash.failure.category == "solver_crash"
    assert res_crash.failure.backend_code == "SOLVE_ERROR"
    assert result_verdict(desc, res_crash) == "not_usable"

    # Timeout
    timeout_res = _mock_fem_result(
        tmp_path / "timeout",
        verdict="error",
        errors=[{"code": "TIMEOUT", "detail": {"timeout_s": 60}}],
    )
    res_timeout = fem_result_to_evaluation_result(timeout_res)
    assert res_timeout.status == "deadline_exceeded"
    assert res_timeout.failure is not None
    assert res_timeout.failure.category == "timeout"
    assert res_timeout.failure.backend_code == "TIMEOUT"
    assert result_verdict(desc, res_timeout) == "not_usable"


# ---------------------------------------------------------------------------
# Registry integration tests
# ---------------------------------------------------------------------------


def test_evaluator_registry_does_not_include_solid_mesh_fem() -> None:
    registry = evaluator_registry()
    assert SOLID_EVALUATOR_ID not in registry
    assert MESH_EVALUATOR_ID not in registry
    assert FEM_EVALUATOR_ID not in registry


def test_evaluator_descriptors() -> None:
    solid_desc = solid_evaluator_descriptor()
    assert solid_desc.evaluator_id == SOLID_EVALUATOR_ID
    assert solid_desc.backend_kind == "cad_kernel"
    assert solid_desc.backend_name == "build123d"
    assert solid_desc.required_checks == (CHECK_BREP_VALID, CHECK_WATERTIGHT)
    assert solid_desc.advisory_checks == (CHECK_VOLUME_CONSERVATION,)

    mesh_desc = mesh_evaluator_descriptor()
    assert mesh_desc.evaluator_id == MESH_EVALUATOR_ID
    assert mesh_desc.backend_kind == "mesher"
    assert mesh_desc.backend_name == "Gmsh"
    assert mesh_desc.required_checks == (CHECK_VOLUME_ELEMENTS, CHECK_ELEMENT_QUALITY)

    fem_desc = fem_evaluator_descriptor()
    assert fem_desc.evaluator_id == FEM_EVALUATOR_ID
    assert fem_desc.backend_kind == "fem_solver"
    assert fem_desc.backend_name == "CalculiX"
    assert fem_desc.required_checks == (CHECK_SOLVER_COMPLETION, CHECK_FORCE_EQUILIBRIUM)

    code_desc = solid_code_evaluator_descriptor()
    assert code_desc.evaluator_id == SOLID_CODE_EVALUATOR_ID
    assert code_desc.backend_kind == "cad_kernel"
    assert code_desc.backend_name == "build123d"
    assert code_desc.required_checks == (CHECK_TOPOLOGY,)
    assert code_desc.advisory_checks == ()


# ---------------------------------------------------------------------------
# Solid Code (spec 285) tests
# ---------------------------------------------------------------------------


def _dummy_limits() -> AppliedLimits:
    return AppliedLimits(
        wall_seconds=120,
        cpu_seconds=480,
        memory_bytes=4 * 1024 * 1024 * 1024,
        processes=32,
        file_bytes=64 * 1024 * 1024,
    )


def test_solid_code_adapter_disabled() -> None:
    record = RunRecord(
        source_sha256="abc",
        limits=_dummy_limits(),
        mechanism="bubblewrap-userns-v1",
        duration_seconds=0.1,
        classification="disabled",
        profile="geometry",
    )
    res = solid_code_run_record_to_evaluation_result(record)
    desc = solid_code_evaluator_descriptor()

    assert res.evaluator_id == SOLID_CODE_EVALUATOR_ID
    assert res.status == "refused"
    assert res.failure is not None
    assert res.failure.category == "not_available"
    assert res.failure.backend_code == "disabled"
    assert "JARVISOS_STUDIO_USER_CODE_ENABLED=1" in (res.failure.message or "")
    assert result_verdict(desc, res) == "not_usable"
    assert len(res.checks) == 1
    assert res.checks[0].id == CHECK_TOPOLOGY
    assert res.checks[0].status == "not_evaluated"


def test_solid_code_adapter_unavailable() -> None:
    record = RunRecord(
        source_sha256="abc",
        limits=_dummy_limits(),
        mechanism="bubblewrap-userns-v1",
        duration_seconds=0.1,
        classification="unavailable",
        profile="geometry",
    )
    res = solid_code_run_record_to_evaluation_result(record)
    desc = solid_code_evaluator_descriptor()

    assert res.evaluator_id == SOLID_CODE_EVALUATOR_ID
    assert res.status == "refused"
    assert res.failure is not None
    assert res.failure.category == "not_available"
    assert res.failure.backend_code == "unavailable"
    assert "bubblewrap" in (res.failure.message or "")
    assert result_verdict(desc, res) == "not_usable"


def test_solid_code_adapter_error_and_host_path_stripping() -> None:
    record = RunRecord(
        source_sha256="abc",
        limits=_dummy_limits(),
        mechanism="bubblewrap-userns-v1",
        duration_seconds=0.5,
        classification="error",
        profile="geometry",
        stderr='File "/opt/workspace/box.py", line 12\nZeroDivisionError: division by zero',
    )
    res = solid_code_run_record_to_evaluation_result(record)
    desc = solid_code_evaluator_descriptor()

    assert res.status == "failed"
    assert res.failure is not None
    assert res.failure.category == "solver_crash"
    assert res.failure.backend_code == "EXECUTION_ERROR"
    assert "/opt/workspace" not in (res.failure.message or "")
    assert "ZeroDivisionError" in (res.failure.message or "")
    assert len(res.failure.message or "") <= 240
    assert result_verdict(desc, res) == "not_usable"


def test_solid_code_adapter_timeout_and_limit() -> None:
    # Timeout
    record_timeout = RunRecord(
        source_sha256="abc",
        limits=_dummy_limits(),
        mechanism="bubblewrap-userns-v1",
        duration_seconds=120.1,
        classification="timeout",
        profile="geometry",
        limit="wall_seconds",
        limit_value=120,
        stderr="subprocess timed out",
    )
    res_timeout = solid_code_run_record_to_evaluation_result(record_timeout)
    desc = solid_code_evaluator_descriptor()
    assert res_timeout.status == "failed"
    assert res_timeout.failure is not None
    assert res_timeout.failure.category == "timeout"
    assert res_timeout.failure.backend_code == "TIMEOUT"
    assert "wall_seconds=120" in (res_timeout.failure.message or "")
    assert result_verdict(desc, res_timeout) == "not_usable"

    # Limit
    record_limit = RunRecord(
        source_sha256="abc",
        limits=_dummy_limits(),
        mechanism="bubblewrap-userns-v1",
        duration_seconds=5.0,
        classification="limit",
        profile="geometry",
        limit="memory_bytes",
        limit_value=1073741824,
        stderr="MemoryError: memory limit reached",
    )
    res_limit = solid_code_run_record_to_evaluation_result(record_limit)
    assert res_limit.status == "failed"
    assert res_limit.failure is not None
    assert res_limit.failure.category == "solver_crash"
    assert res_limit.failure.backend_code == "LIMIT_MEMORY_BYTES"
    assert "memory_bytes=1073741824" in (res_limit.failure.message or "")
    assert result_verdict(desc, res_limit) == "not_usable"


def test_solid_code_adapter_invalid_result() -> None:
    record = RunRecord(
        source_sha256="abc",
        limits=_dummy_limits(),
        mechanism="bubblewrap-userns-v1",
        duration_seconds=0.2,
        classification="invalid_result",
        profile="geometry",
        stderr="invalid result file",
    )
    res = solid_code_run_record_to_evaluation_result(record)
    desc = solid_code_evaluator_descriptor()
    assert res.status == "failed"
    assert res.failure is not None
    assert res.failure.category == "result_parse_error"
    assert res.failure.backend_code == "INVALID_RESULT"
    assert result_verdict(desc, res) == "not_usable"


def test_solid_code_adapter_ok_valid_topology_derives_usable() -> None:
    record = RunRecord(
        source_sha256="abc",
        limits=_dummy_limits(),
        mechanism="bubblewrap-userns-v1",
        duration_seconds=1.2,
        classification="ok",
        profile="geometry",
        result={
            "valid": True,
            "solids": 1,
            "volume": 1000.0,
            "bbox_min": [0.0, 0.0, 0.0],
            "bbox_max": [10.0, 10.0, 10.0],
        },
        artifacts=[
            Artifact(name="model.step", sha256="1" * 64, size_bytes=123),
            Artifact(name="model.stl", sha256="2" * 64, size_bytes=456),
            Artifact(name="model.glb", sha256="3" * 64, size_bytes=789),
        ],
    )
    res = solid_code_run_record_to_evaluation_result(record)
    desc = solid_code_evaluator_descriptor()

    assert res.status == "succeeded"
    assert res.failure is None
    outputs_by_name = {o.name: o.value for o in res.outputs}
    assert outputs_by_name["total_volume"].value == 1000.0
    assert outputs_by_name["total_volume"].unit == "mm**3"
    assert outputs_by_name["total_volume"].value == 1000.0
    assert outputs_by_name["bbox_dx"].value == 10.0
    assert outputs_by_name["bbox_dx"].unit == "mm"

    # 3 artifacts preserved
    art_names = {a.object_id for a in res.output_artifacts}
    assert art_names == {"model.step", "model.stl", "model.glb"}
    art_by_name = {a.object_id: a.content_digest for a in res.output_artifacts}
    assert art_by_name["model.step"] == f"sha256:{'1' * 64}"

    # Topology check passes
    checks_by_id = {c.id: c for c in res.checks}
    assert checks_by_id[CHECK_TOPOLOGY].status == "pass"

    # Usable verdict
    assert result_verdict(desc, res) == "usable"

    # Round trip serialization
    dumped = res.model_dump(mode="json")
    loaded = EvaluationResult.model_validate(dumped)
    assert loaded == res


def test_solid_code_adapter_ok_invalid_topology_derives_not_usable() -> None:
    record = RunRecord(
        source_sha256="abc",
        limits=_dummy_limits(),
        mechanism="bubblewrap-userns-v1",
        duration_seconds=1.0,
        classification="ok",
        profile="geometry",
        result={
            "valid": False,
            "solids": 0,
            "volume": 500.0,
            "bbox_min": [0.0, 0.0, 0.0],
            "bbox_max": [5.0, 5.0, 5.0],
        },
        artifacts=[
            Artifact(name="model.step", sha256="1" * 64, size_bytes=100),
            Artifact(name="model.stl", sha256="2" * 64, size_bytes=100),
            Artifact(name="model.glb", sha256="3" * 64, size_bytes=100),
        ],
    )
    res = solid_code_run_record_to_evaluation_result(record)
    desc = solid_code_evaluator_descriptor()

    assert res.status == "succeeded"
    outputs_by_name = {o.name: o.value for o in res.outputs}
    # Numbers are never changed
    assert outputs_by_name["total_volume"].value == 500.0

    checks_by_id = {c.id: c for c in res.checks}
    assert checks_by_id[CHECK_TOPOLOGY].status == "fail"

    # Invalid shape derives not_usable
    assert result_verdict(desc, res) == "not_usable"


def test_solid_code_adapter_declared_bounds() -> None:
    record = RunRecord(
        source_sha256="abc",
        limits=_dummy_limits(),
        mechanism="bubblewrap-userns-v1",
        duration_seconds=1.0,
        classification="ok",
        profile="geometry",
        result={
            "valid": True,
            "solids": 1,
            "volume": 1000.0,
            "bbox_min": [0.0, 0.0, 0.0],
            "bbox_max": [10.0, 10.0, 10.0],
        },
        artifacts=[
            Artifact(name="model.step", sha256="1" * 64, size_bytes=100),
            Artifact(name="model.stl", sha256="2" * 64, size_bytes=100),
            Artifact(name="model.glb", sha256="3" * 64, size_bytes=100),
        ],
    )
    desc = solid_code_evaluator_descriptor()

    # 1. Bounds match
    case_good = GeometryCase(
        inputs={},
        volume_min=900.0,
        volume_max=1100.0,
        max_extent=15.0,
    )
    res_good = solid_code_run_record_to_evaluation_result(record, case=case_good)
    checks_good = {c.id: c for c in res_good.checks}
    assert checks_good[CHECK_TOPOLOGY].status == "pass"
    assert checks_good[CHECK_VOLUME_MIN].status == "pass"
    assert checks_good[CHECK_VOLUME_MIN].actual == 1000.0
    assert checks_good[CHECK_VOLUME_MIN].limit == 900.0
    assert checks_good[CHECK_VOLUME_MIN].unit == "mm**3"
    assert checks_good[CHECK_VOLUME_MAX].status == "pass"
    assert checks_good[CHECK_MAX_EXTENT].status == "pass"
    assert checks_good[CHECK_MAX_EXTENT].actual == 10.0
    assert checks_good[CHECK_MAX_EXTENT].limit == 15.0
    assert checks_good[CHECK_MAX_EXTENT].unit == "mm"
    assert result_verdict(desc, res_good) == "usable"

    # 2. Volume below volume_min fails
    case_bad_vol = GeometryCase(inputs={}, volume_min=1500.0)
    res_bad_vol = solid_code_run_record_to_evaluation_result(record, case=case_bad_vol)
    checks_bad_vol = {c.id: c for c in res_bad_vol.checks}
    assert checks_bad_vol[CHECK_VOLUME_MIN].status == "fail"
    assert checks_bad_vol[CHECK_VOLUME_MIN].actual == 1000.0
    assert checks_bad_vol[CHECK_VOLUME_MIN].limit == 1500.0
    assert result_verdict(desc, res_bad_vol) == "not_usable"

    # 3. Max extent exceeded fails
    case_bad_extent = GeometryCase(inputs={}, max_extent=5.0)
    res_bad_ext = solid_code_run_record_to_evaluation_result(record, case=case_bad_extent)
    checks_bad_ext = {c.id: c for c in res_bad_ext.checks}
    assert checks_bad_ext[CHECK_MAX_EXTENT].status == "fail"
    assert checks_bad_ext[CHECK_MAX_EXTENT].actual == 10.0
    assert checks_bad_ext[CHECK_MAX_EXTENT].limit == 5.0
    assert result_verdict(desc, res_bad_ext) == "not_usable"


def test_solid_code_adapter_missing_required_check_derives_unchecked() -> None:
    record = RunRecord(
        source_sha256="abc",
        limits=_dummy_limits(),
        mechanism="bubblewrap-userns-v1",
        duration_seconds=1.0,
        classification="ok",
        profile="geometry",
        result={"valid": True, "solids": 1, "volume": 1000.0, "bbox_min": [0.0, 0.0, 0.0], "bbox_max": [10.0, 10.0, 10.0]},
        artifacts=[
            Artifact(name="model.step", sha256="1" * 64, size_bytes=100),
            Artifact(name="model.stl", sha256="2" * 64, size_bytes=100),
            Artifact(name="model.glb", sha256="3" * 64, size_bytes=100),
        ],
    )
    res = solid_code_run_record_to_evaluation_result(record)
    # Strip the required topology check
    res_no_checks = res.model_copy(update={"checks": ()})
    desc = solid_code_evaluator_descriptor()
    assert result_verdict(desc, res_no_checks) == "unchecked"



def test_solid_code_adapter_ok_without_facts_is_parse_error_not_zeros() -> None:
    for facts in ({"valid": True, "solids": 1}, {"valid": True, "solids": 1, "volume": 1.0, "bbox_min": [0.0],
                                                  "bbox_max": [1.0]}, None):
        record = RunRecord(
            source_sha256="abc",
            limits=_dummy_limits(),
            mechanism="bubblewrap-userns-v1",
            duration_seconds=0.1,
            classification="ok",
            profile="geometry",
            result=facts,
        )
        res = solid_code_run_record_to_evaluation_result(record)
        assert res.status == "failed"
        assert res.failure is not None and res.failure.category == "result_parse_error"
        assert res.outputs == ()


def test_build_solid_code_manifest_with_ports() -> None:
    from app.modules.bluecad.spec import canonical_json

    record = RunRecord(
        source_sha256="abc",
        limits=_dummy_limits(),
        mechanism="bubblewrap-userns-v1",
        duration_seconds=1.0,
        classification="ok",
        profile="geometry",
        result={
            "valid": True,
            "solids": 1,
            "volume": 8000.0,
            "bbox_min": [-10.0, -10.0, -10.0],
            "bbox_max": [10.0, 10.0, 10.0],
            "ports": {
                "inlet": {
                    "origin": [-10.0, 0.0, 0.0],
                    "direction": [-1.0, 0.0, 0.0],
                    "interface": "tube",
                    "outer_d": 8.0,
                    "wall_t": 1.0,
                },
                "pad1": {
                    "origin": [0.0, 0.0, -10.0],
                    "direction": [0.0, 0.0, -1.0],
                    "interface": "pad",
                    "pad_d": 12.0,
                },
            },
        },
        artifacts=[
            Artifact(name="model.step", sha256="a" * 64, size_bytes=1000),
            Artifact(name="model.stl", sha256="b" * 64, size_bytes=2000),
            Artifact(name="model.glb", sha256="c" * 64, size_bytes=3000),
        ],
    )

    manifest = build_solid_code_manifest(record)
    assert manifest["manifest_version"] == "bluecad_manifest_v0_1"
    assert manifest["spec_id"] == "solid_code"
    assert "body" in manifest["parts"]
    body = manifest["parts"]["body"]
    assert body["kind"] == "solid_code"
    assert body["volume_mm3"] == 8000.0
    assert body["bbox_mm"] == {"min": [-10.0, -10.0, -10.0], "max": [10.0, 10.0, 10.0]}
    assert "inlet" in body["ports"] and "pad1" in body["ports"]
    assert body["ports"]["inlet"]["outer_d"] == 8.0
    assert body["ports"]["pad1"]["pad_d"] == 12.0

    assert manifest["resolved_ports"] == {"body": body["ports"]}
    assert manifest["assembly"] == {
        "total_volume_mm3": 8000.0,
        "bbox_mm": {"min": [-10.0, -10.0, -10.0], "max": [10.0, 10.0, 10.0]},
    }
    assert manifest["artifacts"]["model.step"] == {"sha256": "a" * 64, "bytes": 1000}
    assert manifest["artifacts"]["model.stl"] == {"sha256": "b" * 64, "bytes": 2000}
    assert manifest["artifacts"]["model.glb"] == {"sha256": "c" * 64, "bytes": 3000}

    # Verify manifest_digest
    digest = manifest["manifest_digest"]
    payload_without_digest = {k: v for k, v in manifest.items() if k != "manifest_digest"}
    expected_digest = hashlib.sha256(canonical_json(payload_without_digest).encode("utf-8")).hexdigest()
    assert digest == expected_digest


def test_build_solid_code_manifest_bare_shape_has_empty_ports() -> None:
    record = RunRecord(
        source_sha256="abc",
        limits=_dummy_limits(),
        mechanism="bubblewrap-userns-v1",
        duration_seconds=1.0,
        classification="ok",
        profile="geometry",
        result={
            "valid": True,
            "solids": 1,
            "volume": 1000.0,
            "bbox_min": [0.0, 0.0, 0.0],
            "bbox_max": [10.0, 10.0, 10.0],
        },
        artifacts=[
            Artifact(name="model.step", sha256="1" * 64, size_bytes=100),
            Artifact(name="model.stl", sha256="2" * 64, size_bytes=100),
            Artifact(name="model.glb", sha256="3" * 64, size_bytes=100),
        ],
    )
    manifest = build_solid_code_manifest(record)
    assert manifest["parts"]["body"]["ports"] == {}
    assert manifest["resolved_ports"] == {"body": {}}


def test_build_solid_code_manifest_reads_artifact_dir_when_no_record_artifacts(tmp_path: Path) -> None:
    art_dir = tmp_path / "artifacts"
    art_dir.mkdir(parents=True)
    (art_dir / "model.step").write_bytes(b"STEP DATA")
    (art_dir / "model.stl").write_bytes(b"STL DATA")
    (art_dir / "model.glb").write_bytes(b"GLB DATA")

    record = RunRecord(
        source_sha256="abc",
        limits=_dummy_limits(),
        mechanism="bubblewrap-userns-v1",
        duration_seconds=1.0,
        classification="ok",
        profile="geometry",
        result={
            "valid": True,
            "solids": 1,
            "volume": 1000.0,
            "bbox_min": [0.0, 0.0, 0.0],
            "bbox_max": [10.0, 10.0, 10.0],
        },
        artifacts=[],
    )
    manifest = build_solid_code_manifest(record, artifact_dir=art_dir)
    assert manifest["artifacts"]["model.step"]["sha256"] == hashlib.sha256(b"STEP DATA").hexdigest()
    assert manifest["artifacts"]["model.step"]["bytes"] == len(b"STEP DATA")


def test_solid_code_adapter_with_manifest_included_in_output_artifacts() -> None:
    record = RunRecord(
        source_sha256="abc",
        limits=_dummy_limits(),
        mechanism="bubblewrap-userns-v1",
        duration_seconds=1.0,
        classification="ok",
        profile="geometry",
        result={
            "valid": True,
            "solids": 1,
            "volume": 1000.0,
            "bbox_min": [0.0, 0.0, 0.0],
            "bbox_max": [10.0, 10.0, 10.0],
        },
        artifacts=[
            Artifact(name="model.step", sha256="1" * 64, size_bytes=100),
            Artifact(name="model.stl", sha256="2" * 64, size_bytes=100),
            Artifact(name="model.glb", sha256="3" * 64, size_bytes=100),
        ],
    )
    manifest_data = build_solid_code_manifest(record)
    res = solid_code_run_record_to_evaluation_result(record, manifest=manifest_data)
    manifest_artifacts = [a for a in res.output_artifacts if a.object_id == "manifest.json"]
    assert len(manifest_artifacts) == 1
    ref = manifest_artifacts[0]
    expected_text = json.dumps(manifest_data, indent=2, sort_keys=True) + "\n"
    expected_digest = f"sha256:{hashlib.sha256(expected_text.encode('utf-8')).hexdigest()}"
    assert ref.content_digest == expected_digest
