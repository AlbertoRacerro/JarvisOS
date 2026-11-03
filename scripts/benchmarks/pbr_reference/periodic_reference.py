"""Independent periodic-steady reference for the spec 170 Tier-1 PBR.

Nothing here imports the production kernel (``pbr_unit``, ``cylinder``, ``pbr_core``):

- Ki₂ is tabulated from its defining elevation integral with ``scipy.integrate.quad`` and interpolated
  in log space; the area rule is an ungraded polar Gauss–Legendre grid with a cubic wall map.
- The day profile, CTMI and Monod factors are re-typed from the spec text (capability 4).
- The 24-hour map is integrated with SciPy ``Radau`` (not SUNDIALS CVODE) on the full (X, N, O₂)
  system, N seeded from the conserved Z = N + qX, and the root is found with ``scipy.optimize.brentq``.

It is a reference for X* at a few fixture points; its accuracy is limited by the coarser optics rule
(about 1e-5 relative in ⟨f⟩), so comparisons use a 1e-4 relative bound on X*.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from numpy.polynomial.legendre import leggauss
from scipy.integrate import quad, solve_ivp
from scipy.optimize import brentq


def _ki2_direct(argument: float) -> float:
    return quad(lambda phi: math.cos(phi) * math.exp(-argument / math.cos(phi)) if phi < math.pi / 2 else 0.0,
                0.0, math.pi / 2, epsabs=1e-15, epsrel=1e-12, limit=200)[0]


_KI2_A = np.concatenate([[0.0], np.geomspace(1e-6, 700.0, 1400)])
_KI2_LOG = np.log(np.array([_ki2_direct(a) for a in _KI2_A]) * np.exp(_KI2_A))


def ki2(argument: np.ndarray) -> np.ndarray:
    a = np.minimum(argument, 700.0)
    return np.where(argument < 700.0, np.exp(np.interp(a, _KI2_A, _KI2_LOG) - a), 0.0)


def _gauss(count: int, start: float, stop: float) -> tuple[np.ndarray, np.ndarray]:
    node, weight = leggauss(count)
    return start + (stop - start) * (node + 1.0) / 2.0, (stop - start) * weight / 2.0


class CylinderLight:
    """Ungraded polar rule over the unit disk (cubic wall map), beam along +y plus 3-D isotropic diffuse."""

    def __init__(self, radial: int = 96, angle: int = 192, direction: int = 128) -> None:
        u, wu = _gauss(radial, 0.0, 1.0)
        radius = 1.0 - u**3
        ring = 6.0 * radius * u**2 * wu  # 2r·|dr/du|·w
        phi, wphi = _gauss(angle, -math.pi / 2, math.pi / 2)
        self.weight = ring[:, None] * wphi[None, :] / math.pi
        x = radius[:, None] * np.cos(phi)[None, :]
        y = radius[:, None] * np.sin(phi)[None, :]
        self.beam_path = y + np.sqrt(np.maximum(0.0, 1.0 - x * x))
        theta, wtheta = _gauss(direction, 0.0, math.pi)
        self.diffuse_path = radius[:, None] * np.cos(theta)[None, :] + np.sqrt(
            np.maximum(0.0, 1.0 - (radius[:, None] * np.sin(theta)[None, :]) ** 2))
        self.diffuse_weight = wtheta / math.pi

    def monod_average(self, surface: float, tau: float, diffuse_fraction: float, saturation: float) -> float:
        if surface <= 0.0:
            return 0.0
        if tau <= 0.0:
            return surface / (saturation + surface)
        half = 0.5 * tau
        diffuse = np.sum(ki2(half * self.diffuse_path) * self.diffuse_weight[None, :], axis=1)
        light = surface * ((1.0 - diffuse_fraction) * np.exp(-half * self.beam_path)
                           + diffuse_fraction * diffuse[:, None])
        return float(np.sum(self.weight * light / (saturation + light)))


@dataclass(frozen=True)
class Case:
    mu_max_h: float
    k_i: float
    k_x: float
    k_n: float
    q: float
    y_o2: float
    t_min: float
    t_opt: float
    t_max: float
    k_d: float
    diameter: float
    peak_par: float
    photoperiod_h: float
    diffuse_fraction: float
    t_mean: float
    t_amp: float
    kla_h: float
    o_sat: float
    dilution_h: float
    x_in: float
    n_in: float
    o_in: float


def ctmi(t: float, case: Case) -> float:
    if not case.t_min < t < case.t_max:
        return 0.0
    lo, opt, hi = case.t_min, case.t_opt, case.t_max
    value = (t - hi) * (t - lo) ** 2 / ((opt - lo) * ((opt - lo) * (t - opt) - (opt - hi) * (opt + lo - 2.0 * t)))
    return max(0.0, value)


def surface_par(hour: float, case: Case) -> float:
    rise = 12.0 - case.photoperiod_h / 2.0
    if rise < hour < rise + case.photoperiod_h:
        return case.peak_par * math.sin(math.pi * (hour - rise) / case.photoperiod_h)
    return 0.0


def periodic_biomass(case: Case, light: CylinderLight | None = None, rtol: float = 1e-10) -> float:
    """Positive periodic X* (kg m⁻³) by brentq on the Radau 24-hour map of the full system."""
    light = light or CylinderLight()
    rise, fall = 12.0 - case.photoperiod_h / 2.0, 12.0 + case.photoperiod_h / 2.0

    def rhs(hour: float, y: np.ndarray) -> list[float]:
        x, n, o = y
        par = surface_par(hour, case)
        temperature = case.t_mean + case.t_amp * math.sin(2.0 * math.pi * (hour - 9.0) / 24.0)
        growth = 0.0
        if par > 0.0:
            tau = case.k_x * max(x, 0.0) * case.diameter
            available = max(n, 0.0)
            growth = (case.mu_max_h * light.monod_average(par, tau, case.diffuse_fraction, case.k_i)
                      * ctmi(temperature, case) * available / (case.k_n + available))
        r_x = (growth - case.k_d) * x
        return [r_x + case.dilution_h * (case.x_in - x),
                -case.q * r_x + case.dilution_h * (case.n_in - n),
                case.y_o2 * r_x - case.kla_h * (o - case.o_sat) + case.dilution_h * (case.o_in - o)]

    def day(x0: float) -> float:
        state = np.array([x0, case.n_in + case.q * (case.x_in - x0), case.o_sat])
        for start, stop in ((0.0, rise), (rise, fall), (fall, 24.0)):
            state = solve_ivp(rhs, (start, stop), state, method="Radau", rtol=rtol, atol=1e-15).y[:, -1]
        return float(state[0] - x0)

    upper = case.x_in + case.n_in / case.q
    lower = 0.0 if case.x_in > 0.0 else 1e-6
    return brentq(day, lower, upper, xtol=1e-13, rtol=1e-12)


def fixture_107(hrt_d: float, **changes: float) -> Case:
    """The spec 170 Monod version of the synthetic 107 fixture (labelled synthetic, not a species)."""
    values = dict(mu_max_h=0.0625, k_i=150.0, k_x=150.0, k_n=0.001, q=0.07, y_o2=1.4, t_min=278.15,
                  t_opt=298.15, t_max=308.15, k_d=0.004166666666666667, diameter=0.05, peak_par=1500.0,
                  photoperiod_h=14.0, diffuse_fraction=0.0, t_mean=295.15, t_amp=3.0, kla_h=5.0, o_sat=0.0075,
                  dilution_h=1.0 / (24.0 * hrt_d), x_in=0.0, n_in=0.05, o_in=0.0075)
    values.update(changes)
    return Case(**values)


if __name__ == "__main__":
    import json

    shared = CylinderLight()
    rows = []
    for hrt, extra in ((3.0, {}), (5.0, {}), (3.0, {"diffuse_fraction": 0.5}), (3.0, {"x_in": 0.1})):
        rows.append({"hrt_d": hrt, **extra, "x_star": periodic_biomass(fixture_107(hrt, **extra), shared)})
    print(json.dumps(rows, indent=2))
