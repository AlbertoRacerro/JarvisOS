"""Tests for Spec 321: Culture temperature as a dynamic state (CULTURE-TEMPERATURE-STATE-1)."""

from __future__ import annotations

import numpy as np
import pytest
from pydantic import ValidationError

from app.modules.process_stack import draft, dynamic_engine
from app.modules.process_stack.draft_models import (
    DraftQuantity,
    SetScenario,
)
from app.modules.process_stack.dynamic_engine import DynamicError, Snapshot
from app.modules.process_stack.dynamic_models import Scenario
from tests.plumbing_170_support import new_workspace
from tests.test_culture_loop_184_acceptance import _loop, _run
from tests.test_process_dynamic_engine import _real_scenario


def _q(value: float, unit: str) -> DraftQuantity:
    return DraftQuantity(value=value, unit=unit)


# ----------------------------------------------------------------
# 1. Analytical exponential relaxation test (zero flow, UA > 0)
# ----------------------------------------------------------------

def test_analytical_relaxation_batch_unit() -> None:
    """In thermal_mode="state" with zero flow and UA > 0, culture temperature relaxes exponentially.

    T(t) = T_env + (T_0 - T_env) * exp(-t / tau), where tau = rho * cp * V / UA.
    """
    workspace = new_workspace()
    duration_s = 7200
    cadence_s = 300
    state, snapshot = _real_scenario(workspace, duration_s, cadence_s=cadence_s)

    payload = dict(snapshot.payload)
    unit = payload["units"][0]
    volume_m3 = float(unit["volume_m3"])
    rho = 1000.0
    cp = 4184.0
    tau = 1800.0  # 30 minute time constant
    ua = rho * cp * volume_m3 / tau
    t_0 = 313.15  # 40 deg C initial
    t_env = 298.15  # 25 deg C ambient (fixture profile)

    payload["scenario"] = dict(
        payload["scenario"],
        thermal_mode="state",
        thermal_ua={"PBR": ua},
        thermal_heat_w={"PBR": 0.0},
        water_density_kg_m3=rho,
        water_cp_j_kg_k=cp,
        initial={"PBR": {"X": 0.2, "N": 0.05, "O2": 0.008, "T": t_0}},
    )
    # Zero flow for pure batch relaxation
    payload["topology"] = dict(payload["topology"], flows={"feed": 0.0, "product": 0.0})

    snap = Snapshot(payload=payload, digest="batch_relaxation_digest")
    result = dynamic_engine.run(snap, cancelled=lambda: False, progress=lambda _p: None)

    assert result.status == "succeeded", result.error
    assert "PBR_T_K" in result.series

    times = np.asarray(result.series["t_s"])
    simulated_t = np.asarray(result.series["PBR_T_K"])
    analytical_t = t_env + (t_0 - t_env) * np.exp(-times / tau)

    # Verify initial value matches T_0
    assert simulated_t[0] == pytest.approx(t_0, abs=1e-9)

    # Verify simulated temperature matches analytical solution within tight tolerance across all points
    max_error = np.max(np.abs(simulated_t - analytical_t))
    assert max_error < 1e-4, f"Max error {max_error} exceeds 1e-4 K"

    # Verify channels manifest records temperature unit
    assert result.manifest["channels"]["PBR_T_K"] == "K"


# ----------------------------------------------------------------
# 2. Steady-state external heat input test (UA > 0, Q_ext > 0)
# ----------------------------------------------------------------

def test_steady_state_external_heat_input() -> None:
    """With external heat input Q_ext and UA > 0, steady state approaches T_ss = T_env + Q_ext / UA."""
    workspace = new_workspace()
    duration_s = 18000  # 5 hours = 10 time constants
    cadence_s = 300
    state, snapshot = _real_scenario(workspace, duration_s, cadence_s=cadence_s)

    payload = dict(snapshot.payload)
    unit = payload["units"][0]
    volume_m3 = float(unit["volume_m3"])
    rho = 1000.0
    cp = 4184.0
    tau = 1800.0
    ua = rho * cp * volume_m3 / tau
    q_ext = 5000.0  # 5 kW heat input
    t_0 = 295.15
    t_env = 298.15
    t_ss = t_env + q_ext / ua

    payload["scenario"] = dict(
        payload["scenario"],
        thermal_mode="state",
        thermal_ua={"PBR": ua},
        thermal_heat_w={"PBR": q_ext},
        water_density_kg_m3=rho,
        water_cp_j_kg_k=cp,
        initial={"PBR": {"X": 0.2, "N": 0.05, "O2": 0.008, "T": t_0}},
    )
    payload["topology"] = dict(payload["topology"], flows={"feed": 0.0, "product": 0.0})

    snap = Snapshot(payload=payload, digest="heat_input_digest")
    result = dynamic_engine.run(snap, cancelled=lambda: False, progress=lambda _p: None)

    assert result.status == "succeeded", result.error
    times = np.asarray(result.series["t_s"])
    simulated_t = np.asarray(result.series["PBR_T_K"])
    analytical_t = t_ss + (t_0 - t_ss) * np.exp(-times / tau)

    # Whole curve matches analytical transient
    max_error = np.max(np.abs(simulated_t - analytical_t))
    assert max_error < 1e-4

    # Steady state is reached within numerical tolerance
    assert simulated_t[-1] == pytest.approx(t_ss, abs=1e-3)


