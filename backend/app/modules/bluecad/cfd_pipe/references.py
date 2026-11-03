"""Analytic and correlation references for developed flow in a smooth circular pipe."""

from __future__ import annotations

import math


def darcy_laminar(re: float) -> float:
    """Hagen-Poiseuille Darcy friction factor, f = 64 / Re."""
    _positive(re)
    return 64.0 / re


def darcy_blasius(re: float) -> float:
    """Blasius smooth-pipe correlation, f = 0.316 Re^-1/4 (stated for ~4e3 < Re < 1e5)."""
    _positive(re)
    return 0.316 * re**-0.25


def darcy_colebrook_smooth(re: float, *, tol: float = 1e-12, max_iter: int = 200) -> float:
    """Colebrook with zero roughness: 1/sqrt(f) = -2 log10(2.51 / (Re sqrt(f)))."""
    _positive(re)
    x = 1.0 / math.sqrt(darcy_blasius(re))  # x = 1 / sqrt(f)
    for _ in range(max_iter):
        nxt = -2.0 * math.log10(2.51 * x / re)
        if abs(nxt - x) < tol * abs(x):
            return 1.0 / nxt**2
        x = nxt
    raise ArithmeticError("Colebrook iteration did not converge")


def pressure_gradient_pa_m(darcy: float, density_kg_m3: float, mean_velocity_m_s: float, diameter_m: float) -> float:
    """Darcy-Weisbach dP/L = f rho U^2 / (2 D)."""
    return darcy * density_kg_m3 * mean_velocity_m_s**2 / (2.0 * diameter_m)


def wall_shear_pa(darcy: float, density_kg_m3: float, mean_velocity_m_s: float) -> float:
    """tau_w = f rho U^2 / 8."""
    return darcy * density_kg_m3 * mean_velocity_m_s**2 / 8.0


def poiseuille_velocity(r_m: float, radius_m: float, mean_velocity_m_s: float) -> float:
    """Developed laminar profile u(r) = 2 U (1 - (r/R)^2)."""
    return 2.0 * mean_velocity_m_s * (1.0 - (r_m / radius_m) ** 2)


def friction_estimate(re: float) -> float:
    """Darcy factor used for pre-run estimates: laminar below 2000, Colebrook smooth above."""
    return darcy_laminar(re) if re <= 2000.0 else darcy_colebrook_smooth(re)


def _positive(re: float) -> None:
    if not math.isfinite(re) or re <= 0:
        raise ValueError("Reynolds number must be positive and finite")
