"""Tests for Spec 322: Dissolved inorganic carbon, carbonate pH, and CO2 mass transfer (CARBON-PH-STATE-1)."""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError

from app.modules.process_stack import carbonate, dynamic_engine
from app.modules.process_stack.dynamic_engine import DynamicError, Snapshot
from app.modules.process_stack.dynamic_models import Scenario
from tests.plumbing_170_support import new_workspace
from tests.test_culture_loop_184_acceptance import _loop, _run
from tests.test_process_dynamic_engine import _real_scenario

# ----------------------------------------------------------------
# 1. carbonate.py unit tests
# ----------------------------------------------------------------

def test_carbonate_speciation_and_roundtrip_freshwater_and_seawater() -> None:
    """Verify solve_ph and alkalinity_from_ph round-trip within 1e-6 and alphas sum to 1."""
    salinities = [0.0, 35.0]
    temperatures = [288.15, 298.15, 308.15]
    dics = [1.0, 2.0, 5.0]
    test_phs = [5.0, 6.0, 7.0, 8.0, 9.0, 10.0]

    for s in salinities:
        for t in temperatures:
            for dic in dics:
                for ph_target in test_phs:
                    # Calculate alkalinity corresponding to target pH
                    alk = carbonate.alkalinity_from_ph(dic, ph_target, t, s)
                    assert math.isfinite(alk)

                    # Solve pH from DIC and alkalinity
                    sol = carbonate.solve_ph(dic, alk, t, s)

                    # 1. Round-trip accuracy: pH matches within 1e-6
                    assert sol["ph"] == pytest.approx(ph_target, abs=1e-6), (
                        f"Round-trip failed: S={s}, T={t}, DIC={dic}, target={ph_target}, solved={sol['ph']}"
                    )

                    # 2. Speciation mass-balance: alpha_0 + alpha_1 + alpha_2 == 1
                    total_alpha = sol["alpha_0"] + sol["alpha_1"] + sol["alpha_2"]
                    assert total_alpha == pytest.approx(1.0, abs=1e-12)

                    # 3. Sum of species matches total DIC
                    total_species = sol["co2_aq_mol_m3"] + sol["hco3_mol_m3"] + sol["co3_mol_m3"]
                    assert total_species == pytest.approx(dic, rel=1e-9)

                    # 4. Species concentrations non-negative
                    assert sol["co2_aq_mol_m3"] >= 0.0
                    assert sol["hco3_mol_m3"] >= 0.0
                    assert sol["co3_mol_m3"] >= 0.0
                    assert sol["h_mol_m3"] > 0.0


def test_carbonate_henry_law_monotonicity_with_temperature() -> None:
    """Verify Henry's law CO2 solubility decreases monotonically with rising temperature."""
    temps = [278.15, 288.15, 298.15, 308.15, 318.15]
    for s in (0.0, 35.0):
        k0_vals = [carbonate.k0_solubility(t, s) for t in temps]
        for i in range(len(k0_vals) - 1):
            assert k0_vals[i] > k0_vals[i + 1], (
                f"K0 not monotonically decreasing: T={temps[i]} -> {temps[i+1]}, K0={k0_vals[i]} -> {k0_vals[i+1]}"
            )

        # Saturation concentration co2_saturation_mol_m3 also decreases monotonically
        p_co2 = 0.00042
        sat_vals = [carbonate.co2_saturation_mol_m3(p_co2, t, s) for t in temps]
        for i in range(len(sat_vals) - 1):
            assert sat_vals[i] > sat_vals[i + 1]


