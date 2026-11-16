"""Sandbox-side entry point. Never import this module in the backend process."""

import errno
import json
import math
import re
import resource
import sys
from pathlib import Path
from typing import Any

# Exit codes that name the limit a run hit; runtime._EXIT_LIMITS maps them back.
EXIT_MEMORY, EXIT_FILE, EXIT_SCRATCH = 3, 4, 5

_PORT_NAME_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _validate_port(name: Any, port: Any) -> dict[str, Any]:
    if not isinstance(name, str) or not _PORT_NAME_RE.fullmatch(name):
        raise ValueError(f"port name must match '^[A-Za-z0-9_]+$', got {name!r}")
    if not isinstance(port, dict):
        raise TypeError(f"port {name!r} must be a dict, got {type(port).__name__}")

    interface = port.get("interface")
    if interface not in ("tube", "pad"):
        raise ValueError(f"port {name!r} interface must be 'tube' or 'pad', got {interface!r}")

    if interface == "tube":
        allowed_keys = {"origin", "direction", "interface", "outer_d", "wall_t"}
        unknown = set(port.keys()) - allowed_keys
        if unknown:
            raise ValueError(f"unknown keys in tube port {name!r}: {', '.join(sorted(str(k) for k in unknown))}")
    else:
        allowed_keys = {"origin", "direction", "interface", "pad_d"}
        unknown = set(port.keys()) - allowed_keys
        if unknown:
            raise ValueError(f"unknown keys in pad port {name!r}: {', '.join(sorted(str(k) for k in unknown))}")

    if "origin" not in port:
        raise ValueError(f"port {name!r} missing 'origin'")
    origin = port["origin"]
    if not isinstance(origin, (list, tuple)) or len(origin) != 3:
        raise ValueError(f"port {name!r} origin must be a sequence of 3 numbers")
    norm_origin: list[float] = []
    for coord in origin:
        if isinstance(coord, bool) or not isinstance(coord, (int, float)) or not math.isfinite(coord):
            raise ValueError(f"port {name!r} origin coordinates must be finite numbers and not bool, got {coord!r}")
        norm_origin.append(float(coord))

    if "direction" not in port:
        raise ValueError(f"port {name!r} missing 'direction'")
    direction = port["direction"]
    if not isinstance(direction, (list, tuple)) or len(direction) != 3:
        raise ValueError(f"port {name!r} direction must be a sequence of 3 numbers")
    norm_direction: list[float] = []
    for coord in direction:
        if isinstance(coord, bool) or not isinstance(coord, (int, float)) or not math.isfinite(coord):
            raise ValueError(f"port {name!r} direction coordinates must be finite numbers and not bool, got {coord!r}")
        norm_direction.append(float(coord))

    norm = math.sqrt(sum(c * c for c in norm_direction))
    if norm == 0.0:
        raise ValueError(f"port {name!r} direction must have non-zero norm")

    if interface == "tube":
        if "outer_d" not in port:
            raise ValueError(f"tube port {name!r} missing 'outer_d'")
        outer_d = port["outer_d"]
        if (
            isinstance(outer_d, bool)
            or not isinstance(outer_d, (int, float))
            or not math.isfinite(outer_d)
            or outer_d <= 0
        ):
            raise ValueError(f"tube port {name!r} outer_d must be a finite positive number and not bool, got {outer_d!r}")

        if "wall_t" not in port:
            raise ValueError(f"tube port {name!r} missing 'wall_t'")
        wall_t = port["wall_t"]
        if (
            isinstance(wall_t, bool)
            or not isinstance(wall_t, (int, float))
            or not math.isfinite(wall_t)
            or wall_t < 0
            or wall_t >= outer_d / 2
        ):
            raise ValueError(
                f"tube port {name!r} wall_t must be finite >= 0 and < outer_d / 2 ({outer_d / 2}), got {wall_t!r}"
            )

        return {
            "origin": norm_origin,
            "direction": norm_direction,
            "interface": "tube",
            "outer_d": float(outer_d),
            "wall_t": float(wall_t),
        }

    if "pad_d" not in port:
        raise ValueError(f"pad port {name!r} missing 'pad_d'")
    pad_d = port["pad_d"]
    if (
        isinstance(pad_d, bool)
        or not isinstance(pad_d, (int, float))
        or not math.isfinite(pad_d)
        or pad_d <= 0
    ):
        raise ValueError(f"pad port {name!r} pad_d must be a finite positive number and not bool, got {pad_d!r}")

    return {
        "origin": norm_origin,
        "direction": norm_direction,
        "interface": "pad",
        "pad_d": float(pad_d),
    }


def _geometry(result: Any, artifact_dir: str = "/work/artifacts") -> dict[str, Any]:
    """Facts and fixed-name exports of the build123d shape (or shape+ports dict) that build returned."""
    import build123d as bd

    shape: Any
    ports_provided = False
    norm_ports: dict[str, Any] = {}

    if isinstance(result, bd.Shape):
        shape = result
    elif isinstance(result, dict):
        allowed_keys = {"shape", "ports"}
        unknown = set(result.keys()) - allowed_keys
        if unknown:
            raise ValueError(f"unknown key(s) in result dict: {', '.join(sorted(str(k) for k in unknown))}")
        if "shape" not in result:
            raise ValueError("result dict must contain 'shape'")
        shape = result["shape"]
        if not isinstance(shape, bd.Shape):
            raise TypeError("result['shape'] must be a build123d Shape")
        if "ports" in result:
            ports_provided = True
            raw_ports = result["ports"]
            if not isinstance(raw_ports, dict):
                raise TypeError("ports must be a dict")
            for name, port in sorted(raw_ports.items()):
                norm_ports[name] = _validate_port(name, port)
    else:
        raise TypeError("build must return one build123d shape, for example a Part, or a dict with 'shape'")

    box = shape.bounding_box()
    facts: dict[str, Any] = {
        "valid": bool(shape.is_valid),
        "solids": len(shape.solids()),
        "volume": float(shape.volume),
        "bbox_min": list(box.min),
        "bbox_max": list(box.max),
    }
    if ports_provided:
        facts["ports"] = norm_ports

    artifacts_path = Path(artifact_dir)
    exports = (
        bd.export_step(shape, str(artifacts_path / "model.step")),
        bd.export_stl(shape, str(artifacts_path / "model.stl")),
        bd.export_gltf(shape, str(artifacts_path / "model.glb"), binary=True),
    )
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
