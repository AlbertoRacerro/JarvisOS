"""Deterministic boundary-surface to solid-face mapping for pressure loads."""

from __future__ import annotations

import math
from typing import Any


class PressureMappingError(ValueError):
    """Structured fail-closed pressure mapping error."""

    def __init__(self, code: str, detail: dict[str, Any]) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


_FACE_NODE_INDEXES: dict[str, dict[int, tuple[int, ...]]] = {
    "C3D4": {
        1: (0, 1, 2),
        2: (0, 3, 1),
        3: (1, 3, 2),
        4: (2, 3, 0),
    },
    "C3D10": {
        1: (0, 1, 2, 4, 5, 6),
        2: (0, 3, 1, 7, 8, 4),
        3: (1, 3, 2, 8, 9, 5),
        4: (2, 3, 0, 9, 7, 6),
    },
}
_EXPECTED_CONNECTIVITY = {
    "C3D4": 4,
    "C3D10": 10,
    "S3": 3,
    "CPS3": 3,
    "S6": 6,
    "CPS6": 6,
}
_CANONICAL_SURFACE_TYPE = {
    "S3": "S3",
    "CPS3": "S3",
    "S6": "S6",
    "CPS6": "S6",
}
_SURFACE_TO_SOLID = {
    "S3": "C3D4",
    "S6": "C3D10",
}


