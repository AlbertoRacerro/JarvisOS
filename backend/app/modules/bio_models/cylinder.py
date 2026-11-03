"""Cylindrical Beer–Lambert light kernel for the Tier-1 photobioreactor (spec 170, capability 3).

An infinite circular cylinder of radius R = D/2 with extinction κ = k_X·X is lit by a collimated beam
travelling along +y normal to the axis plus a full 3-D isotropic diffuse field (scalar-irradiance
convention: in the optically thin limit both amplitudes equal I₀). Work is done in units of R, so
κR = τ/2 with τ = κD.

- Beam: I_b(p) = (1 − f_d)·I₀·exp(−κ·s_b), s_b = y + √(R² − x²).
- Diffuse: I_d(p) = f_d·I₀·S₃(r), S₃(r) = (1/π)∫₀^π Ki₂(κ·s_θ) dθ, s_θ = r·cosθ + √(R² − r²·sin²θ);
  Ki₂(a) = ∫₀^{π/2} cosφ·exp(−a/cosφ) dφ (Bickley–Naylor).
- Response: ⟨f⟩ = (1/πR²)∬ f(I_b + I_d) dA — the response is averaged, never f of the mean.

Numerics are fixed under ``pbr_unit.v1`` (no adaptivity inside the ODE right-hand side):

- Ki₂ is evaluated from a C² cubic spline of ln(eᵃ·Ki₂(a)) over t = ln(1 + a), tabulated once from the
  smooth representation eᵃ·Ki₂(a) = ∫₀^{π/2} 2cos³β·exp(−a·tan²β)/√(1 + cos²β) dβ (64-point
  Gauss–Legendre on the Gaussian support). Ki₂ is set to 0 above a = 760, where it underflows.
- Area rule: polar (r, φ). The wall distance d = 1 − r follows d = q², q = (e^{σu} − 1)/(e^σ − 1),
  σ = ln(1 + √1000), with Gauss–Legendre in u. The square removes the √d wall behaviour of the exact ring
  average (grazing chords) and the exponential grading resolves boundary layers down to τ = 1000.
  The radial map is τ-independent, so every node is fixed for the model version.
- Beam and diffuse angles are split at their wall near-kinks (φ = 0 for the beam, θ = π/2 for the
  diffuse rays) and graded log-uniformly from the ring scale ε = √(1 − r²) to π/2, again with
  Gauss–Legendre. Mirror symmetry halves the beam work.
- At τ = 0 the analytic thin limit f(I₀) is used; area weights are normalised to sum to exactly 1 so the
  τ → 0⁺ limit is continuous.

Accuracy is recorded against an independent adaptive reference in
``scripts/benchmarks/pbr_reference/cylinder_reference.py``.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from functools import cache, lru_cache

import numpy as np
from numpy.polynomial.legendre import leggauss
from scipy.interpolate import CubicSpline

TAU_MAX = 1000.0
RADIAL_NODES = 56
BEAM_NODES_PER_SIDE = 16
DIFFUSE_NODES_PER_SIDE = 16
KI2_TABLE_INTERVALS = 4096
KI2_ARGUMENT_MAX = 760.0
_RADIAL_SIGMA = math.log1p(math.sqrt(TAU_MAX))

Response = Callable[[np.ndarray], np.ndarray]


class CylinderOpticsRangeError(ValueError):
    """The optical depth exceeds the supported Tier-1 domain 0 ≤ τ ≤ 1000."""


@cache
def _gauss_unit(n: int) -> tuple[np.ndarray, np.ndarray]:
    nodes, weights = leggauss(n)
    return (nodes + 1.0) / 2.0, weights / 2.0


def ki2_scaled_exact(argument: np.ndarray | float) -> np.ndarray:
    """eᵃ·Ki₂(a) from the smooth β representation; used to build the table and in tests."""
    a = np.atleast_1d(np.asarray(argument, dtype=float))
    if np.any(a < 0) or not np.all(np.isfinite(a)):
        raise ValueError("Ki2 argument must be finite and >= 0")
    upper = np.where(a > 0, np.minimum(math.pi / 2, 9.0 / np.sqrt(np.maximum(a, 1e-300))), math.pi / 2)
    nodes, weights = leggauss(64)
    beta = (nodes[None, :] + 1.0) * upper[:, None] / 2.0
    cosine = np.cos(beta)
    integrand = 2.0 * cosine**3 * np.exp(-a[:, None] * np.tan(beta) ** 2) / np.sqrt(1.0 + cosine * cosine)
    return np.sum(weights[None, :] * upper[:, None] / 2.0 * integrand, axis=1)


_KI2_T_MAX = math.log1p(KI2_ARGUMENT_MAX)
_KI2_STEP = _KI2_T_MAX / KI2_TABLE_INTERVALS
_KI2_KNOTS = np.linspace(0.0, _KI2_T_MAX, KI2_TABLE_INTERVALS + 1)
_KI2_COEFFICIENTS = CubicSpline(_KI2_KNOTS, np.log(ki2_scaled_exact(np.expm1(_KI2_KNOTS)))).c


def ki2(argument: np.ndarray | float) -> np.ndarray:
    """Production Bickley–Naylor Ki₂(a), a ≥ 0, from the fixed C² table."""
    a = np.asarray(argument, dtype=float)
    t = np.log1p(np.minimum(a, KI2_ARGUMENT_MAX))
    index = np.minimum((t / _KI2_STEP).astype(np.intp), KI2_TABLE_INTERVALS - 1)
    dt = t - _KI2_KNOTS[index]
    c = _KI2_COEFFICIENTS
    log_scaled = ((c[0, index] * dt + c[1, index]) * dt + c[2, index]) * dt + c[3, index]
    return np.where(a < KI2_ARGUMENT_MAX, np.exp(log_scaled - a), 0.0)


def _graded_half_angles(epsilon: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Offsets δ ∈ (0, π/2) per ring, log-graded from scale ε; returns (δ, weights) of shape (rings, n)."""
    u, wu = _gauss_unit(n)
    grading = np.log1p((math.pi / 2) / epsilon)[:, None]
    scale = np.expm1(grading)
    delta = (math.pi / 2) * np.expm1(grading * u[None, :]) / scale
    jacobian = (math.pi / 2) * grading * np.exp(grading * u[None, :]) / scale
    return delta, jacobian * wu[None, :]


