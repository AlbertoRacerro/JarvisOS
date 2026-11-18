from __future__ import annotations

import time

import pytest

from app.modules.bio_models import forms
from app.modules.bio_models import service as bio_models
from app.modules.process_stack import pbr_unit
from app.modules.process_stack.mixed_runtime import JarvisUnitContext
from tests.plumbing_170_support import new_workspace, pbr_document

LIGHT_PARAMETERS = {
    "light.monod": {"K_I": (150.0, "umol/(m**2*s)")},
    "light.haldane": {"K_I": (150.0, "umol/(m**2*s)"), "K_i": (500.0, "umol/(m**2*s)")},
    "light.steele": {"I_opt": (300.0, "umol/(m**2*s)")},
    "light.eilers_peeters_steady": {"I_opt": (300.0, "umol/(m**2*s)"), "beta": (0.5, "1")},
    "light.hill": {"I_k": (150.0, "umol/(m**2*s)"), "n": (2.0, "1")},
}
BASE_PARAMETERS = {
    "K_j_0": (0.001, "kg/m3"), "k_d": (0.004, "1/hour"),
    "a": (1.8, "1"), "b": (0.5, "1"), "c": (0.1, "1"), "d": (0.01, "1"),
    "w_ash": (0.05, "1"), "k_X": (150.0, "m**2/kg"),
}
FACTORS = {
    "light": "light.monod", "optics": "optics.slab_response_average",
    "temperature": "temperature.isothermal", "nutrients": ["nutrient.monod"],
    "combination": "combine.multiplicative", "loss": "loss.first_order",
    "stoichiometry": "stoich.photoautotrophic",
}
PBR_PARAMS = {
    "tube_inner_diameter": 0.05, "peak_par": 1500.0, "photoperiod": 43200.0,
    "diffuse_fraction": 0.2, "temperature_mean": 298.15, "temperature_amplitude": 5.0,
    "oxygen_kla": 20.0 / 3600.0, "oxygen_saturation": 0.008,
}


def _card(workspace_id: str, light_form: str, name: str, changes: dict[str, tuple[float, str]] | None = None):
    parameter_set = bio_models.create_set(workspace_id, f"{name} set")
    values = {**BASE_PARAMETERS, **LIGHT_PARAMETERS[light_form]}
    if changes:
        values.update(changes)
    for symbol, (value, unit) in values.items():
        parameter_set = bio_models.edit_set_value(
            workspace_id, parameter_set["id"], symbol,
            {"value": value, "unit": unit, "expected_unit": unit},
            parameter_set["revision"], parameter_set["digest"])
    return bio_models.create_card(
        workspace_id, name, parameter_set["id"], {**FACTORS, "light": light_form},
        {"value": 0.0625, "unit": "1/hour"})


def _growth(resolved: dict, light_form: str) -> pbr_unit.Growth:
    return pbr_unit.build_growth(PBR_PARAMS, resolved, (0.0, 0.05, 0.0), 0.1)


@pytest.mark.parametrize("light_form", list(LIGHT_PARAMETERS))
def test_resolves_all_registered_light_forms(light_form: str) -> None:
    workspace_id = new_workspace()
    card = _card(workspace_id, light_form, f"Resolved {light_form}")
    resolved = bio_models.resolve_growth_model(
        workspace_id, card["id"], card["revision"], card["digest"])
    assert resolved["card"]["factors"]["light"] == light_form
    growth = _growth(resolved, light_form)
    assert growth.light_form == light_form


@pytest.mark.parametrize(
    ("light_form", "missing"),
    [
        ("light.monod", "K_I"),
        ("light.haldane", "K_i"),
        ("light.steele", "I_opt"),
        ("light.eilers_peeters_steady", "beta"),
        ("light.hill", "n"),
    ],
)
def test_missing_form_symbol_is_typed(light_form: str, missing: str) -> None:
    workspace_id = new_workspace()
    parameter_set = bio_models.create_set(workspace_id, f"Missing {light_form}")
    for symbol, (value, unit) in {**BASE_PARAMETERS, **LIGHT_PARAMETERS[light_form]}.items():
        if symbol == missing:
            continue
        parameter_set = bio_models.edit_set_value(
            workspace_id, parameter_set["id"], symbol,
            {"value": value, "unit": unit, "expected_unit": unit},
            parameter_set["revision"], parameter_set["digest"])
    card = bio_models.create_card(
        workspace_id, f"Missing {light_form}", parameter_set["id"],
        {**FACTORS, "light": light_form}, {"value": 0.0625, "unit": "1/hour"})
    with pytest.raises(bio_models.BioModelError) as raised:
        bio_models.resolve_growth_model(workspace_id, card["id"], card["revision"], card["digest"])
    assert raised.value.code == "PBR_MODEL_CARD_SYMBOL_MISSING"
    assert missing in raised.value.detail["symbols"]