def map_pressure_surface(
    mesh: dict[str, Any],
    surface_set: str,
    *,
    body_set: str = "BODY",
) -> list[dict[str, Any]]:
    """Map every supported surface element to exactly one BODY tetra face."""

    element_sets = mesh.get("element_sets", {})
    entries = list(mesh.get("element_set_entries", {}).get(surface_set, ()))
    if surface_set not in element_sets or not entries:
        raise PressureMappingError(
            "PRESSURE_GROUP_MISSING_OR_EMPTY",
            {"surface_set": surface_set},
        )
    if len(entries) != len(set(entries)):
        raise PressureMappingError(
            "PRESSURE_GROUP_DUPLICATE_MEMBER",
            {"surface_set": surface_set},
        )

    body_ids = set(element_sets.get(body_set, ()))
    if not body_ids:
        raise PressureMappingError(
            "PRESSURE_BODY_MISSING_OR_EMPTY",
            {"body_set": body_set},
        )

    elements = mesh.get("elements", {})
    node_coordinates = mesh.get("node_coordinates", {})
    body_types: set[str] = set()
    for element_id in sorted(body_ids):
        element = elements.get(element_id)
        if element is None:
            raise PressureMappingError(
                "PRESSURE_BODY_MEMBER_MISSING",
                {"body_element_id": element_id},
            )
        element_type = str(element.get("type", "")).upper()
        if element_type not in _FACE_NODE_INDEXES:
            raise PressureMappingError(
                "PRESSURE_UNSUPPORTED_SOLID_FAMILY",
                {
                    "body_element_id": element_id,
                    "body_element_type": element_type,
                },
            )
        _validate_connectivity(
            element_id,
            element_type,
            element.get("nodes"),
            node_coordinates,
        )
        body_types.add(element_type)

    if len(body_types) != 1:
        raise PressureMappingError(
            "PRESSURE_MIXED_SOLID_ORDER",
            {"body_element_types": sorted(body_types)},
        )

    canonical_surface_types: set[str] = set()
    raw_surface_types: set[str] = set()
    surface_elements: list[tuple[int, dict[str, Any]]] = []
    seen_surface_topology: set[frozenset[int]] = set()

    for element_id in entries:
        element = elements.get(element_id)
        if element is None:
            raise PressureMappingError(
                "PRESSURE_GROUP_MEMBER_MISSING",
                {
                    "surface_set": surface_set,
                    "surface_element_id": element_id,
                },
            )

        element_type = str(element.get("type", "")).upper()
        if element_type.startswith(("C3D", "DC3D")):
            raise PressureMappingError(
                "PRESSURE_GROUP_NON_SURFACE_MEMBER",
                {
                    "surface_set": surface_set,
                    "surface_element_id": element_id,
                    "element_type": element_type,
                },
            )
        canonical_type = _CANONICAL_SURFACE_TYPE.get(element_type)
        if canonical_type is None:
            raise PressureMappingError(
                "PRESSURE_UNSUPPORTED_SURFACE_FAMILY",
                {
                    "surface_set": surface_set,
                    "surface_element_id": element_id,
                    "surface_element_type": element_type,
                },
            )

        nodes = _validate_connectivity(
            element_id,
            element_type,
            element.get("nodes"),
            node_coordinates,
        )
        topology = frozenset(nodes)
        if topology in seen_surface_topology:
            raise PressureMappingError(
                "PRESSURE_DUPLICATE_SURFACE_ASSIGNMENT",
                {
                    "surface_set": surface_set,
                    "surface_element_id": element_id,
                },
            )
        seen_surface_topology.add(topology)
        canonical_surface_types.add(canonical_type)
        raw_surface_types.add(element_type)
        surface_elements.append((element_id, element))

    if len(canonical_surface_types) != 1:
        raise PressureMappingError(
            "PRESSURE_MIXED_SURFACE_ORDER",
            {
                "surface_set": surface_set,
                "surface_element_types": sorted(raw_surface_types),
            },
        )

    surface_type = next(iter(canonical_surface_types))
    required_solid_type = _SURFACE_TO_SOLID[surface_type]
    if body_types != {required_solid_type}:
        raise PressureMappingError(
            "PRESSURE_MIXED_ORDER",
            {
                "surface_set": surface_set,
                "surface_element_type": surface_type,
                "body_element_types": sorted(body_types),
            },
        )

    all_face_index = _face_index(elements, node_coordinates)
    mappings: list[dict[str, Any]] = []
    used_body_faces: set[tuple[int, int]] = set()

    for surface_element_id, surface_element in surface_elements:
        surface_nodes = tuple(int(node) for node in surface_element["nodes"])
        candidates = all_face_index.get(frozenset(surface_nodes), [])
        body_candidates = [
            candidate for candidate in candidates if candidate[0] in body_ids
        ]
        outside_candidates = [
            candidate for candidate in candidates if candidate[0] not in body_ids
        ]

        if not body_candidates and outside_candidates:
            raise PressureMappingError(
                "PRESSURE_VOLUME_OUTSIDE_BODY",
                {
                    "surface_set": surface_set,
                    "surface_element_id": surface_element_id,
                    "candidate_body_element_ids": sorted(
                        {candidate[0] for candidate in outside_candidates}
                    ),
                },
            )

        if not body_candidates:
            if _corner_only_order_mismatch(
                surface_nodes,
                surface_type,
                elements,
                body_ids,
                node_coordinates,
            ):
                raise PressureMappingError(
                    "PRESSURE_MIXED_ORDER",
                    {
                        "surface_set": surface_set,
                        "surface_element_id": surface_element_id,
                    },
                )
            raise PressureMappingError(
                "PRESSURE_SURFACE_DETACHED",
                {
                    "surface_set": surface_set,
                    "surface_element_id": surface_element_id,
                },
            )

        if len(body_candidates) != 1:
            raise PressureMappingError(
                "PRESSURE_AMBIGUOUS_NON_MANIFOLD",
                {
                    "surface_set": surface_set,
                    "surface_element_id": surface_element_id,
                    "candidate_faces": [
                        {
                            "body_element_id": item[0],
                            "local_face_number": item[1],
                        }
                        for item in sorted(body_candidates)
                    ],
                },
            )

        body_element_id, local_face_number, matched_face_nodes = body_candidates[0]
        body_face_key = (body_element_id, local_face_number)
        if body_face_key in used_body_faces:
            raise PressureMappingError(
                "PRESSURE_DUPLICATE_BODY_FACE",
                {
                    "surface_set": surface_set,
                    "surface_element_id": surface_element_id,
                    "body_element_id": body_element_id,
                    "local_face_number": local_face_number,
                },
            )
        used_body_faces.add(body_face_key)

        body_element = elements[body_element_id]
        area_mm2, outward_normal = _face_geometry(
            body_element,
            local_face_number,
            node_coordinates,
        )
        mappings.append(
            {
                "surface_set": surface_set,
                "surface_element_id": surface_element_id,
                "surface_element_type": str(surface_element["type"]).upper(),
                "surface_nodes": list(surface_nodes),
                "body_element_id": body_element_id,
                "body_element_type": str(body_element["type"]).upper(),
                "local_face_number": local_face_number,
                "face_label": f"P{local_face_number}",
                "matched_face_nodes": list(matched_face_nodes),
                "area_mm2": area_mm2,
                "outward_unit_normal": list(outward_normal),
            }
        )

    return mappings