def test_carbonate_input_validation() -> None:
    """Verify carbonate.py rejects nonphysical and nonfinite inputs."""
    with pytest.raises(ValueError):
        carbonate.k0_solubility(-298.15)
    with pytest.raises(ValueError):
        carbonate.k0_solubility(298.15, salinity_g_kg=-1.0)
    with pytest.raises(ValueError):
        carbonate.k1_dissociation(0.0)
    with pytest.raises(ValueError):
        carbonate.k2_dissociation(298.15, salinity_g_kg=-5.0)
    with pytest.raises(ValueError):
        carbonate.kw_ion_product(float("nan"))
    with pytest.raises(ValueError):
        carbonate.co2_saturation_mol_m3(-0.00042, 298.15)
    with pytest.raises(ValueError):
        carbonate.alkalinity_from_ph(-1.0, 8.0, 298.15)
    with pytest.raises(ValueError):
        carbonate.alkalinity_from_ph(2.0, -0.5, 298.15)
    with pytest.raises(ValueError):
        carbonate.alkalinity_from_ph(2.0, 14.5, 298.15)
    with pytest.raises(ValueError):
        carbonate.solve_ph(-2.0, 2.0, 298.15)
    with pytest.raises(ValueError):
        carbonate.solve_ph(2.0, float("inf"), 298.15)


# ----------------------------------------------------------------
# 2. Dynamic batch gas-liquid CO2 equilibration test
# ----------------------------------------------------------------

def test_dynamic_batch_gas_liquid_equilibration(monkeypatch: pytest.MonkeyPatch) -> None:
    """In carbon_mode="state" with carbon_kla_h > 0 and zero growth/flow:

    - DIC relaxes toward co2_saturation + bicarbonate/carbonate equilibrium.
    - Total alkalinity remains strictly constant throughout the run.
    - pH evolves smoothly.
    """
    workspace = new_workspace()
    duration_s = 7200
    cadence_s = 300
    _, snapshot = _real_scenario(workspace, duration_s, cadence_s=cadence_s)

    payload = dict(snapshot.payload)
    t_0 = 298.15
    sal = 0.0
    dic_0 = 2.0
    ph_0 = 8.0
    alk_0 = carbonate.alkalinity_from_ph(dic_0, ph_0, t_0, sal)
    kla_h = 10.0
    p_co2 = 0.001  # 1000 ppm gas-phase CO2

    payload["scenario"] = dict(
        payload["scenario"],
        carbon_mode="state",
        carbon_kla_h={"PBR": kla_h},
        co2_partial_pressure_atm={"PBR": p_co2},
        salinity_g_kg=sal,
        initial={"PBR": {"X": 0.2, "N": 0.05, "O2": 0.008, "dic": dic_0, "alkalinity": alk_0}},
    )
    # Zero flow for closed batch unit
    payload["topology"] = dict(payload["topology"], flows={"feed": 0.0, "product": 0.0})

    # Mock zero biological growth/loss (mu=0, loss=0)
    original_growth_for_unit = dynamic_engine._growth_for_unit

    class ZeroGrowthMock:
        def __init__(self, g: Any) -> None:
            self.nitrogen_quota = getattr(g, "nitrogen_quota", 0.0)
            self.oxygen_yield = getattr(g, "oxygen_yield", 0.0)
            self.kla_h = getattr(g, "kla_h", 0.0)
            self.oxygen_saturation = getattr(g, "oxygen_saturation", 0.0)
            self.extinction = getattr(g, "extinction", 0.0)
            self.diameter = getattr(g, "diameter", 0.05)

        def rates_at(self, *args: Any, **kwargs: Any) -> tuple[float, float]:
            return 0.0, 0.0

    monkeypatch.setattr(
        dynamic_engine,
        "_growth_for_unit",
        lambda item, dark=False: ZeroGrowthMock(original_growth_for_unit(item, dark=dark)),
    )

    snap = Snapshot(payload=payload, digest="batch_co2_equil_digest")
    result = dynamic_engine.run(snap, cancelled=lambda: False, progress=lambda _p: None)

    assert result.status == "succeeded", result.error
    assert "PBR_dic_mol_m3" in result.series
    assert "PBR_pH" in result.series
    assert "PBR_co2_aq_mol_m3" in result.series

    dics = np.asarray(result.series["PBR_dic_mol_m3"])
    phs = np.asarray(result.series["PBR_pH"])
    co2_aqs = np.asarray(result.series["PBR_co2_aq_mol_m3"])

    # 1. Initial values match declared state
    assert dics[0] == pytest.approx(dic_0, abs=1e-9)
    assert phs[0] == pytest.approx(ph_0, abs=1e-6)

    # 2. Total alkalinity is strictly conserved across all time steps
    alk_series = [carbonate.alkalinity_from_ph(d, ph, t_0, sal) for d, ph in zip(dics, phs, strict=True)]
    max_alk_drift = max(abs(a - alk_0) for a in alk_series)
    assert max_alk_drift < 1e-6, f"Alkalinity drifted by {max_alk_drift} eq/m³"

    # 3. Gas transfer relaxes co2_aq toward co2_sat
    co2_sat = carbonate.co2_saturation_mol_m3(p_co2, t_0, sal)
    # co2_aq moves closer to co2_sat
    initial_gap = abs(co2_aqs[0] - co2_sat)
    final_gap = abs(co2_aqs[-1] - co2_sat)
    assert final_gap < initial_gap, f"co2_aq did not relax toward co2_sat: {initial_gap} -> {final_gap}"
    # DIC moves monotonically in the direction of the gas exchange (no overshoot past saturation).
    direction = np.sign(co2_sat - co2_aqs[0])
    assert np.all(direction * np.diff(dics) >= -1e-9)
    assert np.all(direction * (co2_sat - co2_aqs) >= -1e-9)

    # 4. Manifest records channel units
    assert result.manifest["channels"]["PBR_dic_mol_m3"] == "mol/m3"
    assert result.manifest["channels"]["PBR_pH"] == "pH"
    assert result.manifest["channels"]["PBR_co2_aq_mol_m3"] == "mol/m3"


