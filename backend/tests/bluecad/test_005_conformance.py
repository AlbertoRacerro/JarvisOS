"""BLUECAD spec-005 conformance tests — REVIEWER-OWNED.

Written by the reviewing tier, independently of the implementation (see
AGENTS.md "Reviewer-owned conformance tests"). Implementation PRs must not
add to, modify, or delete this file.

Design principle: trust nothing the implementation says about itself. These
tests measure the exported artifacts (binary/ASCII STL geometry, manifest
hashes, validation report verdicts) and compare them against analytic truth,
so a placeholder solid, an unplaced assembly, or a stubbed always-pass
validator fails here even if unit tests written alongside the implementation
pass.

Skips cleanly where the bluecad module or build123d is not installed.
"""

from __future__ import annotations

import hashlib
import json
import math
import struct
from pathlib import Path

import pytest

service = pytest.importorskip(
    "app.modules.bluecad.service", reason="bluecad module not present on this branch"
)
pytest.importorskip("build123d", reason="CAD kernel not installed")

OUTER_D = 110.0
WALL_T = 4.0
BEND_R = 400.0


def _spec(parts: list[dict], connections: list[dict] | None = None, declared: dict | None = None) -> dict:
    spec: dict = {"spec_version": "bluecad_geometry_spec_v0_1", "parts": parts}
    spec["connections"] = connections or []
    if declared is not None:
        spec["declared"] = declared
    return spec


def _tube(part_id: str, length: float, frame: dict | None = None) -> dict:
    part: dict = {
        "part_id": part_id,
        "kind": "tube_run",
        "params": {"outer_d": OUTER_D, "wall_t": WALL_T, "length": length},
    }
    if frame is not None:
        part["frame"] = frame
    return part


def _annulus_area(outer_d: float, wall_t: float) -> float:
    inner_d = outer_d - 2.0 * wall_t
    return math.pi / 4.0 * (outer_d**2 - inner_d**2)


# --- STL measurement (artifact-level ground truth) -------------------------


def _stl_triangles(path: Path) -> list[tuple[tuple[float, float, float], ...]]:
    data = path.read_bytes()
    assert len(data) > 84, "STL artifact is implausibly small"
    if not data[:5].lstrip().lower().startswith(b"solid"):
        return _stl_triangles_binary(data)
    # Heuristic: binary files may still start with 'solid'; check size math.
    count = struct.unpack_from("<I", data, 80)[0] if len(data) >= 84 else -1
    if len(data) == 84 + 50 * count:
        return _stl_triangles_binary(data)
    return _stl_triangles_ascii(data)


def _stl_triangles_binary(data: bytes) -> list[tuple[tuple[float, float, float], ...]]:
    count = struct.unpack_from("<I", data, 80)[0]
    assert len(data) == 84 + 50 * count, "binary STL size does not match triangle count"
    tris = []
    offset = 84
    for _ in range(count):
        values = struct.unpack_from("<12f", data, offset)
        tris.append((tuple(values[3:6]), tuple(values[6:9]), tuple(values[9:12])))
        offset += 50
    return tris


def _stl_triangles_ascii(data: bytes) -> list[tuple[tuple[float, float, float], ...]]:
    vertices: list[tuple[float, float, float]] = []
    tris = []
    for raw in data.decode("ascii", errors="strict").splitlines():
        line = raw.strip()
        if line.startswith("vertex"):
            _, x, y, z = line.split()
            vertices.append((float(x), float(y), float(z)))
            if len(vertices) == 3:
                tris.append(tuple(vertices))
                vertices = []
    assert not vertices, "ASCII STL has a dangling vertex list"
    return tris


def _stl_volume(tris) -> float:
    total = 0.0
    for v0, v1, v2 in tris:
        cx = v1[1] * v2[2] - v1[2] * v2[1]
        cy = v1[2] * v2[0] - v1[0] * v2[2]
        cz = v1[0] * v2[1] - v1[1] * v2[0]
        total += (v0[0] * cx + v0[1] * cy + v0[2] * cz) / 6.0
    return abs(total)


def _stl_bbox(tris) -> tuple[list[float], list[float]]:
    xs = [v[axis] for tri in tris for v in tri for axis in (0,)]
    mins = [min(v[a] for tri in tris for v in tri) for a in range(3)]
    maxs = [max(v[a] for tri in tris for v in tri) for a in range(3)]
    assert xs, "STL contains no triangles"
    return mins, maxs


# --- helpers ----------------------------------------------------------------


def _build_and_read(spec: dict, out_dir: Path) -> tuple[dict, dict]:
    service.build_geometry_spec(spec, out_dir)
    report = json.loads((out_dir / "validation_report.json").read_text(encoding="utf-8"))
    manifest_path = out_dir / "manifest.json"
    manifest = (
        json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    )
    return report, manifest


# --- conformance tests ------------------------------------------------------


