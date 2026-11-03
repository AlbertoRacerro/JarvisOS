"""Closed-form references for spec 024-C analytic FEM benchmarks."""

from __future__ import annotations

from app.modules.bluecad.fem_verification_common import (
    FemVerificationError,
    require_positive_finite,
)


def beam_tip_displacement(
    *,
    force_n: float,
    length_mm: float,
    width_mm: float,
    height_mm: float,
    elastic_modulus_mpa: float,
) -> float:
    """Return Euler-Bernoulli tip displacement for a rectangular cantilever."""

    require_positive_finite(
        {
            "force_n": force_n,
            "length_mm": length_mm,
            "width_mm": width_mm,
            "height_mm": height_mm,
            "elastic_modulus_mpa": elastic_modulus_mpa,
        }
    )
    second_moment_mm4 = width_mm * height_mm**3 / 12.0
    return force_n * length_mm**3 / (3.0 * elastic_modulus_mpa * second_moment_mm4)


def lame_open_end_bore_stresses(
    *,
    inner_radius_mm: float,
    outer_radius_mm: float,
    pressure_mpa: float,
) -> dict[str, float]:
    """Return Lamé bore stresses for an open-end thick cylinder."""

    require_positive_finite(
        {
            "inner_radius_mm": inner_radius_mm,
            "outer_radius_mm": outer_radius_mm,
            "pressure_mpa": pressure_mpa,
        }
    )
    if outer_radius_mm <= inner_radius_mm:
        raise FemVerificationError(
            "INVALID_CYLINDER_RADII",
            {
                "inner_radius_mm": inner_radius_mm,
                "outer_radius_mm": outer_radius_mm,
            },
        )
    denominator = outer_radius_mm**2 - inner_radius_mm**2
    hoop = pressure_mpa * (outer_radius_mm**2 + inner_radius_mm**2) / denominator
    return {
        "sigma_theta_mpa": hoop,
        "sigma_r_mpa": -pressure_mpa,
        "sigma_z_mpa": 0.0,
    }


def finite_width_hole_factor(*, diameter_mm: float, width_mm: float) -> float:
    """Return the Pilkey finite-width circular-hole factor on net stress."""

    require_positive_finite({"diameter_mm": diameter_mm, "width_mm": width_mm})
    ratio = diameter_mm / width_mm
    if not 0.0 < ratio < 1.0:
        raise FemVerificationError("INVALID_HOLE_RATIO", {"diameter_to_width": ratio})
    return 3.0 - 3.14 * ratio + 3.667 * ratio**2 - 1.527 * ratio**3


def finite_width_hole_reference(
    *,
    force_n: float,
    width_mm: float,
    diameter_mm: float,
    thickness_mm: float,
) -> dict[str, float]:
    """Return net/gross nominal stresses and the prescribed peak stress."""

    require_positive_finite(
        {
            "force_n": force_n,
            "width_mm": width_mm,
            "diameter_mm": diameter_mm,
            "thickness_mm": thickness_mm,
        }
    )
    factor = finite_width_hole_factor(diameter_mm=diameter_mm, width_mm=width_mm)
    net_nominal = force_n / ((width_mm - diameter_mm) * thickness_mm)
    gross_nominal = force_n / (width_mm * thickness_mm)
    return {
        "diameter_to_width": diameter_mm / width_mm,
        "kt_net_section": factor,
        "sigma_nominal_net_mpa": net_nominal,
        "sigma_nominal_gross_mpa": gross_nominal,
        "sigma_peak_mpa": factor * net_nominal,
    }
