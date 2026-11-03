from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from app.modules.bluecad.assembly import assemble_parts
from app.modules.bluecad.builders import build_part
from app.modules.bluecad.capped_manifold import PARAM_NAMES
from app.modules.bluecad.prompts import PROMPT_VERSION, SYSTEM_TEMPLATE
from app.modules.bluecad.spec import SpecValidationError, canonicalize_geometry_spec

REPO_ROOT = Path(__file__).resolve().parents[3]
SCHEMA_PATH = REPO_ROOT / "schemas" / "bluecad_geometry_spec_v0_1.schema.json"
CANONICAL_VALIDATOR = "app.modules.bluecad.spec.validate_geometry_spec"
RELATIONAL_CONSTRAINTS = {
    "2 * main_wall_t < main_outer_d",
    "2 * branch_wall_t < branch_outer_d",
}
STRICT_INTEGER_TYPE = "int-not-bool"
CANARY_PROFILE = "ubuntu24-py311"
KERNEL_PROOF = pytest.mark.skipif(
    os.getenv("JARVISOS_BLUECAD_CANARY_PROFILE") != CANARY_PROFILE,
    reason="bounded capped-manifold kernel matrix runs in the pinned BLUECAD canary",
)


def _params(branch_count: int = 2) -> dict[str, float | int]:
    return {
        "main_outer_d": 120.0,
        "main_wall_t": 5.0,
        "branch_count": branch_count,
        "branch_outer_d": 60.0,
        "branch_wall_t": 3.0,
        "branch_gap": 20.0,
        "end_gap": 20.0,
        "branch_stub_length": 80.0,
        "cap_thickness": 8.0,
    }


def _part(branch_count: int = 2, part_id: str = "m1") -> dict:
    return {"part_id": part_id, "kind": "capped_manifold", "params": _params(branch_count)}


def _spec(branch_count: int = 2) -> dict:
    return {
        "spec_version": "bluecad_geometry_spec_v0_1",
        "name": f"capped_manifold_{branch_count}",
        "parts": [_part(branch_count)],
        "connections": [],
    }


def _assert_invalid(params: dict, expected_path: str) -> None:
    spec = _spec()
    spec["parts"][0]["params"] = params
    with pytest.raises(SpecValidationError) as exc_info:
        canonicalize_geometry_spec(spec)
    assert exc_info.value.code == "SPEC_INVALID"
    assert exc_info.value.detail["path"] == expected_path


def _require_build123d() -> Any:
    return pytest.importorskip(
        "build123d",
        reason="build123d or one of its native dependencies is not importable",
        exc_type=ImportError,
    )


@pytest.fixture(scope="module")
def capped_builds() -> dict[int, Any]:
    _require_build123d()
    return {branch_count: build_part(_part(branch_count)) for branch_count in (1, 2, 12)}


def _shape_check(shape: object, attribute: str) -> bool:
    value = getattr(shape, attribute)
    return bool(value() if callable(value) else value)


def test_schema_validator_and_prompt_expose_same_closed_contract() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    part_schema = schema["$defs"]["part"]
    strict_integer = schema["$defs"]["positiveInteger1To12"]
    capped = schema["$defs"]["cappedManifoldParams"]
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert part_schema["unevaluatedProperties"] is False
    assert "additionalProperties" not in part_schema
    assert strict_integer["x-jarvis-representation-validator"] == CANONICAL_VALIDATOR
    assert strict_integer["x-jarvis-python-type"] == STRICT_INTEGER_TYPE
    assert set(capped["required"]) == PARAM_NAMES
    assert set(capped["properties"]) == PARAM_NAMES
    assert capped["additionalProperties"] is False
    assert capped["x-jarvis-relational-validator"] == CANONICAL_VALIDATOR
    assert set(capped["x-jarvis-relational-constraints"]) == RELATIONAL_CONSTRAINTS

    valid_part = _spec(2)["parts"][0]
    matching_branches = [
        item
        for item in part_schema["oneOf"]
        if item["properties"]["kind"].get("const") == valid_part["kind"]
    ]
    assert len(matching_branches) == 1
    capped_part = matching_branches[0]
    assert capped_part["properties"]["params"]["$ref"] == "#/$defs/cappedManifoldParams"
    assert set(capped_part["required"]).issubset(valid_part)
    assert set(valid_part).issubset(capped_part["properties"])
    invalid_part = {**valid_part, "unexpected": 1.0}
    assert not set(invalid_part).issubset(capped_part["properties"])

    assert PROMPT_VERSION == "bluecad_ai_loop_v3"
    assert "capped_manifold" in SYSTEM_TEMPLATE
    assert "ports: common, branch_1..branch_n" in SYSTEM_TEMPLATE
    for name in PARAM_NAMES:
        assert f'"{name}"' in SYSTEM_TEMPLATE


