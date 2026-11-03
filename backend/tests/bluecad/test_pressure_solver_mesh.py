from __future__ import annotations

import hashlib
from pathlib import Path

from app.modules.bluecad.fem_pressure_integration import (
    _parse_mesh,
    _solid_solver_mesh_text,
    _write_solid_solver_mesh,
)


def _gmsh_pressure_mesh() -> dict:
    source = """*Heading
*NODE
1,0,0,0
2,1,0,0
3,0,1,0
4,0,0,1
5,0.5,0,0
6,0.5,0.5,0
7,0,0.5,0
8,0,0,0.5
9,0.5,0,0.5
10,0,0.5,0.5
*ELEMENT, TYPE=T3D3, ELSET=Line1
10,1,5,2
*ELEMENT, TYPE=CPS6, ELSET=Surface1
20,1,2,3,5,6,7
*ELEMENT, TYPE=C3D10, ELSET=Volume1
30,1,2,3,4,5,6,7,8,9,10
*ELSET,ELSET=BC_box_fixed
20
*ELSET,ELSET=LOAD_box_loaded
20
*ELSET,ELSET=BODY
30
*NSET,NSET=BC_box_fixed
1,3,4,7,8,10
*NSET,NSET=LOAD_box_loaded
2,5,6,9
"""
    return _parse_mesh(source)


def test_solid_solver_mesh_excludes_boundary_elements() -> None:
    solver_text = _solid_solver_mesh_text(_gmsh_pressure_mesh())

    assert "TYPE=C3D10, ELSET=BODY" in solver_text
    assert "30,1,2,3,4,5,6,7,8,9,10" in solver_text
    assert "TYPE=CPS6" not in solver_text
    assert "TYPE=T3D3" not in solver_text
    assert "Surface1" not in solver_text
    assert "Line1" not in solver_text
    assert "*NSET,NSET=BC_box_fixed" in solver_text
    assert "*NSET,NSET=LOAD_box_loaded" in solver_text

    reparsed = _parse_mesh(solver_text)
    assert set(reparsed["elements"]) == {30}
    assert reparsed["element_sets"]["BODY"] == {30}
    assert reparsed["node_sets"]["BC_box_fixed"] == {1, 3, 4, 7, 8, 10}


def test_solid_solver_mesh_is_retained_as_hashed_artifact(
    tmp_path: Path,
) -> None:
    artifacts: dict = {}

    solver_path = _write_solid_solver_mesh(
        _gmsh_pressure_mesh(),
        tmp_path,
        artifacts,
    )

    assert solver_path == tmp_path / "solver_mesh.inp"
    artifact = artifacts["solver_mesh_inp"]
    assert artifact["path"] == str(solver_path)
    assert artifact["bytes"] == solver_path.stat().st_size
    assert artifact["sha256"] == hashlib.sha256(
        solver_path.read_bytes()
    ).hexdigest()
