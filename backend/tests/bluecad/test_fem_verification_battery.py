from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from app.modules.bluecad.fem_verification_battery import (
    build_battery_report,
    cantilever_spec,
    evaluate_cantilever,
    evaluate_lame,
    evaluate_plate,
    lame_spec,
    nonzero_resultant_balance,
    plate_spec,
    render_battery_report,
    self_equilibrated_reaction_balance,
)


def _frd_block_records(
    name: str,
    components: list[str],
    records: dict[int, list[float]],
) -> str:
    lines = [f" -4  {name:<8} {len(components):4d}    1"]
    for index, component in enumerate(components, start=1):
        lines.append(f" -5  {component:<8}    1    2 {index:4d}    0")
    for node_id, values in sorted(records.items()):
        lines.append(
            " -1"
            + f"{node_id:10d}"
            + "".join(f"{value:12.5E}" for value in values)
        )
    lines.append(" -3")
    return "\n".join(lines) + "\n"


def test_benchmark_specs_freeze_prescribed_mesh_loads_and_order(
    tmp_path: Path,
) -> None:
    step = tmp_path / "model.step"
    manifest = tmp_path / "manifest.json"
    coarse = cantilever_spec(
        step,
        manifest,
        target_size=20.0 / 3.0,
        analysis_id="coarse",
    )
    lame = lame_spec(step, manifest)
    plate = plate_spec(step, manifest)

    assert coarse["mesh"] == {
        "target_size": pytest.approx(20.0 / 3.0),
        "element_order": 2,
    }
    assert coarse["loads"] == [
        {
            "port_label": "beam.loaded",
            "type": "force_total",
            "force": [0.0, -100.0, 0.0],
        }
    ]
    assert len(lame["loads"]) == 8
    assert {item["pressure"] for item in lame["loads"]} == {10.0}
    assert plate["mesh"]["target_size"] == pytest.approx(20.0 / 12.0)
    assert plate["loads"][0]["force"] == [5000.0, 0.0, 0.0]
    assert all(
        spec["mesh"]["element_order"] == 2
        for spec in (coarse, lame, plate)
    )


def test_nonzero_and_self_equilibrated_balance_contracts() -> None:
    assert (
        nonzero_resultant_balance(
            applied_resultant_n=[0.0, -100.0, 0.0],
            reaction_resultant_n=[0.0, 100.0, 0.0],
            primary_axis=1,
        )["verdict"]
        == "pass"
    )
    assert (
        nonzero_resultant_balance(
            applied_resultant_n=[5000.0, 0.0, 0.0],
            reaction_resultant_n=[-4900.0, 30.0, 0.0],
            primary_axis=0,
        )["verdict"]
        == "fail"
    )
    assert (
        self_equilibrated_reaction_balance(
            reaction_resultant_n=[0.0, 0.0, 0.0],
            scalar_force_scale_n=1000.0,
        )["verdict"]
        == "pass"
    )
    assert (
        self_equilibrated_reaction_balance(
            reaction_resultant_n=[6.0, 0.0, 0.0],
            scalar_force_scale_n=1000.0,
        )["verdict"]
        == "fail"
    )


def test_cantilever_evaluation_samples_only_loaded_tip_nodes() -> None:
    mesh = """*NODE
1,200,-5,-5
2,200,5,-5
3,200,5,5
4,200,-5,5
5,0,0,0
*ELEMENT,TYPE=C3D4,ELSET=BODY
1,1,2,3,5
*NSET,NSET=LOAD_beam_loaded
1,2,3,4
"""
    frd = _frd_block_records(
        "DISP",
        ["D1", "D2", "D3"],
        {
            1: [0.0, -1.58, 0.0],
            2: [0.0, -1.59, 0.0],
            3: [0.0, -1.60, 0.0],
            4: [0.0, -1.57, 0.0],
            5: [0.0, -99.0, 0.0],
        },
    )
    result = evaluate_cantilever(
        mesh_text=mesh,
        frd_text=frd,
        reaction_resultant=[0.0, 100.0, 0.0],
        target_size=10.0 / 3.0,
    )
    assert result["verdict"] == "pass"
    assert result["sampling"]["selected"]["node_id"] == 3
    assert result["comparison"]["actual"] == pytest.approx(1.6)


def _lame_stress_at(angle: float) -> list[float]:
    sigma_r = -10.0
    sigma_theta = 16.666666666666668
    cosine = math.cos(angle)
    sine = math.sin(angle)
    sxx = sigma_r * cosine**2 + sigma_theta * sine**2
    syy = sigma_r * sine**2 + sigma_theta * cosine**2
    sxy = (sigma_r - sigma_theta) * sine * cosine
    return [sxx, syy, 0.0, sxy, 0.0, 0.0]


