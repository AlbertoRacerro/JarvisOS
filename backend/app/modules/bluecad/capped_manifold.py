"""Deterministic fluid-open capped branch-manifold primitive."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.modules.bluecad.models import BluecadError, BuiltPart, PortFrame

PART_KIND = "capped_manifold"
PARAM_NAMES = frozenset(
    {
        "main_outer_d",
        "main_wall_t",
        "branch_count",
        "branch_outer_d",
        "branch_wall_t",
        "branch_gap",
        "end_gap",
        "branch_stub_length",
        "cap_thickness",
    }
)


@dataclass(frozen=True)
class CappedManifoldKernelGeometry:
    """One authoritative 073 construction shared by the builder and 074 preflight."""

    part: BuiltPart
    outer_shape: Any
    void_shape: Any


def build_capped_manifold(part: dict[str, Any]) -> BuiltPart:
    """Build one capped header with a common opening and open branch bores."""

    return build_capped_manifold_kernel(part).part


def build_capped_manifold_kernel(part: dict[str, Any]) -> CappedManifoldKernelGeometry:
    """Return the exact solid plus the outer/void shapes used to construct it."""

    p = part["params"]
    main_outer_d = float(p["main_outer_d"])
    main_wall_t = float(p["main_wall_t"])
    branch_count = int(p["branch_count"])
    branch_outer_d = float(p["branch_outer_d"])
    branch_wall_t = float(p["branch_wall_t"])
    branch_gap = float(p["branch_gap"])
    end_gap = float(p["end_gap"])
    branch_stub_length = float(p["branch_stub_length"])
    cap_thickness = float(p["cap_thickness"])

    branch_pitch = branch_outer_d + branch_gap
    header_length = branch_outer_d + 2.0 * end_gap + branch_pitch * (branch_count - 1)
    main_inner_d = main_outer_d - 2.0 * main_wall_t
    branch_inner_d = branch_outer_d - 2.0 * branch_wall_t
    branch_sweep_length = main_outer_d / 2.0 + branch_stub_length

    try:
        import build123d as bd
    except ImportError as exc:  # pragma: no cover - exercised only where dependency absent
        raise BluecadError("KERNEL_ERROR", {"message": "build123d is not installed"}) from exc

    header_outer = bd.extrude(
        bd.Plane.YZ * bd.Circle(radius=main_outer_d / 2.0),
        amount=header_length,
    )
    header_inner = bd.extrude(
        bd.Plane.YZ * bd.Circle(radius=main_inner_d / 2.0),
        amount=header_length,
    )
    cap = bd.Pos(header_length, 0.0, 0.0) * bd.extrude(
        bd.Plane.YZ * bd.Circle(radius=main_outer_d / 2.0),
        amount=cap_thickness,
    )
    outer_shape = header_outer + cap
    void_shape = header_inner

    ports: dict[str, PortFrame] = {
        "common": PortFrame(
            (0.0, 0.0, 0.0),
            (-1.0, 0.0, 0.0),
            main_outer_d,
            main_wall_t,
        )
    }
    for index in range(branch_count):
        x = end_gap + branch_outer_d / 2.0 + branch_pitch * index
        branch_outer = (
            bd.Pos(x, 0.0, 0.0)
            * bd.Rot(Z=90)
            * bd.extrude(
                bd.Plane.YZ * bd.Circle(radius=branch_outer_d / 2.0),
                amount=branch_sweep_length,
            )
        )
        branch_bore = (
            bd.Pos(x, 0.0, 0.0)
            * bd.Rot(Z=90)
            * bd.extrude(
                bd.Plane.YZ * bd.Circle(radius=branch_inner_d / 2.0),
                amount=branch_sweep_length,
            )
        )
        outer_shape = outer_shape + branch_outer
        void_shape = void_shape + branch_bore
        ports[f"branch_{index + 1}"] = PortFrame(
            (x, branch_sweep_length, 0.0),
            (0.0, 1.0, 0.0),
            branch_outer_d,
            branch_wall_t,
        )

    shape = outer_shape - void_shape
    main_radius = main_outer_d / 2.0
    branch_radius = branch_outer_d / 2.0
    radius = max(main_radius, branch_radius)
    volume = float(shape.volume)
    void_volume = float(void_shape.volume)
    if volume <= 0.0 or void_volume <= 0.0:
        raise BluecadError(
            "KERNEL_ERROR",
            {
                "part_id": part["part_id"],
                "message": "capped manifold solid and fluid cavity must have positive volume",
            },
        )
    built = BuiltPart(
        part_id=part["part_id"],
        kind=PART_KIND,
        volume_mm3=volume,
        bbox_mm=(
            (0.0, -main_radius, -radius),
            (header_length + cap_thickness, branch_sweep_length, radius),
        ),
        ports=ports,
        shape=shape,
    )
    return CappedManifoldKernelGeometry(
        part=built,
        outer_shape=outer_shape,
        void_shape=void_shape,
    )
