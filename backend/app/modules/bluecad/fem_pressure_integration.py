"""Pressure-load integration helpers for the BLUECAD CalculiX adapter."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from app.modules.bluecad import fem_adapter_base as _base
from app.modules.bluecad.mesh_adapter import _group_label
from app.modules.bluecad.pressure_mapping import (
    PressureMappingError,
    map_pressure_surface,
    pressure_load_evidence,
)

_VOLUME_ELEMENT_PREFIXES = ("C3D", "DC3D")
_SOLID_PRESSURE_ELEMENT_TYPES = frozenset({"C3D4", "C3D10"})
_artifact_map = _base._artifact_map
_integer_values = _base._integer_values
_parse_header = _base._parse_header
_require_element_set = _base._require_element_set
_require_node_set = _base._require_node_set


def _prepare_pressure_mappings(
    spec: dict[str, Any],
    mesh: dict[str, Any],
    out_path: Path,
    artifacts: dict[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    pressure_mappings: dict[str, list[dict[str, Any]]] = {}
    load_evidence: list[dict[str, Any]] = []
    used_faces: dict[tuple[int, int], str] = {}
    for load in spec.get("loads", []):
        if load.get("type") != "pressure":
            continue
        surface_set = f'LOAD_{_group_label(load["port_label"])}'
        mappings = map_pressure_surface(mesh, surface_set)
        for mapping in mappings:
            key = (
                int(mapping["body_element_id"]),
                int(mapping["local_face_number"]),
            )
            previous = used_faces.get(key)
            if previous is not None:
                raise PressureMappingError(
                    "PRESSURE_DUPLICATE_BODY_FACE",
                    {
                        "surface_set": surface_set,
                        "previous_surface_set": previous,
                        "body_element_id": key[0],
                        "local_face_number": key[1],
                    },
                )
            used_faces[key] = surface_set
        pressure = float(load["pressure"])
        pressure_mappings[surface_set] = mappings
        load_evidence.append(
            pressure_load_evidence(surface_set, pressure, mappings)
        )
    if load_evidence:
        mapping_path = out_path / "pressure_face_mapping.json"
        mapping_path.write_text(
            json.dumps(
                {
                    "schema_version": "bluecad_pressure_face_mapping_v0_1",
                    "body_set": "BODY",
                    "loads": load_evidence,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        artifacts.update(
            _artifact_map({"pressure_face_mapping": mapping_path})
        )
    return pressure_mappings


def _write_solid_solver_mesh(
    mesh: dict[str, Any],
    out_path: Path,
    artifacts: dict[str, Any],
) -> Path:
    """Write a BODY-only mesh so boundary evidence is not solved as 2D elements."""

    solver_mesh_path = out_path / "solver_mesh.inp"
    solver_mesh_path.write_text(
        _solid_solver_mesh_text(mesh),
        encoding="utf-8",
    )
    artifacts.update(_artifact_map({"solver_mesh_inp": solver_mesh_path}))
    return solver_mesh_path


def _solid_solver_mesh_text(mesh: dict[str, Any]) -> str:
    """Render only BODY tetrahedra plus node sets needed by the solver deck.

    Gmsh's CalculiX export includes T3D3/CPS3/CPS6 boundary elements. They are
    retained in the source mesh for topology mapping and audit, but must not enter
    the CalculiX solve because they are standalone 1D/2D elements with no section
    or thickness in this solid-only contract.
    """

    _require_element_set(mesh, "BODY")
    body_ids = sorted(int(value) for value in mesh["element_sets"]["BODY"])
    elements = mesh["elements"]
    node_coordinates = mesh["node_coordinates"]

    body_by_type: dict[str, list[tuple[int, tuple[int, ...]]]] = {}
    required_node_ids: set[int] = set()
    for element_id in body_ids:
        element = elements.get(element_id)
        if element is None:
            raise ValueError(f"BODY element is missing: {element_id}")
        element_type = str(element.get("type", "")).upper()
        if element_type not in _SOLID_PRESSURE_ELEMENT_TYPES:
            raise ValueError(
                f"unsupported BODY element type for solid solver mesh: {element_type}"
            )
        nodes = tuple(int(node) for node in element.get("nodes", ()))
        expected = 4 if element_type == "C3D4" else 10
        if len(nodes) != expected or len(set(nodes)) != expected:
            raise ValueError(
                f"invalid {element_type} connectivity for BODY element {element_id}"
            )
        missing = sorted(node for node in nodes if node not in node_coordinates)
        if missing:
            raise ValueError(
                f"BODY element {element_id} references missing nodes: {missing}"
            )
        required_node_ids.update(nodes)
        body_by_type.setdefault(element_type, []).append((element_id, nodes))

    for node_set in mesh.get("node_sets", {}).values():
        required_node_ids.update(int(node) for node in node_set)

    missing_coordinates = sorted(
        node for node in required_node_ids if node not in node_coordinates
    )
    if missing_coordinates:
        raise ValueError(
            f"solver mesh node sets reference missing nodes: {missing_coordinates}"
        )

    lines = [
        "*HEADING",
        "** BLUECAD solid-only solver mesh derived from retained source mesh",
        "*NODE",
    ]
    for node_id in sorted(required_node_ids):
        x, y, z = node_coordinates[node_id]
        lines.append(
            f"{node_id},{_format_solver_coordinate(x)},"
            f"{_format_solver_coordinate(y)},{_format_solver_coordinate(z)}"
        )

    for element_type in sorted(body_by_type):
        lines.append(f"*ELEMENT, TYPE={element_type}, ELSET=BODY")
        for element_id, nodes in body_by_type[element_type]:
            connectivity = ",".join(str(node) for node in nodes)
            lines.append(f"{element_id},{connectivity}")

    for set_name, node_ids in sorted(mesh.get("node_sets", {}).items()):
        members = sorted(int(node) for node in node_ids)
        if not members:
            continue
        lines.append(f"*NSET,NSET={set_name}")
        for offset in range(0, len(members), 16):
            lines.append(", ".join(str(node) for node in members[offset : offset + 16]))

    lines.append("")
    return "\n".join(lines)


def _format_solver_coordinate(value: Any) -> str:
    """Render a finite coordinate within CalculiX's bounded free-field width."""

    numeric = float(value)
    if not math.isfinite(numeric):
        raise ValueError("solver mesh contains a non-finite node coordinate")
    if numeric == 0.0:
        return "0"
    rendered = f"{numeric:.12g}".replace("e", "E")
    if len(rendered) > 20:
        raise ValueError(
            f"solver mesh coordinate exceeds CalculiX field width: {rendered}"
        )
    return rendered


