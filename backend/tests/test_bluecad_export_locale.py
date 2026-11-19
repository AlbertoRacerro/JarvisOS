"""Building BlueCAD artifacts leaves the process locale as it found it (OpenCascade can reset it to "C")."""

from __future__ import annotations

import locale
from pathlib import Path

from app.modules.bluecad.export import build_artifacts
from app.modules.bluecad.template import TubeTemplateCreate, template_geometry_spec


def test_build_artifacts_restores_process_locale(tmp_path: Path) -> None:
    before = locale.setlocale(locale.LC_ALL)
    spec = template_geometry_spec(TubeTemplateCreate(template="tube", params={
        "outer_d_mm": 20, "wall_t_mm": 2, "length_mm": 80}))
    build_artifacts(spec, tmp_path / "out")
    assert locale.setlocale(locale.LC_ALL) == before
