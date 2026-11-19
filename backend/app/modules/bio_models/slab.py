"""Flat-slab Beer-Lambert optical kernel."""

from __future__ import annotations

import math
from collections.abc import Callable
from functools import cache

import numpy as np
from numpy.polynomial.legendre import leggauss

Response = Callable[[np.ndarray], np.ndarray]


def _finite(name: str, value: float) -> float:
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _validate_albedo(name: str, value: float, *, front: bool = False) -> float:
    result = _finite(name, value)
    if front:
        if not 0.0 <= result < 1.0:
            raise ValueError("front_wall_albedo must be in [0, 1)")
    elif not 0.0 <= result <= 1.0:
        raise ValueError("back_wall_albedo must be in [0, 1]")
    return result


def optical_depth(extinction: float, biomass: float, depth_m: float) -> float:
    """Tau = k_X X L, for extinction, biomass and slab depth in the Tier-1 units."""
    k_x = _finite("extinction", extinction)
    biomass_value = _finite("biomass", biomass)
    depth = _finite("depth_m", depth_m)
    if k_x < 0.0 or biomass_value < 0.0 or depth <= 0.0:
        raise ValueError("slab optical depth needs k_X >= 0, biomass >= 0 and depth_m > 0")
    return k_x * biomass_value * depth


@cache
def _quadrature(n: int) -> tuple[np.ndarray, np.ndarray]:
    nodes, weights = leggauss(n)
    z = (nodes + 1.0) / 2.0
    return z, weights / 2.0


def slab_irradiance_profile(
    i0: float,
    tau: float,
    z_norm: np.ndarray | float,
    back_albedo: float = 0.0,
    front_albedo: float = 0.0,
) -> np.ndarray:
    """Relative flat-slab irradiance profile at normalized depths z in [0, 1]."""
    incident = _finite("i0", i0)
    optical_depth_value = _finite("tau", tau)
    back = _validate_albedo("back_wall_albedo", back_albedo)
    front = _validate_albedo("front_wall_albedo", front_albedo, front=True)
    if incident < 0.0 or optical_depth_value < 0.0:
        raise ValueError("slab irradiance needs i0 >= 0 and tau >= 0")
    z = np.asarray(z_norm, dtype=float)
    if not np.all(np.isfinite(z)) or np.any(z < 0.0) or np.any(z > 1.0):
        raise ValueError("z_norm must contain finite values in [0, 1]")
    denominator = 1.0 - front * back * math.exp(-2.0 * optical_depth_value)
    forward = np.exp(-optical_depth_value * z)
    reflected = back * np.exp(-optical_depth_value * (2.0 - z))
    relative = (forward + reflected) / denominator
    return incident * relative


def response_average_tau(
    response_fn: Response,
    i0: float,
    tau: float,
    back_albedo: float = 0.0,
    front_albedo: float = 0.0,
) -> float:
    """Average a local response over normalized slab depth with fixed Gauss-Legendre quadrature."""
    incident = _finite("i0", i0)
    optical_depth_value = _finite("tau", tau)
    back = _validate_albedo("back_wall_albedo", back_albedo)
    front = _validate_albedo("front_wall_albedo", front_albedo, front=True)
    if incident < 0.0 or optical_depth_value < 0.0:
        raise ValueError("slab response needs i0 >= 0 and tau >= 0")
    z, weights = _quadrature(64)
    irradiance = slab_irradiance_profile(incident, optical_depth_value, z, back, front)
    response = np.asarray(response_fn(np.asarray(irradiance, dtype=float)), dtype=float)
    if response.shape != irradiance.shape:
        raise ValueError("response_fn must preserve the slab quadrature shape")
    return float(np.sum(weights * response))


def wall_transmission_factor(
    elapsed_s: float,
    base_transmittance: float = 1.0,
    fouling_rate_1_s: float = 0.0,
    fouling_floor: float = 0.0,
) -> float:
    """Effective wall transmittance with exponential fouling loss."""
    elapsed = _finite("elapsed_s", elapsed_s)
    base = _finite("base_transmittance", base_transmittance)
    rate = _finite("fouling_rate_1_s", fouling_rate_1_s)
    floor = _finite("fouling_floor", fouling_floor)
    if elapsed < 0.0:
        raise ValueError("elapsed_s must be >= 0")
    if not 0.0 < base <= 1.0:
        raise ValueError("base_transmittance must be in (0, 1]")
    if rate < 0.0:
        raise ValueError("fouling_rate_1_s must be >= 0")
    if not 0.0 <= floor <= 1.0:
        raise ValueError("fouling_floor must be in [0, 1]")
    return float(base * (floor + (1.0 - floor) * math.exp(-rate * elapsed)))
