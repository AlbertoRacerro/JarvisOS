"""Deterministic BLUECAD primitive builders.

Stage 2 keeps build123d usage behind lazy imports so non-kernel schema tests can
run where the CAD dependency is absent. The analytic metadata mirrors the solids
and is used for deterministic manifests and validation tolerances.
"""

from __future__ import annotations

import math
from typing import Any

from app.modules.bluecad.capped_manifold import build_capped_manifold
from app.modules.bluecad.models import BluecadError, BuiltPart, PortFrame


def build_part(part: dict[str, Any]) -> BuiltPart:
    kind = part["kind"]
    if kind == "tube_run":
        return _build_tube_run(part)
    if kind == "bend":
        return _build_bend(part)
    if kind == "joint":
        return _build_socket_joint(part)
    if kind == "manifold":
        return _build_manifold(part)
    if kind == "capped_manifold":
        return build_capped_manifold(part)
    if kind == "float":
        return _build_float(part)
    if kind == "anchor_mount":
        return _build_anchor_mount(part)
    if kind == "harvest_module":
        return _build_harvest_module(part)
    raise BluecadError("SPEC_INVALID", {"part_id": part.get("part_id"), "kind": kind, "message": "unsupported part kind"})


def _annulus_area(outer_d: float, wall_t: float) -> float:
    inner_d = outer_d - 2.0 * wall_t
    return math.pi / 4.0 * (outer_d**2 - inner_d**2)


def _tube_shape(outer_d: float, inner_d: float, length: float) -> Any:
    try:
        import build123d as bd
    except ImportError as exc:  # pragma: no cover - exercised only where dependency absent
        raise BluecadError("KERNEL_ERROR", {"message": "build123d is not installed"}) from exc
    outer = bd.extrude(bd.Plane.YZ * bd.Circle(radius=outer_d / 2.0), amount=length)
    inner = bd.extrude(bd.Plane.YZ * bd.Circle(radius=inner_d / 2.0), amount=length)
    return outer - inner


def _bend_shape(outer_d: float, inner_d: float, bend_radius: float, angle_rad: float) -> Any:
    try:
        import build123d as bd
    except ImportError as exc:  # pragma: no cover - exercised only where dependency absent
        raise BluecadError("KERNEL_ERROR", {"message": "build123d is not installed"}) from exc
    path = bd.JernArc(start=(0.0, 0.0), tangent=(1.0, 0.0), radius=bend_radius, arc_size=math.degrees(angle_rad))
    outer = bd.sweep(bd.Plane.YZ * bd.Circle(radius=outer_d / 2.0), path=path)
    inner = bd.sweep(bd.Plane.YZ * bd.Circle(radius=inner_d / 2.0), path=path)
    return outer - inner


def _build_tube_run(part: dict[str, Any]) -> BuiltPart:
    p = part["params"]
    outer_d = float(p["outer_d"])
    wall_t = float(p["wall_t"])
    length = float(p["length"])
    volume = _annulus_area(outer_d, wall_t) * length
    radius = outer_d / 2.0
    shape = _tube_shape(outer_d, outer_d - 2.0 * wall_t, length)
    return BuiltPart(
        part_id=part["part_id"],
        kind="tube_run",
        volume_mm3=volume,
        bbox_mm=((0.0, -radius, -radius), (length, radius, radius)),
        ports={
            "port_a": PortFrame((0.0, 0.0, 0.0), (-1.0, 0.0, 0.0), outer_d, wall_t),
            "port_b": PortFrame((length, 0.0, 0.0), (1.0, 0.0, 0.0), outer_d, wall_t),
        },
        shape=shape,
    )