# ----------------------------------------------------------------
# 3. Dynamic batch growth test: carbon mass conservation & CO2 limitation
# ----------------------------------------------------------------

def test_dynamic_batch_growth_carbon_conservation_and_limitation() -> None:
    """Verify:

    1. Carbon mass conservation: Delta DIC == -(f_C / M_C) * Delta X in closed batch with kla=0.
    2. CO2 limitation: co2_half_saturation_mol_m3 > 0 reduces growth when [CO2*] is low.
    """
    workspace = new_workspace()
    duration_s = 7200
    cadence_s = 300
    _, snapshot = _real_scenario(workspace, duration_s, cadence_s=cadence_s)

    payload = dict(snapshot.payload)
    t_0 = 298.15
    f_c = 0.50
    m_c = 0.012011  # kg/mol
    dic_0 = 5.0
    ph_0 = 8.0
    alk_0 = carbonate.alkalinity_from_ph(dic_0, ph_0, t_0)

    # Run A: uninhibited growth (co2_half_saturation_mol_m3 = 0.0), kla = 0
    payload["scenario"] = dict(
        payload["scenario"],
        carbon_mode="state",
        carbon_kla_h={"PBR": 0.0},
        biomass_carbon_fraction=f_c,
        co2_half_saturation_mol_m3=0.0,
        initial={"PBR": {"X": 0.2, "N": 0.5, "O2": 0.008, "dic": dic_0, "alkalinity": alk_0}},
    )
    payload["topology"] = dict(payload["topology"], flows={"feed": 0.0, "product": 0.0})

    snap_unlimited = Snapshot(payload=payload, digest="growth_unlimited_digest")
    res_unlimited = dynamic_engine.run(snap_unlimited, cancelled=lambda: False, progress=lambda _p: None)
    assert res_unlimited.status == "succeeded", res_unlimited.error

    xs = np.asarray(res_unlimited.series["PBR_X"])
    dics = np.asarray(res_unlimited.series["PBR_dic_mol_m3"])
    delta_x = xs[-1] - xs[0]
    delta_dic = dics[-1] - dics[0]
    expected_delta_dic = -(f_c / m_c) * delta_x

    # 1. Carbon mass conservation holds to numerical precision
    assert delta_x > 0.005, "Biomass should have grown measurably"
    assert delta_dic == pytest.approx(expected_delta_dic, rel=1e-5), (
        f"Carbon mass conservation violated: Delta DIC={delta_dic}, expected={expected_delta_dic}"
    )

    # Run B: Monod CO2 limitation (co2_half_saturation_mol_m3 = 0.5 mol/m³)
    payload_lim = dict(payload)
    payload_lim["scenario"] = dict(payload["scenario"], co2_half_saturation_mol_m3=0.5)
    snap_limited = Snapshot(payload=payload_lim, digest="growth_limited_digest")
    res_limited = dynamic_engine.run(snap_limited, cancelled=lambda: False, progress=lambda _p: None)
    assert res_limited.status == "succeeded", res_limited.error

    xs_lim = np.asarray(res_limited.series["PBR_X"])
    delta_x_lim = xs_lim[-1] - xs_lim[0]

    # 2. Growth with CO2 limitation is strictly lower than uninhibited growth
    assert delta_x_lim < delta_x, (
        f"CO2 limitation failed to reduce growth: unlimited={delta_x}, limited={delta_x_lim}"
    )


