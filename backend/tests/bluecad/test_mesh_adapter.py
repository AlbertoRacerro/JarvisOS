from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.modules.bluecad.fem_adapter import _parse_mesh
from app.modules.bluecad.mesh_adapter import _gmsh_args, mesh_analysis_spec
from app.modules.bluecad.models import BluecadLoopConfig
from app.modules.bluecad.registry import ToolRegistryError, resolve_tool

FAKE_GMSH = Path(__file__).parent / "fixtures" / "fake_gmsh.py"


def _registry(tmp_path: Path, enabled: bool = True) -> Path:
    path = tmp_path / "tools.yaml"
    path.write_text(
        f"""registry_version: bluecad_tool_registry_v0_1
tools:
  - id: gmsh
    kind: mesher
    integration_mode: subprocess
    version_pin: "fake-1"
    license: {{spdx: GPL-2.0-or-later, boundary: C, verified_date: "2026-07-03"}}
    enabled: {str(enabled).lower()}
    entrypoint: {FAKE_GMSH}
    binary_sha256: {hashlib.sha256(FAKE_GMSH.read_bytes()).hexdigest()}
    provenance_url: file://fake
    capabilities: [step_import, tet_mesh, physical_groups, inp_export]
    health_check: null
""",
        encoding="utf-8",
    )
    return path


def _analysis(tmp_path: Path, element_order: object | None = None) -> dict:
    tmp_path.mkdir(parents=True, exist_ok=True)
    step = tmp_path / "model.step"
    step.write_text("STEP", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "resolved_ports": {
                    "run1": {"port_a": {"origin": [10, 20, 30], "outer_d": 8}},
                    "joint1": {"port_b": {"origin": [-1, 2, 3], "outer_d": 4}},
                }
            }
        ),
        encoding="utf-8",
    )
    mesh: dict[str, object] = {"target_size": 5.0, "refinements": {"run1.port_a": 2.0}}
    if element_order is not None:
        mesh["element_order"] = element_order
    return {
        "schema_version": "bluecad_analysis_spec_v0_1",
        "analysis_id": "a1",
        "analysis_type": "static",
        "geometry": {"step_path": str(step), "manifest_path": str(manifest)},
        "material": {"name": "steel", "E": 200000, "nu": 0.3, "rho": 7.8e-9, "yield_strength": 250},
        "bcs": [{"port_label": "run1.port_a", "kind": "fixed"}],
        "loads": [{"port_label": "joint1.port_b", "type": "force_total", "force": [1, 0, 0]}],
        "mesh": mesh,
        "pass_criteria": {"max_displacement": 1.0},
    }


def test_geo_physical_surfaces_and_real_style_inp_happy_path(tmp_path: Path) -> None:
    result = mesh_analysis_spec(_analysis(tmp_path), tmp_path / "mesh", registry_path=_registry(tmp_path))
    geo = (tmp_path / "mesh" / "mesh.geo").read_text(encoding="utf-8")
    assert geo.count("Physical Surface") == 2
    assert 'Physical Surface("BC_run1_port_a") = Surface In BoundingBox {4, 14, 24, 16, 26, 36};' in geo
    assert 'Physical Surface("LOAD_joint1_port_b") = Surface In BoundingBox {-4, -1, 0, 2, 5, 6};' in geo
    assert "Mesh.SaveGroupsOfElements = -1000;" in geo
    assert "Mesh.SaveGroupsOfNodes = -100;" in geo
    assert result["verdict"] == "pass"
    assert result["attempts"][0]["counts"]["physical_groups"] == {
        "BODY": 1,
        "BC_run1_port_a": 3,
        "LOAD_joint1_port_b": 3,
    }
    assert result["attempts"][0]["counts"]["elements_total"] == 1
    assert result["attempts"][0]["counts"]["volume_element_types"] == {"C3D4": 1}
    assert {"mesh_inp", "mesh_msh", "bluecad_geo", "gmsh_log"} <= set(result["artifacts"])


def test_omitted_and_explicit_order_one_preserve_exact_command_and_output(tmp_path: Path) -> None:
    geo_path = Path("mesh.geo")
    inp_path = Path("mesh.inp")
    assert _gmsh_args(geo_path, inp_path, 1) == [
        "-3",
        "mesh.geo",
        "-format",
        "inp",
        "-o",
        "mesh.inp",
        "-save_all",
    ]

    omitted = mesh_analysis_spec(_analysis(tmp_path / "omitted"), tmp_path / "omitted_out", registry_path=_registry(tmp_path))
    explicit = mesh_analysis_spec(
        _analysis(tmp_path / "explicit", 1),
        tmp_path / "explicit_out",
        registry_path=_registry(tmp_path),
    )
    assert omitted["verdict"] == explicit["verdict"] == "pass"
    assert omitted["attempts"][0]["counts"] == explicit["attempts"][0]["counts"]
    assert (tmp_path / "omitted_out" / "mesh.inp").read_text(encoding="utf-8") == (
        tmp_path / "explicit_out" / "mesh.inp"
    ).read_text(encoding="utf-8")
    assert " -order " not in f" {(tmp_path / 'explicit_out' / 'gmsh.log').read_text(encoding='utf-8')} "


def test_order_two_requests_and_verifies_c3d10(tmp_path: Path) -> None:
    result = mesh_analysis_spec(
        _analysis(tmp_path, 2),
        tmp_path / "quadratic",
        registry_path=_registry(tmp_path),
    )
    assert result["verdict"] == "pass"
    counts = result["attempts"][0]["counts"]
    assert counts["elements_total"] == 1
    assert counts["volume_element_types"] == {"C3D10": 1}
    log = (tmp_path / "quadratic" / "gmsh.log").read_text(encoding="utf-8")
    assert "-order 2" in log