# ----------------------------------------------------------------
# 3. Flow-weighted mixing and closed-loop temperature advection
# ----------------------------------------------------------------

def test_flow_weighted_mixing_and_closed_loop_advection() -> None:
    """Flow-weighted mixing in Mixer, closed-loop advection across PBR + Tank, and kinetics coupling."""
    # Run two loops: one at optimal culture temperature (~298.15 K) and one cold (~280 K)
    # to confirm Growth.rates_at responds to dynamic state temperature.
    extra_warm = {
        "thermal_mode": "state",
        "thermal_ua": {"PBR": 2000.0, "TankA": 500.0},
        "thermal_heat_w": {"PBR": 0.0},
        "initial": {
            "PBR": {"X": 0.2, "N": 0.05, "O2": 0.008, "T": 298.15},
            "PBR2": {"X": 0.2, "N": 0.05, "O2": 0.008, "T": 298.15},
            "PBR3": {"X": 0.2, "N": 0.05, "O2": 0.008, "T": 298.15},
            "PBR4": {"X": 0.2, "N": 0.05, "O2": 0.008, "T": 298.15},
            "TankA": {"X": 0.2, "N": 0.05, "O2": 0.008, "T": 298.15},
            "TankB": {"X": 0.2, "N": 0.05, "O2": 0.008, "T": 298.15},
        },
    }

    extra_cold = {
        "thermal_mode": "state",
        "thermal_ua": {"PBR": 2000.0, "TankA": 500.0},
        "thermal_heat_w": {"PBR": 0.0},
        "initial": {
            "PBR": {"X": 0.2, "N": 0.05, "O2": 0.008, "T": 280.15},
            "PBR2": {"X": 0.2, "N": 0.05, "O2": 0.008, "T": 280.15},
            "PBR3": {"X": 0.2, "N": 0.05, "O2": 0.008, "T": 280.15},
            "PBR4": {"X": 0.2, "N": 0.05, "O2": 0.008, "T": 280.15},
            "TankA": {"X": 0.2, "N": 0.05, "O2": 0.008, "T": 280.15},
            "TankB": {"X": 0.2, "N": 0.05, "O2": 0.008, "T": 280.15},
        },
    }

    _, snap_warm = _loop(
        q_circ=5e-4, tube_length=2.0, tank_volume=0.02, days=1, cadence_s=3600,
        feed_m3_s=5e-5, scenario_extra=extra_warm,
    )
    result_warm = _run(snap_warm)
    assert result_warm.status == "succeeded", result_warm.error

    _, snap_cold = _loop(
        q_circ=5e-4, tube_length=2.0, tank_volume=0.02, days=1, cadence_s=3600,
        feed_m3_s=5e-5, scenario_extra=extra_cold,
    )
    result_cold = _run(snap_cold)
    assert result_cold.status == "succeeded", result_cold.error

    # Verify presence of <unit_tag>_T_K and <loop_id>_T_mean_K
    for tag in ("PBR", "PBR2", "PBR3", "PBR4", "TankA", "TankB"):
        assert f"{tag}_T_K" in result_warm.series
    assert "PBR_T_mean_K" in result_warm.series

    # Verify loop mean temperature equals volume-weighted mean of member units
    pbr_units = [u for u in snap_warm.payload["units"] if u["tag"] in ("PBR", "PBR2", "PBR3", "PBR4", "TankA", "TankB")]
    volumes = np.array([u["volume_m3"] for u in pbr_units])
    total_volume = volumes.sum()

    for row_idx in (0, len(result_warm.series["t_s"]) // 2, -1):
        temps = np.array([result_warm.series[f"{u['tag']}_T_K"][row_idx] for u in pbr_units])
        expected_loop_mean = float((temps * volumes).sum() / total_volume)
        actual_loop_mean = float(result_warm.series["PBR_T_mean_K"][row_idx])
        assert actual_loop_mean == pytest.approx(expected_loop_mean, rel=1e-6)

    # Verify thermal coupling to Growth.rates_at: warm culture grows faster than cold culture
    biomass_warm = result_warm.series["PBR_biomass_kg"][-1]
    biomass_cold = result_cold.series["PBR_biomass_kg"][-1]
    assert biomass_warm > biomass_cold, "Biomass at T_opt must exceed biomass at cold temperature"


# ----------------------------------------------------------------
# 4. Validation and backward-compatibility tests
# ----------------------------------------------------------------

def test_validation_rejects_negative_ua_and_invalid_temperatures() -> None:
    """Validate thermal_ua (>= 0), water properties (> 0), initial T (> 0), and feed stream temperature (> 0)."""
    # 4a. Negative thermal_ua rejected by Pydantic model and prepare
    with pytest.raises((ValidationError, DynamicError)):
        Scenario.model_validate({
            "id": "run",
            "units": ["PBR"],
            "profiles": [{"profile_id": "p", "digest": "d"}],
            "start_utc": "2026-01-01T00:00:00Z",
            "end_utc": "2026-01-01T01:00:00Z",
            "output_cadence_s": 3600,
            "temperature_source": "unit_mean",
            "thermal_ua": {"PBR": -10.0},
        })

    # 4b. Nonpositive water density and heat capacity rejected
    with pytest.raises(ValidationError):
        Scenario.model_validate({
            "id": "run",
            "units": ["PBR"],
            "profiles": [{"profile_id": "p", "digest": "d"}],
            "start_utc": "2026-01-01T00:00:00Z",
            "end_utc": "2026-01-01T01:00:00Z",
            "output_cadence_s": 3600,
            "temperature_source": "unit_mean",
            "water_density_kg_m3": -100.0,
        })

    with pytest.raises(ValidationError):
        Scenario.model_validate({
            "id": "run",
            "units": ["PBR"],
            "profiles": [{"profile_id": "p", "digest": "d"}],
            "start_utc": "2026-01-01T00:00:00Z",
            "end_utc": "2026-01-01T01:00:00Z",
            "output_cadence_s": 3600,
            "temperature_source": "unit_mean",
            "water_cp_j_kg_k": 0.0,
        })

    # 4c. Nonpositive initial T rejected in prepare
    workspace = new_workspace()
    state, _ = _real_scenario(workspace, 3600)
    dir_path = draft.draft_dir(workspace, state["draft_id"])
    rev = draft.load_revision(dir_path, draft._head(dir_path)["revision"])
    sc = dict(rev["document"]["scenarios"]["run"])
    sc["thermal_mode"] = "state"
    sc["initial"]["PBR"]["T"] = -5.0

    draft.patch(workspace, state["draft_id"], rev["revision"], [SetScenario(op="set_scenario", id="run", value=sc)])
    with pytest.raises(DynamicError) as exc_info:
        dynamic_engine.prepare(workspace, state["draft_id"], "run")
    assert exc_info.value.code == "INITIAL_STATE_INVALID"

    # Also zero initial T rejected
    rev = draft.load_revision(dir_path, draft._head(dir_path)["revision"])
    sc["initial"]["PBR"]["T"] = 0.0
    draft.patch(workspace, state["draft_id"], rev["revision"], [SetScenario(op="set_scenario", id="run", value=sc)])
    with pytest.raises(DynamicError) as exc_info:
        dynamic_engine.prepare(workspace, state["draft_id"], "run")
    assert exc_info.value.code == "INITIAL_STATE_INVALID"

    # 4d. Nonpositive feed stream temperature rejected in prepare
    rev = draft.load_revision(dir_path, draft._head(dir_path)["revision"])
    doc = dict(rev["document"])
    doc["scenarios"]["run"]["initial"]["PBR"]["T"] = 298.15
    doc["objects"]["feed"]["spec"]["temperature"] = {"si": 0.0, "unit": "K"}
    draft._write_revision(dir_path, doc, parent=draft._head(dir_path)["revision"], actor="operator", ops=[])
    with pytest.raises(DynamicError) as exc_info:
        dynamic_engine.prepare(workspace, state["draft_id"], "run")
    assert exc_info.value.code == "FEED_TEMPERATURE_INVALID"

    # 4e. Nonpositive temperature during integration rejected in _valid_state
    units = [{"tag": "PBR", "unit": {"type": "PhotobioreactorT1"}}]
    # 7-slot state with slot 6 <= 0 (K)
    nonphysical_state = np.array([0.2, 0.05, 0.008, 0.0, 0.0, 0.0, -1.0])
    with pytest.raises(DynamicError) as exc_info:
        dynamic_engine._valid_state(
            nonphysical_state, units,
            growths=[type("Growth", (), {"extinction": 0, "diameter": 0})()],
            thermal_mode="state",
        )
    assert exc_info.value.code == "STATE_NONPHYSICAL"


def test_backward_compatibility_prescribed_default() -> None:
    """When thermal_mode='prescribed' (the default), existing behavior is 100% unchanged."""
    # Scenario defaults thermal_mode to "prescribed"
    sc = Scenario.model_validate({
        "id": "run",
        "units": ["PBR"],
        "profiles": [{"profile_id": "p", "digest": "d"}],
        "start_utc": "2026-01-01T00:00:00Z",
        "end_utc": "2026-01-01T01:00:00Z",
        "output_cadence_s": 3600,
        "temperature_source": "unit_mean",
    })
    assert sc.thermal_mode == "prescribed"
    assert sc.thermal_ua == {}
    assert sc.thermal_heat_w == {}
    assert sc.water_density_kg_m3 == 1000.0
    assert sc.water_cp_j_kg_k == 4184.0

    # Default run does NOT emit PBR_T_K or loop_T_mean_K
    workspace = new_workspace()
    state, snapshot = _real_scenario(workspace, 3600)
    result = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _p: None)
    assert result.status == "succeeded"
    assert "PBR_temperature" in result.series
    assert "PBR_T_K" not in result.series
    assert "PBR_T_mean_K" not in result.series
