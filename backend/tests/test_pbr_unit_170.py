"""Independent limiting cases and branch checks for the Tier-1 PBR."""

from __future__ import annotations

import json
import math
import runpy
import time
from pathlib import Path

import numpy as np
import pytest
from scipy.integrate import quad

from app.core.config import get_settings
from app.core.database import initialize_database
from app.modules.bio_models import cylinder
from app.modules.bio_models import service as bio_models
from app.modules.engineering.evidence_contracts import ScientificQualificationRecord
from app.modules.process_stack.pbr_adapter import PbrUnitT1Evaluator
from app.modules.process_stack.pbr_unit import (
    Growth,
    PbrFailure,
    _Clock,
    certify,
    solve_periodic,
    thin_growth_rate,
)
from app.modules.workspaces.models import WorkspaceCreate
from app.modules.workspaces.service import create_workspace


def growth(**changes: float) -> Growth:
    values = dict(
        mu_max_h=0.08, light_saturation=100.0, extinction=50.0,
        nitrogen_half_saturation=0.01, nitrogen_quota=0.05, oxygen_yield=1.0,
        temperature_form="temperature.isothermal", temperature_parameters=(),
        loss_form="loss.first_order", loss_parameters=(0.002,), diameter=0.05,
        peak_par=800.0, photoperiod_h=12.0, diffuse_fraction=0.3,
        temperature_mean=298.15, temperature_amplitude=3.0, kla_h=1.0,
        oxygen_saturation=0.008, dilution_h=0.01, inlet=(0.0, 0.05, 0.008),
    )
    values.update(changes)
    return Growth(**values)


def clock() -> _Clock:
    return _Clock(time.monotonic() + 15.0)


@pytest.mark.parametrize("tau", [0.01, 1.0, 10.0, 100.0, 1000.0])
def test_cylinder_linear_beam_matches_independent_chord_integral(tau: float) -> None:
    # Exchange the illuminated-area and path integrals over each chord.
    expected = 4.0 / (math.pi * tau) * quad(
        lambda theta: math.cos(theta) * -math.expm1(-tau * math.cos(theta)),
        0.0, math.pi / 2, epsabs=2e-13, epsrel=2e-12,
    )[0]
    actual = cylinder.response_average_tau(lambda irradiance: irradiance, 1.0, tau, 0.0)
    assert actual == pytest.approx(expected, rel=1e-6, abs=1e-8)


def test_cylinder_thin_limit_and_response_is_local() -> None:
    def response(irradiance: np.ndarray) -> np.ndarray:
        return irradiance / (10.0 + irradiance)
    assert cylinder.response_average_tau(response, 100.0, 0.0, 0.7) == pytest.approx(100.0 / 110.0)
    at_depth = cylinder.response_average_tau(response, 100.0, 30.0, 0.7)
    response_of_mean = response(np.array([cylinder.response_average_tau(lambda i: i, 100.0, 30.0, 0.7)]))[0]
    assert at_depth < response_of_mean  # Jensen: no silent response-of-mean shortcut.


@pytest.mark.parametrize("tau", [0.01, 1.0, 10.0, 100.0, 316.0, 1000.0])
def test_cylinder_nonlinear_response_against_independent_direct_integral(tau: float) -> None:
    reference_path = Path(__file__).resolve().parents[2] / "scripts/benchmarks/pbr_reference/cylinder_reference.py"
    reference = runpy.run_path(str(reference_path))["nonlinear_reference"]
    expected = reference(tau, 800.0, 0.5, 150.0)
    actual = cylinder.response_average_tau(lambda irradiance: irradiance / (150.0 + irradiance),
                                           800.0, tau, 0.5)
    assert abs(actual - expected) <= 1e-4 * abs(expected) + 1e-8


@pytest.mark.parametrize("argument", [0.0, 1e-6, 0.01, 1.0, 10.0, 100.0, 316.0])
def test_ki2_interpolation_against_direct_elevation_integral(argument: float) -> None:
    expected = quad(lambda elevation: math.cos(elevation)
                    * math.exp(-argument / math.cos(elevation)) if elevation < math.pi / 2 else 0.0,
                    0.0, math.pi / 2, epsabs=1e-14, epsrel=1e-12)[0]
    assert cylinder.ki2(argument).item() == pytest.approx(expected, rel=1e-6, abs=1e-145)


