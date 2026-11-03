from __future__ import annotations

import importlib.util
import json
import math
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from app.core.bootstrap import initialize_storage
from app.core.paths import build_paths
from app.modules.bluecad.export import sha256_file
from app.modules.bluecad.fem_adapter import solve_static_analysis
from app.modules.bluecad.ledger import (
    ScriptedFakeBluecadAdapter,
    candidate_work_dir,
)
from app.modules.bluecad.loop import create_bluecad_candidate
from app.modules.bluecad.mesh_adapter import mesh_analysis_spec
from app.modules.bluecad.models import (
    BluecadCandidateCreate,
    BluecadLoopConfig,
)
from app.modules.bluecad.registry import (
    ToolRegistryError,
    check_registry,
    resolve_registry_path,
    resolve_tool,
)
from tests.bluecad.alpha_real_tools_support import (
    analysis_spec,
    assert_full_chain,
    offline_bindings,
)

FIXTURES = Path(__file__).parent / "fixtures"
_DIAGNOSTIC_SUFFIXES = {
    ".inp",
    ".log",
    ".frd",
    ".dat",
    ".sta",
    ".cvg",
    ".geo",
    ".msh",
    ".json",
}
_MAX_DIAGNOSTIC_FILE_BYTES = 2 * 1024 * 1024


def _handle_unavailable(*, strict: bool, reason: str) -> None:
    if strict:
        pytest.fail(f"strict BLUECAD real-tool proof unavailable: {reason}")
    pytest.skip(f"BLUECAD real-tool proof unavailable: {reason}")


