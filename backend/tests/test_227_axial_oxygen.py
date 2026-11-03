"""Axial O2 owner: Garcia-Gordon saturation, Eq. (6) limit, conservation, refusals."""

import math

import pytest

from app.modules.photobioreactor import axial_oxygen as ox

U, R = 0.5, 0.003


def test_garcia_gordon_reference_values():
    assert ox.o2_saturation_umol_per_kg(20.0, 0.0) == pytest.approx(284.65, abs=0.01)
    assert ox.o2_saturation_umol_per_kg(20.0, 35.0) == pytest.approx(225.54, abs=0.01)


def test_saturation_monotonic_and_mol_m3_conversion():
    temps = [ox.o2_saturation_umol_per_kg(t, 0.0) for t in range(0, 41, 5)]
    assert all(a > b for a, b in zip(temps, temps[1:], strict=False))
    sals = [ox.o2_saturation_umol_per_kg(20.0, s) for s in range(0, 43, 7)]
    assert all(a > b for a, b in zip(sals, sals[1:], strict=False))
    assert ox.o2_saturation_mol_m3(20.0, 0.0, 998.2) == pytest.approx(284.6521582689174e-6 * 998.2)


def test_eq6_limit_is_exact_and_s21_2a_numbers():
    length = ox.max_run_length(c_in_mol_m3=0.1, c_out_mol_m3=0.34, u_l_m_s=U, r_o2_mol_m3_s=R)
    assert length == U * (0.34 - 0.1) / R
    fresh = ox.o2_saturation_mol_m3(20.0, 0.0, 998.2)
    sea = ox.o2_saturation_mol_m3(20.0, 35.0, 1024.8)
    lf = ox.max_run_length(c_in_mol_m3=fresh, c_out_mol_m3=3 * fresh, u_l_m_s=U, r_o2_mol_m3_s=R)
    ls = ox.max_run_length(c_in_mol_m3=sea, c_out_mol_m3=3 * sea, u_l_m_s=U, r_o2_mol_m3_s=R)
    assert lf == pytest.approx(94.7, abs=0.1)
    assert ls == pytest.approx(77.0, abs=0.1)
    assert ls < 80.0 < lf


def test_wall_transfer_lengthens_and_roundtrips():
    c_sat = 0.25
    kw = {"c_in_mol_m3": c_sat, "u_l_m_s": U, "r_o2_mol_m3_s": R}
    l0 = ox.max_run_length(c_out_mol_m3=3 * c_sat, **kw)
    lk = ox.max_run_length(c_out_mol_m3=3 * c_sat, k_la_wall_s=1e-3, c_sat_mol_m3=c_sat, **kw)
    assert lk > l0
    out = ox.outlet_concentration(length_m=lk, k_la_wall_s=1e-3, c_sat_mol_m3=c_sat, **kw)
    assert out == pytest.approx(3 * c_sat, rel=1e-12)
    # tiny kLa approaches the Eq. (6) limit
    tiny = ox.max_run_length(c_out_mol_m3=3 * c_sat, k_la_wall_s=1e-12, c_sat_mol_m3=c_sat, **kw)
    assert tiny == pytest.approx(l0, rel=1e-6)


@pytest.mark.parametrize("k", [0.0, 2e-4, 2e-3])
def test_marched_profile_conserves_and_matches_closed_form(k):
    c_sat, c_in, length = 0.25, 0.25, 60.0
    m = ox.march_profile(
        c_in_mol_m3=c_in,
        length_m=length,
        u_l_m_s=U,
        r_o2_mol_m3_s=R,
        k_la_wall_s=k,
        c_sat_mol_m3=c_sat,
        steps=400,
    )
    closed = ox.outlet_concentration(
        c_in_mol_m3=c_in, length_m=length, u_l_m_s=U, r_o2_mol_m3_s=R, k_la_wall_s=k, c_sat_mol_m3=c_sat
    )
    assert m.c_out_mol_m3 == pytest.approx(closed, rel=1e-9)
    # U_L (c_out - c_in) = integral(R dx) - transfer, per unit cross-section
    assert U * (m.c_out_mol_m3 - c_in) == pytest.approx(m.production_mol_m2_s - m.transfer_mol_m2_s_per_m2, rel=1e-9)
    if k == 0.0:
        assert m.transfer_mol_m2_s_per_m2 == 0.0


def test_profile_monotonic_in_x():
    vals = [
        ox.concentration_at(x, c_in_mol_m3=0.25, u_l_m_s=U, r_o2_mol_m3_s=R, k_la_wall_s=1e-3, c_sat_mol_m3=0.25)
        for x in range(0, 100, 10)
    ]
    assert all(b > a for a, b in zip(vals, vals[1:], strict=False))


def test_refusals_carry_reasons():
    base = {"c_in_mol_m3": 0.2, "c_out_mol_m3": 0.5, "u_l_m_s": U, "r_o2_mol_m3_s": R}
    with pytest.raises(ox.AxialOxygenRefused) as e:
        ox.max_run_length(**{**base, "u_l_m_s": 0.0, "r_o2_mol_m3_s": -1.0, "c_out_mol_m3": 0.1})
    assert len(e.value.reasons) == 3
    with pytest.raises(ox.AxialOxygenRefused):
        ox.max_run_length(**{**base, "c_out_mol_m3": math.nan})
    with pytest.raises(ox.AxialOxygenRefused):
        ox.max_run_length(**base, k_la_wall_s=1e-3)  # c_sat missing
    with pytest.raises(ox.AxialOxygenRefused):
        ox.max_run_length(**base, k_la_wall_s=10.0, c_sat_mol_m3=0.2)  # unreachable outlet
    for t, s in ((-1.0, 0.0), (41.0, 0.0), (20.0, -1.0), (20.0, 43.0), (math.inf, 0.0)):
        with pytest.raises(ox.AxialOxygenRefused):
            ox.o2_saturation_umol_per_kg(t, s)
    with pytest.raises(ox.AxialOxygenRefused):
        ox.o2_saturation_mol_m3(20.0, 0.0, 0.0)
    with pytest.raises(ox.AxialOxygenRefused):
        ox.march_profile(c_in_mol_m3=0.2, length_m=0.0, u_l_m_s=U, r_o2_mol_m3_s=R)