def test_canonical_contract_is_stable_and_preserves_existing_version() -> None:
    first = canonicalize_geometry_spec(_spec(2))
    second = canonicalize_geometry_spec(deepcopy(_spec(2)))
    assert first == second
    assert first["spec_version"] == "bluecad_geometry_spec_v0_1"
    assert first["parts"][0]["kind"] == "capped_manifold"


@pytest.mark.parametrize(
    ("mutation", "path"),
    [
        (lambda p: p.__setitem__("branch_gap", 0.0), "$.parts[0].params.branch_gap"),
        (lambda p: p.__setitem__("end_gap", 0.0), "$.parts[0].params.end_gap"),
        (lambda p: p.__setitem__("branch_count", 1.0), "$.parts[0].params.branch_count"),
        (lambda p: p.__setitem__("branch_count", 1.5), "$.parts[0].params.branch_count"),
        (lambda p: p.__setitem__("branch_count", True), "$.parts[0].params.branch_count"),
        (lambda p: p.__setitem__("branch_count", 13), "$.parts[0].params.branch_count"),
        (lambda p: p.__setitem__("main_wall_t", 60.0), "$.parts[0].params.main_wall_t"),
        (lambda p: p.__setitem__("branch_wall_t", 30.0), "$.parts[0].params.branch_wall_t"),
        (lambda p: p.__setitem__("branch_stub_length", 0.0), "$.parts[0].params.branch_stub_length"),
        (lambda p: p.__setitem__("cap_thickness", 0.0), "$.parts[0].params.cap_thickness"),
        (lambda p: p.__setitem__("unexpected", 1.0), "$.parts[0].params"),
    ],
)
def test_invalid_domains_fail_before_kernel_call(mutation, path: str) -> None:
    params = _params()
    mutation(params)
    _assert_invalid(params, path)


def test_derived_overflow_fails_closed() -> None:
    params = _params(12)
    params["branch_outer_d"] = 1.0e308
    params["branch_gap"] = 1.0e308
    _assert_invalid(params, "$.parts[0].params")


@KERNEL_PROOF
@pytest.mark.parametrize("branch_count", [1, 2, 12])
def test_kernel_build_has_exact_ports_final_volume_and_valid_solid(
    branch_count: int,
    capped_builds: dict[int, Any],
) -> None:
    built = capped_builds[branch_count]
    params = _params(branch_count)
    pitch = params["branch_outer_d"] + params["branch_gap"]
    header_length = params["branch_outer_d"] + 2.0 * params["end_gap"] + pitch * (branch_count - 1)
    sweep = params["main_outer_d"] / 2.0 + params["branch_stub_length"]
    radius = max(params["main_outer_d"], params["branch_outer_d"]) / 2.0

    assert built.kind == "capped_manifold"
    assert list(built.ports) == ["common", *[f"branch_{index}" for index in range(1, branch_count + 1)]]
    assert built.ports["common"].origin == (0.0, 0.0, 0.0)
    assert built.ports["common"].direction == (-1.0, 0.0, 0.0)
    for index in range(branch_count):
        port = built.ports[f"branch_{index + 1}"]
        expected_x = params["end_gap"] + params["branch_outer_d"] / 2.0 + pitch * index
        assert port.origin == pytest.approx((expected_x, sweep, 0.0))
        assert port.direction == (0.0, 1.0, 0.0)
        assert port.outer_d == params["branch_outer_d"]
        assert port.wall_t == params["branch_wall_t"]
    assert built.bbox_mm[0] == pytest.approx((0.0, -params["main_outer_d"] / 2.0, -radius))
    assert built.bbox_mm[1] == pytest.approx((header_length + params["cap_thickness"], sweep, radius))
    assert built.volume_mm3 == pytest.approx(float(built.shape.volume), rel=1e-12, abs=1e-9)
    assert _shape_check(built.shape, "is_valid")
    assert _shape_check(built.shape, "is_manifold")


