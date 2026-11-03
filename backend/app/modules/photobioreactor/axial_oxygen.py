"""Axial dissolved-oxygen balance along a plug-flow tube (pure, no I/O).

Balance per unit cross-section, steady plug flow, uniform volumetric O2 production R_O2:

    U_L d[O2]/dx = R_O2 - kLa_wall * ([O2] - [O2*])

With kLa_wall = 0 (no wall/side transfer, the default) this is the S21 Eq. (6) limit
(Molina et al. 2001, J. Biotechnol. 92:113-131): L = U_L * ([O2]out - [O2]in) / R_O2.

Saturation uses the Garcia & Gordon (1992, Limnol. Oceanogr. 37:1307) Benson-Krause fit,
umol/kg, moist air at 1 atm total pressure. Validity: 0 <= t <= 40 degC, 0 <= S <= 42.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

T_RANGE_C = (0.0, 40.0)
S_RANGE = (0.0, 42.0)

_A = (5.80871, 3.20291, 4.17887, 5.10006, -9.86643e-2, 3.80369)
_B = (-7.01577e-3, -7.70028e-3, -1.13864e-2, -9.51519e-3)
_C0 = -2.75915e-7


class AxialOxygenRefused(ValueError):
    """Typed refusal carrying every reason the input is outside the model contract."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = tuple(reasons)


def _finite(name: str, value: float, reasons: list[str]) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        reasons.append(f"{name} must be a finite number")
        return False
    return True


def _positive(name: str, value: float, reasons: list[str]) -> None:
    if _finite(name, value, reasons) and value <= 0:
        reasons.append(f"{name} must be > 0")


def _refuse(reasons: list[str]) -> None:
    if reasons:
        raise AxialOxygenRefused(reasons)


def o2_saturation_umol_per_kg(temperature_c: float, salinity: float) -> float:
    """Garcia & Gordon (1992) O2 solubility at equilibrium with moist air at 1 atm, umol/kg."""
    reasons: list[str] = []
    if _finite("temperature_c", temperature_c, reasons) and not T_RANGE_C[0] <= temperature_c <= T_RANGE_C[1]:
        reasons.append(f"temperature_c {temperature_c} outside correlation range {T_RANGE_C}")
    if _finite("salinity", salinity, reasons) and not S_RANGE[0] <= salinity <= S_RANGE[1]:
        reasons.append(f"salinity {salinity} outside correlation range {S_RANGE}")
    _refuse(reasons)
    ts = math.log((298.15 - temperature_c) / (273.15 + temperature_c))
    ln_c = sum(a * ts**i for i, a in enumerate(_A))
    ln_c += salinity * sum(b * ts**i for i, b in enumerate(_B)) + _C0 * salinity**2
    return math.exp(ln_c)


def o2_saturation_mol_m3(temperature_c: float, salinity: float, liquid_density_kg_m3: float) -> float:
    """Saturation concentration in mol/m3; the liquid density is explicit (no default)."""
    reasons: list[str] = []
    _positive("liquid_density_kg_m3", liquid_density_kg_m3, reasons)
    _refuse(reasons)
    return o2_saturation_umol_per_kg(temperature_c, salinity) * 1e-6 * liquid_density_kg_m3


def _check_balance(u_l: float, r_o2: float, k_la_wall: float, c_sat: float | None, reasons: list[str]) -> None:
    _positive("u_l_m_s", u_l, reasons)
    _positive("r_o2_mol_m3_s", r_o2, reasons)
    if _finite("k_la_wall_s", k_la_wall, reasons) and k_la_wall < 0:
        reasons.append("k_la_wall_s must be >= 0")
    if c_sat is not None:
        _positive("c_sat_mol_m3", c_sat, reasons)


def _drive(r_o2: float, k_la_wall: float, c_sat: float) -> float:
    """Asymptote of the exponential profile: c_sat + R/kLa."""
    return c_sat + r_o2 / k_la_wall


def outlet_concentration(
    *,
    c_in_mol_m3: float,
    length_m: float,
    u_l_m_s: float,
    r_o2_mol_m3_s: float,
    k_la_wall_s: float = 0.0,
    c_sat_mol_m3: float | None = None,
) -> float:
    """Closed-form [O2] at x = length_m. c_sat_mol_m3 is required when k_la_wall_s > 0."""
    return concentration_at(
        length_m,
        c_in_mol_m3=c_in_mol_m3,
        u_l_m_s=u_l_m_s,
        r_o2_mol_m3_s=r_o2_mol_m3_s,
        k_la_wall_s=k_la_wall_s,
        c_sat_mol_m3=c_sat_mol_m3,
    )


