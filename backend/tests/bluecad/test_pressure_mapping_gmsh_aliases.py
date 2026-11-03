from __future__ import annotations

import pytest

from app.modules.bluecad.fem_adapter import _parse_mesh
from app.modules.bluecad.pressure_mapping import map_pressure_surface


@pytest.mark.parametrize(
    ("solid_type", "solid_nodes", "surface_type", "surface_nodes"),
    [
        ("C3D4", "1,2,3,4", "CPS3", "1,2,3"),
        ("C3D10", "1,2,3,4,5,6,7,8,9,10", "CPS6", "1,2,3,5,6,7"),
    ],
)
def test_accepts_real_gmsh_calculix_surface_aliases(
    solid_type: str,
    solid_nodes: str,
    surface_type: str,
    surface_nodes: str,
) -> None:
    mesh = _parse_mesh(
        "\n".join(
            [
                "*NODE",
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
                f"*ELEMENT, TYPE={solid_type}, ELSET=BODY",
                f"1,{solid_nodes}",
                f"*ELEMENT, TYPE={surface_type}, ELSET=LOAD_box_loaded",
                f"100,{surface_nodes}",
                "",
            ]
        )
    )

    mapping = map_pressure_surface(mesh, "LOAD_box_loaded")

    assert mapping[0]["surface_element_type"] == surface_type
    assert mapping[0]["face_label"] == "P1"