def _deck_text(
    spec: dict[str, Any],
    mesh_path: Path,
    mesh: dict[str, Any],
    *,
    pressure_mappings: dict[str, list[dict[str, Any]]] | None = None,
) -> str:
    material = spec["material"]
    _require_element_set(mesh, "BODY")
    lines = [
        "** BLUECAD generated static deck",
        f"*INCLUDE, INPUT={mesh_path.as_posix()}",
        f'*MATERIAL, NAME={material["name"]}',
        "*ELASTIC",
        f'{material["E"]}, {material["nu"]}',
        "*DENSITY",
        f'{material["rho"]}',
        f'*SOLID SECTION, ELSET=BODY, MATERIAL={material["name"]}',
        "*BOUNDARY",
    ]
    fixed_sets: list[str] = []
    for bc in spec.get("bcs", []):
        name = f'BC_{_group_label(bc["port_label"])}'
        _require_node_set(mesh, name)
        if bc.get("kind", "fixed") != "fixed":
            raise ValueError("only fixed boundary conditions are supported")
        fixed_sets.append(name)
        lines.append(f"{name}, 1, 3, 0")
    lines.extend(["*STEP", "*STATIC"])
    for load in spec.get("loads", []):
        name = f'LOAD_{_group_label(load["port_label"])}'
        if load.get("type", "force_total") == "force_total":
            _require_node_set(mesh, name)
            nodes = sorted(mesh["node_sets"][name])
            force = load.get("force", load.get("vector_n"))
            if not force or not nodes:
                raise ValueError(f"load {name} has no force vector or nodes")
            lines.append("*CLOAD")
            for node in nodes:
                for dof, value in enumerate(force, start=1):
                    if value:
                        lines.append(
                            f"{node}, {dof}, "
                            f"{float(value) / len(nodes):.12g}"
                        )
        elif load.get("type") == "pressure":
            mappings = (pressure_mappings or {}).get(name)
            if not mappings:
                raise ValueError(f"pressure load {name} has no face mappings")
            lines.append("*DLOAD")
            for mapping in mappings:
                lines.append(
                    f'{mapping["body_element_id"]}, '
                    f'{mapping["face_label"]}, '
                    f'{float(load["pressure"]):.12g}'
                )
        else:
            raise ValueError("unsupported load type")
    lines.extend(["*NODE FILE", "U", "*EL FILE", "S"])
    if fixed_sets:
        lines.extend(
            [f"*NODE PRINT, NSET={fixed_sets[0]}, GLOBAL=YES", "RF"]
        )
    lines.extend(["*END STEP", ""])
    return "\n".join(lines)


