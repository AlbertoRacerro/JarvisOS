"""Spec 170 section 9.1: Run-level plumbing of the PBR unit through the 168 mixed runtime and the model resolver."""

from __future__ import annotations

import math
import time
from typing import Any

import pytest

from app.modules.bio_models import service as bio_models
from app.modules.process_stack import mixed_runtime, pbr_unit
from app.modules.process_stack.mixed_runtime import JarvisUnitContext
from tests.plumbing_170_support import new_workspace, pbr_document, pin_of

PBR_PARAMETERS = {"K_I": (150, "umol/(m**2*s)"), "K_j_0": (0.001, "kg/m3"), "k_d": (0.004, "1/hour"),
                  "a": (1.8, "1"), "b": (0.5, "1"), "c": (0.1, "1"), "d": (0.01, "1"), "w_ash": (0.05, "1"),
                  "k_X": (150, "m**2/kg")}
FACTORS = {"light": "light.monod", "optics": "optics.slab_response_average", "temperature": "temperature.isothermal",
           "nutrients": ["nutrient.monod"], "combination": "combine.multiplicative", "loss": "loss.first_order",
           "stoichiometry": "stoich.photoautotrophic"}


def _set(workspace_id: str, name: str, skip: tuple[str, ...] = ()) -> dict[str, Any]:
    parameter_set = bio_models.create_set(workspace_id, name)
    for symbol, (value, unit) in PBR_PARAMETERS.items():
        if symbol in skip:
            continue
        parameter_set = bio_models.edit_set_value(
            workspace_id, parameter_set["id"], symbol, {"value": value, "unit": unit, "expected_unit": unit},
            parameter_set["revision"], parameter_set["digest"])
    return parameter_set


def _card(workspace_id: str, name: str, factors: dict[str, Any] | None = None) -> dict[str, Any]:
    parameter_set = _set(workspace_id, f"Set for {name}")
    return bio_models.create_card(workspace_id, name, parameter_set["id"], factors or FACTORS,
                                  {"value": 0.0625, "unit": "1/hour"})


def _context(workspace_id: str | None = None, density: float = 1000.0, validation: bool = False) -> JarvisUnitContext:
    return JarvisUnitContext(density, time.monotonic() + 60.0, {}, validation=validation, workspace_id=workspace_id)


def _inlet(**culture: float | None) -> dict[str, Any]:
    values = {"biomass": 0.0, "nitrogen": 0.05, "oxygen": 0.0, "salinity": 35.0} | culture
    return {"temperature_K": 298.15, "pressure_Pa": 101325.0, "mass_flow_kg_s": 1.0,
            "mass_fractions": {"Water": 1.0}, "vapor_fraction": 0.0, "culture": values}


def _unit(pin: dict[str, str] | None = None) -> dict[str, Any]:
    return pbr_document(model=pin is None)["objects"]["pbr"] | ({"model": pin} if pin else {})


# ---- typed failure travels through the 168 run as segment_failed with the PBR tag

