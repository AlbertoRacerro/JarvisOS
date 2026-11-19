"""Carbonate chemistry, speciation, and pH solver (spec 322).

Deterministic carbonate equilibria in freshwater and seawater as a function of
temperature and salinity:
- Gas-liquid CO2 solubility K0(T, S) (Weiss 1974).
- Carbonic acid dissociation constants K1*(T, S) and K2*(T, S) (Millero et al. 2006).
- Ion product of water Kw*(T, S) (Millero 1995).
- Speciation and total alkalinity from pH.
- Equilibrium pH solver from DIC and total alkalinity via scipy.optimize.brentq.
"""

from __future__ import annotations

import math
from typing import Any

from scipy.optimize import brentq


def k0_solubility(temperature_k: float, salinity_g_kg: float = 0.0) -> float:
    """Henry's law CO2 solubility coefficient K0 in mol / (m³ * atm).

    Formulation: Weiss (1974), Marine Chemistry 2(3): 203-215.
    Converted from mol / (L * atm) to mol / (m³ * atm) via factor 1000.
    """
    if not (math.isfinite(temperature_k) and temperature_k > 0):
        raise ValueError("temperature_k must be finite and positive")
    if not (math.isfinite(salinity_g_kg) and salinity_g_kg >= 0):
        raise ValueError("salinity_g_kg must be finite and nonnegative")

    t_k = float(temperature_k)
    s = float(salinity_g_kg)
    t_100 = t_k / 100.0

    ln_k0 = (
        -58.0931
        + 90.5069 / t_100
        + 22.2940 * math.log(t_100)
        + s * (0.027766 - 0.025888 * t_100 + 0.0050578 * (t_100**2))
    )
    return math.exp(ln_k0) * 1000.0


def k1_dissociation(temperature_k: float, salinity_g_kg: float = 0.0) -> float:
    """First carbonic acid stoichiometric dissociation constant K1* in mol / L.

    Formulation: Millero et al. (2006), Marine Chemistry 100(1-2): 80-94.
    """
    if not (math.isfinite(temperature_k) and temperature_k > 0):
        raise ValueError("temperature_k must be finite and positive")
    if not (math.isfinite(salinity_g_kg) and salinity_g_kg >= 0):
        raise ValueError("salinity_g_kg must be finite and nonnegative")

    t_k = float(temperature_k)
    s = float(salinity_g_kg)
    s_sqrt = math.sqrt(s)

    pk1_0 = -126.34048 + 6320.813 / t_k + 19.568224 * math.log(t_k)
    a1 = 13.4191 * s_sqrt + 0.0331 * s - 5.33e-5 * (s**2)
    b1 = -530.123 * s_sqrt - 6.103 * s
    c1 = -2.06950 * s_sqrt

    pk1 = pk1_0 + a1 + b1 / t_k + c1 * math.log(t_k)
    return 10.0 ** (-pk1)


def k2_dissociation(temperature_k: float, salinity_g_kg: float = 0.0) -> float:
    """Second carbonic acid stoichiometric dissociation constant K2* in mol / L.

    Formulation: Millero et al. (2006), Marine Chemistry 100(1-2): 80-94.
    """
    if not (math.isfinite(temperature_k) and temperature_k > 0):
        raise ValueError("temperature_k must be finite and positive")
    if not (math.isfinite(salinity_g_kg) and salinity_g_kg >= 0):
        raise ValueError("salinity_g_kg must be finite and nonnegative")

    t_k = float(temperature_k)
    s = float(salinity_g_kg)
    s_sqrt = math.sqrt(s)

    pk2_0 = -90.18333 + 5143.692 / t_k + 14.613358 * math.log(t_k)
    a2 = 21.0894 * s_sqrt + 0.1248 * s - 3.687e-4 * (s**2)
    b2 = -772.483 * s_sqrt - 20.051 * s
    c2 = -3.3336 * s_sqrt

    pk2 = pk2_0 + a2 + b2 / t_k + c2 * math.log(t_k)
    return 10.0 ** (-pk2)


def kw_ion_product(temperature_k: float, salinity_g_kg: float = 0.0) -> float:
    """Apparent ion product of water Kw* in (mol / L)².

    Formulation: Millero (1995), Geochimica et Cosmochimica Acta 59(4): 661-677.
    """
    if not (math.isfinite(temperature_k) and temperature_k > 0):
        raise ValueError("temperature_k must be finite and positive")
    if not (math.isfinite(salinity_g_kg) and salinity_g_kg >= 0):
        raise ValueError("salinity_g_kg must be finite and nonnegative")

    t_k = float(temperature_k)
    s = float(salinity_g_kg)
    s_sqrt = math.sqrt(s)

    ln_kw = (
        148.9802
        - 13847.26 / t_k
        - 23.6521 * math.log(t_k)
        + (-5.977 + 118.67 / t_k + 1.0495 * math.log(t_k)) * s_sqrt
        - 0.01615 * s
    )
    return math.exp(ln_kw)


def co2_saturation_mol_m3(
    p_co2_atm: float, temperature_k: float, salinity_g_kg: float = 0.0
) -> float:
    """Return aqueous CO2 saturation concentration [CO2*]_sat in mol / m³."""
    if not (math.isfinite(p_co2_atm) and p_co2_atm >= 0):
        raise ValueError("p_co2_atm must be finite and nonnegative")
    return k0_solubility(temperature_k, salinity_g_kg) * float(p_co2_atm)


