from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.modules.bluecad.fem_adapter import (
    _parse_reactions,
    append_tier3_checks,
    solve_static_analysis,
)
from app.modules.bluecad.registry import ToolRegistryError, resolve_tool

FAKE_CCX = Path(__file__).parent / "fixtures" / "fake_ccx.py"


def _registry(tmp_path: Path, enabled: bool = True) -> Path:
    path = tmp_path / "tools.yaml"
    path.write_text(
        f"""registry_version: bluecad_tool_registry_v0_1
tools:
  - id: calculix
    kind: fem_solver
    integration_mode: subprocess
    version_pin: "fake-ccx-1"
    license: {{spdx: GPL-2.0, boundary: C, verified_date: "2026-07-05"}}
    enabled: {str(enabled).lower()}
    entrypoint: {FAKE_CCX}
    binary_sha256: {hashlib.sha256(FAKE_CCX.read_bytes()).hexdigest()}
    provenance_url: file://fake
    capabilities: [static]
    health_check: null
""",
        encoding="utf-8",
    )
    return path


def _mesh(tmp_path: Path) -> dict:
    mesh = tmp_path / "mesh.inp"
    mesh.write_text(
        """*NODE
1,0,0,0
2,1,0,0
3,0,1,0
4,0,0,1
*ELEMENT, TYPE=C3D4, ELSET=BODY
1,1,2,3,4
*ELEMENT, TYPE=S3, ELSET=BC_run1_port_a
2,1,2,3
*ELEMENT, TYPE=S3, ELSET=LOAD_joint1_port_b
3,2,4,3
""",
        encoding="utf-8",
    )
    return {"artifacts": {"mesh_inp": {"path": str(mesh)}}}


def _spec() -> dict:
    return {
        "schema_version": "bluecad_analysis_spec_v0_1",
        "analysis_id": "a1",
        "analysis_type": "static",
        "geometry": {
            "step_path": "model.step",
            "manifest_path": "manifest.json",
        },
        "material": {
            "name": "steel",
            "E": 200000.0,
            "nu": 0.3,
            "rho": 7.8e-9,
            "yield_strength": 250.0,
        },
        "bcs": [{"port_label": "run1.port_a", "kind": "fixed"}],
        "loads": [
            {
                "port_label": "joint1.port_b",
                "type": "force_total",
                "force": [30.0, 0.0, 0.0],
            }
        ],
        "mesh": {"target_size": 5.0},
        "pass_criteria": [
            {"metric": "max_von_mises", "op": "<=", "value": 300.0}
        ],
    }


def test_happy_path_result_summary_and_native_frd_parser(
    tmp_path: Path,
) -> None:
    result = solve_static_analysis(
        _spec(),
        _mesh(tmp_path),
        tmp_path / "solve",
        registry_path=_registry(tmp_path),
    )
    assert result["verdict"] == "pass"
    assert result["max_displacement"] == {"node_id": 2, "value": 5.0}
    assert result["max_von_mises"] == {
        "element_id": 1,
        "node_id": 2,
        "value": 275.0,
    }
    assert result["reactions"] == [
        {"node_id": 1, "force": [-10.0, 0.0, 0.0]}
    ]
    assert result["reaction_resultant"] == [-10.0, 0.0, 0.0]
    assert {"inp", "frd", "dat", "log"} <= set(result["artifacts"])
    deck = (tmp_path / "solve" / "analysis.inp").read_text(
        encoding="utf-8"
    )
    assert "*INCLUDE, INPUT=../mesh.inp" in deck
    assert str(tmp_path.resolve()) not in deck
    assert "*MATERIAL, NAME=steel" in deck
    assert "*SOLID SECTION, ELSET=BODY, MATERIAL=steel" in deck
    assert "BC_run1_port_a, 1, 3, 0" in deck
    assert "2, 1, 10" in deck
    assert "3, 1, 10" in deck
    assert "4, 1, 10" in deck
    assert "LOAD_joint1_port_b" not in deck.split("*STEP", 1)[0]
    assert "pressure_face_mapping" not in result["artifacts"]