def test_bend_exports_curved_geometry_not_a_straight_tube(tmp_path: Path) -> None:
    """A 90-degree bend must extend laterally by ~bend_radius in the exported
    STL. A straight placeholder solid (any length) only spans outer_d
    laterally, so it fails regardless of which curvature plane convention the
    implementation chose."""
    spec = _spec(
        [
            {
                "part_id": "b1",
                "kind": "bend",
                "params": {
                    "outer_d": OUTER_D,
                    "wall_t": WALL_T,
                    "bend_radius": BEND_R,
                    "angle": math.pi / 2.0,
                },
                "frame": {"origin": [0, 0, 0], "direction": [1, 0, 0]},
            }
        ]
    )
    report, _ = _build_and_read(spec, tmp_path)
    assert report["verdict"] == "pass", f"bend build should pass, got: {report}"
    mins, maxs = _stl_bbox(_stl_triangles(tmp_path / "model.stl"))
    lateral_spans = sorted(maxs[a] - mins[a] for a in range(3))
    # The two largest spans belong to the bend plane; both must be ~R-scale.
    assert lateral_spans[-2] > 0.8 * BEND_R, (
        f"exported bend spans {lateral_spans}; a real 90-degree bend of radius "
        f"{BEND_R} must extend ~R in two axes — a straight tube does not"
    )


def test_assembly_places_connected_parts_not_a_pile_at_origin(tmp_path: Path) -> None:
    """Two collinear tubes (400 + 300 mm) joined port_b->port_a must span
    ~700 mm in the exported STL. Unplaced parts left in their local frames
    overlap near the origin and span only ~400 mm."""
    spec = _spec(
        [
            _tube("t1", 400.0, frame={"origin": [0, 0, 0], "direction": [1, 0, 0]}),
            _tube("t2", 300.0),
        ],
        connections=[{"from": "t1.port_b", "to": "t2.port_a"}],
    )
    report, _ = _build_and_read(spec, tmp_path)
    assert report["verdict"] == "pass", f"assembly build should pass, got: {report}"
    mins, maxs = _stl_bbox(_stl_triangles(tmp_path / "model.stl"))
    max_span = max(maxs[a] - mins[a] for a in range(3))
    assert max_span > 650.0, (
        f"assembled extent {max_span:.1f} mm; two joined tubes of 400+300 mm "
        "must span ~700 mm — parts were not placed by their connection"
    )


def test_manifest_volume_matches_measured_stl_volume(tmp_path: Path) -> None:
    """The manifest's total volume must match the volume measured from the
    exported STL mesh (divergence theorem), so analytic bookkeeping cannot
    diverge from the artifacts actually produced."""
    length = 1000.0
    spec = _spec([_tube("t1", length, frame={"origin": [0, 0, 0], "direction": [1, 0, 0]})])
    report, manifest = _build_and_read(spec, tmp_path)
    assert report["verdict"] == "pass"
    measured = _stl_volume(_stl_triangles(tmp_path / "model.stl"))
    analytic = _annulus_area(OUTER_D, WALL_T) * length
    stated = manifest["assembly"]["total_volume_mm3"]
    assert measured == pytest.approx(analytic, rel=0.02), "STL volume vs analytic"
    assert stated == pytest.approx(measured, rel=0.02), "manifest volume vs STL volume"


def test_manifest_artifact_hashes_match_files_on_disk(tmp_path: Path) -> None:
    spec = _spec([_tube("t1", 500.0, frame={"origin": [0, 0, 0], "direction": [1, 0, 0]})])
    report, manifest = _build_and_read(spec, tmp_path)
    assert report["verdict"] == "pass"
    checked = 0
    for name, entry in manifest["artifacts"].items():
        if name == "manifest.json":
            continue  # self-referential hash conventions vary; files below suffice
        digest = hashlib.sha256((tmp_path / name).read_bytes()).hexdigest()
        assert entry["sha256"] == digest, f"manifest hash for {name} does not match file"
        checked += 1
    assert checked >= 3, "manifest must hash at least STEP, STL, and GLB"


def test_declared_volume_mismatch_yields_fail_verdict(tmp_path: Path) -> None:
    """A spec whose declared volume is 10% off must FAIL validation. Catches
    stubbed always-pass validators."""
    length = 1000.0
    wrong_volume = _annulus_area(OUTER_D, WALL_T) * length * 1.10
    spec = _spec(
        [_tube("t1", length, frame={"origin": [0, 0, 0], "direction": [1, 0, 0]})],
        declared={"total_volume_mm3": {"value": wrong_volume, "rel_tol": 0.001}},
    )
    report, _ = _build_and_read(spec, tmp_path)
    assert report["verdict"] == "fail", "10% declared-volume error must fail validation"
    failing = {c["id"] for c in report["checks"] if c["status"] == "fail"}
    assert "T1_VOLUME_DECL" in failing


def test_unknown_part_kind_is_rejected_before_kernel(tmp_path: Path) -> None:
    spec = _spec(
        [{"part_id": "m1", "kind": "manifold", "params": {"outer_d": 100.0}}]
    )
    with pytest.raises(Exception) as exc_info:
        service.build_geometry_spec(spec, tmp_path)
    assert "SPEC_INVALID" in str(exc_info.value)
    assert not (tmp_path / "model.step").exists(), "no artifacts before validation"