def _diffuse_paths(radius: np.ndarray, epsilon: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    delta, weight = _graded_half_angles(epsilon, n)
    theta = np.concatenate([math.pi / 2 - delta, math.pi / 2 + delta], axis=1)
    weights = np.concatenate([weight, weight], axis=1) / math.pi
    r = radius[:, None]
    path = r * np.cos(theta) + np.sqrt(np.maximum(0.0, 1.0 - (r * np.sin(theta)) ** 2))
    return path, weights


@lru_cache(maxsize=1)
def _geometry() -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Fixed nodes: area weights (rings, beam angles), beam paths, diffuse paths, diffuse weights (units of R)."""
    u, wu = _gauss_unit(RADIAL_NODES)
    q = np.expm1(_RADIAL_SIGMA * u) / math.expm1(_RADIAL_SIGMA)
    dq = _RADIAL_SIGMA * np.exp(_RADIAL_SIGMA * u) / math.expm1(_RADIAL_SIGMA)
    wall_distance = q * q
    radius = 1.0 - wall_distance
    radial_weight = 2.0 * radius * (2.0 * q * dq) * wu
    radial_weight = radial_weight / radial_weight.sum()
    epsilon = np.sqrt(wall_distance * (2.0 - wall_distance))
    delta, beam_weight = _graded_half_angles(epsilon, BEAM_NODES_PER_SIDE)
    phi = np.concatenate([-delta, delta], axis=1)  # mirror half φ ∈ (−π/2, π/2); φ < 0 faces the beam
    phi_weight = np.concatenate([beam_weight, beam_weight], axis=1) / math.pi
    r = radius[:, None]
    beam_path = r * np.sin(phi) + np.sqrt(np.maximum(0.0, 1.0 - (r * np.cos(phi)) ** 2))
    diffuse_path, diffuse_weight = _diffuse_paths(radius, epsilon, DIFFUSE_NODES_PER_SIDE)
    area_weight = radial_weight[:, None] * phi_weight
    for array in (area_weight, beam_path, diffuse_path, diffuse_weight):
        array.setflags(write=False)
    return area_weight, beam_path, diffuse_path, diffuse_weight


def optical_depth(k_x: float, biomass: float, diameter: float) -> float:
    """τ = k_X·X·D, dimensionless (m² kg⁻¹ · kg m⁻³ · m)."""
    return float(k_x) * max(float(biomass), 0.0) * float(diameter)


def relative_field(tau: float, diffuse_fraction: float) -> tuple[np.ndarray, np.ndarray]:
    """Return (area weights, I/I₀) at the fixed nodes. τ is not range-checked here."""
    area_weight, beam_path, diffuse_path, diffuse_weight = _geometry()
    kappa_r = 0.5 * tau
    field = np.exp(-kappa_r * beam_path)
    field *= 1.0 - diffuse_fraction
    if diffuse_fraction > 0.0:
        field += diffuse_fraction * np.sum(ki2(kappa_r * diffuse_path) * diffuse_weight, axis=1)[:, None]
    return area_weight, field


def response_average_tau(response: Response, surface_irradiance: float, tau: float,
                         diffuse_fraction: float) -> float:
    """⟨f⟩ for a vectorised response f(I). No range check, so an integrator may pass transiently."""
    if surface_irradiance <= 0.0:
        return float(response(np.zeros(1))[0])
    if tau <= 0.0:
        return float(response(np.full(1, surface_irradiance))[0])
    area_weight, field = relative_field(tau, diffuse_fraction)
    return float(np.sum(area_weight * response(surface_irradiance * field)))


def check_range(tau: float) -> None:
    if not math.isfinite(tau) or tau < 0.0:
        raise CylinderOpticsRangeError(f"optical depth {tau!r} is not a finite non-negative number")
    if tau > TAU_MAX:
        raise CylinderOpticsRangeError(f"optical depth {tau:.6g} exceeds the Tier-1 limit {TAU_MAX:g}")


def cylinder_response_average(response: Response, surface_irradiance: float, k_x: float, biomass: float,
                              diameter: float, diffuse_fraction: float) -> float:
    """Range-checked ⟨f⟩ over the tube cross-section (the documented production kernel)."""
    values = (surface_irradiance, k_x, biomass, diameter, diffuse_fraction)
    if not all(math.isfinite(float(v)) for v in values):
        raise ValueError("cylinder light inputs must be finite")
    if surface_irradiance < 0 or k_x < 0 or biomass < 0 or diameter <= 0 or not 0 <= diffuse_fraction <= 1:
        raise ValueError("cylinder light needs I0 >= 0, k_X >= 0, X >= 0, D > 0 and 0 <= f_d <= 1")
    tau = optical_depth(k_x, biomass, diameter)
    check_range(tau)
    return response_average_tau(response, float(surface_irradiance), tau, float(diffuse_fraction))


def diffuse_field(radius: np.ndarray | float, tau: float) -> np.ndarray:
    """Production S₃(r) (relative diffuse scalar irradiance) at arbitrary radii r/R ∈ [0, 1]."""
    r = np.atleast_1d(np.asarray(radius, dtype=float))
    d = 1.0 - r
    epsilon = np.sqrt(np.maximum(d * (2.0 - d), 1e-300))
    path, weights = _diffuse_paths(r, epsilon, DIFFUSE_NODES_PER_SIDE)
    return np.sum(ki2(0.5 * tau * path) * weights, axis=1)


def beam_field(x: np.ndarray | float, y: np.ndarray | float, tau: float) -> np.ndarray:
    """Exact relative beam irradiance at (x/R, y/R); the beam travels along +y."""
    x_arr, y_arr = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    return np.exp(-0.5 * tau * (y_arr + np.sqrt(np.maximum(0.0, 1.0 - x_arr * x_arr))))
