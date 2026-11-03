"""Real-tool orchestration for the spec 024-C2 FEM verification battery."""

from __future__ import annotations

import json
import platform
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.modules.bluecad.fem_adapter import solve_static_analysis
from app.modules.bluecad.fem_verification_battery import (
    build_battery_report,
    cantilever_spec,
    evaluate_cantilever,
    evaluate_lame,
    evaluate_plate,
    lame_spec,
    plate_spec,
    render_battery_report,
)
from app.modules.bluecad.fem_verification_common import FemVerificationError
from app.modules.bluecad.fem_verification_fixtures import verify_fixture_index
from app.modules.bluecad.mesh_adapter import mesh_analysis_spec
from app.modules.bluecad.registry import resolve_tool


def run_fem_verification_battery(
    fixture_index_path: str | Path,
    out_dir: str | Path,
    *,
    registry_path: str | Path,
    git_sha: str,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Run all prescribed real Gmsh/CalculiX cases and write JSON/Markdown reports."""

    root = Path(out_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    index_path = Path(fixture_index_path).resolve()
    fixture_root = index_path.parent
    fixture_verification = verify_fixture_index(index_path)
    registry = Path(registry_path).resolve()
    toolchain = {
        tool_id: _tool_record(resolve_tool(tool_id, registry))
        for tool_id in ("gmsh", "calculix")
    }

    coarse_target = 20.0 / 3.0
    fine_target = 10.0 / 3.0
    cantilever_root = fixture_root / "cantilever"
    coarse = _run_case(
        cantilever_spec(
            cantilever_root / "model.step",
            cantilever_root / "manifest.json",
            target_size=coarse_target,
            analysis_id="024-c2-cantilever-coarse",
        ),
        root / "cantilever" / "coarse",
        registry,
    )
    fine = _run_case(
        cantilever_spec(
            cantilever_root / "model.step",
            cantilever_root / "manifest.json",
            target_size=fine_target,
            analysis_id="024-c2-cantilever-fine",
        ),
        root / "cantilever" / "fine",
        registry,
    )
    coarse_evaluation = evaluate_cantilever(
        mesh_text=_artifact_text(coarse["mesh"], "mesh_inp"),
        frd_text=_artifact_text(coarse["fem"], "frd"),
        reaction_resultant=coarse["fem"].get("reaction_resultant"),
        target_size=coarse_target,
    )
    fine_evaluation = evaluate_cantilever(
        mesh_text=_artifact_text(fine["mesh"], "mesh_inp"),
        frd_text=_artifact_text(fine["fem"], "frd"),
        reaction_resultant=fine["fem"].get("reaction_resultant"),
        target_size=fine_target,
    )
    cantilever = _combine_cantilever(
        coarse_evaluation,
        fine_evaluation,
        coarse_mesh=coarse["mesh"],
        fine_mesh=fine["mesh"],
        coarse_fem=coarse["fem"],
        fine_fem=fine["fem"],
        proof_root=root,
    )

    cylinder_root = fixture_root / "segmented_cylinder"
    lame_target = 4.0
    lame_run = _run_case(
        lame_spec(
            cylinder_root / "model.step",
            cylinder_root / "manifest.json",
            target_size=lame_target,
        ),
        root / "lame",
        registry,
    )
    mapping = json.loads(
        _artifact_text(lame_run["fem"], "pressure_face_mapping")
    )
    lame = evaluate_lame(
        mesh_text=_artifact_text(lame_run["mesh"], "mesh_inp"),
        frd_text=_artifact_text(lame_run["fem"], "frd"),
        pressure_loads=mapping.get("loads", []),
        reaction_resultant=lame_run["fem"].get("reaction_resultant"),
        target_size=lame_target,
    )
    _attach_run_evidence(lame, lame_run, root)

    plate_root = fixture_root / "plate_with_hole"
    plate_run = _run_case(
        plate_spec(
            plate_root / "model.step",
            plate_root / "manifest.json",
        ),
        root / "plate_with_hole",
        registry,
    )
    plate = evaluate_plate(
        mesh_text=_artifact_text(plate_run["mesh"], "mesh_inp"),
        frd_text=_artifact_text(plate_run["fem"], "frd"),
        reaction_resultant=plate_run["fem"].get("reaction_resultant"),
        target_size=20.0 / 12.0,
    )
    _attach_run_evidence(plate, plate_run, root)

    timestamp = generated_at or datetime.now(UTC).isoformat()
    report = build_battery_report(
        generated_at=timestamp,
        git_sha=git_sha,
        environment={
            "os": platform.platform(),
            "python": platform.python_version(),
        },
        toolchain=toolchain,
        fixture_verification=fixture_verification,
        cases=[cantilever, lame, plate],
        artifacts={"proof_root": ".", "reports": "reports"},
    )
    report_paths = render_battery_report(report, root / "reports")
    return {
        "report": report,
        "report_artifacts": {
            key: _relative_path(Path(value), root)
            for key, value in report_paths.items()
        },
        "proof_root": str(root),
    }


def _run_case(
    spec: dict[str, Any],
    case_root: Path,
    registry_path: Path,
) -> dict[str, Any]:
    mesh = mesh_analysis_spec(
        spec,
        case_root / "mesh",
        registry_path=registry_path,
        timeout_s=float(spec.get("timeout_s", 300.0)),
    )
    if mesh.get("verdict") != "pass":
        raise FemVerificationError(
            "BATTERY_MESH_FAILED",
            {
                "analysis_id": spec["analysis_id"],
                "errors": mesh.get("errors", []),
            },
        )
    counts = mesh.get("attempts", [{}])[-1].get("counts", {})
    volume_types = counts.get("volume_element_types", {})
    if not volume_types or set(volume_types) != {"C3D10"}:
        raise FemVerificationError(
            "BATTERY_ELEMENT_ORDER_INVALID",
            {
                "analysis_id": spec["analysis_id"],
                "volume_element_types": volume_types,
            },
        )
    fem = solve_static_analysis(
        spec,
        mesh,
        case_root / "fem",
        registry_path=registry_path,
        timeout_s=float(spec.get("timeout_s", 300.0)),
    )
    if fem.get("verdict") != "pass":
        raise FemVerificationError(
            "BATTERY_SOLVE_FAILED",
            {
                "analysis_id": spec["analysis_id"],
                "errors": fem.get("errors", []),
            },
        )
    return {"spec": spec, "mesh": mesh, "fem": fem}


def _combine_cantilever(
    coarse: dict[str, Any],
    fine: dict[str, Any],
    *,
    coarse_mesh: dict[str, Any],
    fine_mesh: dict[str, Any],
    coarse_fem: dict[str, Any],
    fine_fem: dict[str, Any],
    proof_root: Path,
) -> dict[str, Any]:
    coarse_error = float(coarse["comparison"]["relative_error"])
    fine_error = float(fine["comparison"]["relative_error"])
    refinement = {
        "coarse_target_size_mm": coarse["target_size_mm"],
        "fine_target_size_mm": fine["target_size_mm"],
        "coarse_sampled_displacement_mm": coarse["comparison"]["actual"],
        "fine_sampled_displacement_mm": fine["comparison"]["actual"],
        "coarse_relative_error": coarse_error,
        "fine_relative_error": fine_error,
        "fine_not_less_accurate": fine_error <= coarse_error + 1.0e-12,
    }
    refinement["verdict"] = (
        "pass" if refinement["fine_not_less_accurate"] else "fail"
    )
    fine["refinement"] = refinement
    fine["coarse_load_balance"] = coarse["load_balance"]
    fine["runs"] = {
        "coarse": _run_evidence(coarse_mesh, coarse_fem, proof_root),
        "fine": _run_evidence(fine_mesh, fine_fem, proof_root),
    }
    fine["verdict"] = (
        "pass"
        if fine["comparison"]["verdict"] == "pass"
        and fine["load_balance"]["verdict"] == "pass"
        and coarse["load_balance"]["verdict"] == "pass"
        and refinement["verdict"] == "pass"
        else "fail"
    )
    return fine


def _attach_run_evidence(
    case: dict[str, Any],
    run: dict[str, Any],
    proof_root: Path,
) -> None:
    case["run"] = _run_evidence(run["mesh"], run["fem"], proof_root)


def _run_evidence(
    mesh: dict[str, Any],
    fem: dict[str, Any],
    proof_root: Path,
) -> dict[str, Any]:
    return {
        "mesh_counts": mesh.get("attempts", [{}])[-1].get("counts", {}),
        "mesh_attempts": [
            {
                "attempt_no": item.get("attempt_no"),
                "target_size": item.get("target_size"),
                "returncode": item.get("gmsh_returncode"),
                "warnings": item.get("warnings", []),
            }
            for item in mesh.get("attempts", [])
        ],
        "solver": fem.get("solver", {}),
        "global_diagnostics": {
            "max_displacement": fem.get("max_displacement"),
            "max_von_mises": fem.get("max_von_mises"),
        },
        "artifacts": {
            "mesh": _relative_artifacts(mesh.get("artifacts", {}), proof_root),
            "fem": _relative_artifacts(fem.get("artifacts", {}), proof_root),
        },
    }


def _relative_artifacts(
    artifacts: dict[str, Any],
    proof_root: Path,
) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for name, value in sorted(artifacts.items()):
        if not isinstance(value, dict) or "path" not in value:
            continue
        normalized[name] = {
            key: (
                _relative_path(Path(item), proof_root)
                if key == "path"
                else item
            )
            for key, item in value.items()
        }
    return normalized


def _artifact_text(result: dict[str, Any], name: str) -> str:
    try:
        path = Path(result["artifacts"][name]["path"])
    except (KeyError, TypeError) as exc:
        raise FemVerificationError(
            "BATTERY_ARTIFACT_MISSING",
            {"artifact": name},
        ) from exc
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise FemVerificationError(
            "BATTERY_ARTIFACT_UNREADABLE",
            {"artifact": name, "path": str(path)},
        ) from exc


def _relative_path(path: Path, proof_root: Path) -> str:
    resolved = path.resolve()
    if proof_root != resolved and proof_root not in resolved.parents:
        raise FemVerificationError(
            "BATTERY_ARTIFACT_OUTSIDE_PROOF_ROOT",
            {"path": str(resolved), "proof_root": str(proof_root)},
        )
    return resolved.relative_to(proof_root).as_posix()


def _tool_record(tool: dict[str, Any]) -> dict[str, Any]:
    return {
        "tool_id": tool.get("tool_id", tool.get("id")),
        "version_pin": tool.get("version_pin"),
        "binary_sha256": tool.get("binary_sha256"),
        "provenance_url": tool.get("provenance_url"),
    }
