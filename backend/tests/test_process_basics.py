"""Regression tests for the generic process-basics helpers.

The expected numbers are the golden values of an earlier spreadsheet-derived calculation chain
(single loop, branched loop, culture stoichiometry, harvest balance), re-derived independently.
Hydraulics there used the smooth-pipe Blasius fit; here turbulent friction is Colebrook (Clamond),
so turbulent totals are compared with a tolerance and laminar totals (64/Re in both) exactly.
"""

from __future__ import annotations

import math

import pytest

from app.modules.engineering.process_basics import (
    PipeSection,
    carbon_to_gas_exchange_g_d,
    elemental_demand_g_d,
    gas_volume_l,
    harvest_balance,
    molar_gas_volume_l_mol,
    parallel_branch_loop,
    salt_dose_g_d,
    section_hydraulics,
    stock_volume_l_d,
)
from app.modules.process_stack._common import EvaluationRefusal

RHO = 1000.0
MU = 0.001
BEND_ARC = 0.1 * math.pi / 2.0  # 90 degree bend, 100 mm centreline radius


def _branched_loop(branch_count: int, branch_velocity: float):
    di_branch, di_common = 0.05, 0.08
    branch = PipeSection(length_m=10.0 + 2.0 + 2 * BEND_ARC, inner_diameter_m=di_branch, k_minor=2 * 0.3 + 0.1)
    supply = PipeSection(length_m=1.0, inner_diameter_m=di_common, k_minor=0.2 + 0.5)
    ret = PipeSection(length_m=1.5, inner_diameter_m=di_common, k_minor=0.5 + 0.2)
    return parallel_branch_loop(
        common_sections=(supply, ret),
        branch_sections=(branch,),
        branch_count=branch_count,
        branch_flow_m3_s=branch_velocity * math.pi * di_branch**2 / 4.0,
        density_kg_m3=RHO,
        dynamic_viscosity_pa_s=MU,
        pump_efficiency=0.7,
    )


def test_single_loop_reference_case() -> None:
    # 20 m x 30 mm loop, 0.25 m/s seawater-like fluid, lumped K = 8, pump efficiency 0.35.
    area = math.pi * 0.03**2 / 4.0
    result = parallel_branch_loop(
        common_sections=(),
        branch_sections=(PipeSection(length_m=20.0, inner_diameter_m=0.03, k_minor=8.0),),
        branch_count=1,
        branch_flow_m3_s=0.25 * area,
        density_kg_m3=1025.0,
        dynamic_viscosity_pa_s=1.1e-3,
        pump_efficiency=0.35,
    )
    assert result.branch_sections[0].reynolds_number == pytest.approx(6988.64, abs=0.01)
    # Blasius reference 995.21 Pa / 0.50248 W; Colebrook smooth differs by about 1 %.
    assert result.total_pressure_drop_pa == pytest.approx(995.21, rel=0.015)
    assert result.electric_power_w == pytest.approx(0.50248, rel=0.015)
    assert result.hydraulic_power_w == pytest.approx(result.total_pressure_drop_pa * 0.25 * area)


def test_branched_loop_laminar_golden_is_exact() -> None:
    result = _branched_loop(branch_count=2, branch_velocity=0.01)
    assert result.branch_sections[0].reynolds_number == pytest.approx(500.0)
    assert result.common_sections[0].reynolds_number == pytest.approx(625.0)
    assert result.total_flow_m3_s == pytest.approx(3.926990816987242e-05, rel=1e-12)
    assert result.branch_pressure_drop_pa == pytest.approx(1.6112123859659493, rel=1e-9)
    assert result.common_pressure_drop_pa == pytest.approx(0.14038085937500006, rel=1e-9)
    assert result.total_pressure_drop_pa == pytest.approx(1.7515932453409493, rel=1e-9)
    assert result.hydraulic_power_w == pytest.approx(6.87849058955079e-05, rel=1e-9)
    assert result.electric_power_w == pytest.approx(9.8264151279297e-05, rel=1e-9)


def test_branch_count_multiplies_flow_but_not_branch_pressure_drop() -> None:
    two = _branched_loop(branch_count=2, branch_velocity=0.01)
    six = _branched_loop(branch_count=6, branch_velocity=0.01)
    assert six.total_flow_m3_s == pytest.approx(3.0 * two.total_flow_m3_s)
    assert six.branch_pressure_drop_pa == pytest.approx(two.branch_pressure_drop_pa)
    assert six.common_pressure_drop_pa == pytest.approx(0.677490234375, rel=1e-9)
    assert six.total_pressure_drop_pa == pytest.approx(2.288702620340949, rel=1e-9)
    assert six.hydraulic_power_w == pytest.approx(0.00026963142518680633, rel=1e-9)
    assert six.total_pressure_drop_pa == pytest.approx(six.common_pressure_drop_pa + six.branch_pressure_drop_pa)


def test_branched_loop_turbulent_reference_case() -> None:
    result = _branched_loop(branch_count=2, branch_velocity=0.5)
    assert result.branch_sections[0].reynolds_number == pytest.approx(25000.0)
    assert result.common_sections[0].reynolds_number == pytest.approx(31250.0)
    assert result.common_sections[0].velocity_m_s == pytest.approx(0.390625)
    assert result.total_flow_m3_s == pytest.approx(0.001963495408493621, rel=1e-12)
    assert result.branch_sections[0].minor_pressure_drop_pa == pytest.approx(75.0 + 12.5)  # (2 x 0.3 + 0.1) q
    # Blasius reference 1025.68 Pa total, 862.13 Pa branch, 163.55 Pa common.
    assert result.total_pressure_drop_pa == pytest.approx(1025.68, rel=0.04)
    assert result.branch_pressure_drop_pa == pytest.approx(862.13, rel=0.04)
    assert result.common_pressure_drop_pa == pytest.approx(163.55, rel=0.04)


