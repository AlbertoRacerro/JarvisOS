from __future__ import annotations

import pytest

from app.modules.bluecad.fem_adapter import _parse_mesh
from app.modules.bluecad.pressure_mapping import (
    PressureMappingError,
    map_pressure_surface,
    pressure_load_evidence,
)

_C3D4_FACE_NODES = {
    1: [1, 2, 3],
    2: [1, 4, 2],
    3: [2, 4, 3],
    4: [3, 4, 1],
}
_C3D10_FACE_NODES = {
    1: [1, 2, 3, 5, 6, 7],
    2: [1, 4, 2, 8, 9, 5],
    3: [2, 4, 3, 9, 10, 6],
    4: [3, 4, 1, 10, 8, 7],
}


def _mesh_text(
    solid_type: str,
    surface_type: str,
    surface_nodes: list[int],
    *,
    body_lines: list[str] | None = None,
    surface_lines: list[str] | None = None,
    surface_set: str = "LOAD_box_loaded",
) -> str:
    node_lines = [
        "1,0,0,0",
        "2,1,0,0",
        "3,0,1,0",
        "4,0,0,1",
        "5,0.5,0,0",
        "6,0.5,0.5,0",
        "7,0,0.5,0",
        "8,0,0,0.5",
        "9,0.5,0,0.5",
        "10,0,0.5,0.5",
    ]
    solid_nodes = "1,2,3,4" if solid_type == "C3D4" else "1,2,3,4,5,6,7,8,9,10"
    body = body_lines or [f"1,{solid_nodes}"]
    surfaces = surface_lines or [
        "100," + ",".join(str(node) for node in surface_nodes)
    ]
    return "\n".join(
        [
            "*NODE",
            *node_lines,
            f"*ELEMENT, TYPE={solid_type}, ELSET=BODY",
            *body,
            f"*ELEMENT, TYPE={surface_type}, ELSET={surface_set}",
            *surfaces,
            "",
        ]
    )


@pytest.mark.parametrize("face_number", [1, 2, 3, 4])
def test_maps_every_c3d4_face(face_number: int) -> None:
    mesh = _parse_mesh(
        _mesh_text("C3D4", "S3", _C3D4_FACE_NODES[face_number])
    )
    mapping = map_pressure_surface(mesh, "LOAD_box_loaded")
    assert len(mapping) == 1
    assert mapping[0]["body_element_type"] == "C3D4"
    assert mapping[0]["local_face_number"] == face_number
    assert mapping[0]["face_label"] == f"P{face_number}"


@pytest.mark.parametrize("face_number", [1, 2, 3, 4])
def test_maps_every_c3d10_face_with_midside_identity(face_number: int) -> None:
    mesh = _parse_mesh(
        _mesh_text("C3D10", "S6", _C3D10_FACE_NODES[face_number])
    )
    mapping = map_pressure_surface(mesh, "LOAD_box_loaded")
    assert len(mapping) == 1
    assert mapping[0]["body_element_type"] == "C3D10"
    assert mapping[0]["matched_face_nodes"] == _C3D10_FACE_NODES[face_number]
    assert mapping[0]["face_label"] == f"P{face_number}"


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (
            lambda mesh: mesh["element_sets"].pop("LOAD_box_loaded"),
            "PRESSURE_GROUP_MISSING_OR_EMPTY",
        ),
        (
            lambda mesh: mesh["element_set_entries"]["LOAD_box_loaded"].append(100),
            "PRESSURE_GROUP_DUPLICATE_MEMBER",
        ),
        (
            lambda mesh: mesh["elements"][100].update(nodes=[1, 2, 5]),
            "PRESSURE_SURFACE_DETACHED",
        ),
        (
            lambda mesh: mesh["elements"][100].update(type="S4"),
            "PRESSURE_UNSUPPORTED_SURFACE_FAMILY",
        ),
        (
            lambda mesh: mesh["elements"][100].update(nodes=[1, 2]),
            "PRESSURE_MALFORMED_CONNECTIVITY",
        ),
        (
            lambda mesh: mesh["elements"][100].update(type="C3D4", nodes=[1, 2, 3, 4]),
            "PRESSURE_GROUP_NON_SURFACE_MEMBER",
        ),
    ],
)
def test_adversarial_surface_groups_fail_closed(mutator, code: str) -> None:
    mesh = _parse_mesh(_mesh_text("C3D4", "S3", [1, 2, 3]))
    mutator(mesh)
    with pytest.raises(PressureMappingError) as excinfo:
        map_pressure_surface(mesh, "LOAD_box_loaded")
    assert excinfo.value.code == code


def test_rejects_ambiguous_non_manifold_face() -> None:
    text = _mesh_text(
        "C3D4",
        "S3",
        [1, 2, 3],
        body_lines=["1,1,2,3,4", "2,1,2,3,4"],
    )
    mesh = _parse_mesh(text)
    with pytest.raises(PressureMappingError) as excinfo:
        map_pressure_surface(mesh, "LOAD_box_loaded")
    assert excinfo.value.code == "PRESSURE_AMBIGUOUS_NON_MANIFOLD"