@pytest.mark.parametrize(
    ("light_form", "symbol", "bad"),
    [
        ("light.monod", "K_I", 0.0),
        ("light.haldane", "K_i", 0.0),
        ("light.steele", "I_opt", 0.0),
        ("light.eilers_peeters_steady", "beta", -1.0),
        ("light.hill", "n", 0.0),
    ],
)
def test_invalid_light_parameters_are_refused(light_form: str, symbol: str, bad: float) -> None:
    workspace_id = new_workspace()
    unit = "1" if symbol in {"beta", "n"} else "umol/(m**2*s)"
    card = _card(workspace_id, light_form, f"Invalid {light_form}", {symbol: (bad, unit)})
    resolved = bio_models.resolve_growth_model(
        workspace_id, card["id"], card["revision"], card["digest"])
    with pytest.raises(pbr_unit.PbrFailure) as raised:
        _growth(resolved, light_form)
    assert raised.value.code == "PBR_MODEL_CARD_UNAVAILABLE"


@pytest.mark.parametrize("light_form", list(LIGHT_PARAMETERS))
def test_growth_light_matches_forms_in_zero_attenuation(light_form: str) -> None:
    workspace_id = new_workspace()
    card = _card(workspace_id, light_form, f"Parity {light_form}")
    resolved = bio_models.resolve_growth_model(
        workspace_id, card["id"], card["revision"], card["digest"])
    growth = _growth(resolved, light_form)
    irradiance = 500.0
    parameters = growth.light_parameters
    if light_form == "light.monod":
        expected = forms.light_monod(irradiance, *parameters)
    elif light_form == "light.haldane":
        expected = forms.light_haldane(irradiance, *parameters)
    elif light_form == "light.steele":
        expected = forms.light_steele(irradiance, *parameters)
    elif light_form == "light.eilers_peeters_steady":
        expected = forms.light_eilers_peeters_steady(irradiance, *parameters)
    else:
        expected = forms.light_hill(irradiance, *parameters)
    assert growth.light(irradiance, 0.0) == pytest.approx(expected)


@pytest.mark.parametrize("light_form", list(LIGHT_PARAMETERS))
def test_full_photobioreactor_evaluator_accepts_light_form(light_form: str) -> None:
    workspace_id = new_workspace()
    card = _card(workspace_id, light_form, f"Evaluator {light_form}")
    pin = {"card_id": card["id"], "card_revision": card["revision"], "card_digest": card["digest"]}
    unit = pbr_document(model=True)["objects"]["pbr"]
    unit["model"] = pin
    inlet = {
        "temperature_K": 298.15, "pressure_Pa": 101325.0, "mass_flow_kg_s": 1.0,
        "mass_fractions": {"Water": 1.0}, "vapor_fraction": 0.0,
        "culture": {"biomass": 0.0, "nitrogen": 0.05, "oxygen": 0.0, "salinity": 35.0},
    }
    context = JarvisUnitContext(1000.0, 60.0 + time.monotonic(), {}, False, workspace_id)
    evaluation = pbr_unit.evaluate_pbr(unit, inlet, context)
    assert evaluation.result["calculated"] is True


def test_photoinhibition_forms_have_distinct_responses() -> None:
    workspace_id = new_workspace()
    models = {}
    for light_form in ("light.monod", "light.haldane", "light.steele"):
        card = _card(workspace_id, light_form, f"Response {light_form}")
        resolved = bio_models.resolve_growth_model(
            workspace_id, card["id"], card["revision"], card["digest"])
        models[light_form] = _growth(resolved, light_form)
    monod = models["light.monod"].light(1500.0, 0.0)
    haldane = models["light.haldane"].light(1500.0, 0.0)
    steele = models["light.steele"].light(1500.0, 0.0)
    assert haldane < monod
    assert steele < monod
