"""Focused local proof with installed build123d, Gmsh and CalculiX tools (spec 281).

Runs non-private sample geometry through build, mesh and solve, and converts their
native results into checked EvaluationResult envelopes. All outputs stay in a
temporary data root.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.modules.bluecad.fem_adapter import solve_static_analysis
from app.modules.bluecad.mesh_adapter import mesh_analysis_spec
from app.modules.bluecad.registry import check_registry
from app.modules.bluecad.result_adapters import (
    CHECK_BREP_VALID,
    CHECK_ELEMENT_QUALITY,
    CHECK_FORCE_EQUILIBRIUM,
    CHECK_SOLVER_COMPLETION,
    CHECK_VOLUME_CONSERVATION,
    CHECK_VOLUME_ELEMENTS,
    CHECK_WATERTIGHT,
    FEM_EVALUATOR_ID,
    MESH_EVALUATOR_ID,
    SOLID_EVALUATOR_ID,
    fem_evaluator_descriptor,
    fem_result_to_evaluation_result,
    mesh_evaluator_descriptor,
    mesh_result_to_evaluation_result,
    solid_build_result_to_evaluation_result,
    solid_evaluator_descriptor,
)
from app.modules.bluecad.service import build_geometry_spec
from app.modules.engineering.evaluator_contracts import result_verdict

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _make_tools_registry(tmp_path: Path) -> tuple[Path, dict[str, bool]]:
    reg_path = tmp_path / "tools.yaml"
    gmsh_bin = shutil.which("gmsh")
    ccx_bin = shutil.which("ccx")

    has_build123d = False
    try:
        import build123d  # noqa: F401

        has_build123d = True
    except ImportError:
        pass

    tools: list[dict[str, Any]] = [
        {
            "id": "build123d",
            "kind": "cad_kernel",
            "integration_mode": "in_process",
            "version_pin": "0.11.1",
            "license": {"spdx": "Apache-2.0", "boundary": "A", "verified_date": "2026-07-03"},
            "enabled": has_build123d,
            "entrypoint": "build123d",
            "capabilities": ["brep", "sweep", "boolean", "fillet", "step_export", "stl_export"],
            "health_check": f'{sys.executable} -c "import build123d"',
        }
    ]

    has_gmsh = gmsh_bin is not None and Path(gmsh_bin).is_file()
    if has_gmsh and gmsh_bin:
        gmsh_hash = hashlib.sha256(Path(gmsh_bin).read_bytes()).hexdigest()
        tools.append(
            {
                "id": "gmsh",
                "kind": "mesher",
                "integration_mode": "subprocess",
                "version_pin": "4.12.1",
                "license": {"spdx": "GPL-2.0-or-later", "boundary": "C", "verified_date": "2026-10-24"},
                "enabled": True,
                "entrypoint": gmsh_bin,
                "binary_sha256": gmsh_hash,
                "provenance_url": "https://packages.ubuntu.com/noble/gmsh",
                "capabilities": ["step_import", "tet_mesh", "physical_groups", "inp_export"],
                "health_check": f"{gmsh_bin} -version",
            }
        )

    has_ccx = ccx_bin is not None and Path(ccx_bin).is_file()
    if has_ccx and ccx_bin:
        ccx_hash = hashlib.sha256(Path(ccx_bin).read_bytes()).hexdigest()
        tools.append(
            {
                "id": "calculix",
                "kind": "fem_solver",
                "integration_mode": "subprocess",
                "version_pin": "2.22",
                "license": {"spdx": "GPL-2.0", "boundary": "C", "verified_date": "2026-07-03"},
                "enabled": True,
                "entrypoint": ccx_bin,
                "binary_sha256": ccx_hash,
                "provenance_url": "https://packages.ubuntu.com/noble/calculix-ccx",
                "capabilities": ["static", "modal", "thermal"],
                "health_check": None,
            }
        )

    payload = {"registry_version": "bluecad_tool_registry_v0_1", "tools": tools}
    reg_path.write_text(yaml.safe_dump(payload), encoding="utf-8")

    code, _ = check_registry(reg_path)
    tool_status = {
        "build123d": has_build123d,
        "gmsh": has_gmsh and code == 0,
        "calculix": has_ccx and code == 0,
    }
    return reg_path, tool_status


def test_real_tools_proof_build_mesh_solve_adapters(tmp_path: Path) -> None:
    """Proof of solid build, Gmsh mesh, and CalculiX static FEM on sample tube geometry."""
    registry_path, tool_status = _make_tools_registry(tmp_path)

    if not tool_status["build123d"]:
        pytest.skip("build123d is not installed; solid build engine path not proved")

    # 1. Solid Build proof
    tube_fixture = FIXTURES_DIR / "minimal_single_tube.json"
    assert tube_fixture.is_file(), "minimal_single_tube.json fixture is required"
    spec = json.loads(tube_fixture.read_text(encoding="utf-8"))

    build_dir = tmp_path / "solid_proof"
    build_result = build_geometry_spec(spec, build_dir, timeout_s=60.0)

    assert build_result.verdict == "pass", f"build failed: {build_result.errors}"
    assert build_result.manifest is not None

    solid_eval_res = solid_build_result_to_evaluation_result(build_result)
    solid_desc = solid_evaluator_descriptor()

    assert solid_eval_res.evaluator_id == SOLID_EVALUATOR_ID
    assert solid_eval_res.status == "succeeded"
    solid_checks = {c.id: c for c in solid_eval_res.checks}
    assert solid_checks[CHECK_BREP_VALID].status == "pass"
    assert solid_checks[CHECK_WATERTIGHT].status == "pass"
    assert solid_checks[CHECK_VOLUME_CONSERVATION].status == "pass"

    solid_verdict = result_verdict(solid_desc, solid_eval_res)
    assert solid_verdict == "usable"

    # Outputs check
    solid_outputs = {o.name: o.value for o in solid_eval_res.outputs}
    assert solid_outputs["total_volume"].value > 0.0
    assert solid_outputs["total_volume"].unit == "mm**3"

    if not tool_status["gmsh"]:
        pytest.skip("gmsh is not installed or enabled; meshing engine path not proved")

    # 2. Gmsh Mesh proof
    mesh_dir = tmp_path / "mesh_proof"
    analysis_spec = {
        "schema_version": "bluecad_analysis_spec_v0_1",
        "analysis_id": "single-tube-real-proof",
        "analysis_type": "static",
        "geometry": {
            "step_path": str(build_dir / "model.step"),
            "manifest_path": str(build_dir / "manifest.json"),
        },
        "material": {"name": "steel", "E": 200000.0, "nu": 0.3, "rho": 7.8e-9, "yield_strength": 250.0},
        "bcs": [{"port_label": "tube1.port_a", "kind": "fixed"}],
        "loads": [{"port_label": "tube1.port_b", "type": "force_total", "force": [100.0, 0.0, 0.0]}],
        "mesh": {"target_size": 50.0},
        "pass_criteria": [{"metric": "max_displacement", "op": "<=", "value": 100.0}],
        "timeout_s": 60.0,
    }

    mesh_result = mesh_analysis_spec(analysis_spec, mesh_dir, registry_path=registry_path)
    assert mesh_result["verdict"] == "pass", f"meshing failed: {mesh_result.get('errors')}"

    mesh_eval_res = mesh_result_to_evaluation_result(mesh_result, analysis_spec)
    mesh_desc = mesh_evaluator_descriptor()

    assert mesh_eval_res.evaluator_id == MESH_EVALUATOR_ID
    assert mesh_eval_res.status == "succeeded"

    mesh_checks = {c.id: c for c in mesh_eval_res.checks}
    assert mesh_checks[CHECK_VOLUME_ELEMENTS].status == "pass"
    # Real Gmsh logs optimization quality for 3D meshes
    assert mesh_checks[CHECK_ELEMENT_QUALITY].status == "pass"
    assert mesh_checks[CHECK_ELEMENT_QUALITY].actual is not None
    assert mesh_checks[CHECK_ELEMENT_QUALITY].actual >= 0.10

    mesh_verdict = result_verdict(mesh_desc, mesh_eval_res)
    assert mesh_verdict == "usable"

    mesh_outputs = {o.name: o.value for o in mesh_eval_res.outputs}
    assert mesh_outputs["elements_total"].value > 0
    assert "mesh_quality_min" in mesh_outputs

    if not tool_status["calculix"]:
        pytest.skip("calculix is not installed or enabled; static FEM engine path not proved")

    # 3. CalculiX Static FEM proof
    fem_dir = tmp_path / "fem_proof"
    fem_result = solve_static_analysis(analysis_spec, mesh_result, fem_dir, registry_path=registry_path)
    assert fem_result["verdict"] == "pass", f"solve failed: {fem_result.get('errors')}"

    fem_eval_res = fem_result_to_evaluation_result(fem_result, analysis_spec)
    fem_desc = fem_evaluator_descriptor()

    assert fem_eval_res.evaluator_id == FEM_EVALUATOR_ID
    assert fem_eval_res.status == "succeeded"

    fem_checks = {c.id: c for c in fem_eval_res.checks}
    assert fem_checks[CHECK_SOLVER_COMPLETION].status == "pass"
    assert fem_checks[CHECK_FORCE_EQUILIBRIUM].status == "pass"
    assert fem_checks[CHECK_FORCE_EQUILIBRIUM].actual is not None
    # 100 N applied in X balances reactions in X within tolerance
    assert fem_checks[CHECK_FORCE_EQUILIBRIUM].actual <= 0.1  # Very small residual

    fem_verdict = result_verdict(fem_desc, fem_eval_res)
    assert fem_verdict == "usable"

    fem_outputs = {o.name: o.value for o in fem_eval_res.outputs}
    assert fem_outputs["max_displacement"].value > 0.0
    assert fem_outputs["max_displacement"].unit == "mm"
    assert fem_outputs["max_von_mises"].value > 0.0
    assert fem_outputs["max_von_mises"].unit == "MPa"
    assert "reaction_resultant_x" in fem_outputs
