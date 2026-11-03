"""Centrifugal-pump affinity rescaling, Blasius wall shear, Wichterle-Chisti impeller shear, and passage survival tests (spec 228)."""

from __future__ import annotations

import math

import pytest

from app.modules.bluecad.cfd_pipe.references import darcy_blasius, wall_shear_pa
from app.modules.photobioreactor import centrifugal_pump_shear as cps

S04_FLUID = {"rho_kg_m3": 1024.0, "eta_pa_s": 1.8e-3}
S04_TABLE4 = (
    # (Q m3/h, u_tube, Re_tube, tau_tube, u_pump, Re_pump, tau_pump)
    (2.0, 0.37, 9.2e3, 0.57, 0.69, 1.3e4, 1.82),
    (2.4, 0.45, 1.1e4, 0.79, 0.83, 1.5e4, 2.51),
    (2.8, 0.52, 1.3e4, 1.03, 0.97, 1.8e4, 3.29),
    (3.2, 0.60, 1.5e4, 1.30, 1.11, 2.0e4, 4.15),
    (3.6, 0.67, 1.7e4, 1.60, 1.24, 2.3e4, 5.10),
)


@pytest.mark.parametrize(("q", "u_t", "re_t", "tau_t", "u_p", "re_p", "tau_p"), S04_TABLE4)
def test_s04_table4_pipe_and_discharge_wall_shear_exact_to_printed_precision(q, u_t, re_t, tau_t, u_p, re_p, tau_p):
    tube = cps.pipe_wall_shear(q_m3_h=q, diameter_m=0.0436, length_m=20.0, **S04_FLUID)
    pump = cps.pipe_wall_shear(q_m3_h=q, diameter_m=0.032, length_m=1.0, **S04_FLUID)

    assert round(tube.velocity_m_s, 2) == pytest.approx(u_t)
    assert round(tube.reynolds, -2 if re_t < 1e4 else -3) == pytest.approx(re_t)
    assert round(tube.wall_shear_s04_pa, 2) == pytest.approx(tau_t)

    assert round(pump.velocity_m_s, 2) == pytest.approx(u_p)
    assert round(pump.reynolds, -3) == pytest.approx(re_p)
    assert round(pump.wall_shear_s04_pa, 2) == pytest.approx(tau_p)


def test_reuses_216_darcy_blasius_and_wall_shear_owner():
    res = cps.pipe_wall_shear(q_m3_h=2.0, diameter_m=0.0436, length_m=20.0, **S04_FLUID)
    assert res.darcy_f_216 == pytest.approx(darcy_blasius(res.reynolds), rel=1e-12)
    assert res.wall_shear_216_pa == pytest.approx(wall_shear_pa(res.darcy_f_216, 1024.0, res.velocity_m_s), rel=1e-12)
    assert res.wall_shear_216_pa / res.wall_shear_s04_pa == pytest.approx(cps.BLASIUS_COEFFICIENT_RATIO_TO_S04, rel=1e-12)
    assert cps.BLASIUS_COEFFICIENT_RATIO_TO_S04 == pytest.approx(0.316 / 0.3164, rel=1e-12)


def test_wichterle_chisti_impeller_tip_shear_s04_600rpm_and_scaling():
    imp_600 = cps.impeller_tip_shear(n_rev_s=10.0, impeller_diameter_m=0.095, **S04_FLUID)
    assert imp_600.rpm == pytest.approx(600.0)
    assert imp_600.reynolds_local == pytest.approx(51342.222, rel=1e-6)
    assert imp_600.tip_shear_pa == pytest.approx(25.6952, abs=1e-3)
    assert round(imp_600.tip_shear_pa) == 26

    # Scaling: tau ~ N^(3/2) * d_L * rho^(1/2) * eta^(1/2)
    imp_1200 = cps.impeller_tip_shear(n_rev_s=20.0, impeller_diameter_m=0.095, **S04_FLUID)
    assert imp_1200.tip_shear_pa / imp_600.tip_shear_pa == pytest.approx(2.0**1.5, rel=1e-12)


def test_affinity_law_cubic_and_quadratic_scaling():
    cat = cps.KR_32_95_2950_RPM
    p1 = cps.evaluate_affinity_point(cat, target_rpm=600.0, q_m3_h=2.0, rho_kg_m3=1024.0)
    p2 = cps.evaluate_affinity_point(cat, target_rpm=1200.0, q_m3_h=4.0, rho_kg_m3=1024.0)
    # Same q_ref = Q / r -> head scales as r^2 (x4), shaft power as r^3 (x8)
    assert p1.q_ref_m3_h == pytest.approx(p2.q_ref_m3_h, rel=1e-12)
    assert p2.head_m / p1.head_m == pytest.approx(4.0, rel=1e-12)
    assert p2.shaft_power_w / p1.shaft_power_w == pytest.approx(8.0, rel=1e-12)
    assert p1.head_m == pytest.approx(0.25469, abs=1e-4)


