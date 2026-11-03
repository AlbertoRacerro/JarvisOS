"""Airlift riser/downcomer circulation from the Chisti (1989) energy balance (pure, no I/O).

Source: Molina et al. 2001, J. Biotechnol. 92:113-131 (S21), section 2.2.

* Eq. (14) Zuber-Findlay riser holdup:  eps_r = U_G / (lam (U_G + U_L) + U_b)
  (S21 writes eps_r = beta / (lam + U_b/(U_G+U_L)), beta = U_G/(U_G+U_L); identical).
* Eq. (11) bottom-zone loss:  K_B = f L_eq / d_t with a smooth-pipe Darcy friction factor.
  The Blasius owner `darcy_blasius` of the 216 CFD pipe references is reused: it uses 0.316 Re^-1/4
  whereas S21 prints 0.3164 Re^-1/4, i.e. the reused law is 0.13 % lower (0.316/0.3164 = 0.99874).
  No second friction law is added.
* Eq. (10) with K_T neglected, eps_d = 0 (no bubble recirculation) and A_r = A_d:
      U_L = sqrt(c g eps_r h_r / K_B)
  Residual R3: Eq. (10) gives c = 2, while S21 Eqs. (12)-(13) as printed give c = 1. The caller must
  state `energy_balance_reading`; there is no default.

Out of scope: the kLa correlation (S21 Eqs. 15-16).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.modules.bluecad.cfd_pipe.references import darcy_blasius

READINGS: dict[str, float] = {"eq10_factor_2": 2.0, "eq13_as_printed": 1.0}
RE_RANGE = (2.5e3, 1e5)
LAMBDA_RANGE = (1.0, 1.3)
BLASIUS_COEFFICIENT_RATIO_TO_S21 = 0.316 / 0.3164
ASSUMPTIONS = (
    "waterlike broth: density and viscosity are those of the stated liquid",
    "eps_d = 0: complete gas disengagement, no bubble recirculation into the downcomer",
    "K_T (top-section loss) neglected; A_r = A_d",
    "Eq. (11) smooth-pipe turbulent friction via Blasius (0.316) from 216, not S21's 0.3164",
)


class AirliftRefused(ValueError):
    """Typed refusal carrying every reason the input or solution is outside the model contract."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = tuple(reasons)


@dataclass(frozen=True)
class AirliftResult:
    u_l_m_s: float
    eps_r: float
    reynolds: float
    darcy_f: float
    k_b: float
    h_r_m: float
    u_g_m_s: float
    energy_balance_reading: str
    energy_factor: float
    assumptions: tuple[str, ...] = ASSUMPTIONS


def _factor(reading: str, reasons: list[str]) -> float:
    if reading not in READINGS:
        reasons.append(f"energy_balance_reading must be one of {sorted(READINGS)}, got {reading!r}")
        return float("nan")
    return READINGS[reading]


def _pos(name: str, value: float, reasons: list[str]) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        reasons.append(f"{name} must be a finite number")
    elif value <= 0:
        reasons.append(f"{name} must be > 0")


def _validate(
    *,
    reading: str,
    lam: float,
    u_b: float,
    d_t: float,
    l_eq: float,
    rho: float,
    mu: float,
    g: float,
    u_g: float | None = None,
    h_r: float | None = None,
    eps_r: float | None = None,
) -> float:
    reasons: list[str] = []
    c = _factor(reading, reasons)
    for name, v in (
        ("u_b_m_s", u_b),
        ("d_t_m", d_t),
        ("l_eq_m", l_eq),
        ("rho_kg_m3", rho),
        ("mu_pa_s", mu),
        ("g_m_s2", g),
    ):
        _pos(name, v, reasons)
    for name, opt in (("u_g_m_s", u_g), ("h_r_m", h_r), ("eps_r", eps_r)):
        if opt is not None:
            _pos(name, opt, reasons)
    if not isinstance(lam, (int, float)) or not math.isfinite(lam):
        reasons.append("lam must be a finite number")
    elif not LAMBDA_RANGE[0] <= lam <= LAMBDA_RANGE[1]:
        reasons.append(f"lam {lam} outside S21 range {LAMBDA_RANGE}")
    if reasons:
        raise AirliftRefused(reasons)
    return c


def riser_holdup(u_g: float, u_l: float, lam: float, u_b: float) -> float:
    """S21 Eq. (14)."""
    return u_g / (lam * (u_g + u_l) + u_b)


def gas_velocity_for_holdup(eps_r: float, u_l: float, lam: float, u_b: float) -> float:
    """Invert Eq. (14) for U_G given a riser holdup (requires lam * eps_r < 1)."""
    reasons: list[str] = []
    for name, v in (("eps_r", eps_r), ("u_l_m_s", u_l), ("u_b_m_s", u_b)):
        _pos(name, v, reasons)
    if not reasons and (eps_r >= 1 or lam * eps_r >= 1):
        reasons.append("eps_r must satisfy 0 < eps_r and lam * eps_r < 1")
    if reasons:
        raise AirliftRefused(reasons)
    return eps_r * (lam * u_l + u_b) / (1.0 - lam * eps_r)


