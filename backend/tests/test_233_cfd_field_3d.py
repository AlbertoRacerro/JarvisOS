"""Spec 233: computed 3D pipe surfaces and quarter-domain reflection metadata."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from app.modules.bluecad.cfd_pipe import case_runs, casegen, field_export
from tests.test_216_case_runs import _finished, _start, _wait
from tests.test_216_case_runs import setup as setup  # noqa: F401
from tests.test_216_cfd_pipe_case import c1_turbulent
from tests.test_216_field_export import GEOM, POINT
from tests.test_216_process_pin_from_design import water  # noqa: F401

NAMES = (*casegen.FIELD_3D_PATCHES, *(name for name, _ in casegen.FIELD_3D_SECTIONS))


def _export(case: Path, name: str, text: str = POINT, filename: str | None = None) -> None:
    directory = case / "postProcessing" / casegen.FIELD_3D_FUNCTION / "100"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / (filename or f"{name}.vtk")).write_text(text, encoding="utf-8")


def _all(case: Path) -> None:
    for name in NAMES:
        _export(case, name)


def test_control_dict_has_six_surfaces_without_changing_old_object() -> None:
    contract = c1_turbulent()
    text = casegen.generate(contract)["system/controlDict"]
    old = text.split("    pipe_fields\n    {", 1)[1].split("    pipe_fields_3d\n    {", 1)[0]
    assert "symmetry_z" in old and "section_25" not in old
    new = text.split("    pipe_fields_3d\n    {", 1)[1]
    assert new.count("interpolate true;") == 6
    assert "interpolate true;" not in new.split("surfaces\n", 1)[0]
    for name in NAMES:
        assert f"{name}\n" in new
    for _, fraction in casegen.FIELD_3D_SECTIONS:
        assert f"point ({contract.domain.length_m * fraction:.12g} 0 0);" in new
    assert new.count("normal (1 0 0);") == 3
    for token in ("surfaceFormat vtk;", "legacy true;", "format ascii;", "interpolationScheme cellPoint;",
                  "interpolate true;", "fields (p U);"):
        assert token in new
    assert casegen.SYMMETRY == {"domain": "quarter", "axis": "x", "mirror_planes": ["y=0", "z=0"],
                                "basis": casegen.SYMMETRY_BASIS}


def test_reader_six_surfaces_and_reflections(tmp_path) -> None:
    _all(tmp_path)
    out = field_export.read_field_surfaces_3d(tmp_path)
    assert out["status"] == "available" and out["time"] == "100"
    assert out["point_count"] == 24 and out["cell_count"] == 12
    assert [s["name"] for s in out["surfaces"]] == list(NAMES)
    for surface in out["surfaces"]:
        assert surface["provenance"] == "computed"
        assert surface["kind"] in {"wall", "symmetry_plane", "section"}
        assert surface["fields"]["p"]["units"] == "m2/s2 (kinematic)"
        assert surface["fields"]["U"]["units"] == "m/s"
        assert surface["fields"]["|U|"]["values"] == [1, 2, 5, 0]
    operators = out["reflection_operators"]
    assert operators == {
        "y=0": {"position": [1, -1, 1], "vector_field": [1, -1, 1], "scalar": "unchanged"},
        "z=0": {"position": [1, 1, -1], "vector_field": [1, 1, -1], "scalar": "unchanged"},
    }
    point, velocity = [2, 3, 4], [5, 6, 7]
    for plane, expected_point, expected_velocity in (("y=0", [2, -3, 4], [5, -6, 7]),
                                                      ("z=0", [2, 3, -4], [5, 6, -7])):
        signs = operators[plane]
        assert [a * b for a, b in zip(point, signs["position"], strict=True)] == expected_point
        assert [a * b for a, b in zip(velocity, signs["vector_field"], strict=True)] == expected_velocity
    assert operators["y=0"]["scalar"] == "unchanged"


def test_reader_refuses_missing_malformed_and_oversize(tmp_path, monkeypatch) -> None:
    assert field_export.read_field_surfaces_3d(tmp_path)["status"] == "unavailable"
    _all(tmp_path)
    directory = tmp_path / "postProcessing" / casegen.FIELD_3D_FUNCTION / "100"
    (directory / "section_75.vtk").unlink()
    assert "section_75" in field_export.read_field_surfaces_3d(tmp_path)["reason"]
    _export(tmp_path, "section_75", POINT.replace("1 2 3 4", "1 nan 3 4"))
    assert "non-finite" in field_export.read_field_surfaces_3d(tmp_path)["reason"]
    _export(tmp_path, "section_75", GEOM + "POINT_DATA 4\nSCALARS p float\nLOOKUP_TABLE default\n1 2 3 4\n")
    assert "velocity" in field_export.read_field_surfaces_3d(tmp_path)["reason"]
    _export(tmp_path, "section_75")
    monkeypatch.setattr(field_export, "MAX_POINTS", 23)
    large = field_export.read_field_surfaces_3d(tmp_path)
    assert large["status"] == "too_large" and large["points"] == 24 and "surfaces" not in large


def test_run_record_and_route(monkeypatch, setup) -> None:  # noqa: F811
    client, url, _ = setup
    entered, release = threading.Event(), threading.Event()

    def fake(contract, case_dir, **_):
        entered.set()
        assert release.wait(10)
        _all(case_dir)
        return _finished()

    monkeypatch.setattr(case_runs, "execute_pipe_case", fake)
    run_id = _start(client, url).json()["run_id"]
    run_url = f"{url}/{run_id}"
    assert entered.wait(10)
    try:
        record = json.loads((case_runs._component_dir(_ws(client, url), url.split("/")[-2]) /
                             run_id / "run.json").read_text())
        assert record["symmetry"] == casegen.SYMMETRY
        assert client.get(f"{run_url}/fields-3d").json()["status"] == "unavailable"
    finally:
        release.set()
    _wait(client, run_url, {"finished"})
    result = client.get(f"{run_url}/fields-3d").json()
    assert result["status"] == "available" and result["run_id"] == run_id
    assert result["symmetry"] == casegen.SYMMETRY and len(result["surfaces"]) == 6
    assert client.get(f"{url}/not-a-run/fields-3d").status_code == 404
    assert client.get(url.replace("/cfd-runs", "x/cfd-runs") + f"/{run_id}/fields-3d").status_code == 404
    path = case_runs._component_dir(_ws(client, url), url.split("/")[-2]) / run_id / "run.json"
    old = json.loads(path.read_text())
    old.pop("symmetry")
    case_runs._write(path, old)
    assert "symmetry" in client.get(f"{run_url}/fields-3d").json()["reason"]


def _ws(client, url):
    from tests.test_216_case_runs import _ws as existing_ws

    return existing_ws(client, url)