# ----------------------------------------------------------------
# 4. Validation and backward compatibility tests
# ----------------------------------------------------------------

def test_validation_rejects_negative_carbon_kla_and_co2_partial_pressure() -> None:
    """Validate carbon_kla_h (>= 0), co2_partial_pressure_atm (>= 0), salinity (>= 0)."""
    # 4a. Negative carbon_kla_h rejected by Scenario model validator
    with pytest.raises((ValidationError, DynamicError)):
        Scenario.model_validate({
            "id": "run",
            "units": ["PBR"],
            "profiles": [{"profile_id": "p", "digest": "d"}],
            "start_utc": "2026-01-01T00:00:00Z",
            "end_utc": "2026-01-01T01:00:00Z",
            "output_cadence_s": 3600,
            "temperature_source": "unit_mean",
            "carbon_kla_h": {"PBR": -1.0},
        })

    # 4b. Negative co2_partial_pressure_atm rejected by Scenario model validator
    with pytest.raises((ValidationError, DynamicError)):
        Scenario.model_validate({
            "id": "run",
            "units": ["PBR"],
            "profiles": [{"profile_id": "p", "digest": "d"}],
            "start_utc": "2026-01-01T00:00:00Z",
            "end_utc": "2026-01-01T01:00:00Z",
            "output_cadence_s": 3600,
            "temperature_source": "unit_mean",
            "co2_partial_pressure_atm": {"PBR": -0.00042},
        })

    # 4c. Negative salinity rejected by Scenario model validator
    with pytest.raises((ValidationError, DynamicError)):
        Scenario.model_validate({
            "id": "run",
            "units": ["PBR"],
            "profiles": [{"profile_id": "p", "digest": "d"}],
            "start_utc": "2026-01-01T00:00:00Z",
            "end_utc": "2026-01-01T01:00:00Z",
            "output_cadence_s": 3600,
            "temperature_source": "unit_mean",
            "salinity_g_kg": -5.0,
        })


