"""Location sampling, tensor transforms and pressure audits for FEM verification."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from app.modules.bluecad.fem_verification_common import (
    FemVerificationError,
    relative_error,
)
from app.modules.bluecad.fem_verification_parsers import STRESS_COMPONENTS

_LOCATION_KEYS = frozenset({"x", "y", "z", "radius_xy"})


def cylindrical_stress_components(
    stress: Mapping[str, float],
    *,
    x: float,
    y: float,
    center_x: float = 0.0,
    center_y: float = 0.0,
) -> dict[str, float]:
    """Transform a Cartesian tensor to local cylindrical components about z."""

    try:
        values = {name: float(stress[name]) for name in STRESS_COMPONENTS}
    except (KeyError, TypeError, ValueError) as exc:
        raise FemVerificationError("STRESS_TENSOR_COMPONENT_MISSING", {}) from exc
    if not all(math.isfinite(value) for value in values.values()):
        raise FemVerificationError("STRESS_TENSOR_NONFINITE", {})
    dx = x - center_x
    dy = y - center_y
    radius = math.hypot(dx, dy)
    if not math.isfinite(radius) or radius <= 0.0:
        raise FemVerificationError("CYLINDRICAL_DIRECTION_UNDEFINED", {"x": x, "y": y})
    cosine = dx / radius
    sine = dy / radius
    sxx = values["SXX"]
    syy = values["SYY"]
    sxy = values["SXY"]
    sigma_r = cosine**2 * sxx + sine**2 * syy + 2.0 * cosine * sine * sxy
    sigma_theta = sine**2 * sxx + cosine**2 * syy - 2.0 * cosine * sine * sxy
    tau_rtheta = (syy - sxx) * cosine * sine + sxy * (cosine**2 - sine**2)
    return {
        "sigma_r": sigma_r,
        "sigma_theta": sigma_theta,
        "sigma_z": values["SZZ"],
        "tau_rtheta": tau_rtheta,
        "tau_thetaz": -sine * values["SZX"] + cosine * values["SYZ"],
        "tau_zr": cosine * values["SZX"] + sine * values["SYZ"],
    }


def select_nodes_near(
    node_coordinates: Mapping[int, Sequence[float]],
    *,
    targets: Mapping[str, float],
    tolerances: Mapping[str, float],
    candidate_node_ids: Iterable[int] | None = None,
    min_count: int = 1,
) -> list[dict[str, Any]]:
    """Select nodes satisfying explicit coordinate/radius residual limits."""

    keys = set(targets)
    if not keys or keys != set(tolerances) or not keys <= _LOCATION_KEYS:
        raise FemVerificationError(
            "LOCATION_CONSTRAINTS_INVALID",
            {
                "targets": sorted(targets),
                "tolerances": sorted(tolerances),
            },
        )
    if type(min_count) is not int or min_count <= 0:
        raise FemVerificationError(
            "LOCATION_MIN_COUNT_INVALID", {"min_count": min_count}
        )
    for key in keys:
        target = float(targets[key])
        tolerance = float(tolerances[key])
        if not math.isfinite(target) or not math.isfinite(tolerance) or tolerance < 0:
            raise FemVerificationError(
                "LOCATION_LIMIT_INVALID",
                {"key": key, "target": target, "tolerance": tolerance},
            )

    candidate_ids = (
        sorted(int(value) for value in candidate_node_ids)
        if candidate_node_ids is not None
        else sorted(int(value) for value in node_coordinates)
    )
    selected: list[dict[str, Any]] = []
    for node_id in candidate_ids:
        coordinates = node_coordinates.get(node_id)
        if coordinates is None or len(coordinates) < 3:
            raise FemVerificationError("LOCATION_NODE_MISSING", {"node_id": node_id})
        x, y, z = (float(value) for value in coordinates[:3])
        values = {"x": x, "y": y, "z": z, "radius_xy": math.hypot(x, y)}
        residuals = {key: abs(values[key] - float(targets[key])) for key in keys}
        if all(residuals[key] <= float(tolerances[key]) for key in keys):
            selected.append(
                {
                    "node_id": node_id,
                    "coordinates": [x, y, z],
                    "residuals": {key: residuals[key] for key in sorted(keys)},
                }
            )
    if len(selected) < min_count:
        raise FemVerificationError(
            "LOCATION_UNDER_RESOLVED",
            {
                "selected_count": len(selected),
                "min_count": min_count,
                "targets": dict(targets),
                "tolerances": dict(tolerances),
            },
        )
    return selected


def audit_segmented_pressure_surface(
    load_evidence: Sequence[Mapping[str, Any]],
    *,
    expected_surface_sets: Sequence[str],
    expected_area_mm2: float,
    area_relative_tolerance: float = 0.01,
    resultant_fraction_limit: float = 0.005,
) -> dict[str, Any]:
    """Audit segmented pressure groups for uniqueness, area and equilibrium."""

    expected_names = list(expected_surface_sets)
    if len(expected_names) != len(set(expected_names)) or not expected_names:
        raise FemVerificationError("PRESSURE_AUDIT_GROUPS_INVALID", {})
    by_name: dict[str, Mapping[str, Any]] = {}
    for item in load_evidence:
        name = str(item.get("surface_set", ""))
        if not name or name in by_name:
            raise FemVerificationError(
                "PRESSURE_AUDIT_GROUP_DUPLICATE", {"surface_set": name}
            )
        by_name[name] = item
    missing = sorted(set(expected_names) - set(by_name))
    unexpected = sorted(set(by_name) - set(expected_names))
    if missing or unexpected:
        raise FemVerificationError(
            "PRESSURE_AUDIT_GROUP_SET_MISMATCH",
            {"missing": missing, "unexpected": unexpected},
        )

    used_surface_elements: set[int] = set()
    used_body_faces: set[tuple[int, int]] = set()
    total_area = 0.0
    resultant = [0.0, 0.0, 0.0]
    scalar_scale = 0.0
    group_counts: dict[str, int] = {}
    for name in expected_names:
        item = by_name[name]
        mappings = list(item.get("mappings", ()))
        if not mappings:
            raise FemVerificationError(
                "PRESSURE_AUDIT_GROUP_EMPTY", {"surface_set": name}
            )
        group_counts[name] = len(mappings)
        for mapping in mappings:
            surface_element_id = int(mapping["surface_element_id"])
            body_face = (
                int(mapping["body_element_id"]),
                int(mapping["local_face_number"]),
            )
            if surface_element_id in used_surface_elements:
                raise FemVerificationError(
                    "PRESSURE_AUDIT_DUPLICATE_SURFACE_ELEMENT",
                    {"surface_element_id": surface_element_id},
                )
            if body_face in used_body_faces:
                raise FemVerificationError(
                    "PRESSURE_AUDIT_DUPLICATE_BODY_FACE",
                    {
                        "body_element_id": body_face[0],
                        "local_face_number": body_face[1],
                    },
                )
            used_surface_elements.add(surface_element_id)
            used_body_faces.add(body_face)
        area = float(item.get("area_mm2", math.nan))
        force = [float(value) for value in item.get("applied_force_resultant_n", ())]
        scale = float(item.get("scalar_pressure_force_n", math.nan))
        if (
            not math.isfinite(area)
            or len(force) != 3
            or not all(math.isfinite(value) for value in force)
            or not math.isfinite(scale)
        ):
            raise FemVerificationError(
                "PRESSURE_AUDIT_EVIDENCE_INVALID", {"surface_set": name}
            )
        total_area += area
        scalar_scale += abs(scale)
        for axis in range(3):
            resultant[axis] += force[axis]

    if not math.isfinite(expected_area_mm2) or expected_area_mm2 <= 0.0:
        raise FemVerificationError(
            "PRESSURE_AUDIT_EXPECTED_AREA_INVALID",
            {"expected_area_mm2": expected_area_mm2},
        )
    if area_relative_tolerance < 0.0 or resultant_fraction_limit < 0.0:
        raise FemVerificationError("PRESSURE_AUDIT_LIMIT_INVALID", {})
    area_error = relative_error(total_area, expected_area_mm2)
    resultant_norm = math.sqrt(math.fsum(value * value for value in resultant))
    resultant_limit = resultant_fraction_limit * scalar_scale
    verdict = (
        "pass"
        if area_error <= area_relative_tolerance and resultant_norm <= resultant_limit
        else "fail"
    )
    return {
        "surface_sets": expected_names,
        "group_mapping_counts": group_counts,
        "unique_surface_element_count": len(used_surface_elements),
        "unique_body_face_count": len(used_body_faces),
        "area_mm2": total_area,
        "expected_area_mm2": expected_area_mm2,
        "area_relative_error": area_error,
        "area_relative_tolerance": area_relative_tolerance,
        "applied_force_resultant_n": resultant,
        "applied_force_resultant_norm_n": resultant_norm,
        "scalar_pressure_force_scale_n": scalar_scale,
        "resultant_limit_n": resultant_limit,
        "verdict": verdict,
    }