def test_transitional_regime_is_refused() -> None:
    section = PipeSection(length_m=5.0, inner_diameter_m=0.05)
    flow = 0.06 * math.pi * 0.05**2 / 4.0  # Re = 3000
    with pytest.raises(EvaluationRefusal):
        section_hydraulics(section, flow, RHO, MU)


def test_loop_input_validation() -> None:
    section = PipeSection(length_m=1.0, inner_diameter_m=0.05)
    kwargs = {"common_sections": (), "branch_sections": (section,), "density_kg_m3": RHO,
              "dynamic_viscosity_pa_s": MU}
    with pytest.raises(ValueError):
        parallel_branch_loop(branch_count=0, branch_flow_m3_s=1e-4, **kwargs)
    with pytest.raises(ValueError):
        parallel_branch_loop(branch_count=1, branch_flow_m3_s=0.0, **kwargs)
    with pytest.raises(ValueError):
        parallel_branch_loop(branch_count=1, branch_flow_m3_s=1e-4, pump_efficiency=1.5, **kwargs)


def test_culture_stoichiometry_golden() -> None:
    # 20 L culture at 0.15 g/L/d -> 3 g/d of dry biomass; N 6 %, P 0.8 %, C 50 %.
    demand = elemental_demand_g_d(3.0, {"N": 0.06, "P": 0.008, "C": 0.5})
    assert demand == pytest.approx({"N": 0.18, "P": 0.024, "C": 1.5})
    # Reference doses used rounded salt factors 6.07 / 4.46 / 6.99; molar masses agree within 0.1 %.
    assert salt_dose_g_d("sodium_nitrate", demand["N"]) == pytest.approx(1.0926, rel=1e-3)
    assert salt_dose_g_d("sodium_dihydrogen_phosphate_monohydrate", demand["P"]) == pytest.approx(0.10704, rel=2e-3)
    assert salt_dose_g_d("sodium_bicarbonate", demand["C"]) == pytest.approx(10.485, rel=1e-3)
    # 20 mgN/mL, 2 mgP/mL, 20 mgC/mL stocks -> 9, 12 and 75 mL/d.
    assert stock_volume_l_d(demand["N"], 20.0) * 1000.0 == pytest.approx(9.0)
    assert stock_volume_l_d(demand["P"], 2.0) * 1000.0 == pytest.approx(12.0)
    assert stock_volume_l_d(demand["C"], 20.0) * 1000.0 == pytest.approx(75.0)


def test_gas_exchange_and_molar_volume() -> None:
    co2, o2 = carbon_to_gas_exchange_g_d(1.5)
    assert co2 == pytest.approx(5.5, rel=2e-3)  # 44/12
    assert o2 == pytest.approx(4.0, rel=2e-3)  # 32/12 at PQ = 1
    assert carbon_to_gas_exchange_g_d(1.5, photosynthetic_quotient=1.2)[1] == pytest.approx(1.2 * o2)
    assert molar_gas_volume_l_mol(273.15) == pytest.approx(22.414, abs=2e-3)
    assert molar_gas_volume_l_mol(293.15) == pytest.approx(24.055, abs=2e-3)
    # The same oxygen occupies about 7 % more volume at 20 C than at STP.
    stp = gas_volume_l(o2, 31.998, 273.15)
    assert stp == pytest.approx(2.80175, rel=2e-3)
    assert gas_volume_l(o2, 31.998, 293.15) / stp == pytest.approx(293.15 / 273.15)


def test_harvest_balance_golden_applies_recovery_once() -> None:
    balance = harvest_balance(
        production_g_d=3.0, culture_concentration_g_l=0.8, recovery=0.9,
        concentrate_concentration_g_l=20.0, flux_l_m2_h=24.7, filtration_hours_per_day=24.0,
    )
    assert balance.bleed_l_d == pytest.approx(3.75)
    assert balance.side_stream_l_d == pytest.approx(4.166666666666666)
    assert balance.recovered_biomass_g_d == pytest.approx(3.0)
    assert balance.returned_biomass_g_d == pytest.approx(0.33333333333333, rel=1e-9)
    assert balance.concentrate_l_d == pytest.approx(0.15)
    assert balance.filter_area_m2 == pytest.approx(0.007028789923526765, rel=1e-12)
    assert balance.side_stream_biomass_g_d == pytest.approx(balance.recovered_biomass_g_d + balance.returned_biomass_g_d)
    # Perfect recovery: nothing returns and the side-stream equals the bleed.
    full = harvest_balance(
        production_g_d=3.0, culture_concentration_g_l=0.8, recovery=1.0,
        concentrate_concentration_g_l=20.0, flux_l_m2_h=24.7, filtration_hours_per_day=24.0,
    )
    assert full.returned_biomass_g_d == pytest.approx(0.0, abs=1e-12)
    assert full.side_stream_l_d == pytest.approx(full.bleed_l_d)


def test_harvest_balance_refuses_invalid_inputs() -> None:
    base = {"production_g_d": 3.0, "culture_concentration_g_l": 0.8, "recovery": 0.9,
            "concentrate_concentration_g_l": 20.0, "flux_l_m2_h": 24.7, "filtration_hours_per_day": 24.0}
    for override in ({"recovery": 0.0}, {"recovery": 1.1}, {"concentrate_concentration_g_l": 0.5},
                     {"filtration_hours_per_day": 25.0}):
        with pytest.raises(ValueError):
            harvest_balance(**{**base, **override})