def _parse_mesh(text: str) -> dict[str, Any]:
    nodes: set[int] = set()
    node_coordinates: dict[int, tuple[float, float, float]] = {}
    elements: dict[int, dict[str, Any]] = {}
    node_sets: dict[str, set[int]] = {}
    element_sets: dict[str, set[int]] = {}
    element_set_entries: dict[str, list[int]] = {}
    section: str | None = None
    active_set: str | None = None
    active_type: str | None = None

    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("**") or line.startswith("*******"):
            continue
        if line.startswith("*"):
            header, params = _parse_header(line)
            section = None
            active_set = None
            active_type = None
            if header == "NODE":
                section = "node"
            elif header == "ELEMENT":
                section = "element"
                active_type = params.get("TYPE", "").upper()
                active_set = params.get("ELSET")
                if active_set:
                    element_sets.setdefault(active_set, set())
                    element_set_entries.setdefault(active_set, [])
            elif header == "NSET":
                section = "nset"
                active_set = params.get("NSET")
                if not active_set:
                    raise ValueError("NSET section missing NSET name")
                node_sets.setdefault(active_set, set())
            elif header == "ELSET":
                section = "elset"
                active_set = params.get("ELSET")
                if not active_set:
                    raise ValueError("ELSET section missing ELSET name")
                element_sets.setdefault(active_set, set())
                element_set_entries.setdefault(active_set, [])
            continue

        if section == "node":
            node_id, coordinates = _node_record(line)
            if node_id in node_coordinates:
                raise ValueError(f"duplicate node id: {node_id}")
            nodes.add(node_id)
            node_coordinates[node_id] = coordinates
            continue

        values = _integer_values(line)
        if not values:
            continue
        if section == "element":
            element_id = values[0]
            if element_id in elements:
                raise ValueError(f"duplicate element id: {element_id}")
            connectivity = values[1:]
            elements[element_id] = {
                "type": active_type or "",
                "nodes": connectivity,
            }
            if active_set:
                element_sets[active_set].add(element_id)
                element_set_entries[active_set].append(element_id)
        elif section == "nset" and active_set:
            node_sets[active_set].update(values)
        elif section == "elset" and active_set:
            element_sets[active_set].update(values)
            element_set_entries[active_set].extend(values)

    for set_name, element_ids in element_sets.items():
        if set_name.startswith(("BC_", "LOAD_")) and set_name not in node_sets:
            member_nodes: set[int] = set()
            for element_id in element_ids:
                member_nodes.update(
                    elements.get(element_id, {}).get("nodes", [])
                )
            if member_nodes:
                node_sets[set_name] = member_nodes

    node_to_elements: dict[int, set[int]] = {}
    for element_id, element in elements.items():
        if str(element["type"]).startswith(_VOLUME_ELEMENT_PREFIXES):
            for node in element["nodes"]:
                node_to_elements.setdefault(node, set()).add(element_id)
    return {
        "nodes": nodes,
        "node_coordinates": node_coordinates,
        "elements": elements,
        "node_sets": node_sets,
        "element_sets": element_sets,
        "element_set_entries": element_set_entries,
        "node_to_elements": node_to_elements,
    }


def _node_record(line: str) -> tuple[int, tuple[float, float, float]]:
    parts = [part.strip() for part in line.split(",")]
    if len(parts) < 4:
        raise ValueError(f"invalid node record: {line!r}")
    try:
        node_id = int(parts[0])
        coordinates = tuple(float(value) for value in parts[1:4])
    except ValueError as exc:
        raise ValueError(f"invalid node record: {line!r}") from exc
    if not all(math.isfinite(value) for value in coordinates):
        raise ValueError(f"non-finite node coordinates: {node_id}")
    return node_id, coordinates
