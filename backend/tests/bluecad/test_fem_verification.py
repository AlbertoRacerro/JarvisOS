from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.modules.bluecad.fem_verification import (
    FemVerificationError,
    audit_segmented_pressure_surface,
    beam_tip_displacement,
    comparison_record,
    cylindrical_stress_components,
    displacement_block,
    finite_width_hole_factor,
    finite_width_hole_reference,
    lame_open_end_bore_stresses,
    parse_frd_blocks,
    parse_inp_mesh,
    relative_error,
    select_nodes_near,
    stress_block,
    verify_fixture_index,
)

FIXTURES = Path(__file__).parent / "fixtures" / "fem_verification"


def _frd_block(
    name: str, components: list[str], values: list[float], node_id: int = 10
) -> str:
    lines = [f" -4  {name:<8} {len(components):4d}    1"]
    for index, component in enumerate(components, start=1):
        lines.append(f" -5  {component:<8}    1    2 {index:4d}    0")
    lines.append(
        " -1" + f"{node_id:10d}" + "".join(f"{value:12.5E}" for value in values)
    )
    lines.append(" -3")
    return "\n".join(lines) + "\n"


def test_closed_form_reference_values() -> None:
    assert beam_tip_displacement(
        force_n=100.0,
        length_mm=200.0,
        width_mm=10.0,
        height_mm=10.0,
        elastic_modulus_mpa=200000.0,
    ) == pytest.approx(1.6)
    assert lame_open_end_bore_stresses(
        inner_radius_mm=20.0,
        outer_radius_mm=40.0,
        pressure_mpa=10.0,
    ) == pytest.approx(
        {
            "sigma_theta_mpa": 16.666666666666668,
            "sigma_r_mpa": -10.0,
            "sigma_z_mpa": 0.0,
        }
    )
    assert finite_width_hole_factor(diameter_mm=20.0, width_mm=100.0) == pytest.approx(
        2.506464
    )


def test_finite_width_reference_is_bound_to_net_section_stress() -> None:
    reference = finite_width_hole_reference(
        force_n=5000.0,
        width_mm=100.0,
        diameter_mm=20.0,
        thickness_mm=5.0,
    )
    assert reference["sigma_nominal_net_mpa"] == pytest.approx(12.5)
    assert reference["sigma_nominal_gross_mpa"] == pytest.approx(10.0)
    assert reference["sigma_peak_mpa"] == pytest.approx(31.3308)
    assert reference["sigma_peak_mpa"] != pytest.approx(
        reference["kt_net_section"] * reference["sigma_nominal_gross_mpa"]
    )


def test_comparison_record_and_zero_reference_rejection() -> None:
    assert (
        comparison_record(name="beam", actual=1.61, expected=1.6, tolerance=0.02)[
            "verdict"
        ]
        == "pass"
    )
    assert (
        comparison_record(name="beam", actual=1.7, expected=1.6, tolerance=0.02)[
            "verdict"
        ]
        == "fail"
    )
    with pytest.raises(FemVerificationError) as caught:
        relative_error(1.0, 0.0)
    assert caught.value.code == "INVALID_COMPARISON_VALUES"


def test_component_aware_frd_parser_accepts_reordered_headers() -> None:
    stress_components = ["SXY", "SZZ", "SXX", "SZX", "SYY", "SYZ"]
    stress_values = [4.0, 3.0, 1.0, 6.0, 2.0, 5.0]
    text = _frd_block("DISP", ["D3", "D1", "D2"], [0.3, 0.1, 0.2])
    text += _frd_block("STRESS", stress_components, stress_values)
    blocks = parse_frd_blocks(text)
    displacement = displacement_block(blocks)
    stress = stress_block(blocks)
    assert displacement["records"][10] == {
        "D3": 0.3,
        "D1": 0.1,
        "D2": 0.2,
    }
    assert stress["records"][10] == {
        "SXY": 4.0,
        "SZZ": 3.0,
        "SXX": 1.0,
        "SZX": 6.0,
        "SYY": 2.0,
        "SYZ": 5.0,
    }


@pytest.mark.parametrize(
    ("text", "code"),
    [
        (_frd_block("STRESS", ["SXX", "SYY"], [1.0, 2.0]), "FRD_COMPONENT_SET_INVALID"),
        (
            " -4  STRESS      2    1\n"
            " -5  SXX         1    2    1    0\n"
            " -5  SXX         1    2    2    0\n"
            " -1        10 1.00000E+00 2.00000E+00\n"
            " -3\n",
            "FRD_DUPLICATE_COMPONENT",
        ),
        (
            " -4  STRESS      1    1\n"
            " -5  SXX         1    2    1    0\n"
            " -1        10 1.00000E+00\n",
            "FRD_BLOCK_UNTERMINATED",
        ),
    ],
)
def test_frd_parser_fails_closed(text: str, code: str) -> None:
    with pytest.raises(FemVerificationError) as caught:
        blocks = parse_frd_blocks(text)
        stress_block(blocks)
    assert caught.value.code == code


