"""Centrifugal-pump affinity rescaling, Blasius wall shear, Wichterle-Chisti impeller shear, and passage survival (pure, no I/O).

Sources:
* S04: Michels et al. 2016, J. Appl. Phycol. 28:53-62, DOI 10.1007/s10811-015-0559-8, pp. 56, 59-60:
  - Pipe Reynolds number:  Re = rho * u * D / eta
  - Blasius Fanning friction factor (Durst et al. 1996):  C_f = 0.0791 * Re^(-1/4),  tau_w = 0.5 * C_f * rho * u^2.
    Reuses `darcy_blasius` (f = 0.316 * Re^-1/4, C_f = f / 4 = 0.0790 * Re^-1/4) from the 216 CFD pipe reference owner,
    and exposes both the 216 value and the S04-printed 0.0791 value (ratio 0.316 / 0.3164 = 0.99874).
  - Local shear stress adjacent to a rotating centrifugal-pump impeller (Chisti 2010; Wichterle et al. 1996):
        Re_L = N * d_L^2 * rho / eta,      tau_imp = 6.30 * eta * N * Re_L^0.5
    where N is rotational speed (s^-1) and d_L is local impeller diameter (m).
  - Fraction of shear-sensitive cells remaining viable after n passages through a high-shear zone of damage probability phi:
        N_r / N_0 = (1 - phi)^n,          n = (Q / V) * t_exposure
* S24: ARBO SealPro PG 1.1 chemical centrifugal pump brochure (KR-32-95 / 32-95-95 curves at 2950 and 1450 min^-1, pp. 4, 8):
  - Standard centrifugal-pump affinity laws for speed ratio r = N / N_ref at constant impeller diameter:
        Q = Q_ref * r,    H = H_ref * r^2,    P = P_ref * (rho / rho_ref) * r^3
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.modules.bluecad.cfd_pipe.references import darcy_blasius, pressure_gradient_pa_m, wall_shear_pa

RE_BLASIUS_RANGE = (2.5e3, 1.0e5)
RE_IMPELLER_MIN = 100.0
SPEED_RATIO_RANGE = (0.05, 2.0)
S04_FANNING_COEFF = 0.0791
BLASIUS_COEFFICIENT_RATIO_TO_S04 = 0.316 / (4.0 * S04_FANNING_COEFF)
WICHTERLE_CHISTI_COEFF = 6.30

ASSUMPTIONS = (
    "Newtonian apparent viscosity at the stated culture conditions (S04: rho = 1024 kg/m3, eta = 1.8e-3 Pa s)",
    "Smooth circular pipe turbulent Blasius regime (2.5e3 < Re < 1e5); laminar sublayer at wall (Durst et al. 1996)",
    "Wichterle et al. (1996) / Chisti (2010) boundary-layer shear correlation on a rotating impeller tip (tau = 6.30 eta N Re_L^0.5)",
    "Centrifugal pump affinity laws (Q ~ N, H ~ N^2, P ~ rho N^3) without low-Reynolds viscosity/efficiency correction",
)


class PumpShearRefused(ValueError):
    """Typed refusal carrying every reason the input or operating point is outside the model contract."""

    def __init__(self, reasons: list[str]) -> None:
        super().__init__("; ".join(reasons))
        self.reasons = tuple(reasons)


@dataclass(frozen=True)
class PipeShearResult:
    q_m3_h: float
    diameter_m: float
    velocity_m_s: float
    reynolds: float
    fanning_cf_s04: float
    darcy_f_216: float
    wall_shear_s04_pa: float
    wall_shear_216_pa: float
    straight_pressure_drop_s04_pa: float
    straight_pressure_drop_216_pa: float
    straight_head_loss_s04_m: float


@dataclass(frozen=True)
class ImpellerShearResult:
    n_rev_s: float
    rpm: float
    impeller_diameter_m: float
    reynolds_local: float
    tip_shear_pa: float


@dataclass(frozen=True)
class PumpCurveCatalog:
    model: str
    reference_rpm: float
    reference_density_kg_m3: float
    hq_points_m3h_m: tuple[tuple[float, float], ...]
    pq_points_m3h_kw: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class AffinityPointResult:
    reference_rpm: float
    target_rpm: float
    speed_ratio: float
    q_m3_h: float
    q_ref_m3_h: float
    head_m: float
    pressure_increase_pa: float
    shaft_power_w: float
    hydraulic_power_w: float
    implied_efficiency: float


@dataclass(frozen=True)
class OperatingPointResult:
    q_m3_h: float
    k_minor: float
    reference_rpm: float
    system_head_m: float
    system_pressure_drop_pa: float
    straight_pressure_drop_pa: float
    minor_pressure_drop_pa: float
    rpm: float
    n_rev_s: float
    speed_ratio: float
    q_ref_m3_h: float
    shaft_power_w: float
    hydraulic_power_w: float
    impeller_reynolds: float
    impeller_tip_shear_pa: float


@dataclass(frozen=True)
class PassageDamageResult:
    q_m3_h: float
    volume_m3: float
    exposure_hours: float
    recirculation_rate_per_h: float
    passages: float
    damage_fraction_per_passage: float
    surviving_fraction: float
    inactivated_fraction: float


# S24 brochure curves for ARBO SealPro KR-32-95 (labelled 32-95-95 on S24 pp. 4 and 8).
KR_32_95_2950_RPM = PumpCurveCatalog(
    model="ARBO SealPro KR-32-95 (32-95-95, 2-pole 50 Hz)",
    reference_rpm=2950.0,
    reference_density_kg_m3=1000.0,
    hq_points_m3h_m=(
        (1.0, 12.3),
        (3.0, 11.4),
        (6.6, 9.2),
        (10.0, 6.0),
        (12.5, 1.8),
    ),
    pq_points_m3h_kw=(
        (1.0, 0.18),
        (12.5, 0.42),
    ),
)

KR_32_95_1450_RPM = PumpCurveCatalog(
    model="ARBO SealPro KR-32-95 (32-95-95, 4-pole 50 Hz)",
    reference_rpm=1450.0,
    reference_density_kg_m3=1000.0,
    hq_points_m3h_m=(
        (1.0370, 2.6783),
        (2.6127, 2.5929),
        (4.1121, 1.8932),
        (6.0114, 0.8935),
        (7.0110, 0.2938),
    ),
    pq_points_m3h_kw=(
        (1.0057, 0.0403),
        (6.9882, 0.0700),
    ),
)

CATALOG_BY_RPM: dict[int, PumpCurveCatalog] = {
    2950: KR_32_95_2950_RPM,
    1450: KR_32_95_1450_RPM,
}


def _finite(name: str, value: float, reasons: list[str]) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
        reasons.append(f"{name} must be a finite number")
        return False
    return True


def _pos(name: str, value: float, reasons: list[str]) -> None:
    if _finite(name, value, reasons) and value <= 0.0:
        reasons.append(f"{name} must be > 0")


def _nonneg(name: str, value: float, reasons: list[str]) -> None:
    if _finite(name, value, reasons) and value < 0.0:
        reasons.append(f"{name} must be >= 0")


def pipe_wall_shear(
    *,
    q_m3_h: float,
    diameter_m: float,
    rho_kg_m3: float,
    eta_pa_s: float,
    length_m: float = 1.0,
    g_m_s2: float = 9.80665,
) -> PipeShearResult:
    """Evaluate turbulent smooth-pipe velocity, Re, wall shear, and straight-pipe pressure drop."""
    reasons: list[str] = []
    for name, val in (
        ("q_m3_h", q_m3_h),
        ("diameter_m", diameter_m),
        ("rho_kg_m3", rho_kg_m3),
        ("eta_pa_s", eta_pa_s),
        ("length_m", length_m),
        ("g_m_s2", g_m_s2),
    ):
        _pos(name, val, reasons)
    if reasons:
        raise PumpShearRefused(reasons)

    q_m3_s = q_m3_h / 3600.0
    area = math.pi * (diameter_m**2) / 4.0
    u = q_m3_s / area
    re = rho_kg_m3 * u * diameter_m / eta_pa_s
    if not RE_BLASIUS_RANGE[0] <= re <= RE_BLASIUS_RANGE[1]:
        raise PumpShearRefused([f"Reynolds number {re:.4g} outside Blasius validity range {RE_BLASIUS_RANGE}"])

    cf_s04 = S04_FANNING_COEFF * (re**-0.25)
    tau_s04 = 0.5 * cf_s04 * rho_kg_m3 * (u**2)
    dp_s04 = 4.0 * tau_s04 * (length_m / diameter_m)
    head_s04 = dp_s04 / (rho_kg_m3 * g_m_s2)

    f_216 = darcy_blasius(re)
    tau_216 = wall_shear_pa(f_216, rho_kg_m3, u)
    dp_216 = pressure_gradient_pa_m(f_216, rho_kg_m3, u, diameter_m) * length_m

    return PipeShearResult(
        q_m3_h=q_m3_h,
        diameter_m=diameter_m,
        velocity_m_s=u,
        reynolds=re,
        fanning_cf_s04=cf_s04,
        darcy_f_216=f_216,
        wall_shear_s04_pa=tau_s04,
        wall_shear_216_pa=tau_216,
        straight_pressure_drop_s04_pa=dp_s04,
        straight_pressure_drop_216_pa=dp_216,
        straight_head_loss_s04_m=head_s04,
    )


def impeller_tip_shear(
    *,
    n_rev_s: float,
    impeller_diameter_m: float,
    rho_kg_m3: float,
    eta_pa_s: float,
) -> ImpellerShearResult:
    """Wichterle et al. (1996) / Chisti (2010) local shear stress on a centrifugal pump impeller."""
    reasons: list[str] = []
    for name, val in (
        ("n_rev_s", n_rev_s),
        ("impeller_diameter_m", impeller_diameter_m),
        ("rho_kg_m3", rho_kg_m3),
        ("eta_pa_s", eta_pa_s),
    ):
        _pos(name, val, reasons)
    if reasons:
        raise PumpShearRefused(reasons)

    re_l = n_rev_s * (impeller_diameter_m**2) * rho_kg_m3 / eta_pa_s
    if re_l < RE_IMPELLER_MIN:
        raise PumpShearRefused([f"impeller local Reynolds number {re_l:.4g} below boundary-layer minimum {RE_IMPELLER_MIN}"])
    tau = WICHTERLE_CHISTI_COEFF * eta_pa_s * n_rev_s * math.sqrt(re_l)
    return ImpellerShearResult(
        n_rev_s=n_rev_s,
        rpm=n_rev_s * 60.0,
        impeller_diameter_m=impeller_diameter_m,
        reynolds_local=re_l,
        tip_shear_pa=tau,
    )


def _interp_curve(points: tuple[tuple[float, float], ...], q_ref: float, label: str) -> float:
    q_min, q_max = points[0][0], points[-1][0]
    if not q_min <= q_ref <= q_max:
        raise PumpShearRefused([f"reference flow {q_ref:.4g} m3/h outside brochure {label} domain [{q_min:.4g}, {q_max:.4g}] m3/h"])
    for (q0, y0), (q1, y1) in zip(points, points[1:], strict=False):
        if q0 <= q_ref <= q1:
            if q1 == q0:
                return y0
            return y0 + (q_ref - q0) / (q1 - q0) * (y1 - y0)
    return points[-1][1]


def evaluate_affinity_point(
    catalog: PumpCurveCatalog,
    *,
    target_rpm: float,
    q_m3_h: float,
    rho_kg_m3: float = 1024.0,
    g_m_s2: float = 9.80665,
) -> AffinityPointResult:
    """Evaluate pump head and shaft power at (target_rpm, q_m3_h) via affinity rescaling of a brochure catalog."""
    reasons: list[str] = []
    for name, val in (("target_rpm", target_rpm), ("q_m3_h", q_m3_h), ("rho_kg_m3", rho_kg_m3), ("g_m_s2", g_m_s2)):
        _pos(name, val, reasons)
    if reasons:
        raise PumpShearRefused(reasons)

    r = target_rpm / catalog.reference_rpm
    if not SPEED_RATIO_RANGE[0] <= r <= SPEED_RATIO_RANGE[1]:
        raise PumpShearRefused([f"affinity speed ratio {r:.4g} outside validity range {SPEED_RATIO_RANGE}"])

    q_ref = q_m3_h / r
    h_ref = _interp_curve(catalog.hq_points_m3h_m, q_ref, "H-Q")
    p_ref_kw = _interp_curve(catalog.pq_points_m3h_kw, q_ref, "P-Q")

    head_m = h_ref * (r**2)
    dp_pa = rho_kg_m3 * g_m_s2 * head_m
    shaft_w = p_ref_kw * 1000.0 * (rho_kg_m3 / catalog.reference_density_kg_m3) * (r**3)
    hyd_w = (q_m3_h / 3600.0) * dp_pa
    eff = hyd_w / shaft_w if shaft_w > 0 else 0.0

    return AffinityPointResult(
        reference_rpm=catalog.reference_rpm,
        target_rpm=target_rpm,
        speed_ratio=r,
        q_m3_h=q_m3_h,
        q_ref_m3_h=q_ref,
        head_m=head_m,
        pressure_increase_pa=dp_pa,
        shaft_power_w=shaft_w,
        hydraulic_power_w=hyd_w,
        implied_efficiency=eff,
    )


def solve_operating_point(
    catalog: PumpCurveCatalog,
    *,
    q_m3_h: float,
    tube_diameter_m: float,
    tube_length_m: float,
    impeller_diameter_m: float,
    rho_kg_m3: float,
    eta_pa_s: float,
    k_minor: float = 0.0,
    g_m_s2: float = 9.80665,
) -> OperatingPointResult:
    """Solve for the pump rotational speed N where pump head matches loop hydraulic head loss at flow Q."""
    reasons: list[str] = []
    for name, val in (
        ("q_m3_h", q_m3_h),
        ("tube_diameter_m", tube_diameter_m),
        ("tube_length_m", tube_length_m),
        ("impeller_diameter_m", impeller_diameter_m),
        ("rho_kg_m3", rho_kg_m3),
        ("eta_pa_s", eta_pa_s),
        ("g_m_s2", g_m_s2),
    ):
        _pos(name, val, reasons)
    _nonneg("k_minor", k_minor, reasons)
    if reasons:
        raise PumpShearRefused(reasons)

    pipe = pipe_wall_shear(
        q_m3_h=q_m3_h,
        diameter_m=tube_diameter_m,
        rho_kg_m3=rho_kg_m3,
        eta_pa_s=eta_pa_s,
        length_m=tube_length_m,
        g_m_s2=g_m_s2,
    )
    dp_minor = k_minor * 0.5 * rho_kg_m3 * (pipe.velocity_m_s**2)
    dp_sys = pipe.straight_pressure_drop_s04_pa + dp_minor
    h_sys = dp_sys / (rho_kg_m3 * g_m_s2)

    q_min = max(catalog.hq_points_m3h_m[0][0], catalog.pq_points_m3h_kw[0][0])
    q_max = min(catalog.hq_points_m3h_m[-1][0], catalog.pq_points_m3h_kw[-1][0])
    eps = 1.0 + 1e-14
    rpm_lo = max((q_m3_h * catalog.reference_rpm / q_max) * eps, catalog.reference_rpm * SPEED_RATIO_RANGE[0] * eps)
    rpm_hi = min((q_m3_h * catalog.reference_rpm / q_min) / eps, catalog.reference_rpm * SPEED_RATIO_RANGE[1] / eps)
    if rpm_lo > rpm_hi:
        raise PumpShearRefused(
            [f"no valid affinity speed ratio in {SPEED_RATIO_RANGE} covers Q={q_m3_h} m3/h on brochure domain [{q_min:.4g}, {q_max:.4g}] m3/h"]
        )

    h_lo = evaluate_affinity_point(catalog, target_rpm=rpm_lo, q_m3_h=q_m3_h, rho_kg_m3=rho_kg_m3, g_m_s2=g_m_s2).head_m
    h_hi = evaluate_affinity_point(catalog, target_rpm=rpm_hi, q_m3_h=q_m3_h, rho_kg_m3=rho_kg_m3, g_m_s2=g_m_s2).head_m
    if not h_lo <= h_sys <= h_hi:
        raise PumpShearRefused(
            [f"system head {h_sys:.4g} m at Q={q_m3_h} m3/h outside brochure-reachable head range [{h_lo:.4g}, {h_hi:.4g}] m"]
        )

    for _ in range(120):
        mid = 0.5 * (rpm_lo + rpm_hi)
        h_mid = evaluate_affinity_point(catalog, target_rpm=mid, q_m3_h=q_m3_h, rho_kg_m3=rho_kg_m3, g_m_s2=g_m_s2).head_m
        if h_mid < h_sys:
            rpm_lo = mid
        else:
            rpm_hi = mid
        if rpm_hi - rpm_lo <= 1e-12 * mid:
            break

    rpm = 0.5 * (rpm_lo + rpm_hi)
    pt = evaluate_affinity_point(catalog, target_rpm=rpm, q_m3_h=q_m3_h, rho_kg_m3=rho_kg_m3, g_m_s2=g_m_s2)
    imp = impeller_tip_shear(
        n_rev_s=rpm / 60.0,
        impeller_diameter_m=impeller_diameter_m,
        rho_kg_m3=rho_kg_m3,
        eta_pa_s=eta_pa_s,
    )
    return OperatingPointResult(
        q_m3_h=q_m3_h,
        k_minor=k_minor,
        reference_rpm=catalog.reference_rpm,
        system_head_m=h_sys,
        system_pressure_drop_pa=dp_sys,
        straight_pressure_drop_pa=pipe.straight_pressure_drop_s04_pa,
        minor_pressure_drop_pa=dp_minor,
        rpm=rpm,
        n_rev_s=imp.n_rev_s,
        speed_ratio=pt.speed_ratio,
        q_ref_m3_h=pt.q_ref_m3_h,
        shaft_power_w=pt.shaft_power_w,
        hydraulic_power_w=pt.hydraulic_power_w,
        impeller_reynolds=imp.reynolds_local,
        impeller_tip_shear_pa=imp.tip_shear_pa,
    )


def passage_survival(
    *,
    q_m3_h: float,
    volume_m3: float,
    exposure_hours: float,
    damage_fraction_per_passage: float,
) -> PassageDamageResult:
    """S04 p. 60 pump-passage survival equation: N_r / N_0 = (1 - phi)^n, n = (Q / V) * t."""
    reasons: list[str] = []
    _pos("q_m3_h", q_m3_h, reasons)
    _pos("volume_m3", volume_m3, reasons)
    _nonneg("exposure_hours", exposure_hours, reasons)
    if _finite("damage_fraction_per_passage", damage_fraction_per_passage, reasons) and not (
        0.0 <= damage_fraction_per_passage <= 1.0
    ):
        reasons.append("damage_fraction_per_passage must be in [0, 1]")
    if reasons:
        raise PumpShearRefused(reasons)

    rate_h = q_m3_h / volume_m3
    passages = rate_h * exposure_hours
    if damage_fraction_per_passage == 1.0:
        surviving = 1.0 if passages == 0.0 else 0.0
    else:
        surviving = (1.0 - damage_fraction_per_passage) ** passages
    return PassageDamageResult(
        q_m3_h=q_m3_h,
        volume_m3=volume_m3,
        exposure_hours=exposure_hours,
        recirculation_rate_per_h=rate_h,
        passages=passages,
        damage_fraction_per_passage=damage_fraction_per_passage,
        surviving_fraction=surviving,
        inactivated_fraction=1.0 - surviving,
    )