def test_pressure_uses_explicit_body_face_and_hashed_mapping_artifact(
    tmp_path: Path,
) -> None:
    spec = _spec()
    spec["loads"] = [
        {
            "port_label": "joint1.port_b",
            "type": "pressure",
            "pressure": 2.0,
        }
    ]
    result = solve_static_analysis(
        spec,
        _mesh(tmp_path),
        tmp_path / "pressure",
        registry_path=_registry(tmp_path),
    )
    assert result["verdict"] == "pass", result
    deck = (tmp_path / "pressure" / "analysis.inp").read_text(
        encoding="utf-8"
    )
    assert "*DLOAD\n1, P3, 2" in deck
    assert "LOAD_joint1_port_b, P" not in deck

    artifact = result["artifacts"]["pressure_face_mapping"]
    mapping_path = Path(artifact["path"])
    assert hashlib.sha256(mapping_path.read_bytes()).hexdigest() == artifact["sha256"]
    evidence = json.loads(mapping_path.read_text(encoding="utf-8"))
    load = evidence["loads"][0]
    assert load["surface_set"] == "LOAD_joint1_port_b"
    assert load["mapping_count"] == 1
    assert load["pressure_mpa"] == 2.0
    assert load["mappings"][0]["face_label"] == "P3"
    assert str(tmp_path.resolve()) not in mapping_path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("surface_nodes", "face_label"),
    [
        ([1, 2, 3], "P1"),
        ([1, 4, 2], "P2"),
        ([2, 4, 3], "P3"),
        ([3, 4, 1], "P4"),
    ],
)
def test_pressure_deck_emits_each_calculix_tetra_face(
    tmp_path: Path,
    surface_nodes: list[int],
    face_label: str,
) -> None:
    mesh_path = tmp_path / f"mesh-{face_label}.inp"
    mesh_path.write_text(
        "\n".join(
            [
                "*NODE",
                "1,0,0,0",
                "2,1,0,0",
                "3,0,1,0",
                "4,0,0,1",
                "*ELEMENT, TYPE=C3D4, ELSET=BODY",
                "1,1,2,3,4",
                "*ELEMENT, TYPE=S3, ELSET=BC_run1_port_a",
                "2,1,2,3",
                "*ELEMENT, TYPE=S3, ELSET=LOAD_joint1_port_b",
                "3," + ",".join(str(node) for node in surface_nodes),
                "",
            ]
        ),
        encoding="utf-8",
    )
    spec = _spec()
    spec["loads"] = [
        {
            "port_label": "joint1.port_b",
            "type": "pressure",
            "pressure": 2.0,
        }
    ]
    result = solve_static_analysis(
        spec,
        {"artifacts": {"mesh_inp": {"path": str(mesh_path)}}},
        tmp_path / f"solve-{face_label}",
        registry_path=_registry(tmp_path),
    )
    assert result["verdict"] == "pass", result
    deck = Path(result["artifacts"]["inp"]["path"]).read_text(encoding="utf-8")
    assert f"*DLOAD\n1, {face_label}, 2" in deck


def test_duplicate_body_face_across_pressure_groups_fails_closed(
    tmp_path: Path,
) -> None:
    mesh_path = tmp_path / "duplicate-pressure-face.inp"
    mesh_path.write_text(
        """*NODE
1,0,0,0
2,1,0,0
3,0,1,0
4,0,0,1
*ELEMENT, TYPE=C3D4, ELSET=BODY
1,1,2,3,4
*ELEMENT, TYPE=S3, ELSET=BC_run1_port_a
2,1,2,3
*ELEMENT, TYPE=S3, ELSET=LOAD_joint1_port_b
3,2,4,3
*ELEMENT, TYPE=S3, ELSET=LOAD_joint2_port_c
4,2,4,3
""",
        encoding="utf-8",
    )
    spec = _spec()
    spec["loads"] = [
        {
            "port_label": "joint1.port_b",
            "type": "pressure",
            "pressure": 1.0,
        },
        {
            "port_label": "joint2.port_c",
            "type": "pressure",
            "pressure": 1.0,
        },
    ]
    result = solve_static_analysis(
        spec,
        {"artifacts": {"mesh_inp": {"path": str(mesh_path)}}},
        tmp_path / "duplicate-pressure-face",
        registry_path=_registry(tmp_path),
    )
    assert result["verdict"] == "error"
    assert result["errors"] == [
        {
            "code": "PARSE_ERROR",
            "detail": {
                "mapping_code": "PRESSURE_DUPLICATE_BODY_FACE",
                "surface_set": "LOAD_joint2_port_c",
                "previous_surface_set": "LOAD_joint1_port_b",
                "body_element_id": 1,
                "local_face_number": 3,
            },
        }
    ]