def test_inp_parser_preserves_coordinates_connectivity_and_groups() -> None:
    parsed = parse_inp_mesh(
        """*NODE
1,0,0,0
2,1,0,0
3,0,1,0
4,0,0,1
*ELEMENT,TYPE=C3D4,ELSET=BODY
10,1,2,3,4
*ELEMENT,TYPE=S3,ELSET=LOAD_FACE
20,1,2,3
*NSET,NSET=TIP
2,3
*ELSET,ELSET=LOAD_FACE
20
"""
    )
    assert parsed["node_coordinates"][2] == (1.0, 0.0, 0.0)
    assert parsed["elements"][10] == {"type": "C3D4", "nodes": [1, 2, 3, 4]}
    assert parsed["node_sets"]["TIP"] == {2, 3}
    assert parsed["element_sets"]["LOAD_FACE"] == {20}
    assert parsed["element_set_entries"]["LOAD_FACE"] == [20, 20]


def test_cartesian_to_cylindrical_transform() -> None:
    stress = {
        "SXX": 20.0,
        "SYY": 10.0,
        "SZZ": 3.0,
        "SXY": 0.0,
        "SYZ": 5.0,
        "SZX": 7.0,
    }
    at_x = cylindrical_stress_components(stress, x=20.0, y=0.0)
    assert at_x == pytest.approx(
        {
            "sigma_r": 20.0,
            "sigma_theta": 10.0,
            "sigma_z": 3.0,
            "tau_rtheta": 0.0,
            "tau_thetaz": 5.0,
            "tau_zr": 7.0,
        }
    )
    at_y = cylindrical_stress_components(stress, x=0.0, y=20.0)
    assert at_y["sigma_r"] == pytest.approx(10.0)
    assert at_y["sigma_theta"] == pytest.approx(20.0)
    with pytest.raises(FemVerificationError) as caught:
        cylindrical_stress_components(stress, x=0.0, y=0.0)
    assert caught.value.code == "CYLINDRICAL_DIRECTION_UNDEFINED"


def test_location_selection_reports_residuals_and_under_resolution() -> None:
    nodes = {
        1: (20.0, 0.0, 79.9),
        2: (0.0, 20.0, 80.1),
        3: (40.0, 0.0, 80.0),
    }
    selected = select_nodes_near(
        nodes,
        targets={"radius_xy": 20.0, "z": 80.0},
        tolerances={"radius_xy": 0.01, "z": 0.2},
        min_count=2,
    )
    assert [item["node_id"] for item in selected] == [1, 2]
    assert selected[0]["residuals"] == pytest.approx({"radius_xy": 0.0, "z": 0.1})
    with pytest.raises(FemVerificationError) as caught:
        select_nodes_near(
            nodes,
            targets={"radius_xy": 20.0, "z": 80.0},
            tolerances={"radius_xy": 0.01, "z": 0.05},
            min_count=2,
        )
    assert caught.value.code == "LOCATION_UNDER_RESOLVED"


def _evidence(name: str, surface_id: int, face: int, force: list[float]) -> dict:
    return {
        "surface_set": name,
        "area_mm2": 10.0,
        "scalar_pressure_force_n": 10.0,
        "applied_force_resultant_n": force,
        "mappings": [
            {
                "surface_element_id": surface_id,
                "body_element_id": surface_id + 100,
                "local_face_number": face,
            }
        ],
    }


def test_segmented_surface_audit_checks_area_equilibrium_and_uniqueness() -> None:
    report = audit_segmented_pressure_surface(
        [
            _evidence("LOAD_A", 1, 1, [10.0, 0.0, 0.0]),
            _evidence("LOAD_B", 2, 2, [-10.0, 0.0, 0.0]),
        ],
        expected_surface_sets=["LOAD_A", "LOAD_B"],
        expected_area_mm2=20.0,
    )
    assert report["verdict"] == "pass"
    assert report["applied_force_resultant_norm_n"] == pytest.approx(0.0)

    duplicate = _evidence("LOAD_B", 1, 2, [-10.0, 0.0, 0.0])
    with pytest.raises(FemVerificationError) as caught:
        audit_segmented_pressure_surface(
            [_evidence("LOAD_A", 1, 1, [10.0, 0.0, 0.0]), duplicate],
            expected_surface_sets=["LOAD_A", "LOAD_B"],
            expected_area_mm2=20.0,
        )
    assert caught.value.code == "PRESSURE_AUDIT_DUPLICATE_SURFACE_ELEMENT"


def test_checked_in_fixture_index_is_hash_bound() -> None:
    verified = verify_fixture_index(FIXTURES / "fixture_index.json")
    assert [item["name"] for item in verified["fixtures"]] == [
        "cantilever",
        "segmented_cylinder",
        "plate_with_hole",
    ]
    cylinder_manifest = json.loads(
        (FIXTURES / "segmented_cylinder" / "manifest.json").read_text(encoding="utf-8")
    )
    assert cylinder_manifest["dimensions_mm"]["bore_band_count"] == 8
    assert cylinder_manifest["dimensions_mm"]["selection_half_side"] == 20.5
    assert math.isclose(
        cylinder_manifest["resolved_ports"]["cylinder"]["bore_01"]["outer_d"] * 0.75,
        20.5,
    )
    assert cylinder_manifest["resolved_ports"]["cylinder"]["fixed"] == {
        "direction": [0.0, 0.0, -1.0],
        "origin": [0.0, 0.0, -40.0],
        "outer_d": 54.0,
    }