def _losses(u_l: float, d_t: float, l_eq: float, rho: float, mu: float) -> tuple[float, float, float]:
    re = rho * u_l * d_t / mu
    f = darcy_blasius(re)
    return re, f, f * l_eq / d_t


def _check_re(re: float) -> None:
    if not RE_RANGE[0] < re < RE_RANGE[1]:
        raise AirliftRefused([f"Reynolds number {re:.4g} outside S21 Eq. (11) range {RE_RANGE}"])


def _check_eps(eps_r: float) -> None:
    if not 0.0 < eps_r < 1.0:
        raise AirliftRefused([f"riser holdup {eps_r:.4g} outside (0, 1)"])


def _solve_u_l(balance) -> float:
    """Root of balance(U_L) = 0 where balance is positive at small U_L and decreasing."""
    lo, hi = 1e-9, 1.0
    while balance(hi) > 0:
        hi *= 2.0
        if hi > 1e6:
            raise AirliftRefused(["no circulation velocity bracket found"])
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if balance(mid) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < 1e-15 * hi:
            break
    return 0.5 * (lo + hi)


def solve_circulation(
    *,
    h_r_m: float,
    u_g_m_s: float,
    d_t_m: float,
    l_eq_m: float,
    lam: float,
    u_b_m_s: float,
    rho_kg_m3: float,
    mu_pa_s: float,
    g_m_s2: float,
    energy_balance_reading: str,
) -> AirliftResult:
    """Forward solve U_L from riser height and superficial gas velocity (holdup from Eq. 14)."""
    c = _validate(
        reading=energy_balance_reading,
        lam=lam,
        u_b=u_b_m_s,
        d_t=d_t_m,
        l_eq=l_eq_m,
        rho=rho_kg_m3,
        mu=mu_pa_s,
        g=g_m_s2,
        u_g=u_g_m_s,
        h_r=h_r_m,
    )

    def balance(u_l: float) -> float:
        _, _, k_b = _losses(u_l, d_t_m, l_eq_m, rho_kg_m3, mu_pa_s)
        eps = riser_holdup(u_g_m_s, u_l, lam, u_b_m_s)
        return c * g_m_s2 * eps * h_r_m - u_l * u_l * k_b

    u_l = _solve_u_l(balance)
    re, f, k_b = _losses(u_l, d_t_m, l_eq_m, rho_kg_m3, mu_pa_s)
    eps = riser_holdup(u_g_m_s, u_l, lam, u_b_m_s)
    _check_re(re)
    _check_eps(eps)
    return AirliftResult(u_l, eps, re, f, k_b, h_r_m, u_g_m_s, energy_balance_reading, c)


def solve_circulation_at_holdup(
    *,
    h_r_m: float,
    eps_r: float,
    d_t_m: float,
    l_eq_m: float,
    rho_kg_m3: float,
    mu_pa_s: float,
    g_m_s2: float,
    energy_balance_reading: str,
) -> AirliftResult:
    """Forward solve U_L with the riser holdup imposed directly (e.g. a measured value)."""
    c = _validate(
        reading=energy_balance_reading,
        lam=1.0,
        u_b=1.0,
        d_t=d_t_m,
        l_eq=l_eq_m,
        rho=rho_kg_m3,
        mu=mu_pa_s,
        g=g_m_s2,
        h_r=h_r_m,
        eps_r=eps_r,
    )
    _check_eps(eps_r)
    u_l = _solve_u_l(lambda u: c * g_m_s2 * eps_r * h_r_m - u * u * _losses(u, d_t_m, l_eq_m, rho_kg_m3, mu_pa_s)[2])
    re, f, k_b = _losses(u_l, d_t_m, l_eq_m, rho_kg_m3, mu_pa_s)
    _check_re(re)
    return AirliftResult(u_l, eps_r, re, f, k_b, h_r_m, float("nan"), energy_balance_reading, c)


def required_riser_height(
    *,
    u_l_m_s: float,
    u_g_m_s: float,
    d_t_m: float,
    l_eq_m: float,
    lam: float,
    u_b_m_s: float,
    rho_kg_m3: float,
    mu_pa_s: float,
    g_m_s2: float,
    energy_balance_reading: str,
) -> AirliftResult:
    """Inverse: h_r = U_L^2 K_B(U_L) / (c g eps_r(U_L)), closed form."""
    c = _validate(
        reading=energy_balance_reading,
        lam=lam,
        u_b=u_b_m_s,
        d_t=d_t_m,
        l_eq=l_eq_m,
        rho=rho_kg_m3,
        mu=mu_pa_s,
        g=g_m_s2,
        u_g=u_g_m_s,
    )
    _pos_reasons: list[str] = []
    _pos("u_l_m_s", u_l_m_s, _pos_reasons)
    if _pos_reasons:
        raise AirliftRefused(_pos_reasons)
    re, f, k_b = _losses(u_l_m_s, d_t_m, l_eq_m, rho_kg_m3, mu_pa_s)
    eps = riser_holdup(u_g_m_s, u_l_m_s, lam, u_b_m_s)
    _check_re(re)
    _check_eps(eps)
    h = u_l_m_s * u_l_m_s * k_b / (c * g_m_s2 * eps)
    return AirliftResult(u_l_m_s, eps, re, f, k_b, h, u_g_m_s, energy_balance_reading, c)
