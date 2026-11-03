#!/usr/bin/env python3
"""Explicit maintainer generator for spec 024-C FEM verification fixtures."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import build123d as bd
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire
from OCP.BRepFeat import BRepFeat_SplitShape
from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt

GENERATOR_VERSION = "bluecad_fem_verification_fixtures_v0_1"
FIXED_TIMESTAMP = datetime(2026, 7, 11, tzinfo=UTC)
ROOT = Path(__file__).resolve().parent


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def _export(shape: bd.Shape, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not bd.export_step(shape, path, timestamp=FIXED_TIMESTAMP, write_pcurves=False):
        raise RuntimeError(f"failed to export {path}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _port(origin: list[float], direction: list[float], diameter: float) -> dict:
    return {"origin": origin, "direction": direction, "outer_d": diameter}


def _beam() -> tuple[bd.Shape, dict]:
    shape = bd.Box(
        200.0,
        10.0,
        10.0,
        align=(bd.Align.MIN, bd.Align.CENTER, bd.Align.CENTER),
    )
    manifest = {
        "fixture": "cantilever",
        "dimensions_mm": {"length": 200.0, "width": 10.0, "height": 10.0},
        "resolved_ports": {
            "beam": {
                "fixed": _port([0.0, 0.0, 0.0], [-1.0, 0.0, 0.0], 12.0),
                "loaded": _port([200.0, 0.0, 0.0], [1.0, 0.0, 0.0], 12.0),
            }
        },
    }
    return shape, manifest


def _segmented_cylinder() -> tuple[bd.Shape, dict]:
    outer = bd.Cylinder(
        40.0,
        160.0,
        align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN),
    )
    inner = bd.Cylinder(
        20.0,
        160.0,
        align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.MIN),
    )
    annulus = outer - inner
    inner_face = min(
        [face for face in annulus.faces() if face.geom_type == bd.GeomType.CYLINDER],
        key=lambda face: face.area,
    )
    splitter = BRepFeat_SplitShape(annulus.wrapped)
    for z_value in range(20, 160, 20):
        circle = gp_Circ(
            gp_Ax2(gp_Pnt(0.0, 0.0, float(z_value)), gp_Dir(0.0, 0.0, 1.0)),
            20.0,
        )
        edge = BRepBuilderAPI_MakeEdge(circle).Edge()
        wire = BRepBuilderAPI_MakeWire(edge).Wire()
        splitter.Add(wire, inner_face.wrapped)
    splitter.Build()
    if not splitter.IsDone():
        raise RuntimeError("failed to split the cylinder bore")
    shape = bd.Solid(splitter.Shape())
    bore_faces = [
        face
        for face in shape.faces()
        if face.geom_type == bd.GeomType.CYLINDER
        and abs(face.bounding_box().max.X - 20.0) < 1.0e-7
    ]
    bands = sorted(
        (
            round(face.bounding_box().min.Z, 7),
            round(face.bounding_box().max.Z, 7),
        )
        for face in bore_faces
    )
    expected = [(float(start), float(start + 20)) for start in range(0, 160, 20)]
    if bands != expected:
        raise RuntimeError(f"unexpected bore partition: {bands}")

    half_side = 20.5
    selection_diameter = half_side / 0.75
    ports = {
        f"bore_{index + 1:02d}": _port(
            [0.0, 0.0, 10.0 + 20.0 * index],
            [1.0, 0.0, 0.0],
            selection_diameter,
        )
        for index in range(8)
    }
    ports["fixed"] = _port([0.0, 0.0, -40.0], [0.0, 0.0, -1.0], 54.0)
    manifest = {
        "fixture": "segmented_open_end_cylinder",
        "dimensions_mm": {
            "inner_radius": 20.0,
            "outer_radius": 40.0,
            "length": 160.0,
            "bore_band_length": 20.0,
            "bore_band_count": 8,
            "selection_half_side": half_side,
        },
        "resolved_ports": {"cylinder": ports},
    }
    return shape, manifest


def _plate() -> tuple[bd.Shape, dict]:
    plate = bd.Box(
        200.0,
        100.0,
        5.0,
        align=(bd.Align.MIN, bd.Align.CENTER, bd.Align.CENTER),
    )
    hole = bd.Cylinder(
        10.0,
        5.0,
        align=(bd.Align.CENTER, bd.Align.CENTER, bd.Align.CENTER),
    ).locate(bd.Pos(100.0, 0.0, 0.0))
    shape = plate - hole
    manifest = {
        "fixture": "finite_width_plate_with_hole",
        "dimensions_mm": {
            "length": 200.0,
            "width": 100.0,
            "thickness": 5.0,
            "hole_diameter": 20.0,
        },
        "resolved_ports": {
            "plate": {
                "fixed": _port([0.0, 0.0, 0.0], [-1.0, 0.0, 0.0], 102.0),
                "loaded": _port([200.0, 0.0, 0.0], [1.0, 0.0, 0.0], 102.0),
            }
        },
    }
    return shape, manifest


def generate() -> dict:
    fixtures = {
        "cantilever": _beam(),
        "segmented_cylinder": _segmented_cylinder(),
        "plate_with_hole": _plate(),
    }
    index_entries = []
    for name, (shape, manifest) in fixtures.items():
        directory = ROOT / name
        step_path = directory / "model.step"
        manifest_path = directory / "manifest.json"
        _export(shape, step_path)
        _write_json(manifest_path, manifest)
        index_entries.append(
            {
                "name": name,
                "dimensions_mm": manifest["dimensions_mm"],
                "files": {
                    "step": {
                        "path": step_path.relative_to(ROOT).as_posix(),
                        "sha256": _sha256(step_path),
                    },
                    "manifest": {
                        "path": manifest_path.relative_to(ROOT).as_posix(),
                        "sha256": _sha256(manifest_path),
                    },
                },
            }
        )
    payload = {
        "schema_version": "bluecad_fem_verification_fixture_index_v0_1",
        "generator_version": GENERATOR_VERSION,
        "build123d_version": "0.11.1",
        "fixtures": index_entries,
    }
    _write_json(ROOT / "fixture_index.json", payload)
    return payload


if __name__ == "__main__":
    print(json.dumps(generate(), indent=2, sort_keys=True))
