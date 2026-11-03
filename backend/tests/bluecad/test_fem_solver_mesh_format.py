from __future__ import annotations

import math

import pytest

from app.modules.bluecad.fem_pressure_integration import (
    _format_solver_coordinate,
    _parse_mesh,
    _solid_solver_mesh_text,
)


def test_solid_solver_mesh_bounds_coordinate_fields_and_round_trips() -> None:
    coordinates = {
        1: (40.0, -9.7971743931789996e-15, 160.0),
        2: (39.206899513941998, 7.9258457279758998, 160.0),
        3: (-0.99722766952276998, 39.987567280032998, 0.0),
        4: (20.0, -4.8985871965889997e-15, 80.0),
    }
    mesh = {
        "node_coordinates": coordinates,
        "elements": {1: {"type": "C3D4", "nodes": [1, 2, 3, 4]}},
        "element_sets": {"BODY": {1}},
        "node_sets": {"BC_cylinder_fixed": {3, 4}},
    }

    rendered = _solid_solver_mesh_text(mesh)
    node_lines = rendered.split("*NODE\n", 1)[1].split("*ELEMENT", 1)[0].splitlines()
    assert node_lines
    for line in node_lines:
        fields = line.split(",")
        assert len(fields) == 4
        assert all(len(field) <= 20 for field in fields[1:])

    parsed = _parse_mesh(rendered)
    for node_id, expected in coordinates.items():
        assert parsed["node_coordinates"][node_id] == pytest.approx(
            expected,
            rel=1.0e-11,
            abs=1.0e-20,
        )


def test_solver_coordinate_formatter_rejects_nonfinite_values() -> None:
    for value in (math.inf, -math.inf, math.nan):
        with pytest.raises(ValueError, match="non-finite"):
            _format_solver_coordinate(value)