def test_native_reaction_parser_returns_per_node_vectors(tmp_path: Path) -> None:
    dat_path = tmp_path / "native.dat"
    dat_path.write_text(
        """forces (fx,fy,fz) for set BC_FIXED and time 0.5000000E+00
 1  1.000000E+00 0.000000E+00 0.000000E+00

forces (fx,fy,fz) for set BC_FIXED and time 0.1000000E+01
 1  1.000000D+01 -2.000000E+00 3.000000E+00
 4 -4.000000E+00  5.000000E+00 6.000000E+00

""",
        encoding="utf-8",
    )
    assert _parse_reactions(dat_path) == [
        {"node_id": 1, "force": [10.0, -2.0, 3.0]},
        {"node_id": 4, "force": [-4.0, 5.0, 6.0]},
    ]


def test_tier3_pass_fail_and_unknown_metric(tmp_path: Path) -> None:
    result = solve_static_analysis(
        _spec(),
        _mesh(tmp_path),
        tmp_path / "solve",
        registry_path=_registry(tmp_path),
    )
    report = {
        "report_version": "bluecad_validation_report_v0_1",
        "spec_id": "sha256:" + "0" * 64,
        "manifest_sha256": None,
        "verdict": "pass",
        "checks": [],
        "errors": [],
    }
    passed = append_tier3_checks(
        report,
        result,
        [{"metric": "max_von_mises", "op": "<=", "value": 300.0}],
    )
    failed = append_tier3_checks(
        report,
        result,
        [{"metric": "max_von_mises", "op": "<=", "value": 200.0}],
    )
    unknown = append_tier3_checks(
        report,
        result,
        [{"metric": "bogus", "op": "<=", "value": 1.0}],
    )
    assert passed["checks"][0]["status"] == "pass"
    assert failed["checks"][0]["status"] == "fail"
    assert failed["verdict"] == "fail"
    assert unknown["errors"] == [
        {"code": "UNKNOWN_METRIC", "detail": {"metric": "bogus"}}
    ]


@pytest.mark.parametrize(
    ("dirname", "code", "timeout"),
    [
        ("solve_error", "SOLVE_ERROR", 2),
        ("diverged", "SOLVE_DIVERGED", 2),
        ("parse_error", "PARSE_ERROR", 2),
        ("timeout", "TIMEOUT", 0.1),
    ],
)
def test_error_taxonomy(
    tmp_path: Path,
    dirname: str,
    code: str,
    timeout: float,
) -> None:
    result = solve_static_analysis(
        _spec(),
        _mesh(tmp_path),
        tmp_path / dirname,
        registry_path=_registry(tmp_path),
        timeout_s=timeout,
    )
    assert result["verdict"] == "error"
    assert result["errors"][0]["code"] == code
    assert "log" in result["artifacts"]


@pytest.mark.bluecad_ccx
def test_real_ccx_a4_disabled_registry_skip(tmp_path: Path) -> None:
    with pytest.raises(ToolRegistryError) as excinfo:
        resolve_tool("calculix", _registry(tmp_path, enabled=False))
    if excinfo.value.code == "TOOL_DISABLED":
        pytest.skip(
            "A4 closure requires maintainer-enabled real CalculiX registry entry"
        )