def concentration_at(
    x_m: float,
    *,
    c_in_mol_m3: float,
    u_l_m_s: float,
    r_o2_mol_m3_s: float,
    k_la_wall_s: float = 0.0,
    c_sat_mol_m3: float | None = None,
) -> float:
    reasons: list[str] = []
    _check_balance(u_l_m_s, r_o2_mol_m3_s, k_la_wall_s, c_sat_mol_m3, reasons)
    _finite("c_in_mol_m3", c_in_mol_m3, reasons)
    if _finite("x_m", x_m, reasons) and x_m < 0:
        reasons.append("x_m must be >= 0")
    if k_la_wall_s > 0 and c_sat_mol_m3 is None:
        reasons.append("c_sat_mol_m3 is required when k_la_wall_s > 0")
    _refuse(reasons)
    if k_la_wall_s == 0:
        return c_in_mol_m3 + r_o2_mol_m3_s * x_m / u_l_m_s
    assert c_sat_mol_m3 is not None
    inf = _drive(r_o2_mol_m3_s, k_la_wall_s, c_sat_mol_m3)
    return inf + (c_in_mol_m3 - inf) * math.exp(-k_la_wall_s * x_m / u_l_m_s)


def max_run_length(
    *,
    c_in_mol_m3: float,
    c_out_mol_m3: float,
    u_l_m_s: float,
    r_o2_mol_m3_s: float,
    k_la_wall_s: float = 0.0,
    c_sat_mol_m3: float | None = None,
) -> float:
    """Length at which [O2] reaches c_out. With kLa_wall = 0 this is exactly S21 Eq. (6)."""
    reasons: list[str] = []
    _check_balance(u_l_m_s, r_o2_mol_m3_s, k_la_wall_s, c_sat_mol_m3, reasons)
    ok_in = _finite("c_in_mol_m3", c_in_mol_m3, reasons)
    ok_out = _finite("c_out_mol_m3", c_out_mol_m3, reasons)
    if ok_in and ok_out and c_out_mol_m3 <= c_in_mol_m3:
        reasons.append("c_out_mol_m3 must exceed c_in_mol_m3")
    if k_la_wall_s > 0 and c_sat_mol_m3 is None:
        reasons.append("c_sat_mol_m3 is required when k_la_wall_s > 0")
    _refuse(reasons)
    if k_la_wall_s == 0:
        return u_l_m_s * (c_out_mol_m3 - c_in_mol_m3) / r_o2_mol_m3_s
    assert c_sat_mol_m3 is not None
    inf = _drive(r_o2_mol_m3_s, k_la_wall_s, c_sat_mol_m3)
    if c_out_mol_m3 >= inf:
        raise AxialOxygenRefused(
            [f"c_out_mol_m3 {c_out_mol_m3} is not reachable: wall transfer caps the profile at {inf}"]
        )
    return -(u_l_m_s / k_la_wall_s) * math.log((inf - c_out_mol_m3) / (inf - c_in_mol_m3))


@dataclass(frozen=True)
class MarchResult:
    c_out_mol_m3: float
    transfer_mol_m2_s_per_m2: float  # integral of kLa_wall*(c - c_sat) dx, mol m-3 s-1 * m = mol m-2 s-1
    production_mol_m2_s: float  # integral of R_O2 dx
    steps: int


def march_profile(
    *,
    c_in_mol_m3: float,
    length_m: float,
    u_l_m_s: float,
    r_o2_mol_m3_s: float,
    k_la_wall_s: float = 0.0,
    c_sat_mol_m3: float | None = None,
    steps: int = 2000,
) -> MarchResult:
    """Fixed-step RK4 marching check, independent of the closed form.

    The state carries the accumulated wall transfer so that conservation
    U_L (c_out - c_in) = integral(R_O2 dx) - transfer can be tested per unit cross-section.
    """
    reasons: list[str] = []
    _check_balance(u_l_m_s, r_o2_mol_m3_s, k_la_wall_s, c_sat_mol_m3, reasons)
    _finite("c_in_mol_m3", c_in_mol_m3, reasons)
    if _finite("length_m", length_m, reasons) and length_m <= 0:
        reasons.append("length_m must be > 0")
    if not isinstance(steps, int) or steps < 1:
        reasons.append("steps must be an integer >= 1")
    if k_la_wall_s > 0 and c_sat_mol_m3 is None:
        reasons.append("c_sat_mol_m3 is required when k_la_wall_s > 0")
    _refuse(reasons)
    sat = c_sat_mol_m3 if c_sat_mol_m3 is not None else 0.0

    def rhs(c: float) -> tuple[float, float]:
        t = k_la_wall_s * (c - sat)
        return (r_o2_mol_m3_s - t) / u_l_m_s, t

    h = length_m / steps
    c = c_in_mol_m3
    transfer = 0.0
    for _ in range(steps):
        k1, t1 = rhs(c)
        k2, t2 = rhs(c + 0.5 * h * k1)
        k3, t3 = rhs(c + 0.5 * h * k2)
        k4, t4 = rhs(c + h * k3)
        c += h * (k1 + 2 * k2 + 2 * k3 + k4) / 6.0
        transfer += h * (t1 + 2 * t2 + 2 * t3 + t4) / 6.0
    return MarchResult(c, transfer, r_o2_mol_m3_s * length_m, steps)