def test_periodic_productive_orbit_conserves_nutrient_and_is_repeatable() -> None:
    model = growth()
    first = solve_periodic(model, clock())
    second = solve_periodic(model, clock())
    assert first == second
    assert first.branch == "productive" and first.x_star > 0
    orbit = certify(model, first.x_star, clock())
    assert all(abs(error) <= limit for error, limit in zip(orbit.residuals, orbit.tolerances, strict=True))
    assert orbit.z_identity_max <= orbit.tolerances[1]
    # The O₂ affine map closes independently of the biomass/nitrogen conserved state.
    assert orbit.initial[2] >= 0.0
    assert abs(orbit.samples[-1][2] - orbit.initial[2]) <= orbit.tolerances[2]
    # Independently close each daily-mean carrier balance using the quadrature
    # states, not generation inferred from inlet minus outlet.
    final = orbit.samples[-1]
    means = [final[index] / 24.0 for index in range(3, 6)]
    rate_mean, oxygen_transfer_mean = final[6] / 24.0, final[7] / 24.0
    generation = (rate_mean, -model.nitrogen_quota * rate_mean,
                  model.oxygen_yield * rate_mean - oxygen_transfer_mean)
    flow_m3_s = model.dilution_h / 3600.0  # choose V = 1 m³
    for index in range(3):
        balance = flow_m3_s * (model.inlet[index] - means[index]) + generation[index] / 3600.0
        periodic_drift = (final[index] - orbit.initial[index]) / 86400.0
        assert balance == pytest.approx(periodic_drift, abs=2e-13)


def test_washout_and_numerical_tie_do_not_select_productive_branch() -> None:
    base = growth()
    critical = thin_growth_rate(base)
    washout = solve_periodic(growth(dilution_h=critical * 1.1), clock())
    assert washout.branch == "washout" and washout.x_star == 0.0
    with pytest.raises(PbrFailure) as raised:
        solve_periodic(growth(dilution_h=critical * (1.0 - 1e-9)), clock())
    assert raised.value.code == "PBR_BRANCH_UNRESOLVED"
    with pytest.raises(PbrFailure) as raised:
        solve_periodic(growth(dilution_h=critical * (1.0 - 1e-7)), clock())
    assert raised.value.code == "PBR_BRANCH_UNRESOLVED"


@pytest.mark.parametrize("hrt_d, branch", [(0.1, "washout"), (100.0, "productive")])
def test_short_and_long_hrt_keep_periodic_certification(hrt_d: float, branch: str) -> None:
    model = growth(dilution_h=1.0 / (24.0 * hrt_d))
    solved = solve_periodic(model, clock())
    assert solved.branch == branch
    orbit = certify(model, solved.x_star, clock())
    assert all(abs(error) <= limit for error, limit in zip(orbit.residuals, orbit.tolerances, strict=True))


def test_dark_no_feedback_matches_closed_form_and_refuses_unbounded_growth() -> None:
    # In darkness r_X = -k_d X; the periodic state is an ordinary CSTR balance.
    model = growth(extinction=0.0, nitrogen_quota=0.0, peak_par=0.0,
                   inlet=(0.2, 0.05, 0.01), kla_h=0.0, dilution_h=0.1)
    solved = solve_periodic(model, clock())
    assert solved.x_star == pytest.approx(0.1 * 0.2 / 0.102, rel=1e-9)
    orbit = certify(model, solved.x_star, clock())
    assert orbit.initial[1] == pytest.approx(0.05)
    with pytest.raises(PbrFailure) as raised:
        solve_periodic(growth(extinction=0.0, nitrogen_quota=0.0, inlet=(0.01, 0.05, 0.008)), clock())
    assert raised.value.code == "PBR_NO_FINITE_PRODUCTIVE_STATE"


def test_dark_oxygen_depletion_is_typed_nonphysical_failure() -> None:
    model = growth(extinction=0.0, nitrogen_quota=0.0, peak_par=0.0,
                   inlet=(0.2, 0.05, 0.0), kla_h=0.0, dilution_h=0.1)
    solved = solve_periodic(model, clock())
    with pytest.raises(PbrFailure) as raised:
        certify(model, solved.x_star, clock())
    assert raised.value.code == "PBR_NONPHYSICAL_STATE"
    assert "oxygen" in str(raised.value).lower()