def _stub_dwsim(monkeypatch: pytest.MonkeyPatch, *, mixture_density: float | None) -> None:
    phases = [{"name": "Mixture", "density_kg_m3": mixture_density}] if mixture_density else []

    def flash(state: dict[str, Any], *_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {**state, "phases": phases}

    monkeypatch.setattr(mixed_runtime, "_flash", flash)
    # Without a solved density the feed culture cannot be converted; hand the PBR the culture directly so the
    # runtime reaches the unit with density None (the unflashed-inlet case of mixed_runtime).
    monkeypatch.setattr(mixed_runtime, "_culture_for_feed",
                        lambda *_a, **_k: _inlet()["culture"])


def test_inlet_without_a_solved_density_is_a_typed_pbr_failure_not_a_segment_crash(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    _stub_dwsim(monkeypatch, mixture_density=None)
    document = pbr_document(model=True)
    record = mixed_runtime.run(document, action="run", client=object(), dwsim_version="10.2.9",
                               mcp_sha256="a" * 64, run_dir=tmp_path, workspace_id=None)
    solve = record["mixed_solve"]
    assert record["status"] == "segment_failed" and solve["status"] == "segment_failed"
    assert solve["failed_segment"] == "PBR"
    assert solve["errors"]["code"] == "PBR_INLET_DENSITY_UNAVAILABLE"
    assert solve["errors"]["error_type"] == "PbrFailure"
    message = solve["errors"]["message"]
    assert "density" in message and len(message) <= 600
    assert solve["message"]
    # 168 records a Jarvis-unit failure under failed_segment only; the editor matches either field (see the node test).
    assert solve["failed_units"] == []


def test_failed_pbr_run_keeps_the_unit_tag_code_and_plain_message_within_600_characters() -> None:
    class Typed(RuntimeError):
        code = "PBR_NONPHYSICAL_STATE"

    def failing(_unit: dict, _inlet: dict, _context: JarvisUnitContext) -> None:
        raise Typed("Dissolved oxygen fell below zero over the day; raise kLa. " + "x" * 900)

    unit = _unit({"card_id": "c", "card_revision": "r-" + "a" * 16, "card_digest": "b" * 64})
    unit["tag"] = "PBR-1"
    original = mixed_runtime.JARVIS_EVALUATORS["PhotobioreactorT1"]
    mixed_runtime.JARVIS_EVALUATORS["PhotobioreactorT1"] = failing
    try:
        with pytest.raises(mixed_runtime.SegmentFailure) as raised:
            mixed_runtime._call_evaluator(unit, _inlet(), _context())
    finally:
        mixed_runtime.JARVIS_EVALUATORS["PhotobioreactorT1"] = original
    assert raised.value.segment == "PBR-1"
    assert raised.value.detail["code"] == "PBR_NONPHYSICAL_STATE"
    assert raised.value.detail["message"].startswith("Dissolved oxygen fell below zero")
    assert len(raised.value.detail["message"]) == 600


def test_the_mixed_runtime_hands_a_missing_density_to_the_pbr_as_nan_while_other_units_still_fail(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    seen: list[float] = []

    def evaluate(unit: dict, inlet: dict, context: JarvisUnitContext):  # noqa: ANN202
        seen.append(context.inlet_density_kg_m3)
        raise pbr_unit.PbrFailure("PBR_NO_THROUGHFLOW", "stop here")

    monkeypatch.setitem(mixed_runtime.JARVIS_EVALUATORS, "PhotobioreactorT1", evaluate)
    _stub_dwsim(monkeypatch, mixture_density=None)
    record = mixed_runtime.run(pbr_document(model=True), action="run", client=object(), dwsim_version="10.2.9",
                               mcp_sha256="a" * 64, run_dir=tmp_path)
    assert len(seen) == 1 and math.isnan(seen[0])
    assert record["mixed_solve"]["errors"]["code"] == "PBR_NO_THROUGHFLOW"


# ---- evaluator: density, validation pass-through, culture input refusals

def test_evaluator_refuses_nan_density_in_run_mode_with_the_typed_code() -> None:
    with pytest.raises(pbr_unit.PbrFailure) as raised:
        pbr_unit.evaluate_pbr(_unit(), _inlet(), _context(density=math.nan))
    assert raised.value.code == "PBR_INLET_DENSITY_UNAVAILABLE"
    assert "Mixer or Heater" in str(raised.value)


def test_validation_mode_copies_carrier_and_culture_with_zero_generation_and_never_solves(
        monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("the solver must not run in validation mode")

    for name in ("solve_periodic", "certify", "build_growth", "hydraulics", "_resolve"):
        monkeypatch.setattr(pbr_unit, name, forbidden)
    inlet = _inlet(nitrogen=None)  # even an unknown culture field passes through; validation reports it instead
    evaluation = pbr_unit.evaluate_pbr(_unit(), inlet, _context(density=math.nan, validation=True))
    outlet = evaluation.outlets["outlet"]
    assert {key: outlet[key] for key in inlet} == inlet
    assert outlet is not inlet and outlet["culture"] is not inlet["culture"]
    assert outlet["owner"] == "jarvis_bio"
    assert evaluation.culture_generation == {} and evaluation.culture_generation_allowance == {}
    assert evaluation.result["calculated"] is False and evaluation.result["findings"] == []


@pytest.fixture()
def workspace_and_pin() -> tuple[str, dict[str, str]]:
    workspace_id = new_workspace()
    return workspace_id, pin_of(_card(workspace_id, "Run-level card"))


@pytest.mark.parametrize(("field", "code"), [("nitrogen", "PBR_REQUIRES_NITROGEN"), ("oxygen", "PBR_REQUIRES_OXYGEN"),
                                              ("biomass", "PBR_REQUIRES_BIOMASS")])
@pytest.mark.parametrize("missing", [None, math.nan])
def test_run_refuses_unknown_culture_inputs_with_their_codes(
        workspace_and_pin: tuple[str, dict[str, str]], field: str, code: str, missing: float | None) -> None:
    workspace_id, pin = workspace_and_pin
    with pytest.raises(pbr_unit.PbrFailure) as raised:
        pbr_unit.evaluate_pbr(_unit(pin), _inlet(**{field: missing}), _context(workspace_id))
    assert raised.value.code == code


def test_run_accepts_an_explicit_zero_oxygen_and_nitrogen(workspace_and_pin: tuple[str, dict[str, str]]) -> None:
    workspace_id, pin = workspace_and_pin
    for culture in ({"oxygen": 0.0}, {"nitrogen": 0.0}):
        evaluation = pbr_unit.evaluate_pbr(_unit(pin), _inlet(**culture), _context(workspace_id))
        assert evaluation.result["calculated"] is True
        assert evaluation.outlets["outlet"]["culture"]["oxygen"] >= 0.0


# ---- model card resolution

@pytest.mark.parametrize("override", [{"nutrients": ["nutrient.droop"]}, {"light": "light.steele"},
                                      {"light": "light.eilers_peeters_steady"}])
def test_unsupported_forms_are_refused_with_form_unsupported(override: dict[str, Any]) -> None:
    workspace_id = new_workspace()
    card = _card(workspace_id, "Unsupported T1 form", {**FACTORS, **override})
    with pytest.raises(bio_models.BioModelError) as raised:
        bio_models.resolve_growth_model(workspace_id, card["id"], card["revision"], card["digest"])
    assert raised.value.code == "PBR_MODEL_CARD_FORM_UNSUPPORTED"
    findings = bio_models.pbr_model_findings(workspace_id, "PBR", pin_of(card))
    assert [(item["severity"], item["code"]) for item in findings] == [("blocker", "PBR_MODEL_CARD_FORM_UNSUPPORTED")]


def test_a_card_without_a_stoichiometry_factor_cannot_exist_and_so_cannot_be_pinned() -> None:
    workspace_id = new_workspace()
    parameter_set = _set(workspace_id, "No stoichiometry")
    factors = {key: value for key, value in FACTORS.items() if key != "stoichiometry"}
    with pytest.raises(bio_models.BioModelError) as raised:
        bio_models.create_card(workspace_id, "No stoich", parameter_set["id"], factors,
                               {"value": 0.0625, "unit": "1/hour"})
    assert raised.value.code == "factor_family_required"
    assert "stoichiometry" in str(raised.value)


def test_nutrient_index_zero_is_the_167_nitrogen_field_and_a_second_nutrient_is_refused() -> None:
    workspace_id = new_workspace()
    model = bio_models.resolve_growth_model(workspace_id, *_pin_values(_card(workspace_id, "One nutrient")))
    growth = pbr_unit.build_growth(
        {"tube_inner_diameter": 0.05, "peak_par": 1500.0, "photoperiod": 43200.0, "diffuse_fraction": 0.2,
         "temperature_mean": 298.15, "temperature_amplitude": 5.0, "oxygen_kla": 20.0 / 3600.0,
         "oxygen_saturation": 0.008}, model, (0.0, 0.05, 0.0), 0.1)
    assert growth.nitrogen_half_saturation == model["parameters"]["K_j_0"] == 0.001
    two = _card(workspace_id, "Two nutrients", {**FACTORS, "nutrients": ["nutrient.monod", "nutrient.monod"]})
    with pytest.raises(bio_models.BioModelError) as raised:
        bio_models.resolve_growth_model(workspace_id, *_pin_values(two))
    assert raised.value.code == "PBR_MODEL_CARD_FORM_UNSUPPORTED" and "nutrients" in str(raised.value)
    sparse = bio_models.create_card(
        workspace_id, "No K_j_0", _set(workspace_id, "Sparse", skip=("K_j_0",))["id"], FACTORS,
        {"value": 0.0625, "unit": "1/hour"})
    with pytest.raises(bio_models.BioModelError) as missing:
        bio_models.resolve_growth_model(workspace_id, *_pin_values(sparse))
    assert missing.value.code == "PBR_MODEL_CARD_SYMBOL_MISSING" and missing.value.detail["symbols"] == ["K_j_0"]


def _pin_values(card: dict[str, Any]) -> tuple[str, str, str]:
    return card["id"], card["revision"], card["digest"]
