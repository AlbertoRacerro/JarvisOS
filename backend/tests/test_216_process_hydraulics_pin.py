"""Spec 216 contract 8 (Process side): a PBR unit pins a published CFD reduced model for its loop hydraulics."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.modules.bluecad.cfd_pipe.reduced_model import evaluate
from app.modules.bluecad.cfd_pipe.studio_publication import studio_source
from app.modules.process_stack import draft, pbr_unit, properties
from app.modules.process_stack.draft import DraftError
from app.modules.process_stack.draft_models import AddUnit, SetUnitHydraulics, SetUnitParams
from tests.plumbing_170_support import new_workspace, pbr_document, pbr_ops, pbr_quantities, pin_of
from tests.test_170_plumbing_runtime import _card, _context, _inlet
from tests.test_216_component_reduced_models import _add_published
from tests.test_216_reduced_model import c1_model

RHO, MU = 998.2, 1.002e-3  # the validated fluid of the C1 model (contract default water_20C)
DIAMETER = 0.046


def _pin(model: Any, source: str, version_id: str = "reduced-v1", **overrides: Any) -> dict[str, Any]:
    return {"kind": "cfd_reduced_model", "studio_version_id": version_id,
            "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
            "reduced_model_version_id": model.version_id, "geometry_digest": model.geometry_digest} | overrides


@pytest.fixture(autouse=True)
def water(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(self: Any, request: Any) -> Any:
        outputs = [SimpleNamespace(name="density", value=SimpleNamespace(value=RHO)),
                   SimpleNamespace(name="dynamic_viscosity", value=SimpleNamespace(value=MU))]
        return SimpleNamespace(status="succeeded", outputs=outputs, failure=None)

    monkeypatch.setattr(properties.CoolPropPropertyEvaluator, "evaluate", fake)


def _params(velocity: float = 0.5) -> dict[str, float]:
    return {"temperature_mean": 298.15, "liquid_velocity": velocity, "tube_inner_diameter": DIAMETER, "tube_length": 100.0,
            "tube_count": 10.0, "baffle_friction_multiplier": 1.5, "pump_efficiency": 60.0}


def _published() -> tuple[str, Any, str, str]:
    workspace_id = new_workspace()
    model = c1_model()
    source = studio_source(model)
    _add_published(workspace_id, "reduced-v1", source)
    return workspace_id, model, source, "reduced-v1"


# ---- draft op

def test_draft_op_accepts_clears_and_refuses() -> None:
    model = c1_model()
    pin = _pin(model, studio_source(model))
    document = pbr_document()
    pinned = draft.apply_ops(document, [SetUnitHydraulics(op="set_unit_hydraulics", unit="pbr", hydraulics=pin)])
    assert pinned["objects"]["pbr"]["hydraulics_model"] == pin | {"out_of_range": "refuse"}
    cleared = draft.apply_ops(pinned, [SetUnitHydraulics(op="set_unit_hydraulics", unit="pbr", hydraulics=None)])
    assert "hydraulics_model" not in cleared["objects"]["pbr"]
    with pytest.raises(ValidationError):
        SetUnitHydraulics(op="set_unit_hydraulics", unit="pbr", hydraulics=pin | {"source_sha256": "xyz"})
    with pytest.raises(ValidationError):
        SetUnitHydraulics(op="set_unit_hydraulics", unit="pbr", hydraulics=pin | {"extra": 1})


def test_draft_op_refuses_a_non_pbr_unit() -> None:
    model = c1_model()
    pin = _pin(model, studio_source(model))
    document = draft.apply_ops(pbr_document(), [AddUnit(op="add_unit", id="heater", type="Heater", tag="H1", x=0, y=0)])
    with pytest.raises(DraftError) as raised:
        draft.apply_ops(document, [SetUnitHydraulics(op="set_unit_hydraulics", unit="heater", hydraulics=pin)])
    assert raised.value.code == "unit_hydraulics_unsupported"


# ---- hydraulics

def test_pinned_in_range_pressure_drop_is_the_reduced_model_gradient() -> None:
    workspace_id, model, source, version_id = _published()
    pin = _pin(model, source)
    params = _params()
    hydro = pbr_unit.hydraulics(params, pin=pin, workspace_id=workspace_id)
    reynolds = 0.5 * DIAMETER * RHO / MU
    gradient = evaluate(model, reynolds=reynolds, diameter_m=DIAMETER, density_kg_m3=RHO, dynamic_viscosity_pa_s=MU)
    assert gradient.status == "in_range"
    assert hydro["pressure_drop"] == pytest.approx(gradient.pressure_gradient_pa_m * 100.0 * 1.5, rel=1e-12)
    assert hydro["cfd"]["status"] == "in_range" and hydro["cfd"]["relative_uncertainty"] == gradient.relative_uncertainty
    assert hydro["cfd"]["fidelity"] == pbr_unit.CFD_FIDELITY and hydro["pumping_power"] > 0


def test_refuse_mode_out_of_range_reports_no_pressure_drop() -> None:
    workspace_id, model, source, _ = _published()
    hydro = pbr_unit.hydraulics(_params(0.1), pin=_pin(model, source), workspace_id=workspace_id)
    assert hydro["pressure_drop"] is None and hydro["pumping_power"] is None
    assert hydro["cfd"]["status"] == "refused" and hydro["cfd"]["violations"]


def test_warn_mode_out_of_range_reports_numbers_without_uncertainty() -> None:
    workspace_id, model, source, _ = _published()
    hydro = pbr_unit.hydraulics(_params(0.1), pin=_pin(model, source, out_of_range="warn"), workspace_id=workspace_id)
    assert hydro["pressure_drop"] > 0 and hydro["cfd"]["status"] == "out_of_range_warned"
    assert hydro["cfd"]["relative_uncertainty"] is None and hydro["cfd"]["violations"]


@pytest.mark.parametrize("mutate", ["sha", "version", "missing", "no_workspace"])
def test_unavailable_model_fails_visibly_without_fallback(mutate: str) -> None:
    workspace_id, model, source, _ = _published()
    pin = _pin(model, source)
    if mutate == "sha":
        pin["source_sha256"] = "0" * 64
    elif mutate == "version":
        pin["reduced_model_version_id"] = "not-the-version"
    elif mutate == "missing":
        pin["studio_version_id"] = "absent"
    with pytest.raises(pbr_unit.PbrFailure) as raised:
        pbr_unit.hydraulics(_params(), pin=pin, workspace_id=None if mutate == "no_workspace" else workspace_id)
    assert raised.value.code == "PBR_HYDRAULICS_MODEL_UNAVAILABLE"


# ---- unit result and cache

def _unit_for(workspace_id: str, hydraulics_pin: dict[str, Any] | None) -> dict[str, Any]:
    document = draft.apply_ops(draft.empty_document("pbr"), [
        *pbr_ops(), SetUnitParams(op="set_unit_params", unit="pbr", values=pbr_quantities(
            tube_inner_diameter=(DIAMETER, "m")))])
    unit = document["objects"]["pbr"] | {"model": pin_of(_card(workspace_id, "Hydraulics card"))}
    return unit | ({"hydraulics_model": hydraulics_pin} if hydraulics_pin else {})


def test_unit_result_shows_fidelity_and_caveats() -> None:
    workspace_id, model, source, _ = _published()
    plain = pbr_unit.evaluate_pbr(_unit_for(workspace_id, None), _inlet(), _context(workspace_id)).result
    assert plain["hydraulics_model"] == {"fidelity": pbr_unit.CORRELATION_FIDELITY}
    assert plain["reported"]["pressure_drop"]["label"].endswith("(screening)")
    pinned = pbr_unit.evaluate_pbr(_unit_for(workspace_id, _pin(model, source)), _inlet(), _context(workspace_id)).result
    block = pinned["hydraulics_model"]
    assert block["fidelity"] == pbr_unit.CFD_FIDELITY and block["status"] == "in_range"
    assert block["studio_version_id"] == "reduced-v1" and block["reduced_model_version_id"] == model.version_id
    assert block["geometry_digest"] == model.geometry_digest and block["violations"] == []
    assert "screening" not in pinned["reported"]["pressure_drop"]["label"]
    assert pinned["reported"]["pressure_drop"]["value"] is not None


def test_unit_result_refusal_is_a_visible_finding_and_caveat() -> None:
    workspace_id, model, source, _ = _published()
    unit = _unit_for(workspace_id, _pin(model, source))
    unit["params"]["liquid_velocity"] = dict(unit["params"]["liquid_velocity"], si=0.1)
    result = pbr_unit.evaluate_pbr(unit, _inlet(), _context(workspace_id)).result
    assert result["reported"]["pressure_drop"]["value"] is None
    assert any(item["code"] == "PBR_HYDRAULICS_MODEL_REFUSED" for item in result["findings"])
    assert any("refused" in caveat for caveat in result["caveats"])


def test_cache_key_differs_between_pinned_and_unpinned() -> None:
    model = c1_model()
    unit = {"tag": "PBR"}
    args = ({"a": 1.0}, {"card_id": "c"}, "digest", {}, 1000.0)
    plain = pbr_unit._cache_key(unit, *args)
    pinned = pbr_unit._cache_key(unit | {"hydraulics_model": _pin(model, studio_source(model))}, *args)
    warned = pbr_unit._cache_key(unit | {"hydraulics_model": _pin(model, studio_source(model), out_of_range="warn")}, *args)
    assert len({plain, pinned, warned}) == 3


def test_dynamic_runs_say_the_pin_is_not_applied(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.process_stack import dynamic_engine

    monkeypatch.setattr(dynamic_engine.pbr_unit, "hydraulics", lambda params: {"pressure_drop": 1.0})
    item = {"tag": "PBR", "unit": {"id": "pbr", "type": "PhotobioreactorT1", "hydraulics_model": {"kind": "cfd_reduced_model"}},
            "volume_m3": 1.0, "params": {"tube_inner_diameter": 0.1, "tube_count": 1.0, "liquid_velocity": 0.1}}
    topology = {"units": [{"id": "pbr", "tag": "PBR", "type": "PhotobioreactorT1"}], "streams": [], "culture_loops": []}
    _, findings = dynamic_engine._flow_hydraulics(topology, [item], {})
    assert [(f["code"], f["hydraulics_fidelity"]) for f in findings] == [
        ("PBR_HYDRAULICS_PIN_NOT_APPLIED_DYNAMIC", pbr_unit.CORRELATION_FIDELITY)]
    item["unit"].pop("hydraulics_model")
    assert dynamic_engine._flow_hydraulics(topology, [item], {})[1] == []
