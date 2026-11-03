"""Spec 216 contract 6: pressure and velocity field slice parsed from a case's own legacy VTK export (offline)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from app.modules.bluecad.cfd_pipe import case_runs, casegen
from app.modules.bluecad.cfd_pipe.field_export import FieldExportError, parse_legacy_vtk, read_field_slice
from tests.test_216_case_runs import _finished, _start, _wait
from tests.test_216_case_runs import setup as setup  # noqa: F401  (fixture)
from tests.test_216_cfd_pipe_case import c1_turbulent
from tests.test_216_process_pin_from_design import water  # noqa: F401  (autouse fixture)

GEOM = """# vtk DataFile Version 2.0
symmetry_z
ASCII
DATASET POLYDATA
POINTS 4 float
0 0 0
1 0 0
1 1 0
0 1 0
POLYGONS 2 8
3 0 1 2
3 0 2 3
"""
POINT = GEOM + """POINT_DATA 4
FIELD attributes 2
p 1 4 float
1 2 3 4
U 3 4 float
1 0 0  2 0 0  3 4 0  0 0 0
"""
CELL = GEOM + """CELL_DATA 2
SCALARS p float 1
LOOKUP_TABLE default
5 7
VECTORS U float
3 4 0  0 0 2
"""


def _write(case: Path, time_name: str, name: str, text: str) -> None:
    directory = case / "postProcessing" / "pipe_fields" / time_name
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(text, encoding="utf-8")


def test_point_data_slice(tmp_path) -> None:
    _write(tmp_path, "100", "symmetry_z.vtk", POINT)
    out = read_field_slice(tmp_path)
    assert out["status"] == "available" and out["time"] == "100" and out["association"] == "point"
    assert out["point_count"] == 4 and out["cell_count"] == 2 and out["source_files"] == ["symmetry_z.vtk"]
    assert out["polygons"] == [[0, 1, 2], [0, 2, 3]] and out["points"][2] == [1.0, 1.0, 0.0]
    assert out["fields"]["p"] == {"values": [1.0, 2.0, 3.0, 4.0], "min": 1.0, "max": 4.0,
                                  "units": "m2/s2 (kinematic)"}
    assert out["fields"]["U"]["magnitude_min"] == 0.0 and out["fields"]["U"]["magnitude_max"] == 5.0
    assert out["fields"]["U"]["units"] == "m/s" and out["plane"]["patch"] == "symmetry_z"


def test_cell_data_slice_and_separate_files(tmp_path) -> None:
    _write(tmp_path, "7", "symmetry_z.vtk", CELL)
    out = read_field_slice(tmp_path)
    assert out["association"] == "cell" and out["fields"]["p"]["values"] == [5.0, 7.0]
    assert out["fields"]["U"]["values"] == [[3.0, 4.0, 0.0], [0.0, 0.0, 2.0]]
    split = tmp_path / "split"
    _write(split, "1", "p.vtk", GEOM + "CELL_DATA 2\nSCALARS p float 1\nLOOKUP_TABLE default\n1 2\n")
    assert read_field_slice(split)["status"] == "unavailable"  # U missing: nothing invented
    _write(split, "1", "U.vtk", GEOM + "CELL_DATA 2\nVECTORS U float\n1 0 0 0 1 0\n")
    assert read_field_slice(split)["source_files"] == ["U.vtk", "p.vtk"]


@pytest.mark.parametrize("text,fragment", [
    (POINT.replace("p 1 4 float\n1 2 3 4", "p 1 3 float\n1 2 3"), "3 entries, expected 4"),
    (POINT.replace("1 2 3 4", "1 nan 3 4"), "non-finite"),
    (POINT.replace("1 2 3 4", "1 inf 3 4"), "non-finite"),
    (POINT.replace("POLYGONS 2 8", "POLYGONS 2 9"), "size"),
    (POINT.replace("3 0 2 3", "3 0 2 9"), "missing point"),
    (POINT.replace("ASCII", "BINARY"), "ASCII"),
    (POINT.replace("POINT_DATA 4", "POINT_DATA 5"), "expected 5"),
    (POINT + "WIBBLE 1\n", "unsupported"),
    (POINT[: len(POINT) - 6], "fewer values"),
])
def test_refusals(tmp_path, text, fragment) -> None:
    with pytest.raises(FieldExportError, match=fragment):
        parse_legacy_vtk(text)
    _write(tmp_path, "1", "s.vtk", text)
    out = read_field_slice(tmp_path)
    assert out["status"] == "unavailable" and "values" not in str(out.get("fields"))


def test_missing_velocity_is_unavailable(tmp_path) -> None:
    _write(tmp_path, "1", "s.vtk", GEOM + "CELL_DATA 2\nSCALARS p float 1\nLOOKUP_TABLE default\n1 2\n")
    out = read_field_slice(tmp_path)
    assert out["status"] == "unavailable" and "velocity" in out["reason"]


def test_latest_time_is_used_and_absent_or_xml_is_unavailable(tmp_path) -> None:
    assert read_field_slice(tmp_path)["status"] == "unavailable"
    _write(tmp_path, "9", "s.vtk", CELL)
    _write(tmp_path, "10", "s.vtk", POINT)
    _write(tmp_path, "junk", "s.vtk", "x")
    assert read_field_slice(tmp_path)["time"] == "10"
    xml = tmp_path / "xml"
    _write(xml, "1", "s.vtp", "<VTKFile/>")
    assert "XML" in read_field_slice(xml)["reason"]


def test_oversize_slice_is_reported_not_truncated(tmp_path, monkeypatch) -> None:
    from app.modules.bluecad.cfd_pipe import field_export

    monkeypatch.setattr(field_export, "MAX_CELLS", 1)
    _write(tmp_path, "1", "s.vtk", POINT)
    out = read_field_slice(tmp_path)
    assert out["status"] == "too_large" and "polygons" not in out and out["cells"] == 2


def test_casegen_writes_the_typed_field_surface() -> None:
    text = casegen.generate(c1_turbulent())["system/controlDict"]
    for expected in ("pipe_fields", "type surfaces;", "surfaceFormat vtk;", "interpolationScheme cellPoint;",
                     "fields (p U);", "symmetry_z", "type patch;", "patches (symmetry_z);", "format ascii;"):
        assert expected in text
    assert text == casegen.generate(c1_turbulent())["system/controlDict"]


def _finished_with_fields(monkeypatch, client, url, text: str | None):
    def fake(contract, case_dir, **_):
        if text is not None:
            _write(case_dir, "100", "symmetry_z.vtk", text)
        return _finished()

    monkeypatch.setattr(case_runs, "execute_pipe_case", fake)
    run_url = f"{url}/{_start(client, url).json()['run_id']}"
    _wait(client, run_url, {"finished"})
    return run_url


def test_fields_route(monkeypatch, setup) -> None:  # noqa: F811
    client, url, _ = setup
    run_url = _finished_with_fields(monkeypatch, client, url, POINT)
    body = client.get(f"{run_url}/fields").json()
    assert body["status"] == "available" and body["run_id"] in run_url and body["fields"]["p"]["max"] == 4.0
    bare = _finished_with_fields(monkeypatch, client, url, None)
    assert client.get(f"{bare}/fields").json()["status"] == "unavailable"
    assert client.get(f"{url}/{'0' * 32}/fields").status_code == 404
    assert client.get(f"{url}/not-a-run/fields").status_code == 404


def test_fields_route_for_unfinished_run(monkeypatch, setup) -> None:  # noqa: F811
    import threading

    client, url, _ = setup
    release = threading.Event()

    def fake(contract, case_dir, *, cancel, **_):
        release.wait(10)
        return _finished()

    monkeypatch.setattr(case_runs, "execute_pipe_case", fake)
    run_url = f"{url}/{_start(client, url).json()['run_id']}"
    try:
        body = client.get(f"{run_url}/fields").json()
        assert body["status"] == "unavailable" and body["reason"].startswith("the run is ")
    finally:
        release.set()
        _wait(client, run_url, {"finished"})
        time.sleep(0)


