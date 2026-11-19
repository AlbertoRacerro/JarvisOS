"""Spec 323 flat-slab optics and wall transmission tests."""

from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
import pytest
from pydantic import ValidationError

from app.modules.bio_models import slab
from app.modules.process_stack import draft, dynamic_engine
from app.modules.process_stack.draft_models import SetScenario
from app.modules.process_stack.dynamic_models import Scenario
from app.modules.process_stack.pbr_unit import Growth
from tests.plumbing_170_support import new_workspace
from tests.test_process_dynamic_engine import _real_scenario


@pytest.mark.parametrize("tau", [0.1, 1.0, 5.0])
def test_flat_slab_monod_matches_analytical_integral(tau: float) -> None:
    i0 = 800.0
    ki = 150.0
    actual = slab.response_average_tau(
        lambda irradiance: irradiance / (ki + irradiance),
        i0,
        tau,
        back_albedo=0.0,
    )
    expected = (1.0 / tau) * math.log((ki + i0) / (ki + i0 * math.exp(-tau)))
    assert actual == pytest.approx(expected, abs=1e-10, rel=1e-10)


def test_reflective_back_wall_monotonic() -> None:
    def response(irradiance: np.ndarray) -> np.ndarray:
        return irradiance / (150.0 + irradiance)

    values = [
        slab.response_average_tau(response, 800.0, 2.0, back_albedo=albedo)
        for albedo in (0.0, 0.5, 0.9)
    ]
    assert values[0] < values[1] < values[2]


def test_wall_transmission_decay_and_validation() -> None:
    clean = slab.wall_transmission_factor(0.0, 0.8, 0.001, 0.2)
    partly_fouled = slab.wall_transmission_factor(100.0, 0.8, 0.001, 0.2)
    fully_fouled = slab.wall_transmission_factor(1_000_000.0, 0.8, 0.001, 0.2)
    assert clean == pytest.approx(0.8)
    assert partly_fouled < clean
    assert fully_fouled == pytest.approx(0.8 * 0.2, rel=1e-8)

    for kwargs in (
        {"base_transmittance": 0.0},
        {"base_transmittance": 1.1},
        {"fouling_rate_1_s": -0.1},
        {"fouling_floor": -0.1},
        {"fouling_floor": 1.1},
    ):
        with pytest.raises(ValueError):
            slab.wall_transmission_factor(0.0, **kwargs)

    with pytest.raises(ValueError):
        slab.slab_irradiance_profile(800.0, 1.0, np.array([0.0, 0.5, 1.0]), back_albedo=1.1)
    with pytest.raises(ValueError):
        slab.slab_irradiance_profile(800.0, 1.0, np.array([0.0, 0.5, 1.0]), front_albedo=1.0)


def test_scenario_wall_validation() -> None:
    base = dict(
        id="run",
        units=["PBR"],
        profiles=[{"profile_id": "p", "digest": "d"}],
        start_utc="2026-01-01T00:00:00Z",
        end_utc="2026-01-01T01:00:00Z",
        output_cadence_s=3600,
        temperature_source="unit_mean",
    )
    Scenario.model_validate({**base, "back_wall_albedo": {"PBR": 0.5},
                             "wall_transmittance": {"PBR": 0.8},
                             "wall_fouling_rate_1_s": {"PBR": 0.001},
                             "wall_fouling_floor": {"PBR": 0.2}})
    for field, value in (
        ("back_wall_albedo", {"PBR": 1.1}),
        ("wall_transmittance", {"PBR": 0.0}),
        ("wall_fouling_rate_1_s", {"PBR": -0.1}),
        ("wall_fouling_floor", {"PBR": 1.1}),
    ):
        with pytest.raises(ValidationError):
            Scenario.model_validate({**base, field: value})


def _growth() -> Growth:
    return Growth(
        mu_max_h=0.08, light_saturation=150.0, extinction=50.0,
        nitrogen_half_saturation=0.01, nitrogen_quota=0.05, oxygen_yield=1.0,
        temperature_form="temperature.isothermal", temperature_parameters=(),
        loss_form="loss.first_order", loss_parameters=(0.002,), diameter=0.05,
        peak_par=800.0, photoperiod_h=12.0, diffuse_fraction=0.0,
        temperature_mean=298.15, temperature_amplitude=0.0,
        kla_h=1.0, oxygen_saturation=0.008, dilution_h=0.01,
        inlet=(0.0, 0.05, 0.008),
        optics_geometry="flat_slab", slab_depth_m=0.05,
        back_wall_albedo=0.9, wall_transmittance=0.8,
    )


def test_growth_flat_slab_wall_transmittance() -> None:
    flat = _growth()
    attenuated = flat.light(800.0, 1.0)
    unattenuated = replace(flat, wall_transmittance=1.0).light(800.0, 1.0)
    assert attenuated < unattenuated
    cylinder = replace(flat, optics_geometry="cylinder", wall_transmittance=1.0)
    assert cylinder.light(800.0, 0.0) == pytest.approx(cylinder._light_scalar(800.0))


def test_dynamic_run_clean_vs_fouled_wall_yield() -> None:
    workspace_id = new_workspace()
    state, clean_snapshot = _real_scenario(workspace_id, 3600)
    clean_scenario = dict(clean_snapshot.payload["scenario"])
    fouled_scenario = dict(clean_scenario)
    fouled_scenario.update({
        "optics_geometry": {"PBR": "flat_slab"},
        "back_wall_albedo": {"PBR": 0.0},
        "wall_transmittance": {"PBR": 1.0},
        "wall_fouling_rate_1_s": {"PBR": 0.001},
        "wall_fouling_floor": {"PBR": 0.2},
    })
    clean_scenario.update({
        "optics_geometry": {"PBR": "flat_slab"},
        "back_wall_albedo": {"PBR": 0.0},
        "wall_transmittance": {"PBR": 1.0},
        "wall_fouling_rate_1_s": {"PBR": 0.0},
        "wall_fouling_floor": {"PBR": 0.0},
    })

    clean_state = draft.patch(
        workspace_id, state["draft_id"], state["revision"],
        [SetScenario(op="set_scenario", id="run", value=clean_scenario)],
    )
    clean_prepared = dynamic_engine.prepare(workspace_id, clean_state["draft_id"], "run")
    clean_result = dynamic_engine.run(
        clean_prepared, cancelled=lambda: False, progress=lambda _value: None,
    )

    fouled_state = draft.patch(
        workspace_id, clean_state["draft_id"], clean_state["revision"],
        [SetScenario(op="set_scenario", id="run", value=fouled_scenario)],
    )
    fouled_prepared = dynamic_engine.prepare(workspace_id, fouled_state["draft_id"], "run")
    fouled_result = dynamic_engine.run(
        fouled_prepared, cancelled=lambda: False, progress=lambda _value: None,
    )
    assert clean_result.status == fouled_result.status == "succeeded"
    assert fouled_result.series["PBR_X"][-1] < clean_result.series["PBR_X"][-1]