def speciation_fractions(
    ph: float, temperature_k: float, salinity_g_kg: float = 0.0
) -> tuple[float, float, float]:
    """Return dimensionless carbonate fractions (alpha_0, alpha_1, alpha_2).

    alpha_0 = [CO2*] / DIC
    alpha_1 = [HCO3-] / DIC
    alpha_2 = [CO3(2-)] / DIC
    satisfying alpha_0 + alpha_1 + alpha_2 == 1.
    """
    if not (math.isfinite(ph) and 0.0 <= ph <= 14.0):
        raise ValueError("ph must be finite and between 0 and 14")
    k1 = k1_dissociation(temperature_k, salinity_g_kg)
    k2 = k2_dissociation(temperature_k, salinity_g_kg)
    h_mol_l = 10.0 ** (-float(ph))
    denom = h_mol_l**2 + k1 * h_mol_l + k1 * k2
    a0 = (h_mol_l**2) / denom
    a1 = (k1 * h_mol_l) / denom
    a2 = (k1 * k2) / denom
    return a0, a1, a2


def alkalinity_from_ph(
    dic_mol_m3: float, ph: float, temperature_k: float, salinity_g_kg: float = 0.0
) -> float:
    """Calculate total carbonate + water alkalinity in eq / m³ from DIC and pH.

    Alkalinity = [HCO3-] + 2[CO3(2-)] + [OH-] - [H+]
    where [OH-] and [H+] are converted to mol / m³ (1000 * mol / L).
    """
    if not (math.isfinite(dic_mol_m3) and dic_mol_m3 >= 0):
        raise ValueError("dic_mol_m3 must be finite and nonnegative")
    if not (math.isfinite(ph) and 0.0 <= ph <= 14.0):
        raise ValueError("ph must be finite and between 0 and 14")

    _a0, a1, a2 = speciation_fractions(ph, temperature_k, salinity_g_kg)
    kw = kw_ion_product(temperature_k, salinity_g_kg)
    h_mol_l = 10.0 ** (-float(ph))
    h_m3 = 1000.0 * h_mol_l
    oh_m3 = 1000.0 * (kw / h_mol_l)

    return (a1 + 2.0 * a2) * float(dic_mol_m3) + (oh_m3 - h_m3)


def solve_ph(
    dic_mol_m3: float,
    alkalinity_eq_m3: float,
    temperature_k: float,
    salinity_g_kg: float = 0.0,
) -> dict[str, Any]:
    """Solve equilibrium pH and carbonate speciation for given DIC and alkalinity.

    Returns dict with keys:
      - ph: float
      - h_mol_m3: float ([H+] in mol / m³)
      - co2_aq_mol_m3: float ([CO2*] in mol / m³)
      - hco3_mol_m3: float ([HCO3-] in mol / m³)
      - co3_mol_m3: float ([CO3(2-)] in mol / m³)
      - alpha_0: float
      - alpha_1: float
      - alpha_2: float
      - dic_mol_m3: float
      - alkalinity_eq_m3: float
    """
    if not (math.isfinite(dic_mol_m3) and dic_mol_m3 >= 0):
        raise ValueError("dic_mol_m3 must be finite and nonnegative")
    if not math.isfinite(alkalinity_eq_m3):
        raise ValueError("alkalinity_eq_m3 must be finite")
    if not (math.isfinite(temperature_k) and temperature_k > 0):
        raise ValueError("temperature_k must be finite and positive")
    if not (math.isfinite(salinity_g_kg) and salinity_g_kg >= 0):
        raise ValueError("salinity_g_kg must be finite and nonnegative")

    dic = float(dic_mol_m3)
    target_alk = float(alkalinity_eq_m3)
    k1 = k1_dissociation(temperature_k, salinity_g_kg)
    k2 = k2_dissociation(temperature_k, salinity_g_kg)
    kw = kw_ion_product(temperature_k, salinity_g_kg)

    def residual(ph_val: float) -> float:
        h = 10.0 ** (-ph_val)
        denom = h**2 + k1 * h + k1 * k2
        a1 = (k1 * h) / denom
        a2 = (k1 * k2) / denom
        h_m3 = 1000.0 * h
        oh_m3 = 1000.0 * (kw / h)
        return (a1 + 2.0 * a2) * dic + (oh_m3 - h_m3) - target_alk

    low, high = 0.0, 14.0
    flow, fhigh = residual(low), residual(high)
    if flow * fhigh > 0:
        if flow > 0:
            low = -2.0
            flow = residual(low)
        if fhigh < 0:
            high = 16.0
            fhigh = residual(high)
        if flow * fhigh > 0:
            raise ValueError(
                f"Alkalinity {target_alk} cannot be bracketed in pH range [{low}, {high}]"
            )

    ph_root = float(brentq(residual, low, high, xtol=1e-12, rtol=1e-12))
    h_final = 10.0 ** (-ph_root)
    d = h_final**2 + k1 * h_final + k1 * k2
    a0 = (h_final**2) / d
    a1 = (k1 * h_final) / d
    a2 = (k1 * k2) / d

    return {
        "ph": ph_root,
        "h_mol_m3": 1000.0 * h_final,
        "co2_aq_mol_m3": a0 * dic,
        "hco3_mol_m3": a1 * dic,
        "co3_mol_m3": a2 * dic,
        "alpha_0": a0,
        "alpha_1": a1,
        "alpha_2": a2,
        "dic_mol_m3": dic,
        "alkalinity_eq_m3": target_alk,
    }