def test_rejects_surface_mapped_only_to_volume_outside_body() -> None:
    mesh = _parse_mesh(_mesh_text("C3D4", "S3", [1, 2, 3]))
    mesh["element_sets"]["BODY"].clear()
    mesh["element_set_entries"]["BODY"].clear()
    mesh["elements"][2] = mesh["elements"].pop(1)
    mesh["element_sets"]["OTHER"] = {2}
    mesh["element_set_entries"]["OTHER"] = [2]
    mesh["element_sets"]["BODY"] = {3}
    mesh["element_set_entries"]["BODY"] = [3]
    mesh["elements"][3] = {
        "type": "C3D4",
        "nodes": [5, 6, 7, 8],
    }
    mesh["elements"][100]["nodes"] = [1, 2, 3]
    with pytest.raises(PressureMappingError) as excinfo:
        map_pressure_surface(mesh, "LOAD_box_loaded")
    assert excinfo.value.code == "PRESSURE_VOLUME_OUTSIDE_BODY"


def test_rejects_mixed_surface_and_solid_order() -> None:
    mesh = _parse_mesh(_mesh_text("C3D10", "S3", [1, 2, 3]))
    with pytest.raises(PressureMappingError) as excinfo:
        map_pressure_surface(mesh, "LOAD_box_loaded")
    assert excinfo.value.code == "PRESSURE_MIXED_ORDER"


def test_rejects_duplicate_surface_topology() -> None:
    mesh = _parse_mesh(
        _mesh_text(
            "C3D4",
            "S3",
            [1, 2, 3],
            surface_lines=["100,1,2,3", "101,3,2,1"],
        )
    )
    with pytest.raises(PressureMappingError) as excinfo:
        map_pressure_surface(mesh, "LOAD_box_loaded")
    assert excinfo.value.code == "PRESSURE_DUPLICATE_SURFACE_ASSIGNMENT"


def test_rejects_empty_pressure_group() -> None:
    mesh = _parse_mesh(_mesh_text("C3D4", "S3", [1, 2, 3]))
    mesh["element_sets"]["LOAD_box_loaded"].clear()
    mesh["element_set_entries"]["LOAD_box_loaded"].clear()
    with pytest.raises(PressureMappingError) as excinfo:
        map_pressure_surface(mesh, "LOAD_box_loaded")
    assert excinfo.value.code == "PRESSURE_GROUP_MISSING_OR_EMPTY"


def test_rejects_missing_pressure_group_member() -> None:
    mesh = _parse_mesh(_mesh_text("C3D4", "S3", [1, 2, 3]))
    mesh["element_sets"]["LOAD_box_loaded"].add(999)
    mesh["element_set_entries"]["LOAD_box_loaded"].append(999)
    with pytest.raises(PressureMappingError) as excinfo:
        map_pressure_surface(mesh, "LOAD_box_loaded")
    assert excinfo.value.code == "PRESSURE_GROUP_MEMBER_MISSING"


def test_rejects_empty_body_group() -> None:
    mesh = _parse_mesh(_mesh_text("C3D4", "S3", [1, 2, 3]))
    mesh["element_sets"]["BODY"].clear()
    mesh["element_set_entries"]["BODY"].clear()
    with pytest.raises(PressureMappingError) as excinfo:
        map_pressure_surface(mesh, "LOAD_box_loaded")
    assert excinfo.value.code == "PRESSURE_BODY_MISSING_OR_EMPTY"


def test_rejects_unsupported_solid_family() -> None:
    mesh = _parse_mesh(_mesh_text("C3D4", "S3", [1, 2, 3]))
    mesh["elements"][1]["type"] = "C3D8"
    with pytest.raises(PressureMappingError) as excinfo:
        map_pressure_surface(mesh, "LOAD_box_loaded")
    assert excinfo.value.code == "PRESSURE_UNSUPPORTED_SOLID_FAMILY"


def test_rejects_malformed_body_connectivity() -> None:
    mesh = _parse_mesh(_mesh_text("C3D4", "S3", [1, 2, 3]))
    mesh["elements"][1]["nodes"] = [1, 2, 3]
    with pytest.raises(PressureMappingError) as excinfo:
        map_pressure_surface(mesh, "LOAD_box_loaded")
    assert excinfo.value.code == "PRESSURE_MALFORMED_CONNECTIVITY"


def test_rejects_mixed_body_orders() -> None:
    mesh = _parse_mesh(_mesh_text("C3D4", "S3", [1, 2, 3]))
    mesh["elements"][2] = {
        "type": "C3D10",
        "nodes": [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
    }
    mesh["element_sets"]["BODY"].add(2)
    mesh["element_set_entries"]["BODY"].append(2)
    with pytest.raises(PressureMappingError) as excinfo:
        map_pressure_surface(mesh, "LOAD_box_loaded")
    assert excinfo.value.code == "PRESSURE_MIXED_SOLID_ORDER"


def test_c3d10_requires_midside_node_identity() -> None:
    mesh = _parse_mesh(_mesh_text("C3D10", "S6", [1, 2, 3, 5, 6, 8]))
    with pytest.raises(PressureMappingError) as excinfo:
        map_pressure_surface(mesh, "LOAD_box_loaded")
    assert excinfo.value.code == "PRESSURE_SURFACE_DETACHED"


def test_pressure_evidence_uses_area_and_inward_resultant() -> None:
    mesh = _parse_mesh(_mesh_text("C3D4", "S3", [1, 2, 3]))
    evidence = pressure_load_evidence(
        "LOAD_box_loaded",
        2.0,
        map_pressure_surface(mesh, "LOAD_box_loaded"),
    )
    assert evidence["area_mm2"] == pytest.approx(0.5)
    assert evidence["scalar_pressure_force_n"] == pytest.approx(1.0)
    assert evidence["applied_force_resultant_n"] == pytest.approx([0.0, 0.0, 1.0])