def test_order_two_fails_when_tool_ignores_request(tmp_path: Path) -> None:
    result = mesh_analysis_spec(
        _analysis(tmp_path, 2),
        tmp_path / "ignoreorder",
        registry_path=_registry(tmp_path),
    )
    assert result["verdict"] == "fail"
    assert result["errors"] == [
        {
            "code": "MESH_ELEMENT_ORDER_MISMATCH",
            "detail": {
                "requested_order": 2,
                "expected_volume_type": "C3D10",
                "actual_volume_types": {"C3D4": 1},
            },
        }
    ]


def test_order_two_rejects_positive_negative_jacobian_count(tmp_path: Path) -> None:
    result = mesh_analysis_spec(
        _analysis(tmp_path, 2),
        tmp_path / "negativejac",
        registry_path=_registry(tmp_path),
    )
    assert result["verdict"] == "fail"
    assert result["errors"] == [
        {
            "code": "MESH_HIGH_ORDER_INVALID",
            "detail": {
                "requested_order": 2,
                "diagnostics": ["Warning: 244 elements with jac. < 0"],
                "reported_invalid_elements": 244,
            },
        }
    ]
    assert len(result["attempts"]) == 1
    assert result["attempts"][0]["counts"]["volume_element_types"] == {"C3D10": 1}


def test_order_two_accepts_explicit_zero_negative_jacobian_count(tmp_path: Path) -> None:
    result = mesh_analysis_spec(
        _analysis(tmp_path, 2),
        tmp_path / "zerojac",
        registry_path=_registry(tmp_path),
    )
    assert result["verdict"] == "pass"
    assert result["attempts"][0]["counts"]["volume_element_types"] == {"C3D10": 1}


@pytest.mark.parametrize("value", [0, 3, True, 1.0, "2"])
def test_invalid_runtime_element_order_fails_before_execution(tmp_path: Path, value: object) -> None:
    spec = _analysis(tmp_path, value)
    with pytest.raises(ValueError, match="mesh.element_order"):
        mesh_analysis_spec(spec, tmp_path / "invalid", registry_path=_registry(tmp_path))
    assert not (tmp_path / "invalid" / "mesh.geo").exists()


def test_c3d10_connectivity_is_not_truncated_by_fem_parser() -> None:
    text = """*NODE
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
*ELEMENT, TYPE=C3D10, ELSET=BODY
1,1,2,3,4,5,6,7,8,9,10
*NSET, NSET=BC_root
1,3,4,7,8,10
*NSET, NSET=LOAD_tip
2,5,6,9
"""
    mesh = _parse_mesh(text)
    assert mesh["elements"][1] == {"type": "C3D10", "nodes": list(range(1, 11))}
    assert mesh["node_to_elements"][10] == {1}


def test_empty_group_fails(tmp_path: Path) -> None:
    result = mesh_analysis_spec(_analysis(tmp_path), tmp_path / "emptycase", registry_path=_registry(tmp_path))
    assert result["verdict"] == "fail"
    assert result["errors"][0] == {"code": "MESH_GROUP_EMPTY", "detail": {"group": "BC_run1_port_a"}}


def test_mesh_fail_retries_once_with_half_target(tmp_path: Path) -> None:
    result = mesh_analysis_spec(_analysis(tmp_path), tmp_path / "failcase", registry_path=_registry(tmp_path))
    assert result["verdict"] == "fail"
    assert [attempt["target_size"] for attempt in result["attempts"]] == [5.0, 2.5]
    assert result["errors"][0]["code"] == "MESH_FAIL"


def test_injection_rejected_before_write(tmp_path: Path) -> None:
    spec = _analysis(tmp_path)
    spec["bcs"][0]["port_label"] = 'a"; Kill;'
    with pytest.raises(ValueError):
        mesh_analysis_spec(spec, tmp_path / "mesh", registry_path=_registry(tmp_path))
    assert not (tmp_path / "mesh" / "mesh.geo").exists()


def test_loop_config_accepts_only_integer_element_orders(tmp_path: Path) -> None:
    spec = _analysis(tmp_path, 2)
    spec.pop("geometry")
    spec["pass_criteria"] = [{"metric": "max_displacement", "op": "<=", "value": 1.0}]
    config = BluecadLoopConfig(analysis_spec=spec)
    assert config.analysis_spec is not None
    assert config.analysis_spec["mesh"]["element_order"] == 2
    for invalid in (0, 3, True, 1.0, "2"):
        spec["mesh"]["element_order"] = invalid
        with pytest.raises(ValueError):
            BluecadLoopConfig(analysis_spec=spec)


def test_analysis_schema_full_static_example_round_trips(tmp_path: Path) -> None:
    schema = json.loads(
        (Path(__file__).parents[3] / "schemas" / "bluecad_analysis_spec_v0_1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    spec = _analysis(tmp_path, 2)
    order_schema = schema["properties"]["mesh"]["properties"]["element_order"]
    assert {key: order_schema[key] for key in ("type", "enum", "default")} == {
        "type": "integer",
        "enum": [1, 2],
        "default": 1,
    }
    assert spec["schema_version"] == schema["properties"]["schema_version"]["const"]
    assert spec["analysis_type"] in schema["properties"]["analysis_type"]["enum"]
    assert {"material", "bcs", "loads", "pass_criteria"} <= set(spec)


@pytest.mark.bluecad_gmsh
def test_real_gmsh_a3_disabled_registry_skip(tmp_path: Path) -> None:
    with pytest.raises(ToolRegistryError) as excinfo:
        resolve_tool("gmsh", _registry(tmp_path, enabled=False))
    if excinfo.value.code == "TOOL_DISABLED":
        pytest.skip("A3 closure requires maintainer-enabled real gmsh registry entry")