def test_carbon_mode_off_backward_compatibility() -> None:
    """Default carbon_mode="off" preserves bit-for-bit behavior and does NOT emit carbon series."""
    sc = Scenario.model_validate({
        "id": "run",
        "units": ["PBR"],
        "profiles": [{"profile_id": "p", "digest": "d"}],
        "start_utc": "2026-01-01T00:00:00Z",
        "end_utc": "2026-01-01T01:00:00Z",
        "output_cadence_s": 3600,
        "temperature_source": "unit_mean",
    })
    assert sc.carbon_mode == "off"
    assert sc.carbon_kla_h == {}
    assert sc.co2_partial_pressure_atm == {}
    assert sc.biomass_carbon_fraction == 0.50
    assert sc.co2_half_saturation_mol_m3 == 0.0
    assert sc.salinity_g_kg == 0.0

    workspace = new_workspace()
    _, snapshot = _real_scenario(workspace, 3600)
    result = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _p: None)
    assert result.status == "succeeded", result.error
    assert "PBR_X" in result.series
    assert "PBR_dic_mol_m3" not in result.series
    assert "PBR_pH" not in result.series
    assert "PBR_co2_aq_mol_m3" not in result.series


# ----------------------------------------------------------------
# 5. Multi-unit culture loop test with carbon states
# ----------------------------------------------------------------

def test_culture_loop_carbon_states_and_mean_series() -> None:
    """In a culture loop with multiple units, carbon_mode="state" emits unit and loop series."""
    alk0 = carbonate.alkalinity_from_ph(2.0, 8.0, 298.15)
    extra_carbon = {
        "carbon_mode": "state",
        "carbon_kla_h": {"PBR": 5.0},
        "co2_partial_pressure_atm": {"PBR": 0.001},
        "initial": {
            "PBR": {"X": 0.2, "N": 0.05, "O2": 0.008, "dic": 2.0, "alkalinity": alk0},
            "PBR2": {"X": 0.2, "N": 0.05, "O2": 0.008, "dic": 2.0, "alkalinity": alk0},
            "PBR3": {"X": 0.2, "N": 0.05, "O2": 0.008, "dic": 2.0, "alkalinity": alk0},
            "PBR4": {"X": 0.2, "N": 0.05, "O2": 0.008, "dic": 2.0, "alkalinity": alk0},
            "TankA": {"X": 0.2, "N": 0.05, "O2": 0.008, "dic": 2.0, "alkalinity": alk0},
            "TankB": {"X": 0.2, "N": 0.05, "O2": 0.008, "dic": 2.0, "alkalinity": alk0},
        },
    }

    _, snap = _loop(
        q_circ=5e-4, tube_length=2.0, tank_volume=0.02, days=1, cadence_s=3600,
        feed_m3_s=5e-5, scenario_extra=extra_carbon,
    )
    result = _run(snap)
    assert result.status == "succeeded", result.error

    pbr_units = [u for u in snap.payload["units"] if u["tag"] in ("PBR", "PBR2", "PBR3", "PBR4", "TankA", "TankB")]
    volumes = np.array([u["volume_m3"] for u in pbr_units])
    total_volume = volumes.sum()

    for u in pbr_units:
        tag = u["tag"]
        assert f"{tag}_dic_mol_m3" in result.series
        assert f"{tag}_pH" in result.series
        assert f"{tag}_co2_aq_mol_m3" in result.series

    assert "PBR_dic_mean_mol_m3" in result.series
    assert "PBR_pH_mean" in result.series

    # Verify loop mean DIC equals volume-weighted mean of member units
    for row_idx in (0, len(result.series["t_s"]) // 2, -1):
        dics = np.array([result.series[f"{u['tag']}_dic_mol_m3"][row_idx] for u in pbr_units])
        expected_loop_dic = float((dics * volumes).sum() / total_volume)
        actual_loop_dic = float(result.series["PBR_dic_mean_mol_m3"][row_idx])
        assert actual_loop_dic == pytest.approx(expected_loop_dic, rel=1e-6)

        phs = np.array([result.series[f"{u['tag']}_pH"][row_idx] for u in pbr_units])
        expected_loop_ph = float((phs * volumes).sum() / total_volume)
        actual_loop_ph = float(result.series["PBR_pH_mean"][row_idx])
        assert actual_loop_ph == pytest.approx(expected_loop_ph, rel=1e-6)