def _require_toolchain(
    request: pytest.FixtureRequest,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    strict = bool(
        request.config.getoption("--require-bluecad-real-tools")
    )
    if importlib.util.find_spec("build123d") is None:
        _handle_unavailable(
            strict=strict,
            reason="build123d is not installed",
        )
    try:
        registry_path = resolve_registry_path()
        gmsh = resolve_tool("gmsh", registry_path)
        calculix = resolve_tool("calculix", registry_path)
        exit_code, output = check_registry(registry_path)
    except (ToolRegistryError, OSError) as exc:
        _handle_unavailable(strict=strict, reason=str(exc))
    if exit_code != 0:
        _handle_unavailable(strict=strict, reason=output)
    for entry in (gmsh, calculix):
        if str(entry["version_pin"]).lower().startswith("fake"):
            _handle_unavailable(
                strict=strict,
                reason=f"{entry['id']} uses a fake version pin",
            )
    return gmsh, calculix, output


def _copy_text_diagnostics(
    source_root: Path,
    destination: Path,
) -> None:
    for source in source_root.rglob("*"):
        if (
            not source.is_file()
            or source.suffix.lower() not in _DIAGNOSTIC_SUFFIXES
        ):
            continue
        if source.stat().st_size > _MAX_DIAGNOSTIC_FILE_BYTES:
            continue
        target = destination / source.relative_to(source_root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _preserve_candidate_diagnostics(candidates: list[Any]) -> None:
    configured = os.getenv("JARVISOS_BLUECAD_PROOF_DEBUG_DIR")
    if not configured:
        return
    destination_root = Path(configured)
    destination_root.mkdir(parents=True, exist_ok=True)
    for candidate in candidates:
        source_root = (
            candidate_work_dir("main", candidate.id, 1)
            / "simulation"
        )
        _copy_text_diagnostics(
            source_root,
            destination_root / candidate.id,
        )


def _preserve_direct_diagnostics(
    source_root: Path,
    name: str,
) -> None:
    configured = os.getenv("JARVISOS_BLUECAD_PROOF_DEBUG_DIR")
    if not configured:
        return
    destination = Path(configured) / name
    destination.mkdir(parents=True, exist_ok=True)
    _copy_text_diagnostics(source_root, destination)


def _create_candidate(adapter: ScriptedFakeBluecadAdapter) -> Any:
    payload = BluecadCandidateCreate(
        brief_text="deterministic single tube real-tool alpha proof",
        loop_config=BluecadLoopConfig(
            max_attempts_per_tier=1,
            tier_ladder=["external:cheap"],
            analysis_spec=analysis_spec(),
        ),
    )
    return create_bluecad_candidate(
        "main",
        payload,
        adapters={"scaleway": adapter},
        bindings=offline_bindings(),
        force_external_allowed=True,
    )


def _quadratic_box_spec(
    step_path: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    return {
        "schema_version": "bluecad_analysis_spec_v0_1",
        "analysis_id": "quadratic-box-smoke",
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
        "bcs": [{"port_label": "box.fixed", "kind": "fixed"}],
        "loads": [
            {
                "port_label": "box.loaded",
                "type": "force_total",
                "force": [100.0, 0.0, 0.0],
            }
        ],
        "mesh": {"target_size": 4.0, "element_order": 2},
        "pass_criteria": [
            {
                "metric": "max_displacement",
                "op": "<=",
                "value": 1.0,
            },
            {
                "metric": "max_von_mises",
                "op": "<=",
                "value": 1000.0,
            },
        ],
        "timeout_s": 120.0,
    }


def _pressure_box_spec(
    step_path: Path,
    manifest_path: Path,
) -> dict[str, Any]:
    spec = _quadratic_box_spec(step_path, manifest_path)
    spec["analysis_id"] = "pressure-box-patch"
    spec["loads"] = [
        {
            "port_label": "box.loaded",
            "type": "pressure",
            "pressure": 1.0,
        }
    ]
    return spec


def _write_box_fixture(
    proof_root: Path,
    *,
    length_x: float,
) -> tuple[Path, Path]:
    import build123d as bd

    proof_root.mkdir(parents=True)
    step_path = proof_root / "model.step"
    shape = bd.Box(
        length_x,
        10.0,
        10.0,
        align=(bd.Align.MIN, bd.Align.MIN, bd.Align.MIN),
    )
    bd.export_step(shape, step_path)
    manifest_path = proof_root / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "resolved_ports": {
                    "box": {
                        "fixed": {
                            "origin": [0.0, 5.0, 5.0],
                            "direction": [-1.0, 0.0, 0.0],
                            "outer_d": 12.0,
                        },
                        "loaded": {
                            "origin": [length_x, 5.0, 5.0],
                            "direction": [1.0, 0.0, 0.0],
                            "outer_d": 12.0,
                        },
                    }
                }
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return step_path, manifest_path


def test_ordinary_mode_skips_unavailable_real_tools() -> None:
    with pytest.raises(pytest.skip.Exception):
        _handle_unavailable(strict=False, reason="missing")


def test_strict_mode_fails_unavailable_real_tools() -> None:
    with pytest.raises(pytest.fail.Exception):
        _handle_unavailable(strict=True, reason="missing")


@pytest.mark.bluecad_kernel
@pytest.mark.bluecad_gmsh
@pytest.mark.bluecad_ccx
def test_strict_real_tool_full_chain_twice(
    request: pytest.FixtureRequest,
) -> None:
    gmsh, calculix, registry_output = _require_toolchain(request)
    initialize_storage(seed_default=True)
    fixture_text = (
        FIXTURES / "minimal_single_tube.json"
    ).read_text(encoding="utf-8")
    adapter = ScriptedFakeBluecadAdapter(
        [fixture_text, fixture_text]
    )

    candidates = [_create_candidate(adapter) for _ in range(2)]
    _preserve_candidate_diagnostics(candidates)
    assert len(adapter.prompts) == 2
    manifests = [
        assert_full_chain(candidate) for candidate in candidates
    ]
    assert manifests[0].read_bytes() == manifests[1].read_bytes()
    assert sha256_file(manifests[0]) == sha256_file(manifests[1])

    proof = {
        "schema_version": "bluecad_alpha_real_tool_proof_v0_1",
        "registry_path": str(resolve_registry_path()),
        "registry_check": registry_output,
        "tools": {
            "gmsh": {
                key: gmsh[key]
                for key in (
                    "entrypoint",
                    "version_pin",
                    "binary_sha256",
                )
            },
            "calculix": {
                key: calculix[key]
                for key in (
                    "entrypoint",
                    "version_pin",
                    "binary_sha256",
                )
            },
        },
        "candidate_ids": [
            candidate.id for candidate in candidates
        ],
        "manifest_sha256": sha256_file(manifests[0]),
        "provider_mode": (
            "caller-injected-offline-scripted-adapter"
        ),
    }
    proof_path = Path(
        os.getenv(
            "JARVISOS_BLUECAD_PROOF_JSON",
            str(build_paths().data_root / "bluecad-alpha-proof.json"),
        )
    )
    proof_path.parent.mkdir(parents=True, exist_ok=True)
    proof_path.write_text(
        json.dumps(proof, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


@pytest.mark.bluecad_kernel
@pytest.mark.bluecad_gmsh
@pytest.mark.bluecad_ccx
def test_strict_real_tool_quadratic_mesh_and_solve(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    _require_toolchain(request)
    proof_root = tmp_path / "quadratic-box"
    step_path, manifest_path = _write_box_fixture(
        proof_root,
        length_x=20.0,
    )
    spec = _quadratic_box_spec(step_path, manifest_path)
    registry_path = resolve_registry_path()

    mesh_result = mesh_analysis_spec(
        spec,
        proof_root / "mesh",
        registry_path=registry_path,
        timeout_s=120.0,
    )
    assert mesh_result["verdict"] == "pass", mesh_result
    counts = mesh_result["attempts"][-1]["counts"]
    assert counts["elements_total"] > 0
    assert counts["volume_element_types"] == {
        "C3D10": counts["elements_total"]
    }
    assert all(
        value > 0
        for value in counts["physical_groups"].values()
    )

    fem_result = solve_static_analysis(
        spec,
        mesh_result,
        proof_root / "fem",
        registry_path=registry_path,
        timeout_s=120.0,
    )
    _preserve_direct_diagnostics(
        proof_root,
        "quadratic-box",
    )
    assert fem_result["verdict"] == "pass", fem_result
    assert fem_result["solver"]["returncode"] == 0
    for metric in ("max_displacement", "max_von_mises"):
        value = float(fem_result[metric]["value"])
        assert math.isfinite(value) and value > 0.0


@pytest.mark.bluecad_kernel
@pytest.mark.bluecad_gmsh
@pytest.mark.bluecad_ccx
def test_strict_real_tool_pressure_patch_reaction_balance(
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    _require_toolchain(request)
    proof_root = tmp_path / "pressure-box"
    step_path, manifest_path = _write_box_fixture(
        proof_root,
        length_x=10.0,
    )
    spec = _pressure_box_spec(step_path, manifest_path)
    registry_path = resolve_registry_path()

    mesh_result = mesh_analysis_spec(
        spec,
        proof_root / "mesh",
        registry_path=registry_path,
        timeout_s=120.0,
    )
    assert mesh_result["verdict"] == "pass", mesh_result
    counts = mesh_result["attempts"][-1]["counts"]
    assert counts["volume_element_types"] == {
        "C3D10": counts["elements_total"]
    }

    fem_result = solve_static_analysis(
        spec,
        mesh_result,
        proof_root / "fem",
        registry_path=registry_path,
        timeout_s=120.0,
    )
    _preserve_direct_diagnostics(
        proof_root,
        "pressure-box",
    )
    assert fem_result["verdict"] == "pass", fem_result

    mapping_artifact = fem_result["artifacts"][
        "pressure_face_mapping"
    ]
    mapping_path = Path(mapping_artifact["path"])
    mapping = json.loads(
        mapping_path.read_text(encoding="utf-8")
    )
    load = mapping["loads"][0]
    assert load["mapping_count"] > 0
    assert load["area_mm2"] == pytest.approx(100.0, rel=0.01)
    assert load["scalar_pressure_force_n"] == pytest.approx(
        100.0,
        rel=0.01,
    )
    applied = [
        float(value)
        for value in load["applied_force_resultant_n"]
    ]
    assert math.sqrt(sum(value * value for value in applied)) == (
        pytest.approx(100.0, rel=0.01)
    )

    reactions = fem_result["reactions"]
    reaction = fem_result["reaction_resultant"]
    assert reactions
    assert reaction is not None
    imbalance = [
        float(reaction[axis]) + applied[axis]
        for axis in range(3)
    ]
    assert math.sqrt(sum(value * value for value in imbalance)) <= 1.0
    assert abs(float(reaction[1])) <= 0.5
    assert abs(float(reaction[2])) <= 0.5
