"""Growth-model pieces shared by ``photobioreactor.day_night`` v2 (107) and ``jarvis.pbr_unit_t1`` (170).

Each function keeps the exact floating-point expression 107 v2 used, so v2 stays bitwise identical.
Time is in hours from midnight; irradiance in µmol m⁻² s⁻¹; temperature in K; concentrations in kg m⁻³.
"""
from __future__ import annotations

import math
from typing import TypeVar

Light = TypeVar("Light")


def synthetic_day_inputs(
    hour: float, photoperiod: float, peak_par: float, temperature_mean: float, temperature_amplitude: float
) -> tuple[float, float]:
    """Return the 107 day-profile surface PAR (half-sine) and temperature at one hour."""
    day_hour = hour % 24.0
    sunrise = 12.0 - photoperiod / 2.0
    par = (
        peak_par * math.sin(math.pi * (day_hour - sunrise) / photoperiod)
        if sunrise < day_hour < sunrise + photoperiod
        else 0.0
    )
    temperature = temperature_mean + temperature_amplitude * math.sin(2.0 * math.pi * (day_hour - 9.0) / 24.0)
    return par, temperature


def cardinal_temperature(t: float, t_min: float, t_opt: float, t_max: float) -> float:
    """Rosso cardinal temperature model with inflexion (CTMI)."""
    if not t_min < t < t_max:
        return 0.0
    numerator = (t - t_max) * (t - t_min) ** 2
    denominator = (t_opt - t_min) * ((t_opt - t_min) * (t - t_opt) - (t_opt - t_max) * (t_opt + t_min - 2.0 * t))
    return max(0.0, numerator / denominator)


def light_response(light: Light, saturation: float, inhibition: float) -> Light:
    """Local light factor I/(K + I + I²·(1/K_i)); inhibition = 0 gives Monod. Works on NumPy arrays."""
    return light / (saturation + light + light**2 * inhibition)  # type: ignore[operator]


def nitrogen_factor(nitrogen: float, half_saturation: float) -> float:
    """Monod nitrogen factor with N clipped at zero (only in the factor, as in 107)."""
    available = max(nitrogen, 0.0)
    return available / (half_saturation + available)
