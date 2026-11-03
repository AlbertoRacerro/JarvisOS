"""Deterministic case evaluation and reporting for spec 024-C2."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.modules.bluecad.fem_verification_analytics import (
    beam_tip_displacement,
    finite_width_hole_reference,
    lame_open_end_bore_stresses,
)
from app.modules.bluecad.fem_verification_common import (
    FemVerificationError,
    comparison_record,
    deterministic_mean,
)
from app.modules.bluecad.fem_verification_parsers import (
    displacement_block,
    parse_frd_blocks,
    parse_inp_mesh,
    stress_block,
)
from app.modules.bluecad.fem_verification_sampling import (
    audit_segmented_pressure_surface,
    cylindrical_stress_components,
)

MATERIAL = {
    "name": "steel",
    "E": 200000.0,
    "nu": 0.3,
    "rho": 7.85e-9,
    "yield_strength": 250.0,
}
PILKEY_SOURCE = {
    "title": "Peterson's Stress Concentration Factors",
    "edition": "3rd",
    "authors": ["Walter D. Pilkey", "Deborah F. Pilkey"],
    "publisher": "Wiley",
    "isbn_13": "9780470048245",
    "nominal_stress_convention": "net section",
}


def cantilever_spec(
    step_path: Path,
    manifest_path: Path,
    *,
    target_size: float,
    analysis_id: str,
) -> dict[str, Any]:
    return _base_spec(
        step_path,
        manifest_path,
        analysis_id=analysis_id,
        bcs=[{"port_label": "beam.fixed", "kind": "fixed"}],
        loads=[
            {
                "port_label": "beam.loaded",
                "type": "force_total",
                "force": [0.0, -100.0, 0.0],
            }
        ],
        target_size=target_size,
    )


def lame_spec(
    step_path: Path,
    manifest_path: Path,
    *,
    target_size: float = 5.0,
) -> dict[str, Any]:
    return _base_spec(
        step_path,
        manifest_path,
        analysis_id="024-c2-lame",
        bcs=[{"port_label": "cylinder.fixed", "kind": "fixed"}],
        loads=[
            {
                "port_label": f"cylinder.bore_{index:02d}",
                "type": "pressure",
                "pressure": 10.0,
            }
            for index in range(1, 9)
        ],
        target_size=target_size,
        timeout_s=600.0,
    )


def plate_spec(
    step_path: Path,
    manifest_path: Path,
    *,
    target_size: float = 20.0 / 12.0,
) -> dict[str, Any]:
    return _base_spec(
        step_path,
        manifest_path,
        analysis_id="024-c2-plate-hole",
        bcs=[{"port_label": "plate.fixed", "kind": "fixed"}],
        loads=[
            {
                "port_label": "plate.loaded",
                "type": "force_total",
                "force": [5000.0, 0.0, 0.0],
            }
        ],
        target_size=target_size,
        timeout_s=900.0,
    )


def evaluate_cantilever(
    *,
    mesh_text: str,
    frd_text: str,
    reaction_resultant: Sequence[float] | None,
    target_size: float,
) -> dict[str, Any]:
    mesh = parse_inp_mesh(mesh_text)
    displacement = displacement_block(parse_frd_blocks(frd_text))
    load_nodes = _required_node_set(mesh, "LOAD_beam_loaded")
    records = displacement["records"]
    samples = []
    for node_id in sorted(load_nodes):
        if node_id not in records:
            continue
        values = records[node_id]
        vector = [float(values[name]) for name in ("D1", "D2", "D3")]
        samples.append(
            {
                "node_id": node_id,
                "coordinates": list(mesh["node_coordinates"][node_id]),
                "displacement": vector,
                "magnitude_mm": math.sqrt(
                    math.fsum(value * value for value in vector)
                ),
            }
        )
    if len(samples) < 4:
        raise FemVerificationError(
            "CANTILEVER_TIP_UNDER_RESOLVED",
            {"sample_count": len(samples), "required": 4},
        )
    peak = max(samples, key=lambda item: (item["magnitude_mm"], -item["node_id"]))
    expected = beam_tip_displacement(
        force_n=100.0,
        length_mm=200.0,
        width_mm=10.0,
        height_mm=10.0,
        elastic_modulus_mpa=200000.0,
    )
    comparison = comparison_record(
        name="cantilever_tip_displacement",
        actual=float(peak["magnitude_mm"]),
        expected=expected,
        tolerance=0.02,
    )
    balance = nonzero_resultant_balance(
        applied_resultant_n=[0.0, -100.0, 0.0],
        reaction_resultant_n=reaction_resultant,
        primary_axis=1,
    )
    return {
        "case_id": "cantilever",
        "target_size_mm": target_size,
        "analytic": {
            "formula": "F L^3 / (3 E I), I = b h^3 / 12",
            "inputs": {
                "F_n": 100.0,
                "L_mm": 200.0,
                "b_mm": 10.0,
                "h_mm": 10.0,
                "E_mpa": 200000.0,
            },
            "expected_tip_displacement_mm": expected,
        },
        "sampling": {
            "surface_set": "LOAD_beam_loaded",
            "selected": peak,
            "sample_count": len(samples),
        },
        "comparison": comparison,
        "load_balance": balance,
        "verdict": _all_pass(comparison, balance),
    }


def evaluate_lame(
    *,
    mesh_text: str,
    frd_text: str,
    pressure_loads: Sequence[Mapping[str, Any]],
    reaction_resultant: Sequence[float] | None,
    target_size: float,
) -> dict[str, Any]:
    mesh = parse_inp_mesh(mesh_text)
    stress = stress_block(parse_frd_blocks(frd_text))
    records = stress["records"]
    expected_sets = [f"LOAD_cylinder_bore_{index:02d}" for index in range(1, 9)]
    audit = audit_segmented_pressure_surface(
        pressure_loads,
        expected_surface_sets=expected_sets,
        expected_area_mm2=2.0 * math.pi * 20.0 * 160.0,
        area_relative_tolerance=0.01,
        resultant_fraction_limit=0.005,
    )
    bore_nodes = _surface_nodes(mesh, expected_sets)
    candidates = []
    for node_id in sorted(bore_nodes & set(records)):
        x, y, z = mesh["node_coordinates"][node_id]
        radius = math.hypot(x, y)
        if abs(radius - 20.0) <= 0.2:
            candidates.append((node_id, x, y, z, radius))
    if not candidates:
        raise FemVerificationError("LAME_BORE_SAMPLES_MISSING", {})
    nearest_z_residual = min(abs(item[3] - 80.0) for item in candidates)
    layer = [
        item
        for item in candidates
        if abs(item[3] - 80.0) <= nearest_z_residual + 1.0e-6
    ]
    angles = {round(math.atan2(item[2], item[1]), 6) for item in layer}
    if len(layer) < 8 or len(angles) < 8:
        raise FemVerificationError(
            "LAME_ANGULAR_SAMPLES_INSUFFICIENT",
            {"sample_count": len(layer), "distinct_angles": len(angles)},
        )
    transformed = []
    for node_id, x, y, z, radius in layer:
        components = cylindrical_stress_components(records[node_id], x=x, y=y)
        transformed.append(
            {
                "node_id": node_id,
                "coordinates": [x, y, z],
                "radius_mm": radius,
                "stress_cartesian_mpa": records[node_id],
                "stress_cylindrical_mpa": components,
            }
        )
    mean_radius = deterministic_mean(item["radius_mm"] for item in transformed)
    mean_z = deterministic_mean(item["coordinates"][2] for item in transformed)
    if abs(mean_radius - 20.0) / 20.0 > 0.01 or abs(mean_z - 80.0) > target_size:
        raise FemVerificationError(
            "LAME_SAMPLE_LOCATION_INVALID",
            {
                "mean_radius_mm": mean_radius,
                "mean_z_mm": mean_z,
                "target_size_mm": target_size,
            },
        )
    expected = lame_open_end_bore_stresses(
        inner_radius_mm=20.0,
        outer_radius_mm=40.0,
        pressure_mpa=10.0,
    )
    hoop = deterministic_mean(
        item["stress_cylindrical_mpa"]["sigma_theta"] for item in transformed
    )
    radial = deterministic_mean(
        item["stress_cylindrical_mpa"]["sigma_r"] for item in transformed
    )
    axial = deterministic_mean(
        item["stress_cylindrical_mpa"]["sigma_z"] for item in transformed
    )
    hoop_comparison = comparison_record(
        name="lame_bore_hoop_stress",
        actual=hoop,
        expected=expected["sigma_theta_mpa"],
        tolerance=0.05,
    )
    radial_comparison = comparison_record(
        name="lame_bore_radial_stress",
        actual=radial,
        expected=expected["sigma_r_mpa"],
        tolerance=0.10,
    )
    if radial >= 0.0:
        radial_comparison["verdict"] = "fail"
        radial_comparison["sign_check"] = "fail"
    else:
        radial_comparison["sign_check"] = "pass"
    reaction_balance = self_equilibrated_reaction_balance(
        reaction_resultant_n=reaction_resultant,
        scalar_force_scale_n=float(audit["scalar_pressure_force_scale_n"]),
    )
    return {
        "case_id": "lame_open_end_cylinder",
        "target_size_mm": target_size,
        "analytic": {
            "formula": "Lamé open-end thick-cylinder solution",
            "inputs": {"a_mm": 20.0, "b_mm": 40.0, "p_mpa": 10.0},
            **expected,
        },
        "sampling": {
            "method": "bore nodes in axial layer nearest z=L/2; arithmetic mean",
            "sample_count": len(transformed),
            "distinct_angle_count": len(angles),
            "mean_radius_mm": mean_radius,
            "mean_z_mm": mean_z,
            "selected": transformed,
        },
        "sampled": {
            "sigma_theta_mpa": hoop,
            "sigma_r_mpa": radial,
            "sigma_z_mpa": axial,
        },
        "comparisons": [hoop_comparison, radial_comparison],
        "pressure_audit": audit,
        "reaction_balance": reaction_balance,
        "verdict": _all_pass(
            hoop_comparison,
            radial_comparison,
            audit,
            reaction_balance,
        ),
    }


def evaluate_plate(
    *,
    mesh_text: str,
    frd_text: str,
    reaction_resultant: Sequence[float] | None,
    target_size: float,
) -> dict[str, Any]:
    mesh = parse_inp_mesh(mesh_text)
    stress = stress_block(parse_frd_blocks(frd_text))
    records = stress["records"]
    selected = []
    for side, target_y in (("positive_y", 10.0), ("negative_y", -10.0)):
        sample = _nearest_stress_node(
            mesh["node_coordinates"],
            records,
            target=(100.0, target_y, 0.0),
            max_distance=target_size,
            radial_center=(100.0, 0.0),
            expected_radius=10.0,
            radius_tolerance=0.1,
            max_abs_z=target_size / 2.0,
        )
        transformed = cylindrical_stress_components(
            records[sample["node_id"]],
            x=sample["coordinates"][0],
            y=sample["coordinates"][1],
            center_x=100.0,
            center_y=0.0,
        )
        selected.append(
            {
                "side": side,
                **sample,
                "stress_cartesian_mpa": records[sample["node_id"]],
                "stress_local_mpa": transformed,
            }
        )
    if selected[0]["node_id"] == selected[1]["node_id"]:
        raise FemVerificationError("PLATE_SYMMETRIC_SAMPLE_DUPLICATE", {})
    expected = finite_width_hole_reference(
        force_n=5000.0,
        width_mm=100.0,
        diameter_mm=20.0,
        thickness_mm=5.0,
    )
    tangential = deterministic_mean(
        item["stress_local_mpa"]["sigma_theta"] for item in selected
    )
    comparison = comparison_record(
        name="finite_width_hole_tangential_stress",
        actual=tangential,
        expected=expected["sigma_peak_mpa"],
        tolerance=0.07,
    )
    balance = nonzero_resultant_balance(
        applied_resultant_n=[5000.0, 0.0, 0.0],
        reaction_resultant_n=reaction_resultant,
        primary_axis=0,
    )
    return {
        "case_id": "finite_width_plate_with_hole",
        "target_size_mm": target_size,
        "source": PILKEY_SOURCE,
        "analytic": {
            "formula": (
                "Kt = 3 - 3.14x + 3.667x^2 - 1.527x^3; "
                "sigma_peak = Kt sigma_nom,net"
            ),
            "inputs": {
                "F_n": 5000.0,
                "W_mm": 100.0,
                "d_mm": 20.0,
                "t_mm": 5.0,
            },
            **expected,
        },
        "sampling": {
            "method": "symmetric transverse-diameter points at mid-thickness",
            "selected": selected,
        },
        "sampled_tangential_stress_mpa": tangential,
        "comparison": comparison,
        "load_balance": balance,
        "diagnostic_global_max_only": True,
        "verdict": _all_pass(comparison, balance),
    }


def nonzero_resultant_balance(
    *,
    applied_resultant_n: Sequence[float],
    reaction_resultant_n: Sequence[float] | None,
    primary_axis: int,
) -> dict[str, Any]:
    applied = _vector3(applied_resultant_n, "applied_resultant_n")
    reaction = _vector3(reaction_resultant_n, "reaction_resultant_n")
    scale = math.sqrt(math.fsum(value * value for value in applied))
    if scale <= 0.0:
        raise FemVerificationError("LOAD_BALANCE_ZERO_SCALE", {})
    imbalance = [applied[index] + reaction[index] for index in range(3)]
    imbalance_norm = math.sqrt(math.fsum(value * value for value in imbalance))
    transverse = [
        abs(reaction[index]) for index in range(3) if index != primary_axis
    ]
    max_transverse = max(transverse, default=0.0)
    return {
        "applied_resultant_n": applied,
        "reaction_resultant_n": reaction,
        "imbalance_n": imbalance,
        "imbalance_norm_n": imbalance_norm,
        "imbalance_limit_n": 0.01 * scale,
        "max_transverse_reaction_n": max_transverse,
        "transverse_limit_n": 0.005 * scale,
        "verdict": (
            "pass"
            if imbalance_norm <= 0.01 * scale
            and max_transverse <= 0.005 * scale
            else "fail"
        ),
    }


def self_equilibrated_reaction_balance(
    *,
    reaction_resultant_n: Sequence[float] | None,
    scalar_force_scale_n: float,
) -> dict[str, Any]:
    reaction = _vector3(reaction_resultant_n, "reaction_resultant_n")
    if not math.isfinite(scalar_force_scale_n) or scalar_force_scale_n <= 0.0:
        raise FemVerificationError(
            "REACTION_BALANCE_SCALE_INVALID",
            {"scale": scalar_force_scale_n},
        )
    norm = math.sqrt(math.fsum(value * value for value in reaction))
    limit = 0.005 * scalar_force_scale_n
    return {
        "reaction_resultant_n": reaction,
        "reaction_norm_n": norm,
        "reaction_limit_n": limit,
        "verdict": "pass" if norm <= limit else "fail",
    }


def build_battery_report(
    *,
    generated_at: str,
    git_sha: str,
    environment: Mapping[str, str],
    toolchain: Mapping[str, Any],
    fixture_verification: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    artifacts: Mapping[str, Any],
) -> dict[str, Any]:
    if not generated_at or not git_sha:
        raise FemVerificationError("REPORT_IDENTITY_MISSING", {})
    case_list = [dict(case) for case in cases]
    if {case.get("case_id") for case in case_list} != {
        "cantilever",
        "lame_open_end_cylinder",
        "finite_width_plate_with_hole",
    }:
        raise FemVerificationError("REPORT_CASE_SET_INVALID", {})
    return {
        "schema_version": "bluecad_fem_verification_battery_v0_1",
        "generated_at": generated_at,
        "git_sha": git_sha,
        "environment": dict(sorted(environment.items())),
        "toolchain": dict(toolchain),
        "fixture_verification": dict(fixture_verification),
        "cases": case_list,
        "artifacts": dict(artifacts),
        "limitations": [
            "linear elasticity",
            "static analysis",
            "ideal benchmark geometry",
            "acceptance evidence, not engineering certification",
        ],
        "verdict": (
            "pass"
            if all(case.get("verdict") == "pass" for case in case_list)
            else "fail"
        ),
    }


def render_battery_report(
    report: Mapping[str, Any], out_dir: str | Path
) -> dict[str, str]:
    root = Path(out_dir)
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "bluecad_fem_verification_battery.json"
    markdown_path = root / "bluecad_fem_verification_battery.md"
    json_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    markdown_path.write_text(_report_markdown(report), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def _report_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# BLUECAD FEM verification battery",
        "",
        f"- Verdict: **{report['verdict']}**",
        f"- Generated at: `{report['generated_at']}`",
        f"- Git SHA: `{report['git_sha']}`",
        "",
    ]
    for case in report["cases"]:
        lines.extend(
            [
                f"## {case['case_id']}",
                "",
                f"Verdict: **{case['verdict']}**",
                "",
            ]
        )
        comparisons = case.get("comparisons") or [case.get("comparison")]
        for comparison in comparisons:
            if comparison:
                lines.append(
                    f"- `{comparison['name']}`: actual "
                    f"`{comparison['actual']:.9g}`, expected "
                    f"`{comparison['expected']:.9g}`, relative error "
                    f"`{comparison['relative_error']:.6%}`, tolerance "
                    f"`{comparison['tolerance']:.6%}` — "
                    f"**{comparison['verdict']}**"
                )
        lines.append("")
    lines.extend(["## Limitations", ""])
    lines.extend(f"- {item}" for item in report["limitations"])
    return "\n".join(lines) + "\n"


def _base_spec(
    step_path: Path,
    manifest_path: Path,
    *,
    analysis_id: str,
    bcs: list[dict[str, Any]],
    loads: list[dict[str, Any]],
    target_size: float,
    timeout_s: float = 300.0,
) -> dict[str, Any]:
    return {
        "schema_version": "bluecad_analysis_spec_v0_1",
        "analysis_id": analysis_id,
        "analysis_type": "static",
        "geometry": {
            "step_path": str(step_path),
            "manifest_path": str(manifest_path),
        },
        "material": dict(MATERIAL),
        "bcs": bcs,
        "loads": loads,
        "mesh": {"target_size": target_size, "element_order": 2},
        "pass_criteria": [],
        "timeout_s": timeout_s,
    }


def _required_node_set(mesh: Mapping[str, Any], name: str) -> set[int]:
    values = set(mesh.get("node_sets", {}).get(name, ()))
    if not values:
        raise FemVerificationError("REQUIRED_NODE_SET_MISSING", {"name": name})
    return {int(value) for value in values}


def _surface_nodes(mesh: Mapping[str, Any], names: Sequence[str]) -> set[int]:
    nodes: set[int] = set()
    for name in names:
        element_ids = set(mesh.get("element_sets", {}).get(name, ()))
        if not element_ids:
            raise FemVerificationError(
                "REQUIRED_SURFACE_SET_MISSING",
                {"name": name},
            )
        for element_id in element_ids:
            element = mesh["elements"].get(element_id)
            if element is None:
                raise FemVerificationError(
                    "SURFACE_ELEMENT_MISSING",
                    {"element_id": element_id},
                )
            nodes.update(int(value) for value in element["nodes"])
    return nodes


def _nearest_stress_node(
    coordinates: Mapping[int, Sequence[float]],
    records: Mapping[int, Mapping[str, float]],
    *,
    target: tuple[float, float, float],
    max_distance: float,
    radial_center: tuple[float, float],
    expected_radius: float,
    radius_tolerance: float,
    max_abs_z: float,
) -> dict[str, Any]:
    candidates = []
    for node_id in sorted(set(coordinates) & set(records)):
        x, y, z = (float(value) for value in coordinates[node_id][:3])
        radius = math.hypot(x - radial_center[0], y - radial_center[1])
        if abs(radius - expected_radius) > radius_tolerance or abs(z) > max_abs_z:
            continue
        distance = math.sqrt(
            (x - target[0]) ** 2
            + (y - target[1]) ** 2
            + (z - target[2]) ** 2
        )
        candidates.append((distance, node_id, x, y, z, radius))
    if not candidates:
        raise FemVerificationError(
            "PLATE_HOLE_SAMPLE_MISSING",
            {"target": list(target)},
        )
    distance, node_id, x, y, z, radius = min(candidates)
    if distance > max_distance:
        raise FemVerificationError(
            "PLATE_HOLE_SAMPLE_TOO_REMOTE",
            {
                "target": list(target),
                "distance_mm": distance,
                "limit_mm": max_distance,
            },
        )
    return {
        "node_id": node_id,
        "coordinates": [x, y, z],
        "target": list(target),
        "location_residual_mm": distance,
        "radius_mm": radius,
    }


def _vector3(values: Sequence[float] | None, name: str) -> list[float]:
    if values is None or len(values) != 3:
        raise FemVerificationError("VECTOR3_INVALID", {"name": name})
    vector = [float(value) for value in values]
    if not all(math.isfinite(value) for value in vector):
        raise FemVerificationError("VECTOR3_INVALID", {"name": name})
    return vector


def _all_pass(*records: Mapping[str, Any]) -> str:
    return (
        "pass"
        if all(record.get("verdict") == "pass" for record in records)
        else "fail"
    )
