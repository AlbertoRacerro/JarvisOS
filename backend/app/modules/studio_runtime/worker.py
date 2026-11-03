"""Sandbox-side entry point. Never import this module in the backend process."""

import errno
import json
import resource
import sys
from pathlib import Path
from typing import Any

# Exit codes that name the limit a run hit; runtime._EXIT_LIMITS maps them back.
EXIT_MEMORY, EXIT_FILE, EXIT_SCRATCH = 3, 4, 5


def _geometry(shape: Any) -> dict[str, Any]:
    """Facts and fixed-name exports of the one build123d shape that build returned."""
    import build123d as bd

    if not isinstance(shape, bd.Shape):
        raise TypeError("build must return one build123d shape, for example a Part")
    box = shape.bounding_box()
    facts = {"valid": bool(shape.is_valid), "solids": len(shape.solids()), "volume": float(shape.volume),
             "bbox_min": list(box.min), "bbox_max": list(box.max)}
    exports = (bd.export_step(shape, "/work/artifacts/model.step"),
               bd.export_stl(shape, "/work/artifacts/model.stl"),
               bd.export_gltf(shape, "/work/artifacts/model.glb", binary=True))
    if not all(exports):
        raise RuntimeError("STEP, STL or GLB export failed")
    return facts


def main() -> None:
    request = json.loads(Path("/work/request.json").read_text(encoding="utf-8"))
    limits = request["limits"]
    for kind, value in (
        (resource.RLIMIT_CPU, limits["cpu_seconds"]),
        (resource.RLIMIT_AS, limits["memory_bytes"]),
        (resource.RLIMIT_NPROC, limits["processes"]),
        (resource.RLIMIT_FSIZE, limits["file_bytes"]),
    ):
        resource.setrlimit(kind, (value, value))
    namespace: dict[str, Any] = {"__name__": "__studio_user_code__"}
    exec(compile(request["source"], "<studio-source>", "exec"), namespace)  # noqa: S102 - inside bwrap only
    result = namespace["main"](request["inputs"])
    if request.get("profile") == "geometry":
        result = _geometry(result)
    Path("/work/result.json").write_text(json.dumps(result), encoding="utf-8")


if __name__ == "__main__":
    try:
        main()
    except MemoryError:
        print("MemoryError: the memory limit was reached", file=sys.stderr)
        sys.exit(EXIT_MEMORY)
    except OSError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit({errno.EFBIG: EXIT_FILE, errno.ENOSPC: EXIT_SCRATCH}.get(exc.errno or 0, 1))
    except Exception as exc:  # noqa: BLE001 - errors cross only as bounded stderr
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