def test_lame_evaluation_uses_midlength_angular_bore_samples() -> None:
    node_lines = []
    element_lines = []
    stress_records: dict[int, list[float]] = {}
    node_id = 1
    for group_index in range(8):
        group_nodes = []
        for offset in range(3):
            angle = 2.0 * math.pi * (group_index * 3 + offset) / 24.0
            x = 20.0 * math.cos(angle)
            y = 20.0 * math.sin(angle)
            node_lines.append(f"{node_id},{x:.12g},{y:.12g},80")
            stress_records[node_id] = _lame_stress_at(angle)
            group_nodes.append(node_id)
            node_id += 1
        element_lines.extend(
            [
                "*ELEMENT,TYPE=S3,"
                f"ELSET=LOAD_cylinder_bore_{group_index + 1:02d}",
                f"{100 + group_index},"
                + ",".join(str(value) for value in group_nodes),
            ]
        )
    mesh = (
        "*NODE\n"
        + "\n".join(node_lines)
        + "\n"
        + "\n".join(element_lines)
        + "\n"
    )
    frd = _frd_block_records(
        "STRESS",
        ["SXX", "SYY", "SZZ", "SXY", "SYZ", "SZX"],
        stress_records,
    )
    expected_area = 2.0 * math.pi * 20.0 * 160.0
    pressure_loads = []
    for index in range(8):
        pressure_loads.append(
            {
                "surface_set": f"LOAD_cylinder_bore_{index + 1:02d}",
                "area_mm2": expected_area / 8.0,
                "scalar_pressure_force_n": 10.0 * expected_area / 8.0,
                "applied_force_resultant_n": [0.0, 0.0, 0.0],
                "mappings": [
                    {
                        "surface_element_id": 100 + index,
                        "body_element_id": 1000 + index,
                        "local_face_number": 1,
                    }
                ],
            }
        )
    result = evaluate_lame(
        mesh_text=mesh,
        frd_text=frd,
        pressure_loads=pressure_loads,
        reaction_resultant=[0.0, 0.0, 0.0],
        target_size=5.0,
    )
    assert result["verdict"] == "pass"
    assert result["sampling"]["distinct_angle_count"] == 24
    assert result["sampled"]["sigma_theta_mpa"] == pytest.approx(
        16.6666666667,
        rel=1e-5,
    )
    assert result["sampled"]["sigma_r_mpa"] == pytest.approx(
        -10.0,
        rel=1e-5,
    )


def test_plate_evaluation_uses_symmetric_mid_thickness_hole_nodes() -> None:
    mesh = """*NODE
1,100,10,0
2,100,-10,0
3,99,10,2.5
4,101,-10,-2.5
*ELEMENT,TYPE=C3D4,ELSET=BODY
1,1,2,3,4
"""
    stress = [31.3308, 0.0, 0.0, 0.0, 0.0, 0.0]
    frd = _frd_block_records(
        "STRESS",
        ["SXX", "SYY", "SZZ", "SXY", "SYZ", "SZX"],
        {1: stress, 2: stress},
    )
    result = evaluate_plate(
        mesh_text=mesh,
        frd_text=frd,
        reaction_resultant=[-5000.0, 0.0, 0.0],
        target_size=20.0 / 12.0,
    )
    assert result["verdict"] == "pass"
    assert result["sampled_tangential_stress_mpa"] == pytest.approx(31.3308)
    assert result["source"]["nominal_stress_convention"] == "net section"


def test_report_rendering_is_deterministic_for_normalized_inputs(
    tmp_path: Path,
) -> None:
    cases = [
        {
            "case_id": "cantilever",
            "verdict": "pass",
            "comparison": {
                "name": "a",
                "actual": 1.0,
                "expected": 1.0,
                "relative_error": 0.0,
                "tolerance": 0.02,
                "verdict": "pass",
            },
        },
        {
            "case_id": "lame_open_end_cylinder",
            "verdict": "pass",
            "comparisons": [
                {
                    "name": "b",
                    "actual": 1.0,
                    "expected": 1.0,
                    "relative_error": 0.0,
                    "tolerance": 0.05,
                    "verdict": "pass",
                }
            ],
        },
        {
            "case_id": "finite_width_plate_with_hole",
            "verdict": "pass",
            "comparison": {
                "name": "c",
                "actual": 1.0,
                "expected": 1.0,
                "relative_error": 0.0,
                "tolerance": 0.07,
                "verdict": "pass",
            },
        },
    ]
    kwargs = {
        "generated_at": "2026-07-11T10:00:00+00:00",
        "git_sha": "abc123",
        "environment": {"python": "3.11.0", "os": "test-os"},
        "toolchain": {
            "gmsh": {"version_pin": "x"},
            "calculix": {"version_pin": "y"},
        },
        "fixture_verification": {"fixtures": []},
        "cases": cases,
        "artifacts": {"proof_root": "."},
    }
    first = build_battery_report(**kwargs)
    second = build_battery_report(**kwargs)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    paths = render_battery_report(first, tmp_path)
    first_json = Path(paths["json"]).read_bytes()
    first_markdown = Path(paths["markdown"]).read_bytes()
    paths = render_battery_report(second, tmp_path)
    assert Path(paths["json"]).read_bytes() == first_json
    assert Path(paths["markdown"]).read_bytes() == first_markdown