@pytest.mark.parametrize("rpm_ref", [2950, 1450])
@pytest.mark.parametrize("k_minor", [0.0, 21.6, 25.0])
def test_solve_operating_point_matches_system_head_and_monotonic_in_k(rpm_ref, k_minor):
    cat = cps.CATALOG_BY_RPM[rpm_ref]
    op = cps.solve_operating_point(
        cat,
        q_m3_h=2.0,
        tube_diameter_m=0.0436,
        tube_length_m=20.0,
        impeller_diameter_m=0.095,
        k_minor=k_minor,
        **S04_FLUID,
    )
    pt = cps.evaluate_affinity_point(cat, target_rpm=op.rpm, q_m3_h=2.0, rho_kg_m3=1024.0)
    assert pt.head_m == pytest.approx(op.system_head_m, rel=1e-9)
    assert op.system_pressure_drop_pa == pytest.approx(op.straight_pressure_drop_pa + op.minor_pressure_drop_pa, rel=1e-12)


def test_operating_point_600rpm_closure_around_k_21_6():
    kw = {
        "q_m3_h": 2.0,
        "tube_diameter_m": 0.0436,
        "tube_length_m": 20.0,
        "impeller_diameter_m": 0.095,
        **S04_FLUID,
    }
    op_2950 = cps.solve_operating_point(cps.KR_32_95_2950_RPM, k_minor=21.6, **kw)
    op_1450 = cps.solve_operating_point(cps.KR_32_95_1450_RPM, k_minor=21.6, **kw)
    assert op_2950.rpm == pytest.approx(601.56, abs=0.1)
    assert op_1450.rpm == pytest.approx(598.65, abs=0.1)

    op_lo = cps.solve_operating_point(cps.KR_32_95_1450_RPM, k_minor=0.0, **kw)
    op_hi = cps.solve_operating_point(cps.KR_32_95_2950_RPM, k_minor=25.0, **kw)
    assert op_lo.rpm == pytest.approx(487.33, abs=0.1)
    assert op_hi.rpm == pytest.approx(617.35, abs=0.1)


def test_passage_survival_s04_closures_and_limiting_cases():
    # S04 p. 60: Q = 2.0 m3/h, V = 0.040 m3 -> 50 h^-1 -> 360 passages in 7.2 h; at phi = 0.01, ~97 % inactivated
    s = cps.passage_survival(q_m3_h=2.0, volume_m3=0.040, exposure_hours=7.2, damage_fraction_per_passage=0.01)
    assert s.recirculation_rate_per_h == pytest.approx(50.0)
    assert s.passages == pytest.approx(360.0)
    assert s.surviving_fraction == pytest.approx(0.99**360, rel=1e-12)
    assert round(s.inactivated_fraction * 100) == 97

    # Zero exposure or zero phi -> 100 % survival; phi = 1 with t > 0 -> 0 % survival
    assert cps.passage_survival(q_m3_h=2.0, volume_m3=0.040, exposure_hours=0.0, damage_fraction_per_passage=1.0).surviving_fraction == 1.0
    assert cps.passage_survival(q_m3_h=2.0, volume_m3=0.040, exposure_hours=7.2, damage_fraction_per_passage=0.0).surviving_fraction == 1.0
    assert cps.passage_survival(q_m3_h=2.0, volume_m3=0.040, exposure_hours=7.2, damage_fraction_per_passage=1.0).surviving_fraction == 0.0


def test_typed_refusals():
    with pytest.raises(cps.PumpShearRefused, match="Reynolds number"):
        # Very low flow -> laminar Re < 2500
        cps.pipe_wall_shear(q_m3_h=0.05, diameter_m=0.0436, **S04_FLUID)
    with pytest.raises(cps.PumpShearRefused, match="diameter_m"):
        cps.pipe_wall_shear(q_m3_h=2.0, diameter_m=-0.04, **S04_FLUID)
    with pytest.raises(cps.PumpShearRefused, match="impeller local Reynolds"):
        cps.impeller_tip_shear(n_rev_s=1e-4, impeller_diameter_m=0.01, **S04_FLUID)
    with pytest.raises(cps.PumpShearRefused, match="affinity speed ratio"):
        cps.evaluate_affinity_point(cps.KR_32_95_2950_RPM, target_rpm=50.0, q_m3_h=0.5)
    with pytest.raises(cps.PumpShearRefused, match="brochure H-Q domain"):
        cps.evaluate_affinity_point(cps.KR_32_95_2950_RPM, target_rpm=600.0, q_m3_h=5.0)
    with pytest.raises(cps.PumpShearRefused, match="brochure-reachable head range"):
        cps.solve_operating_point(
            cps.KR_32_95_2950_RPM,
            q_m3_h=2.0,
            tube_diameter_m=0.0436,
            tube_length_m=20.0,
            impeller_diameter_m=0.095,
            k_minor=20000.0,
            **S04_FLUID,
        )
    with pytest.raises(cps.PumpShearRefused, match="damage_fraction_per_passage"):
        cps.passage_survival(q_m3_h=2.0, volume_m3=0.040, exposure_hours=1.0, damage_fraction_per_passage=1.2)
    with pytest.raises(cps.PumpShearRefused, match="finite"):
        cps.pipe_wall_shear(q_m3_h=math.nan, diameter_m=0.0436, **S04_FLUID)