@KERNEL_PROOF
def test_common_and_branch_bores_are_open_and_cap_is_closed(capped_builds: dict[int, Any]) -> None:
    bd = _require_build123d()

    params = _params(2)
    built = capped_builds[2]
    pitch = params["branch_outer_d"] + params["branch_gap"]
    header_length = params["branch_outer_d"] + 2.0 * params["end_gap"] + pitch
    main_inner_d = params["main_outer_d"] - 2.0 * params["main_wall_t"]
    branch_inner_d = params["branch_outer_d"] - 2.0 * params["branch_wall_t"]
    sweep = params["main_outer_d"] / 2.0 + params["branch_stub_length"]

    common_probe = bd.Pos(0.1, 0.0, 0.0) * bd.extrude(
        bd.Plane.YZ * bd.Circle(radius=main_inner_d / 4.0),
        amount=header_length - 0.2,
    )
    assert float((built.shape & common_probe).volume) == pytest.approx(0.0, abs=1e-8)

    for index in range(2):
        x = params["end_gap"] + params["branch_outer_d"] / 2.0 + pitch * index
        bore_probe = (
            bd.Pos(x, 0.0, 0.0)
            * bd.Rot(Z=90)
            * bd.extrude(
                bd.Plane.YZ * bd.Circle(radius=branch_inner_d / 4.0),
                amount=sweep - 0.1,
            )
        )
        assert float((built.shape & bore_probe).volume) == pytest.approx(0.0, abs=1e-8)

    cap_probe = bd.Pos(header_length + params["cap_thickness"] / 4.0, 0.0, 0.0) * bd.extrude(
        bd.Plane.YZ * bd.Circle(radius=main_inner_d / 4.0),
        amount=params["cap_thickness"] / 2.0,
    )
    assert float((built.shape & cap_probe).volume) > 0.0


@KERNEL_PROOF
@pytest.mark.parametrize("branch_count", [1, 2, 12])
def test_mirrored_parallel_path_assembly_is_consistent(branch_count: int) -> None:
    _require_build123d()
    params = _params(branch_count)
    branch_length = 500.0
    parts = [_part(branch_count, "left")]
    parts.extend(
        {
            "part_id": f"run_{index}",
            "kind": "tube_run",
            "params": {
                "outer_d": params["branch_outer_d"],
                "wall_t": params["branch_wall_t"],
                "length": branch_length,
            },
        }
        for index in range(1, branch_count + 1)
    )
    parts.append(_part(branch_count, "right"))
    connections = []
    for index in range(1, branch_count + 1):
        connections.extend(
            [
                {"from": f"left.branch_{index}", "to": f"run_{index}.port_a"},
                {
                    "from": f"run_{index}.port_b",
                    "to": f"right.branch_{branch_count + 1 - index}",
                },
            ]
        )
    canonical = canonicalize_geometry_spec(
        {
            "spec_version": "bluecad_geometry_spec_v0_1",
            "parts": parts,
            "connections": connections,
        }
    )
    assembled = assemble_parts(canonical)
    assert set(assembled) == {part["part_id"] for part in parts}
    assert assembled["right"].ports["common"].direction == pytest.approx((1.0, 0.0, 0.0))


@KERNEL_PROOF
def test_repeated_builds_have_identical_manifest_entries(capped_builds: dict[int, Any]) -> None:
    first = capped_builds[2].manifest_entry()
    second = build_part(deepcopy(_part(2))).manifest_entry()
    assert first == second