def _build_bend(part: dict[str, Any]) -> BuiltPart:
    p = part["params"]
    outer_d = float(p["outer_d"])
    wall_t = float(p["wall_t"])
    bend_radius = float(p["bend_radius"])
    angle = float(p["angle"])
    volume = _annulus_area(outer_d, wall_t) * bend_radius * angle
    radius = outer_d / 2.0
    shape = _bend_shape(outer_d, outer_d - 2.0 * wall_t, bend_radius, angle)
    end_x = bend_radius * math.sin(angle)
    end_y = bend_radius * (1.0 - math.cos(angle))
    end_dir = (math.cos(angle), math.sin(angle), 0.0)
    return BuiltPart(
        part_id=part["part_id"],
        kind="bend",
        volume_mm3=volume,
        bbox_mm=((min(0.0, end_x) - radius, min(0.0, end_y) - radius, -radius), (max(0.0, end_x) + radius, max(0.0, end_y) + radius, radius)),
        ports={
            "port_a": PortFrame((0.0, 0.0, 0.0), (-1.0, 0.0, 0.0), outer_d, wall_t),
            "port_b": PortFrame((end_x, end_y, 0.0), end_dir, outer_d, wall_t),
        },
        shape=shape,
    )


def _build_socket_joint(part: dict[str, Any]) -> BuiltPart:
    p = part["params"]
    mating_outer_d = float(p["outer_d"])
    wall_t = float(p["wall_t"])
    socket_len = float(p["socket_len"])
    sleeve_outer_d = mating_outer_d + 2.0 * wall_t
    volume = math.pi / 4.0 * (sleeve_outer_d**2 - mating_outer_d**2) * socket_len
    radius = sleeve_outer_d / 2.0
    shape = _tube_shape(sleeve_outer_d, mating_outer_d, socket_len)
    return BuiltPart(
        part_id=part["part_id"],
        kind="joint",
        volume_mm3=volume,
        bbox_mm=((0.0, -radius, -radius), (socket_len, radius, radius)),
        ports={
            "port_a": PortFrame((0.0, 0.0, 0.0), (-1.0, 0.0, 0.0), mating_outer_d, wall_t),
            "port_b": PortFrame((socket_len, 0.0, 0.0), (1.0, 0.0, 0.0), mating_outer_d, wall_t),
        },
        shape=shape,
    )


def _solid_cylinder_x(diameter: float, length: float) -> Any:
    try:
        import build123d as bd
    except ImportError as exc:  # pragma: no cover
        raise BluecadError("KERNEL_ERROR", {"message": "build123d is not installed"}) from exc
    return bd.extrude(bd.Plane.YZ * bd.Circle(radius=diameter / 2.0), amount=length)


def _box_shape(base_w: float, base_l: float, base_t: float) -> Any:
    try:
        import build123d as bd
    except ImportError as exc:  # pragma: no cover
        raise BluecadError("KERNEL_ERROR", {"message": "build123d is not installed"}) from exc
    return bd.Pos(base_l / 2.0, 0.0, base_t / 2.0) * bd.Box(base_l, base_w, base_t)


def _solid_cylinder_y(diameter: float, length: float) -> Any:
    try:
        import build123d as bd
    except ImportError as exc:  # pragma: no cover
        raise BluecadError("KERNEL_ERROR", {"message": "build123d is not installed"}) from exc
    return bd.extrude(bd.Plane.XZ * bd.Circle(radius=diameter / 2.0), amount=length)


