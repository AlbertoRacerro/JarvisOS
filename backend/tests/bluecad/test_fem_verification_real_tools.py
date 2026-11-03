from __future__ import annotations

import json
import math
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from app.modules.bluecad.fem_pressure_integration import _parse_mesh
from app.modules.bluecad.fem_verification import (
    audit_segmented_pressure_surface,
    verify_fixture_index,
)
from app.modules.bluecad.mesh_adapter import mesh_analysis_spec
from app.modules.bluecad.pressure_mapping import (
    map_pressure_surface,
    pressure_load_evidence,
)
from app.modules.bluecad.registry import (
    ToolRegistryError,
    check_registry,
    resolve_registry_path,
    resolve_tool,
)

FIXTURES = Path(__file__).parent / "fixtures" / "fem_verification"
_DIAGNOSTIC_SUFFIXES = {".geo", ".inp", ".json", ".log", ".msh"}


def _handle_unavailable(*, strict: bool, reason: str) -> None:
    if strict:
        pytest.fail(f"strict BLUECAD real-tool proof unavailable: {reason}")
    pytest.skip(f"BLUECAD real-tool proof unavailable: {reason}")


def _require_gmsh(request: pytest.FixtureRequest) -> tuple[dict[str, Any], str]:
    strict = bool(request.config.getoption("--require-bluecad-real-tools"))
    try:
        registry_path = resolve_registry_path()
        gmsh = resolve_tool("gmsh", registry_path)
        exit_code, output = check_registry(registry_path)
    except (ToolRegistryError, OSError) as exc:
        _handle_unavailable(strict=strict, reason=str(exc))
    if exit_code != 0:
        _handle_unavailable(strict=strict, reason=output)
    if str(gmsh["version_pin"]).lower().startswith("fake"):
        _handle_unavailable(strict=strict, reason="gmsh uses a fake version pin")
    return gmsh, output


def _segmented_cylinder_spec(step_path: Path, manifest_path: Path) -> dict[str, Any]:
    return {
        "schema_version": "bluecad_analysis_spec_v0_1",
        "analysis_id": "segmented-lame-bore-audit",
        "analysis_type": "static",
        "geometry": {
            "step_path": str(step_path),
            "manifest_path": str(manifest_path),
        },
        "material": {
            "name": "steel",
            "E": 200000.0,
            "nu": 0.3,
            "rho": 7.85e-9,
            "yield_strength": 250.0,
        },
        "bcs": [{"port_label": "cylinder.fixed", "kind": "fixed"}],
        "loads": [
            {
                "port_label": f"cylinder.bore_{index:02d}",
                "type": "pressure",
                "pressure": 1.0,
            }
            for index in range(1, 9)
        ],
        "mesh": {"target_size": 4.0, "element_order": 2},
        "pass_criteria": [],
        "timeout_s": 180.0,
    }


def _preserve_diagnostics(source_root: Path) -> None:
    configured = os.getenv("JARVISOS_BLUECAD_PROOF_DEBUG_DIR")
    if not configured:
        return
    destination = Path(configured) / "fem-verification-c1-segmented-bore"
    destination.mkdir(parents=True, exist_ok=True)
    for source in source_root.rglob("*"):
        if source.is_file() and source.suffix.lower() in _DIAGNOSTIC_SUFFIXES:
            target = destination / source.relative_to(source_root)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


@pytest.mark.bluecad_kernel
@pytest.mark.bluecad_gmsh
def test_strict_real_gmsh_segmented_bore_group_area_and_resultant(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    gmsh, registry_output = _require_gmsh(request)
    fixture_verification = verify_fixture_index(FIXTURES / "fixture_index.json")
    fixture_root = FIXTURES / "segmented_cylinder"
    spec = _segmented_cylinder_spec(
        fixture_root / "model.step",
        fixture_root / "manifest.json",
    )
    proof_root = tmp_path / "segmented-lame-bore"
    mesh_result = mesh_analysis_spec(
        spec,
        proof_root / "mesh",
        registry_path=resolve_registry_path(),
        timeout_s=180.0,
    )
    assert mesh_result["verdict"] == "pass", mesh_result
    counts = mesh_result["attempts"][-1]["counts"]
    assert counts["volume_element_types"] == {"C3D10": counts["elements_total"]}

    mesh_path = Path(mesh_result["artifacts"]["mesh_inp"]["path"])
    mesh = _parse_mesh(mesh_path.read_text(encoding="utf-8"))
    fixed_ids = mesh["element_sets"].get("BC_cylinder_fixed", set())
    assert fixed_ids
    fixed_nodes = {
        node_id
        for element_id in fixed_ids
        for node_id in mesh["elements"][element_id]["nodes"]
    }
    assert fixed_nodes
    assert (
        max(abs(float(mesh["node_coordinates"][node_id][2])) for node_id in fixed_nodes)
        <= 1.0e-6
    )

    expected_sets = [f"LOAD_cylinder_bore_{index:02d}" for index in range(1, 9)]
    evidence = []
    for surface_set in expected_sets:
        mappings = map_pressure_surface(mesh, surface_set)
        evidence.append(pressure_load_evidence(surface_set, 1.0, mappings))

    audit = audit_segmented_pressure_surface(
        evidence,
        expected_surface_sets=expected_sets,
        expected_area_mm2=2.0 * math.pi * 20.0 * 160.0,
        area_relative_tolerance=0.01,
        resultant_fraction_limit=0.005,
    )
    assert audit["verdict"] == "pass", audit
    assert all(value > 0 for value in audit["group_mapping_counts"].values())
    assert audit["unique_surface_element_count"] == sum(
        audit["group_mapping_counts"].values()
    )

    proof = {
        "schema_version": "bluecad_fem_verification_c1_real_audit_v0_1",
        "registry_check": registry_output,
        "gmsh": {
            key: gmsh[key] for key in ("entrypoint", "version_pin", "binary_sha256")
        },
        "fixture_verification": fixture_verification,
        "mesh_counts": counts,
        "audit": audit,
    }
    proof_path = proof_root / "segmented_bore_audit.json"
    proof_path.parent.mkdir(parents=True, exist_ok=True)
    proof_path.write_text(
        json.dumps(proof, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    _preserve_diagnostics(proof_root)