def pressure_load_evidence(
    surface_set: str,
    pressure: float,
    mappings: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build deterministic scalar-area and vector-resultant evidence."""

    if not math.isfinite(pressure):
        raise PressureMappingError(
            "PRESSURE_VALUE_INVALID",
            {"surface_set": surface_set},
        )

    area_mm2 = sum(float(item["area_mm2"]) for item in mappings)
    applied_force = [0.0, 0.0, 0.0]
    for item in mappings:
        item_area = float(item["area_mm2"])
        for axis, normal in enumerate(item["outward_unit_normal"]):
            applied_force[axis] -= pressure * item_area * float(normal)

    if not math.isfinite(area_mm2) or not all(
        math.isfinite(value) for value in applied_force
    ):
        raise PressureMappingError(
            "PRESSURE_RESULTANT_INVALID",
            {"surface_set": surface_set},
        )

    return {
        "surface_set": surface_set,
        "pressure_mpa": pressure,
        "mapping_count": len(mappings),
        "area_mm2": area_mm2,
        "scalar_pressure_force_n": abs(pressure) * area_mm2,
        "applied_force_resultant_n": applied_force,
        "mappings": mappings,
    }


def _face_index(
    elements: dict[int, dict[str, Any]],
    node_coordinates: dict[int, tuple[float, float, float]],
) -> dict[frozenset[int], list[tuple[int, int, tuple[int, ...]]]]:
    index: dict[frozenset[int], list[tuple[int, int, tuple[int, ...]]]] = {}
    for element_id, element in elements.items():
        element_type = str(element.get("type", "")).upper()
        if element_type not in _FACE_NODE_INDEXES:
            continue
        nodes = _validate_connectivity(
            int(element_id),
            element_type,
            element.get("nodes"),
            node_coordinates,
        )
        for local_face_number, indexes in _FACE_NODE_INDEXES[element_type].items():
            face_nodes = tuple(nodes[index_value] for index_value in indexes)
            index.setdefault(frozenset(face_nodes), []).append(
                (int(element_id), local_face_number, face_nodes)
            )
    return index


def _corner_only_order_mismatch(
    surface_nodes: tuple[int, ...],
    surface_type: str,
    elements: dict[int, dict[str, Any]],
    body_ids: set[int],
    node_coordinates: dict[int, tuple[float, float, float]],
) -> bool:
    corners = frozenset(surface_nodes[:3])
    required_solid_type = _SURFACE_TO_SOLID[surface_type]
    for element_id in body_ids:
        element = elements[element_id]
        element_type = str(element.get("type", "")).upper()
        nodes = _validate_connectivity(
            element_id,
            element_type,
            element.get("nodes"),
            node_coordinates,
        )
        for indexes in _FACE_NODE_INDEXES[element_type].values():
            face_corners = frozenset(nodes[index_value] for index_value in indexes[:3])
            if face_corners == corners:
                return element_type != required_solid_type
    return False


def _validate_connectivity(
    element_id: int,
    element_type: str,
    raw_nodes: Any,
    node_coordinates: dict[int, tuple[float, float, float]],
) -> tuple[int, ...]:
    expected = _EXPECTED_CONNECTIVITY[element_type]
    if not isinstance(raw_nodes, (list, tuple)):
        raise PressureMappingError(
            "PRESSURE_MALFORMED_CONNECTIVITY",
            {
                "element_id": element_id,
                "element_type": element_type,
            },
        )
    try:
        nodes = tuple(int(node) for node in raw_nodes)
    except (TypeError, ValueError) as exc:
        raise PressureMappingError(
            "PRESSURE_MALFORMED_CONNECTIVITY",
            {
                "element_id": element_id,
                "element_type": element_type,
            },
        ) from exc

    if len(nodes) != expected or len(set(nodes)) != expected:
        raise PressureMappingError(
            "PRESSURE_MALFORMED_CONNECTIVITY",
            {
                "element_id": element_id,
                "element_type": element_type,
                "expected_nodes": expected,
                "actual_nodes": len(nodes),
            },
        )

    missing_nodes = sorted(node for node in nodes if node not in node_coordinates)
    if missing_nodes:
        raise PressureMappingError(
            "PRESSURE_MALFORMED_CONNECTIVITY",
            {
                "element_id": element_id,
                "element_type": element_type,
                "missing_node_ids": missing_nodes,
            },
        )
    return nodes


def _face_geometry(
    body_element: dict[str, Any],
    local_face_number: int,
    node_coordinates: dict[int, tuple[float, float, float]],
) -> tuple[float, tuple[float, float, float]]:
    element_type = str(body_element["type"]).upper()
    nodes = tuple(int(node) for node in body_element["nodes"])
    face_indexes = _FACE_NODE_INDEXES[element_type][local_face_number]
    a, b, c = (
        node_coordinates[nodes[index_value]]
        for index_value in face_indexes[:3]
    )

    ab = tuple(b[axis] - a[axis] for axis in range(3))
    ac = tuple(c[axis] - a[axis] for axis in range(3))
    normal = (
        ab[1] * ac[2] - ab[2] * ac[1],
        ab[2] * ac[0] - ab[0] * ac[2],
        ab[0] * ac[1] - ab[1] * ac[0],
    )
    norm = math.sqrt(sum(value * value for value in normal))
    if not math.isfinite(norm) or norm <= 0.0:
        raise PressureMappingError(
            "PRESSURE_DEGENERATE_FACE",
            {"local_face_number": local_face_number},
        )

    face_corner_ids = {nodes[index_value] for index_value in face_indexes[:3]}
    opposite_id = next(node for node in nodes[:4] if node not in face_corner_ids)
    opposite = node_coordinates[opposite_id]
    centroid = tuple(
        (a[axis] + b[axis] + c[axis]) / 3.0
        for axis in range(3)
    )
    to_opposite = tuple(
        opposite[axis] - centroid[axis]
        for axis in range(3)
    )
    if sum(normal[axis] * to_opposite[axis] for axis in range(3)) > 0.0:
        normal = tuple(-value for value in normal)

    unit = tuple(value / norm for value in normal)
    return 0.5 * norm, unit
