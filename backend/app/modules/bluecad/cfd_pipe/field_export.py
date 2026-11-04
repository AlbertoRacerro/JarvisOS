"""Pressure and velocity field slice of a finished pipe case, read from OpenFOAM's own VTK export (spec 216 contract 6).

The case's ``pipe_fields`` function object writes the ``symmetry_z`` patch (the z = 0 axial section) as ASCII legacy
VTK POLYDATA under ``postProcessing/pipe_fields/<time>/``. This module parses that text strictly and returns a
JSON-safe payload. Only values present in the file are reported: a missing array, a count mismatch or a non-finite
value refuses the whole export, and a slice above the size limits is reported ``too_large`` rather than truncated.
The XML (``.vtp``) and binary variants are not read; they are refused with a plain reason.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from app.modules.bluecad.cfd_pipe.casegen import (
    FIELD_3D_FUNCTION,
    FIELD_3D_PATCHES,
    FIELD_3D_SECTIONS,
    FIELD_FUNCTION,
    FIELD_SURFACE,
)

FIELD_DIR = Path("postProcessing") / FIELD_FUNCTION
MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_CELLS = 40000
MAX_POINTS = 80000
PLANE = {"patch": FIELD_SURFACE, "normal": [0.0, 0.0, 1.0], "axis": "x",
         "description": "axial section through the pipe axis (symmetry plane z = 0 of the modelled quarter)"}
_UNITS = {"p": "m2/s2 (kinematic)", "U": "m/s"}
_ARRAYS = {"p": 1, "U": 3}


class FieldExportError(ValueError):
    pass


class _Tokens:
    def __init__(self, text: str) -> None:
        self.items, self.at = text.split(), 0

    def peek(self) -> str | None:
        return self.items[self.at] if self.at < len(self.items) else None

    def next(self) -> str:
        token = self.peek()
        if token is None:
            raise FieldExportError("VTK file ended unexpectedly")
        self.at += 1
        return token

    def integer(self) -> int:
        try:
            value = int(self.next())
        except ValueError as exc:
            raise FieldExportError("VTK file has a non-integer where a count was expected") from exc
        if value < 0:
            raise FieldExportError("VTK file has a negative count")
        return value

    def numbers(self, count: int) -> list[float]:
        if count > len(self.items) - self.at:
            raise FieldExportError("VTK file has fewer values than declared")
        try:
            values = [float(token) for token in self.items[self.at:self.at + count]]
        except ValueError as exc:
            raise FieldExportError("VTK file has a non-numeric value") from exc
        self.at += count
        if not all(math.isfinite(v) for v in values):
            raise FieldExportError("VTK file has a non-finite value")
        return values


def _rows(flat: list[float], width: int) -> list[list[float]]:
    return [flat[i:i + width] for i in range(0, len(flat), width)]


def parse_legacy_vtk(text: str) -> dict[str, Any]:
    """Parse ASCII legacy VTK POLYDATA into ``points``, ``polygons`` and ``point``/``cell`` arrays (name -> rows)."""
    lines = text.split("\n", 3)
    if len(lines) < 4 or not lines[0].startswith("# vtk DataFile"):
        raise FieldExportError("not a legacy VTK file")
    if lines[2].strip().upper() != "ASCII":
        raise FieldExportError("only ASCII legacy VTK is read; binary VTK is refused")
    tok = _Tokens(lines[3])
    if (tok.next(), tok.next()) != ("DATASET", "POLYDATA"):
        raise FieldExportError("only DATASET POLYDATA is read")
    points: list[list[float]] | None = None
    polygons: list[list[int]] | None = None
    data: dict[str, dict[str, list[list[float]]]] = {"point": {}, "cell": {}}
    where: str | None = None
    counts: dict[str, int] = {}

    def array(name: str, width: int, rows: int) -> None:
        if where is None:
            raise FieldExportError("VTK array before POINT_DATA or CELL_DATA")
        if rows != counts[where]:
            raise FieldExportError(f"array {name!r} has {rows} entries, expected {counts[where]}")
        if name in data[where]:
            raise FieldExportError(f"array {name!r} appears twice")
        data[where][name] = _rows(tok.numbers(width * rows), width)

    while (word := tok.peek()) is not None:
        tok.next()
        key = word.upper()
        if key == "POINTS":
            n = tok.integer()
            tok.next()
            points = _rows(tok.numbers(3 * n), 3)
        elif key == "POLYGONS":
            n, size = tok.integer(), tok.integer()
            polygons, used = [], 0
            for _ in range(n):
                k = tok.integer()
                ids = [int(v) for v in tok.numbers(k)]
                polygons.append(ids)
                used += k + 1
            if used != size:
                raise FieldExportError("POLYGONS size does not match its connectivity")
        elif key in {"POINT_DATA", "CELL_DATA"}:
            where = key.split("_")[0].lower()
            counts[where] = tok.integer()
        elif key == "FIELD":
            tok.next()
            for _ in range(tok.integer()):
                name, width, rows = tok.next(), tok.integer(), tok.integer()
                tok.next()
                if name in _ARRAYS:
                    array(name, width, rows)
                else:
                    tok.numbers(width * rows)  # e.g. TIME or CYCLE metadata
        elif key in {"SCALARS", "VECTORS"}:
            name = tok.next()
            tok.next()
            width = 1
            if key == "SCALARS":
                ahead = tok.peek()
                if ahead is not None and ahead.isdigit():
                    width = tok.integer()
                if (tok.next(), tok.next()) != ("LOOKUP_TABLE", "default"):
                    raise FieldExportError("SCALARS needs LOOKUP_TABLE default")
            else:
                width = 3
            if where is None:
                raise FieldExportError("VTK array before POINT_DATA or CELL_DATA")
            array(name, width, counts[where])
        else:
            raise FieldExportError(f"unsupported VTK section {word!r}")
    if points is None or polygons is None:
        raise FieldExportError("VTK file lacks POINTS or POLYGONS")
    if any(len(poly) < 3 or any(i >= len(points) for i in poly) for poly in polygons):
        raise FieldExportError("a polygon is degenerate or indexes a missing point")
    if "point" in counts and counts["point"] != len(points) or "cell" in counts and counts["cell"] != len(polygons):
        raise FieldExportError("data count does not match the geometry")
    return {"points": points, "polygons": polygons, **data}


def _latest_time_dir(root: Path) -> tuple[str, Path] | None:
    times = []
    for directory in root.glob("*") if root.is_dir() else []:
        try:
            times.append((float(directory.name), directory))
        except ValueError:
            continue
    if not times or not all(math.isfinite(t) for t, _ in times):
        return None
    _, directory = max(times, key=lambda item: item[0])
    return directory.name, directory


def _mag(vectors: list[list[float]]) -> list[float]:
    return [math.sqrt(sum(c * c for c in v)) for v in vectors]


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason}


def read_field_slice(case: Path) -> dict[str, Any]:
    """Payload for the latest-time field export of ``case``; ``unavailable`` / ``too_large`` instead of a guess."""
    try:
        latest = _latest_time_dir(case / FIELD_DIR)
        if latest is None:
            return _unavailable("the case wrote no field export")
        time, directory = latest
        files = sorted(p for p in directory.iterdir() if p.is_file() and not p.is_symlink())
        if any(p.suffix == ".vtp" for p in files) and not any(p.suffix == ".vtk" for p in files):
            return _unavailable("the field export is in XML (vtp) form, which is not read; legacy ASCII VTK is required")
        files = [p for p in files if p.suffix == ".vtk"]
        if not files:
            return _unavailable("the latest field export directory has no VTK file")
        merged: dict[str, Any] | None = None
        for file in files:
            if file.stat().st_size > MAX_FILE_BYTES:
                return {"status": "too_large", "reason": f"{file.name} exceeds {MAX_FILE_BYTES} bytes", "time": time}
            part = parse_legacy_vtk(file.read_text(encoding="utf-8"))
            if merged is None:
                merged = {**part, "point": dict(part["point"]), "cell": dict(part["cell"])}
                continue
            if part["points"] != merged["points"] or part["polygons"] != merged["polygons"]:
                raise FieldExportError("VTK files of one export disagree on geometry")
            for assoc in ("point", "cell"):
                if set(part[assoc]) & set(merged[assoc]):
                    raise FieldExportError("VTK files of one export repeat an array")
                merged[assoc].update(part[assoc])
        assert merged is not None
        if len(merged["polygons"]) > MAX_CELLS or len(merged["points"]) > MAX_POINTS:
            return {"status": "too_large", "time": time, "cells": len(merged["polygons"]),
                    "points": len(merged["points"]),
                    "reason": f"the slice exceeds {MAX_CELLS} cells or {MAX_POINTS} points; it is not truncated"}
        association = next((a for a in ("point", "cell") if set(_ARRAYS) <= set(merged[a])), None)
        if association is None:
            return _unavailable("the export lacks a complete pressure p and velocity U array set")
        p, u = merged[association]["p"], merged[association]["U"]
        speed = _mag(u)
        pressure = [row[0] for row in p]
        return {
            "status": "available", "time": time, "plane": PLANE, "association": association,
            "points": merged["points"], "polygons": merged["polygons"],
            "point_count": len(merged["points"]), "cell_count": len(merged["polygons"]),
            "source_files": [f.name for f in files],
            "fields": {
                "p": {"values": pressure, "min": min(pressure), "max": max(pressure), "units": _UNITS["p"]},
                "U": {"values": u, "magnitude_min": min(speed), "magnitude_max": max(speed), "units": _UNITS["U"]},
            },
        }
    except (FieldExportError, OSError, UnicodeDecodeError) as exc:
        return _unavailable(f"the field export could not be read: {exc}")


_SURFACE_KINDS = {"wall": "wall", "symmetry_y": "symmetry_plane", "symmetry_z": "symmetry_plane",
                  **{name: "section" for name, _ in FIELD_3D_SECTIONS}}
REFLECTION_OPERATORS = {
    "y=0": {"position": [1, -1, 1], "vector_field": [1, -1, 1], "scalar": "unchanged"},
    "z=0": {"position": [1, 1, -1], "vector_field": [1, 1, -1], "scalar": "unchanged"},
}


def _surface_name(stem: str) -> str | None:
    """Accept combined and per-field OpenFOAM VTK filenames, only for declared surfaces."""
    for name in _SURFACE_KINDS:
        if stem in {name, f"p_{name}", f"U_{name}", f"{name}_p", f"{name}_U"}:
            return name
    return None


def read_field_surfaces_3d(case: Path) -> dict[str, Any]:
    """Read the six computed surfaces at the latest export time, without partial results."""
    try:
        latest = _latest_time_dir(case / "postProcessing" / FIELD_3D_FUNCTION)
        if latest is None:
            return _unavailable("the case wrote no 3D field export")
        time, directory = latest
        files = sorted(p for p in directory.iterdir() if p.is_file() and not p.is_symlink() and
                       p.suffix.lower() == ".vtk")
        if not files:
            return _unavailable("the latest 3D field export has no legacy VTK files")
        grouped: dict[str, list[Path]] = {name: [] for name in _SURFACE_KINDS}
        for file in files:
            name = _surface_name(file.stem)
            if name is None:
                raise FieldExportError(f"unexpected 3D surface file {file.name}")
            if file.stat().st_size > MAX_FILE_BYTES:
                return {"status": "too_large", "time": time,
                        "reason": f"{file.name} exceeds {MAX_FILE_BYTES} bytes"}
            grouped[name].append(file)
        missing = [name for name, paths in grouped.items() if not paths]
        if missing:
            return _unavailable(f"the 3D export lacks surfaces: {', '.join(missing)}")
        surfaces: list[dict[str, Any]] = []
        total_points = total_cells = 0
        for name in (*FIELD_3D_PATCHES, *(section for section, _ in FIELD_3D_SECTIONS)):
            merged: dict[str, Any] | None = None
            for file in grouped[name]:
                part = parse_legacy_vtk(file.read_text(encoding="utf-8"))
                if merged is None:
                    merged = part
                else:
                    if part["points"] != merged["points"] or part["polygons"] != merged["polygons"]:
                        raise FieldExportError(f"VTK files for {name} disagree on geometry")
                    for assoc in ("point", "cell"):
                        if set(part[assoc]) & set(merged[assoc]):
                            raise FieldExportError(f"VTK files for {name} repeat an array")
                        merged[assoc].update(part[assoc])
            assert merged is not None
            points, polygons = merged["points"], merged["polygons"]
            total_points += len(points)
            total_cells += len(polygons)
            if total_points > MAX_POINTS or total_cells > MAX_CELLS:
                return {"status": "too_large", "time": time, "points": total_points, "cells": total_cells,
                        "reason": f"the computed surfaces exceed {MAX_CELLS} cells or {MAX_POINTS} points"}
            if not points or not polygons:
                raise FieldExportError(f"surface {name} has no geometry")
            # A vertex probe and colour map require interpolated values at each vertex.
            arrays = merged["point"]
            if set(_ARRAYS) - set(arrays):
                return _unavailable(f"surface {name} lacks point pressure p or velocity U")
            if any(len(row) != 1 for row in arrays["p"]) or any(len(row) != 3 for row in arrays["U"]):
                raise FieldExportError(f"surface {name} has an invalid p or U array width")
            pressure = [row[0] for row in arrays["p"]]
            velocity = arrays["U"]
            speed = _mag(velocity)
            if not all(math.isfinite(value) for value in speed):
                raise FieldExportError(f"surface {name} has a non-finite speed")
            surfaces.append({
                "name": name, "kind": _SURFACE_KINDS[name], "provenance": "computed",
                "points": points, "polygons": polygons, "point_count": len(points), "cell_count": len(polygons),
                "source_files": [file.name for file in grouped[name]],
                "fields": {
                    "p": {"values": pressure, "min": min(pressure), "max": max(pressure), "units": _UNITS["p"]},
                    "U": {"values": velocity, "magnitude_min": min(speed), "magnitude_max": max(speed),
                          "units": _UNITS["U"]},
                    "|U|": {"values": speed, "min": min(speed), "max": max(speed), "units": _UNITS["U"]},
                },
            })
        return {"status": "available", "time": time, "surfaces": surfaces, "point_count": total_points,
                "cell_count": total_cells, "reflection_operators": REFLECTION_OPERATORS}
    except (FieldExportError, OSError, UnicodeDecodeError) as exc:
        return _unavailable(f"the 3D field export could not be read: {exc}")