def _build_manifold(part: dict[str, Any]) -> BuiltPart:
    p = part["params"]
    outer_d = float(p["outer_d_main"])
    wall_t = float(p["wall_t"])
    length = float(p["length"])
    n_out = int(p["n_out"])
    out_d = float(p["out_d"])
    out_wall_t = float(p["out_wall_t"])
    spacing = float(p["spacing"])
    header = _tube_shape(outer_d, outer_d - 2.0 * wall_t, length)
    branch_len = outer_d / 2.0
    shape = header
    try:
        import build123d as bd
    except ImportError as exc:  # pragma: no cover
        raise BluecadError("KERNEL_ERROR", {"message": "build123d is not installed"}) from exc
    start_x = length / 2.0 - spacing * (n_out - 1) / 2.0
    ports = {
        "in_a": PortFrame((0.0, 0.0, 0.0), (-1.0, 0.0, 0.0), outer_d, wall_t),
        "in_b": PortFrame((length, 0.0, 0.0), (1.0, 0.0, 0.0), outer_d, wall_t),
    }
    for index in range(n_out):
        x = start_x + spacing * index
        branch = bd.Pos(x, 0.0, 0.0) * bd.Rot(Z=90) * _tube_shape(out_d, out_d - 2.0 * out_wall_t, branch_len)
        shape = shape + branch
        ports[f"out_{index + 1}"] = PortFrame((x, outer_d / 2.0, 0.0), (0.0, 1.0, 0.0), out_d, out_wall_t)
    volume = _annulus_area(outer_d, wall_t) * length + n_out * _annulus_area(out_d, out_wall_t) * branch_len
    radius = max(outer_d, out_d) / 2.0
    return BuiltPart(part["part_id"], "manifold", volume, ((0.0, -radius, -radius), (length, outer_d / 2.0 + out_d / 2.0, radius)), ports, shape)


def _build_float(part: dict[str, Any]) -> BuiltPart:
    p = part["params"]
    outer_d = float(p["outer_d"])
    length = float(p["length"])
    n_mounts = int(p["n_mounts"])
    pad_d = float(p["pad_d"])
    shape = _solid_cylinder_x(outer_d, length)
    radius = outer_d / 2.0
    spacing = length / (n_mounts + 1)
    ports = {f"mount_{i + 1}": PortFrame((spacing * (i + 1), 0.0, radius), (0.0, 0.0, 1.0), interface="pad", pad_d=pad_d) for i in range(n_mounts)}
    volume = math.pi * radius**2 * length
    return BuiltPart(part["part_id"], "float", volume, ((0.0, -radius, -radius), (length, radius, radius)), ports, shape)


def _build_anchor_mount(part: dict[str, Any]) -> BuiltPart:
    p = part["params"]
    base_w = float(p["base_w"])
    base_l = float(p["base_l"])
    base_t = float(p["base_t"])
    eye_d = float(p["eye_d"])
    try:
        import build123d as bd
    except ImportError as exc:  # pragma: no cover
        raise BluecadError("KERNEL_ERROR", {"message": "build123d is not installed"}) from exc
    lug = bd.Pos(base_l / 2.0, -base_w / 2.0, base_t + eye_d / 2.0) * _solid_cylinder_y(eye_d, base_w)
    shape = _box_shape(base_w, base_l, base_t) + lug
    volume = base_w * base_l * base_t + math.pi * (eye_d / 2.0) ** 2 * base_w
    pad_d = min(base_w, base_l)
    return BuiltPart(part["part_id"], "anchor_mount", volume, ((0.0, -base_w / 2.0, 0.0), (base_l, base_w / 2.0, base_t + eye_d)), {"mount_a": PortFrame((base_l / 2.0, 0.0, 0.0), (0.0, 0.0, -1.0), interface="pad", pad_d=pad_d)}, shape)


def _build_harvest_module(part: dict[str, Any]) -> BuiltPart:
    p = part["params"]
    outer_d = float(p["outer_d"])
    height = float(p["height"])
    wall_t = float(p["wall_t"])
    port_d = float(p["port_d"])
    shape = _solid_cylinder_x(outer_d, height)
    radius = outer_d / 2.0
    volume = math.pi * radius**2 * height
    return BuiltPart(
        part["part_id"], "harvest_module", volume, ((0.0, -radius, -radius), (height, radius, radius)),
        {
            "in_a": PortFrame((0.0, 0.0, 0.0), (-1.0, 0.0, 0.0), port_d, wall_t),
            "out_a": PortFrame((height, 0.0, 0.0), (1.0, 0.0, 0.0), port_d, wall_t),
            "drain_a": PortFrame((height / 2.0, 0.0, -radius), (0.0, 0.0, -1.0), port_d, wall_t),
        }, shape)
