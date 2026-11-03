"""Deterministic internal fluid domain of a parametric BlueCad smooth tube."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.bluecad.spec import SpecValidationError, canonicalize_geometry_spec

DOMAIN_VERSION: Literal["cfd_pipe_fluid_domain_v1"] = "cfd_pipe_fluid_domain_v1"


class FluidDomainError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class FluidDomain(BaseModel):
    """Straight circular fluid volume bounded by the tube bore.

    ``geometry_digest`` is the canonical GeometrySpec ``spec_id``; any change to
    the tube parameters changes it, which is what downstream obsolescence keys on.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    domain_version: Literal["cfd_pipe_fluid_domain_v1"] = DOMAIN_VERSION
    geometry_digest: str = Field(pattern=r"^[a-z0-9_]+:[0-9a-f]{64}$")
    source_part_id: str = Field(min_length=1)
    inner_diameter_m: float = Field(gt=0)
    length_m: float = Field(gt=0)
    patches: tuple[str, ...] = ("inlet", "outlet", "wall")


def derive_fluid_domain(geometry_spec: Mapping[str, Any]) -> FluidDomain:
    """Derive the bore of exactly one ``tube_run`` part; refuse anything else.

    GeometrySpec lengths are millimetres. The bore diameter is ``outer_d - 2 wall_t``.
    """

    try:
        canonical = canonicalize_geometry_spec(geometry_spec)
    except SpecValidationError as exc:
        raise FluidDomainError("geometry_invalid", "GeometrySpec is not valid.") from exc
    parts = canonical["parts"]
    if len(parts) != 1 or canonical.get("connections"):
        raise FluidDomainError(
            "geometry_unsupported",
            "Only a single straight smooth tube has a derivable fluid domain in this slice.",
        )
    part = parts[0]
    if part["kind"] != "tube_run":
        raise FluidDomainError(
            "geometry_unsupported", f"Part kind {part['kind']!r} has no fluid-domain derivation in this slice."
        )
    params = part["params"]
    inner_mm = float(params["outer_d"]) - 2.0 * float(params["wall_t"])
    if inner_mm <= 0:
        raise FluidDomainError("geometry_invalid", "Tube wall leaves no bore.")
    return FluidDomain(
        geometry_digest=canonical["spec_id"],
        source_part_id=part["part_id"],
        inner_diameter_m=inner_mm / 1000.0,
        length_m=float(params["length"]) / 1000.0,
    )
