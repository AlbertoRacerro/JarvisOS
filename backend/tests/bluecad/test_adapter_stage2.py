from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app.modules.bluecad.service import build_geometry_spec, build_geometry_spec_file

pytest.importorskip("build123d", reason="CAD kernel not importable", exc_type=ImportError)

pytestmark = [
    pytest.mark.bluecad_kernel,
    pytest.mark.skipif(importlib.util.find_spec("build123d") is None, reason="build123d is not installed"),
]

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.json").read_text(encoding="utf-8"))


def _expected(name: str) -> dict:
    return json.loads((FIXTURE_DIR / f"{name}.expected.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", ["minimal_single_tube", "chain_tube_bend_joint", "u_shape_two_bends"])
def test_build_fixtures_export_artifacts_and_match_analytic_volumes(tmp_path: Path, name: str) -> None:
    result = build_geometry_spec(_fixture(name), tmp_path)

    assert result.verdict == "pass"
    assert result.report["verdict"] == "pass"
    for artifact in ("model.step", "model.stl", "model.glb", "manifest.json", "validation_report.json"):
        path = tmp_path / artifact
        assert path.exists()
        assert path.stat().st_size > 0
    expected = _expected(name)
    actual = result.manifest["assembly"]["total_volume_mm3"]
    tolerance = 0.005 if any(part["kind"] == "bend" for part in result.manifest["parts"].values()) else 0.001
    assert actual == pytest.approx(expected["analytic_total_volume_mm3"], rel=tolerance)


def test_repeated_builds_have_identical_manifest_geometry(tmp_path: Path) -> None:
    spec = _fixture("chain_tube_bend_joint")
    first = build_geometry_spec(spec, tmp_path / "a")
    second = build_geometry_spec(spec, tmp_path / "b")

    assert first.spec_id == second.spec_id
    assert first.manifest["parts"] == second.manifest["parts"]
    assert first.manifest["resolved_ports"] == second.manifest["resolved_ports"]
    assert first.manifest["assembly"] == second.manifest["assembly"]


def test_volume_declared_ten_percent_off_fails_validation(tmp_path: Path) -> None:
    spec = _fixture("minimal_single_tube")
    spec["declared"]["total_volume_mm3"]["value"] *= 1.1
    spec["declared"]["total_volume_mm3"]["rel_tol"] = 0.001

    result = build_geometry_spec(spec, tmp_path)

    assert result.verdict == "fail"
    check = next(item for item in result.report["checks"] if item["id"] == "T1_VOLUME_DECL")
    assert check["status"] == "fail"
    assert "relative error" in check["hint"]


def test_port_mismatch_is_structured_build_error(tmp_path: Path) -> None:
    spec = _fixture("chain_tube_bend_joint")
    spec["parts"][1]["params"]["outer_d"] = 90.0

    result = build_geometry_spec(spec, tmp_path)

    assert result.verdict == "error"
    assert result.errors[0]["code"] == "PORT_MISMATCH"
    assert not (tmp_path / "model.step").exists()
    assert result.report["checks"][0]["status"] == "skip"


def test_unknown_part_kind_rejected_before_kernel_call(tmp_path: Path) -> None:
    spec_path = tmp_path / "bad.json"
    spec = _fixture("minimal_single_tube")
    spec["parts"][0]["kind"] = "unsupported_kind"
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    result = build_geometry_spec_file(spec_path, tmp_path / "out")

    assert result.verdict == "error"
    assert result.errors[0]["code"] == "SPEC_INVALID"


def test_cli_exit_codes(tmp_path: Path) -> None:
    spec_path = FIXTURE_DIR / "minimal_single_tube.json"
    ok_out = tmp_path / "ok"
    proc = subprocess.run([sys.executable, "-m", "app.modules.bluecad", "build", str(spec_path), "--out", str(ok_out)], cwd=Path(__file__).parents[2], text=True, capture_output=True, check=False)
    assert proc.returncode == 0
    assert (ok_out / "validation_report.json").exists()

    fail_spec = _fixture("minimal_single_tube")
    fail_spec["declared"]["total_volume_mm3"]["value"] *= 1.1
    fail_path = tmp_path / "fail.json"
    fail_path.write_text(json.dumps(fail_spec), encoding="utf-8")
    proc = subprocess.run([sys.executable, "-m", "app.modules.bluecad", "build", str(fail_path), "--out", str(tmp_path / "fail")], cwd=Path(__file__).parents[2], text=True, capture_output=True, check=False)
    assert proc.returncode == 1

    bad_spec = _fixture("minimal_single_tube")
    bad_spec["parts"][0]["kind"] = "unsupported_kind"
    bad_path = tmp_path / "bad.json"
    bad_path.write_text(json.dumps(bad_spec), encoding="utf-8")
    proc = subprocess.run([sys.executable, "-m", "app.modules.bluecad", "build", str(bad_path), "--out", str(tmp_path / "bad")], cwd=Path(__file__).parents[2], text=True, capture_output=True, check=False)
    assert proc.returncode == 2
