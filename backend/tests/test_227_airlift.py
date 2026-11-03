"""Airlift owner: Eq. (14) holdup, Eq. (11) Blasius loss, Eq. (10) balance, refusals."""

import pytest

from app.modules.bluecad.cfd_pipe.references import darcy_blasius
from app.modules.photobioreactor import airlift as al

BASE = {"d_t_m": 0.053, "l_eq_m": 80.0, "rho_kg_m3": 998.2, "mu_pa_s": 1.002e-3, "g_m_s2": 9.80665}
FLOW = {"lam": 1.1, "u_b_m_s": 0.4}


def test_holdup_eq14_and_inverse():
    eps = al.riser_holdup(0.031, 0.5, 1.1, 0.4)
    assert eps == pytest.approx(0.0315, abs=1e-4)
    assert al.gas_velocity_for_holdup(eps, 0.5, 1.1, 0.4) == pytest.approx(0.031, rel=1e-12)
    beta = 0.031 / 0.531
    assert eps == pytest.approx(beta / (1.1 + 0.4 / 0.531), rel=1e-12)


@pytest.mark.parametrize("reading", sorted(al.READINGS))
@pytest.mark.parametrize("u_g", [0.031, 0.157, 0.3])
def test_inverse_forward_roundtrip(reading, u_g):
    req = al.required_riser_height(u_l_m_s=0.5, u_g_m_s=u_g, energy_balance_reading=reading, **FLOW, **BASE)
    fwd = al.solve_circulation(h_r_m=req.h_r_m, u_g_m_s=u_g, energy_balance_reading=reading, **FLOW, **BASE)
    assert fwd.u_l_m_s == pytest.approx(0.5, rel=1e-9)
    assert fwd.eps_r == pytest.approx(req.eps_r, rel=1e-9)
    assert fwd.k_b == pytest.approx(req.k_b, rel=1e-9)


def test_coordinator_hand_checks():
    c2 = al.required_riser_height(u_l_m_s=0.5, u_g_m_s=0.031, energy_balance_reading="eq10_factor_2", **FLOW, **BASE)
    c1 = al.required_riser_height(u_l_m_s=0.5, u_g_m_s=0.031, energy_balance_reading="eq13_as_printed", **FLOW, **BASE)
    assert c2.h_r_m == pytest.approx(15.1, abs=0.1)
    assert c1.h_r_m == pytest.approx(30.3, abs=0.1)
    assert c1.h_r_m == pytest.approx(2 * c2.h_r_m, rel=1e-12)


def test_factor_two_vs_printed_ratio_at_fixed_holdup():
    kw = {"h_r_m": 4.0, "eps_r": 0.2, **BASE}
    u2 = al.solve_circulation_at_holdup(energy_balance_reading="eq10_factor_2", **kw).u_l_m_s
    u1 = al.solve_circulation_at_holdup(energy_balance_reading="eq13_as_printed", **kw).u_l_m_s
    # U_L^(7/4) is proportional to c with Blasius K_B, so the ratio is 2^(4/7) ~ 1.49
    assert u2 / u1 == pytest.approx(2 ** (4 / 7), rel=1e-9)


def test_reuses_darcy_blasius():
    r = al.solve_circulation(h_r_m=4.0, u_g_m_s=0.1, energy_balance_reading="eq10_factor_2", **FLOW, **BASE)
    assert r.darcy_f == darcy_blasius(r.reynolds)
    assert r.k_b == pytest.approx(r.darcy_f * 80.0 / 0.053)
    assert r.reynolds == pytest.approx(998.2 * r.u_l_m_s * 0.053 / 1.002e-3)
    assert al.BLASIUS_COEFFICIENT_RATIO_TO_S21 == pytest.approx(0.99873, abs=1e-5)
    assert any("eps_d = 0" in a for a in r.assumptions) and any("waterlike" in a for a in r.assumptions)


def test_reading_is_required_and_validated():
    with pytest.raises(TypeError):
        al.solve_circulation(h_r_m=4.0, u_g_m_s=0.1, **FLOW, **BASE)  # type: ignore[call-arg]
    with pytest.raises(al.AirliftRefused) as e:
        al.solve_circulation(h_r_m=4.0, u_g_m_s=0.1, energy_balance_reading="default", **FLOW, **BASE)
    assert "energy_balance_reading" in e.value.reasons[0]


def test_refusals():
    ok = {"h_r_m": 4.0, "u_g_m_s": 0.1, "energy_balance_reading": "eq10_factor_2", **BASE}
    with pytest.raises(al.AirliftRefused):
        al.solve_circulation(lam=0.9, u_b_m_s=0.4, **ok)
    with pytest.raises(al.AirliftRefused):
        al.solve_circulation(lam=1.4, u_b_m_s=0.4, **ok)
    with pytest.raises(al.AirliftRefused):
        al.solve_circulation(lam=1.1, u_b_m_s=0.0, **ok)
    with pytest.raises(al.AirliftRefused):
        al.solve_circulation(lam=1.1, u_b_m_s=0.4, **{**ok, "u_g_m_s": 0.0})
    with pytest.raises(al.AirliftRefused):
        al.solve_circulation(lam=1.1, u_b_m_s=0.4, **{**ok, "d_t_m": -0.05})
    # Re out of the Eq. (11) range: a very short riser gives Re < 2.5e3
    with pytest.raises(al.AirliftRefused, match="Reynolds"):
        al.solve_circulation(lam=1.1, u_b_m_s=0.4, **{**ok, "h_r_m": 0.01})
    with pytest.raises(al.AirliftRefused, match="Reynolds"):
        al.required_riser_height(u_l_m_s=3.0, u_g_m_s=0.1, energy_balance_reading="eq10_factor_2", **FLOW, **BASE)
    with pytest.raises(al.AirliftRefused):
        al.gas_velocity_for_holdup(0.95, 0.5, 1.1, 0.4)  # lam * eps >= 1


def test_monotonic_in_gas_and_height():
    kw = {"energy_balance_reading": "eq10_factor_2", **FLOW, **BASE}
    us = [al.solve_circulation(h_r_m=4.0, u_g_m_s=g, **kw).u_l_m_s for g in (0.05, 0.1, 0.2)]
    assert us[0] < us[1] < us[2]
    hs = [al.solve_circulation(h_r_m=h, u_g_m_s=0.1, **kw).u_l_m_s for h in (2.0, 4.0, 8.0)]
    assert hs[0] < hs[1] < hs[2]
