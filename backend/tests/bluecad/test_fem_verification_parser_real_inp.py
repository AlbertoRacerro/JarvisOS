from app.modules.bluecad.fem_verification import parse_inp_mesh


def test_verification_inp_parser_ignores_gmsh_heading_payload() -> None:
    parsed = parse_inp_mesh(
        """*Heading
 /tmp/proof-root/mesh/mesh.inp
*NODE
1,0,0,0
2,1,0,0
3,0,1,0
4,0,0,1
*ELEMENT,TYPE=C3D4,ELSET=BODY
1,1,2,3,4
"""
    )

    assert parsed["node_coordinates"][1] == (0.0, 0.0, 0.0)
    assert parsed["elements"][1] == {
        "type": "C3D4",
        "nodes": [1, 2, 3, 4],
    }
