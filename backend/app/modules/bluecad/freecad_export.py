"""Fixed, side-effect-free FreeCAD FCStd exporter for JarvisOS."""

from __future__ import annotations

import importlib
import json
import os
import re
import sys

OBJECT_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")


def main() -> int:
    sys.path[0] = os.path.dirname(sys.executable)
    FreeCAD = importlib.import_module("FreeCAD")
    Import = importlib.import_module("Import")

    if len(sys.argv) not in (4, 5):
        return 2
    mode = sys.argv[1]
    fcstd_path = sys.argv[2]
    out_dir = sys.argv[3]
    if mode not in ("list", "export"):
        return 2

    document = FreeCAD.openDocument(fcstd_path)
    try:
        if mode == "list":
            objects = []
            for obj in document.Objects:
                shape = getattr(obj, "Shape", None)
                if shape is not None:
                    objects.append({
                        "name": getattr(obj, "Name", ""),
                        "label": getattr(obj, "Label", ""),
                        "type_id": getattr(obj, "TypeId", ""),
                    })
            print(json.dumps(objects, ensure_ascii=False))
            return 0

        if len(sys.argv) != 5:
            return 2
        object_name = sys.argv[4]
        if OBJECT_NAME_RE.fullmatch(object_name) is None:
            return 2
        obj = document.getObject(object_name)
        if obj is None or getattr(obj, "Shape", None) is None:
            return 3
        Import.export([obj], os.path.join(out_dir, "model.step"))
        return 0
    finally:
        FreeCAD.closeDocument(document.Name)


if __name__ == "__main__":
    raise SystemExit(main())