def test_headless_106_descriptor_points_to_valid_unqualified_ledger() -> None:
    descriptor = PbrUnitT1Evaluator().descriptor()
    assert descriptor.backend_kind == "dynamic_simulator" and descriptor.fidelity == "reduced_order"
    ledger_path = Path(__file__).resolve().parents[2] / descriptor.qualification_record_ref.object_id
    record = ScientificQualificationRecord.model_validate(json.loads(ledger_path.read_text())["qualification_record"])
    assert record.validity.qualification_status == "unqualified"
    assert all(item.kind == "synthetic_fixture" for item in record.provenance)


def test_model_pin_resolves_exact_revision_and_refuses_unsupported_or_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "jarvis"))
    get_settings.cache_clear()
    initialize_database()
    workspace = create_workspace(WorkspaceCreate(name="PBR model pin", slug="pbr-model-pin", status="active"))
    parameters = {"K_I": (150, "umol/(m**2*s)"), "K_j_0": (.001, "kg/m3"), "k_d": (.004, "1/hour"),
                  "a": (1.8, "1"), "b": (.5, "1"), "c": (.1, "1"), "d": (.01, "1"),
                  "w_ash": (.05, "1"), "k_X": (150, "m**2/kg")}
    parameter_set = bio_models.create_set(workspace.id, "Synthetic PBR set")
    for symbol, (value, unit) in parameters.items():
        parameter_set = bio_models.edit_set_value(
            workspace.id, parameter_set["id"], symbol,
            {"value": value, "unit": unit, "expected_unit": unit},
            parameter_set["revision"], parameter_set["digest"])
    factors = {"light": "light.monod", "optics": "optics.slab_response_average",
               "temperature": "temperature.isothermal", "nutrients": ["nutrient.monod"],
               "combination": "combine.multiplicative", "loss": "loss.first_order",
               "stoichiometry": "stoich.photoautotrophic"}
    card = bio_models.create_card(workspace.id, "Synthetic PBR card", parameter_set["id"],
                                  factors, {"value": .0625, "unit": "1/hour"})
    resolved = bio_models.resolve_growth_model(workspace.id, card["id"], card["revision"], card["digest"])
    assert resolved["parameters"]["k_X"] == 150
    assert resolved["set"]["revision"] == parameter_set["revision"]
    assert resolved["candidate_symbols"]
    changed = bio_models.edit_set_value(workspace.id, parameter_set["id"], "k_X",
                                        {"value": 250, "unit": "m**2/kg", "expected_unit": "m**2/kg"},
                                        parameter_set["revision"], parameter_set["digest"])
    assert changed["revision"] != parameter_set["revision"]
    assert bio_models.resolve_growth_model(workspace.id, card["id"], card["revision"], card["digest"])["parameters"]["k_X"] == 150
    with pytest.raises(bio_models.BioModelError) as mismatch:
        bio_models.resolve_growth_model(workspace.id, card["id"], card["revision"], "0" * 64)
    assert mismatch.value.code == "PBR_MODEL_CARD_UNAVAILABLE"
    haldane = bio_models.create_card(workspace.id, "Unsupported T1", parameter_set["id"],
                                     {**factors, "light": "light.haldane"}, {"value": .0625, "unit": "1/hour"})
    with pytest.raises(bio_models.BioModelError) as unsupported:
        bio_models.resolve_growth_model(workspace.id, haldane["id"], haldane["revision"], haldane["digest"])
    assert unsupported.value.code == "PBR_MODEL_CARD_FORM_UNSUPPORTED"
    sparse_set = bio_models.create_set(workspace.id, "Missing PBR symbols")
    sparse_card = bio_models.create_card(workspace.id, "Incomplete", sparse_set["id"],
                                         factors, {"value": .0625, "unit": "1/hour"})
    with pytest.raises(bio_models.BioModelError) as missing:
        bio_models.resolve_growth_model(workspace.id, sparse_card["id"], sparse_card["revision"], sparse_card["digest"])
    assert missing.value.code == "PBR_MODEL_CARD_SYMBOL_MISSING"
    assert "k_X" in missing.value.detail["symbols"]
