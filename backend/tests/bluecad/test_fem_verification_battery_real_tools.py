from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from app.modules.bluecad.fem_verification_runner import (
    run_fem_verification_battery,
)
from app.modules.bluecad.registry import (
    ToolRegistryError,
    check_registry,
    resolve_registry_path,
    resolve_tool,
)

FIXTURES = Path(__file__).parent / "fixtures" / "fem_verification"


def _handle_unavailable(*, strict: bool, reason: str) -> None:
    if strict:
        pytest.fail(f"strict BLUECAD real-tool proof unavailable: {reason}")
    pytest.skip(f"BLUECAD real-tool proof unavailable: {reason}")


def _require_toolchain(request: pytest.FixtureRequest) -> Path:
    strict = bool(request.config.getoption("--require-bluecad-real-tools"))
    try:
        registry_path = resolve_registry_path()
        gmsh = resolve_tool("gmsh", registry_path)
        ccx = resolve_tool("calculix", registry_path)
        exit_code, output = check_registry(registry_path)
    except (ToolRegistryError, OSError) as exc:
        _handle_unavailable(strict=strict, reason=str(exc))
    if exit_code != 0:
        _handle_unavailable(strict=strict, reason=output)
    for tool in (gmsh, ccx):
        if str(tool["version_pin"]).lower().startswith("fake"):
            tool_id = tool.get("tool_id", tool.get("id", "tool"))
            _handle_unavailable(
                strict=strict,
                reason=f"{tool_id} uses a fake version pin",
            )
    return Path(registry_path)


@pytest.fixture(scope="module")
def real_battery(
    request: pytest.FixtureRequest,
    tmp_path_factory: pytest.TempPathFactory,
) -> dict[str, Any]:
    registry_path = _require_toolchain(request)
    root = tmp_path_factory.mktemp("fem-verification-c2") / "battery"
    try:
        result = run_fem_verification_battery(
            FIXTURES / "fixture_index.json",
            root,
            registry_path=registry_path,
            git_sha=os.getenv(
                "JARVISOS_BLUECAD_PROOF_GIT_SHA",
                os.getenv("GITHUB_SHA", "local-real-tool-proof"),
            ),
        )
    finally:
        _preserve_debug_root(root)
    configured_report = os.getenv("JARVISOS_BLUECAD_FEM_BATTERY_JSON")
    if configured_report:
        destination = Path(configured_report)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(result["report"], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return result


def _preserve_debug_root(root: Path) -> None:
    configured_debug = os.getenv("JARVISOS_BLUECAD_PROOF_DEBUG_DIR")
    if configured_debug and root.is_dir():
        destination = Path(configured_debug) / "fem-verification-c2-battery"
        shutil.copytree(root, destination, dirs_exist_ok=True)


@pytest.mark.bluecad_kernel
@pytest.mark.bluecad_gmsh
@pytest.mark.bluecad_ccx
def test_real_cantilever_tip_displacement_and_refinement(
    real_battery: dict[str, Any],
) -> None:
    case = _case(real_battery, "cantilever")
    assert case["verdict"] == "pass", case
    assert case["comparison"]["relative_error"] <= 0.02
    assert case["sampling"]["selected"]["coordinates"][0] == pytest.approx(
        200.0,
        abs=1.0e-6,
    )
    assert case["refinement"]["fine_not_less_accurate"] is True
    assert case["load_balance"]["verdict"] == "pass"
    assert case["coarse_load_balance"]["verdict"] == "pass"


@pytest.mark.bluecad_kernel
@pytest.mark.bluecad_gmsh
@pytest.mark.bluecad_ccx
def test_real_lame_location_stresses_area_and_equilibrium(
    real_battery: dict[str, Any],
) -> None:
    case = _case(real_battery, "lame_open_end_cylinder")
    assert case["verdict"] == "pass", case
    comparisons = {item["name"]: item for item in case["comparisons"]}
    assert comparisons["lame_bore_hoop_stress"]["relative_error"] <= 0.05
    assert comparisons["lame_bore_radial_stress"]["relative_error"] <= 0.10
    assert comparisons["lame_bore_radial_stress"]["sign_check"] == "pass"
    assert case["sampling"]["distinct_angle_count"] >= 8
    assert case["pressure_audit"]["area_relative_error"] <= 0.01
    assert case["pressure_audit"]["verdict"] == "pass"
    assert case["reaction_balance"]["verdict"] == "pass"


@pytest.mark.bluecad_kernel
@pytest.mark.bluecad_gmsh
@pytest.mark.bluecad_ccx
def test_real_plate_hole_symmetric_tangential_stress(
    real_battery: dict[str, Any],
) -> None:
    case = _case(real_battery, "finite_width_plate_with_hole")
    assert case["verdict"] == "pass", case
    assert case["comparison"]["relative_error"] <= 0.07
    assert len(case["sampling"]["selected"]) == 2
    assert {item["side"] for item in case["sampling"]["selected"]} == {
        "positive_y",
        "negative_y",
    }
    assert all(
        abs(item["coordinates"][0] - 100.0) <= 20.0 / 12.0
        for item in case["sampling"]["selected"]
    )
    assert case["source"]["nominal_stress_convention"] == "net section"
    assert case["load_balance"]["verdict"] == "pass"


@pytest.mark.bluecad_kernel
@pytest.mark.bluecad_gmsh
@pytest.mark.bluecad_ccx
def test_real_battery_report_is_complete_and_bounded(
    real_battery: dict[str, Any],
) -> None:
    report = real_battery["report"]
    assert report["verdict"] == "pass", report
    assert report["schema_version"] == "bluecad_fem_verification_battery_v0_1"
    assert set(report["toolchain"]) == {"gmsh", "calculix"}
    assert all(
        report["toolchain"][tool]["binary_sha256"]
        for tool in report["toolchain"]
    )
    assert {item["case_id"] for item in report["cases"]} == {
        "cantilever",
        "lame_open_end_cylinder",
        "finite_width_plate_with_hole",
    }
    proof_root = Path(real_battery["proof_root"])
    for relative in real_battery["report_artifacts"].values():
        assert not Path(relative).is_absolute()
        assert (proof_root / relative).is_file()


def _case(result: dict[str, Any], case_id: str) -> dict[str, Any]:
    return next(
        item
        for item in result["report"]["cases"]
        if item["case_id"] == case_id
    )
