"""Spec 181: one run outcome, results state demotion, results view and deterministic KPIs."""

from __future__ import annotations

import copy
import os
from typing import Any

import pytest

from app.modules.process_stack import draft, results_view

REV = "3:" + "b" * 16


def _stream(sid: str, tag: str, source: str | None = None, target: str | None = None,
            source_port: int = 0, target_port: int = 0) -> dict[str, Any]:
    return {"id": sid, "kind": "stream", "type": "MaterialStream", "tag": tag,
            "source": {"unit": source, "port": source_port} if source else None,
            "target": {"unit": target, "port": target_port} if target else None, "spec": {}}


def _unit(uid: str, unit_type: str, tag: str, **params: float) -> dict[str, Any]:
    return {"id": uid, "kind": "unit", "type": unit_type, "tag": tag,
            "params": {key: {"si": value} for key, value in params.items()}}


def _document() -> dict[str, Any]:
    """Feed -> PBR -> separator -> concentrate + clarified products."""
    objects = [
        _stream("f", "Feed", target="pbr"), _unit("pbr", "PhotobioreactorT1", "PBR-1",
                                                   tube_count=10, tube_inner_diameter=0.05, tube_length=20),
        _stream("o", "Broth", "pbr", "sep"), _unit("sep", "SpecifiedSeparator", "SEP-1"),
        _stream("c", "Concentrate", "sep", None, source_port=0), _stream("w", "Clarified", "sep", None, source_port=1),
    ]
    return {"name": "t", "compounds": ["Water"], "property_package": "NRTL",
            "objects": {item["id"]: item for item in objects}}


def _culture(biomass: float | None, density: float = 1000.0, nitrogen: float | None = 0.05) -> dict[str, Any]:
    def value(si: float | None) -> dict[str, Any]:
        if si is None:
            return {"mass_specific": None, "display": None, "reason": "not specified on feed Feed"}
        return {"mass_specific": si / density, "si": si, "display": {"value": si, "unit": "kg/m3"}}
    return {"status": "completed", "density_kg_m3": density,
            "values": {"biomass": value(biomass), "nitrogen": value(nitrogen)}}


def _stream_result(flow: float) -> dict[str, Any]:
    return {"mass_flow_kg_s": flow, "temperature_K": 298.15, "pressure_Pa": 101325.0, "vapor_fraction": 0.0,
            "mass_fractions": {"Water": 1.0}, "molar_flow_mol_s": flow / 0.018015}


def _converged_run() -> dict[str, Any]:
    return {
        "run_id": "a" * 32, "action": "run", "status": "completed", "draft_revision": REV,
        "started_at": "2026-01-02T00:00:00+00:00",
        "streams": {"Feed": _stream_result(0.01), "Broth": _stream_result(0.01),
                    "Concentrate": _stream_result(0.001), "Clarified": _stream_result(0.009)},
        "units": {"PBR-1": {"owner": "jarvis_bio", "calculated": True, "reported": {
            "volumetric_productivity": {"value": 0.25, "units": "kg/(m³·d)", "label": "Net volumetric biomass productivity"},
            "net_biomass_production": {"value": 2.0, "units": "kg/d", "label": "Net biomass production rate"},
            "volumetric_flow_m3_h": {"value": 0.036, "units": "m³/h", "label": "Carrier volumetric flow Q"},
            "pressure_drop": {"value": 0.0, "units": "Pa", "label": "Tube pressure drop"},
            "lambda_h": {"value": None, "units": "1/h", "label": "Thin-culture growth rate"}}},
            "SEP-1": {"owner": "jarvis_bio", "calculated": True, "reported": {}}},
        "culture": {"Feed": _culture(0.0), "Broth": _culture(2.0),
                    "Concentrate": _culture(18.0), "Clarified": _culture(0.0)},
        "mixed_solve": {"status": "completed", "reason": "converged", "history": [
            {"iteration": 1, "max_normalized_residual": 3.0, "worst_tear": "T"},
            {"iteration": 2, "max_normalized_residual": 0.4, "worst_tear": "T"}],
            "balances": {"carrier_mass": {"in": 0.01, "out": 0.01, "residual": 1e-9, "passed": True}}},
    }


# ---------------------------------------------------------------- outcome


_FAILED_OBJECT = {"tag": "H1", "calculated": False, "error": "not calculated"}


@pytest.mark.parametrize(("run", "state", "reason"), [
    ({"status": "validated", "action": "validate"}, "validated", None),
    ({"status": "completed", "action": "run", "solve": {"ok": True, "errors": [], "failed_objects": []}},
     "converged", None),
    ({"status": "unconverged", "action": "run", "mixed_solve": {"reason": "max_iterations", "history": [
        {"max_normalized_residual": 7.5, "worst_tear": "Return"}]}}, "non_converged", "max_iterations"),
    ({"status": "failed", "action": "run", "solve": {"ok": False, "errors": ["boom"], "failed_objects": []}},
     "non_converged", "DWSIM_SOLVE_NOT_CONVERGED"),
    ({"status": "failed", "action": "run", "solve": {"ok": True, "errors": [], "failed_objects": [_FAILED_OBJECT]}},
     "non_converged", "DWSIM_SOLVE_NOT_CONVERGED"),
    ({"status": "failed", "action": "run", "solve": {"ok": True, "errors": [], "failed_objects": [
        {"tag": "R1", "calculated": False, "error": "x", "code": "KINETICS_VERIFICATION_FAILED"}]}},
     "failed", "KINETICS_VERIFICATION_FAILED"),
    ({"status": "check_failed", "action": "validate", "dwsim_check": {"findings": [
        {"severity": "blocker", "object": "H1", "message": "no outlet"}]}}, "failed", "check_failed"),
    ({"status": "materialization_mismatch", "action": "run"}, "failed", "materialization_mismatch"),
    ({"status": "materialization_failed", "action": "run", "error": "x",
      "error_detail": {"step": "dwsim_flowsheet_new"}}, "failed", "materialization_failed"),
    ({"status": "segment_failed", "action": "run", "mixed_solve": {
        "reason": "light_full_mismatch", "failed_segment": "mixed-1-0", "failed_units": ["PBR-1"],
        "message": "light and full pass disagree", "history": []}}, "failed", "light_full_mismatch"),
    ({"status": "JARVIS_SOLVE_TIMEOUT", "action": "run", "error": "DWSIM did not finish",
      "error_detail": {"step": "dwsim_solve_run"}}, "failed", "JARVIS_SOLVE_TIMEOUT"),
    ({"status": "native_reload_failed", "action": "run"}, "failed", "native_reload_failed"),
    ({"status": "runtime_failed", "action": "run", "error": "RuntimeError"}, "failed", "runtime_failed"),
    ({"status": "something_new", "action": "run"}, "failed", "something_new"),
])
def test_every_known_status_maps_to_exactly_one_outcome(run: dict[str, Any], state: str, reason: str | None) -> None:
    outcome = draft.run_outcome({"run_id": "r", **run})
    assert outcome["state"] == state and outcome["reason"] == reason
    assert outcome["label"] == results_view.OUTCOME_LABELS[state]
    assert set(outcome) == {"state", "label", "reason", "message", "failing", "residual", "iterations",
                            "worst_tear", "results_available", "artifact"}
    if state == "converged":
        assert outcome["results_available"] == "converged"
    else:
        assert outcome["results_available"] in {"none", "last_iterate"}


def test_outcome_diagnostics_are_carried_and_unknowns_stay_null() -> None:
    mixed = draft.run_outcome({"status": "unconverged", "action": "run", "run_id": "r",
                               "streams": {"S": _stream_result(1.0)}, "mixed_solve": {
                                   "reason": "wall_budget", "history": [
                                       {"max_normalized_residual": 9.0, "worst_tear": "A"},
                                       {"max_normalized_residual": 6.0, "worst_tear": "Return"}]}})
    assert (mixed["residual"], mixed["iterations"], mixed["worst_tear"]) == (6.0, 2, "Return")
    assert mixed["results_available"] == "last_iterate" and mixed["artifact"] == "runs/r/"
    assert "wall_budget" in mixed["message"]
    failed = draft.run_outcome({"status": "failed", "action": "run", "run_id": "r", "solved_case_sha256": "x",
                                "solve": {"ok": False, "errors": ["Infinite loop detected"],
                                          "failed_objects": [_FAILED_OBJECT]}})
    assert failed["failing"] == [{"tag": "H1", "stage": "dwsim_solve", "error": "not calculated"}]
    assert failed["message"] == "Infinite loop detected" and failed["artifact"] == "runs/r/solved.dwxmz"
    assert (failed["residual"], failed["iterations"], failed["worst_tear"]) == (None, None, None)
    segment = draft.run_outcome({"status": "segment_failed", "action": "run", "mixed_solve": {
        "reason": "validation_failed", "failed_segment": "mixed-validate-0", "history": [],
        "errors": {"materialization_diffs": [{"path": "objects.M.y", "expected": -21, "actual": 50}]}}})
    assert segment["failing"] == [{"tag": None, "stage": "mixed-validate-0",
                                   "error": "1 materialization difference(s); first objects.M.y: draft expects -21, "
                                            "DWSIM holds 50"}]
    timeout = draft.run_outcome({"status": "JARVIS_SOLVE_TIMEOUT", "action": "run", "error": "DWSIM did not finish",
                                 "error_detail": {"step": "dwsim_solve_run"}})
    assert timeout["failing"] == [{"tag": None, "stage": "dwsim_solve_run", "error": "DWSIM did not finish"}]
    assert timeout["results_available"] == "none"


def _recycle_document(feed_kg_s: float = 0.01) -> dict[str, Any]:
    feed = _stream("f", "Feed", target="m")
    feed["spec"] = {"mass_flow": {"si": feed_kg_s}}
    objects = [feed, _unit("m", "Mixer", "MIX"), _unit("r", "Recycle", "REC"), _stream("p", "Product", "m", None)]
    return {"name": "t", "compounds": ["Water"], "property_package": "NRTL",
            "objects": {item["id"]: item for item in objects}}


def _recycle_run(error_kg_h: str, tolerance: float | None = None) -> dict[str, Any]:
    unit: dict[str, Any] = {"calculated": True, "error": "",
                            "reported": {"Mass Flow Error": {"value": error_kg_h, "units": "kg/h"}}}
    if tolerance is not None:
        unit["mass_flow_tolerance_kg_s"] = tolerance
    return {"status": "completed", "action": "run", "run_id": "r", "draft_revision": REV,
            "solve": {"ok": True, "errors": [], "failed_objects": []}, "units": {"REC": unit}}


def test_completed_run_with_native_recycle_error_above_its_tolerance_is_not_converged() -> None:
    document = _recycle_document()  # compiled tolerance 1e-5 x 0.01 kg/s = 1e-7 kg/s = 3.6e-4 kg/h
    within = draft.run_outcome(_recycle_run("0.00032"), document)
    assert within["state"] == "converged"
    # Spec 181 fact 7: DWSIM's default 36 kg/h tolerance "converged" a 0.01 kg/s loop with a 0.0276 kg/h error.
    legacy = draft.run_outcome(_recycle_run("0.027647999999835804"), document)
    assert legacy["state"] == "non_converged" and legacy["reason"] == "NATIVE_RECYCLE_NOT_CONVERGED"
    assert legacy["failing"][0]["tag"] == "REC" and legacy["failing"][0]["stage"] == "native_recycle"
    assert legacy["results_available"] == "last_iterate"
    stored = draft.run_outcome(_recycle_run("0.0005", tolerance=2e-7))
    assert stored["state"] == "converged"  # the stored compiled tolerance needs no document
    assert draft.run_outcome(_recycle_run("0.0009", tolerance=2e-7))["state"] == "non_converged"
    # A legacy mixed run records no per-segment tolerance: no invented bound.
    mixed_legacy = {**_recycle_run("0.5"), "mixed_solve": {"status": "completed", "history": []}}
    assert draft.run_outcome(mixed_legacy, document)["state"] == "converged"


def test_stored_outcome_is_authoritative_and_legacy_runs_compute_on_read() -> None:
    run = _recycle_run("0.027647999999835804")
    assert results_view.outcome_of(run, _recycle_document())["state"] == "non_converged"
    stored = {**run, "outcome": {"state": "converged", "label": "Converged"}}
    assert results_view.outcome_of(stored)["state"] == "converged"


# ---------------------------------------------------------------- results state


def _solved_and_failed() -> tuple[dict[str, Any], dict[str, Any]]:
    solved = {"run_id": "solved", "action": "run", "status": "completed", "draft_revision": REV,
              "started_at": "2026-01-01T00:00:00+00:00", "materialization_fingerprint": "fp",
              "solve": {"ok": True, "errors": [], "failed_objects": []}}
    failed = {"run_id": "later", "action": "run", "status": "failed", "draft_revision": REV,
              "started_at": "2026-01-02T00:00:00+00:00",
              "solve": {"ok": False, "errors": ["no convergence"], "failed_objects": [_FAILED_OBJECT]}}
    return solved, failed


def test_results_state_demotes_older_converged_run_after_a_later_non_mixed_failed_attempt() -> None:
    solved, failed = _solved_and_failed()
    head = {"revision": REV, "seq": 3}
    assert draft.results_state(head, [solved])["state"] == "current"
    state = draft.results_state(head, [failed, solved])
    assert state["state"] == "stale" and state["run_id"] == "solved"
    assert state["last_attempt"]["outcome"]["state"] == "non_converged"
    assert state["outcome"]["reason"] == "DWSIM_SOLVE_NOT_CONVERGED"
    runtime = {**failed, "status": "runtime_failed", "solve": None, "error": "RuntimeError"}
    assert draft.results_state(head, [runtime, solved])["state"] == "stale"
    # A validate attempt is not a Run and never demotes a converged answer.
    validate = {**failed, "action": "validate", "status": "check_failed"}
    assert draft.results_state(head, [validate, solved])["state"] == "current"


def test_a_completed_run_that_is_not_converged_is_never_the_current_answer() -> None:
    head = {"revision": REV, "seq": 3}
    run = {**_recycle_run("0.027647999999835804", tolerance=1e-7), "started_at": "2026-01-01T00:00:00+00:00"}
    state = draft.results_state(head, [run])
    assert state["state"] == "none" and state["last_attempt"]["outcome"]["state"] == "non_converged"


# ---------------------------------------------------------------- results view and KPIs


def _kpi(view: dict[str, Any], kpi_id: str) -> dict[str, Any]:
    return next(item for item in view["kpis"] if item["id"] == kpi_id)


def test_converged_view_streams_units_boundary_and_kpis() -> None:
    view = results_view.build(_converged_run(), _document())
    assert view["label"] == "current" and view["value_label"] is None
    assert view["outcome"]["state"] == "converged" and view["outcome"]["iterations"] == 2
    feed = view["streams"]["Feed"]
    assert feed["role"] == "feed" and feed["to"] == "PBR-1" and feed["from"] is None
    assert feed["temperature"] == {"value": 25.0, "unit": "degC"}
    assert feed["mass_flow"] == {"value": 36.0, "unit": "kg/h"}
    assert feed["volumetric_flow"]["value"] is None and feed["volumetric_flow"]["status"] == "unavailable"
    assert feed["mass_fractions"]["Water"] == {"value": 1.0, "unit": "kg/kg"}
    assert feed["culture"]["biomass"] == {"value": 0.0, "unit": "kg/m3"}  # a real zero stays the number 0
    assert view["streams"]["Concentrate"]["role"] == "product"
    pbr = view["units"]["PBR-1"]
    assert pbr["inlets"] == ["Feed"] and pbr["outlets"] == ["Broth"]
    assert pbr["quantities"]["pressure_drop"]["value"] == 0.0
    assert pbr["quantities"]["lambda_h"] == {"value": None, "status": "not_computed", "reason": "reported without a value"}
    assert view["units"]["SEP-1"]["outlets"] == ["Concentrate", "Clarified"]
    assert view["boundary"]["inputs"] == ["Feed"] and view["boundary"]["outputs"] == ["Clarified", "Concentrate"]
    assert view["boundary"]["totals"]["mass_flow_in"] == {"value": pytest.approx(36.0), "unit": "kg/h"}
    assert view["boundary"]["totals"]["by_compound_out"]["Water"]["value"] == pytest.approx(36.0)
    assert view["balances"]["mass"]["passed"] is True

    product = _kpi(view, "biomass_product_rate")  # 18 kg/m3 x 0.001 kg/s / 1000 kg/m3 x 86400 s/d (+ zero)
    assert product["status"] == "available" and product["value"] == pytest.approx(1.5552) and product["unit"] == "kg/d"
    assert product["sources"]
    assert _kpi(view, "pbr_volumetric_productivity:PBR-1")["value"] == 0.25
    assert _kpi(view, "pbr_net_biomass_production:PBR-1")["value"] == 2.0
    area = _kpi(view, "pbr_projected_area_productivity:PBR-1")
    assert area["value"] == pytest.approx(2.0 / (10 * 0.05 * 20)) and "not ground footprint" in area["label"]
    assert _kpi(view, "culture_throughput:PBR-1")["value"] == 0.036
    assert _kpi(view, "outlet_biomass_concentration:Concentrate")["value"] == 18.0
    assert _kpi(view, "outlet_biomass_concentration:Clarified")["value"] == 0.0
    # 18 kg/m3 x 0.001 kg/s over 2 kg/m3 x 0.01 kg/s
    assert _kpi(view, "separator_biomass_recovery:SEP-1")["value"] == pytest.approx(0.9)
    assert _kpi(view, "water_input")["value"] == pytest.approx(36.0)
    assert _kpi(view, "nitrogen_input")["value"] == pytest.approx(0.05 * 0.01 / 1000 * 86400)
    assert _kpi(view, "mass_balance_closure")["value"] == pytest.approx(1e-7)
    for kpi_id in ("co2_feed", "co2_uptake", "co2_per_biomass"):
        co2 = _kpi(view, kpi_id)
        assert co2["status"] == "unavailable" and co2["value"] is None
        assert co2["reason"] == results_view.NO_CARBON_REASON
    for item in view["kpis"]:
        assert set(item) == {"id", "label", "value", "unit", "status", "reason", "definition", "sources"}


def test_kpis_are_unavailable_with_reasons_when_sources_are_missing() -> None:
    document = _document()
    del document["objects"]["pbr"]["params"]["tube_length"]
    document["compounds"] = ["Ethanol"]
    run = _converged_run()
    run["culture"]["Concentrate"] = _culture(None)
    del run["culture"]["Clarified"]
    view = results_view.build(run, document)
    assert _kpi(view, "pbr_projected_area_productivity:PBR-1")["reason"] == "missing PBR geometry: tube_length"
    assert _kpi(view, "water_input")["reason"] == "Water is not a draft compound"
    assert _kpi(view, "biomass_product_rate")["status"] == "unavailable"
    concentration = _kpi(view, "outlet_biomass_concentration:Concentrate")
    assert concentration["status"] == "unavailable" and concentration["reason"] == "not specified on feed Feed"
    assert _kpi(view, "outlet_biomass_concentration:Clarified")["reason"] == "the stream carries no culture values"
    assert _kpi(view, "separator_biomass_recovery:SEP-1")["status"] == "unavailable"
    assert view["streams"]["Concentrate"]["culture"]["biomass"]["status"] == "unavailable"
    assert view["streams"]["Clarified"]["culture"] is None
    del run["units"]["PBR-1"]["reported"]["volumetric_productivity"]
    view = results_view.build(run, document)
    assert _kpi(view, "pbr_volumetric_productivity:PBR-1")["reason"] == "not reported by the PBR evaluator"
    run.pop("mixed_solve")
    run["mass_balance"] = {"status": "unavailable", "error": "X"}
    assert _kpi(results_view.build(run, document), "mass_balance_closure")["status"] == "unavailable"


def test_non_converged_view_is_labelled_last_iterate_and_has_no_kpis() -> None:
    run = _converged_run()
    run["status"] = "unconverged"
    run["mixed_solve"]["reason"] = "max_iterations"
    view = results_view.build(run, _document())
    assert view["outcome"]["state"] == "non_converged" and view["label"] == "last_iterate"
    assert view["value_label"] == results_view.LAST_ITERATE_LABEL
    assert view["streams"]["Feed"]["mass_flow"]["label"] == results_view.LAST_ITERATE_LABEL
    assert view["streams"]["Concentrate"]["culture"]["biomass"]["label"] == results_view.LAST_ITERATE_LABEL
    assert view["units"]["PBR-1"]["quantities"]["volumetric_productivity"]["label"] == results_view.LAST_ITERATE_LABEL
    for item in view["kpis"]:
        assert item["status"] == "unavailable" and item["value"] is None
        assert item["reason"] in {results_view.NOT_CONVERGED_REASON, results_view.NO_CARBON_REASON}
    assert view["boundary"]["totals"]["mass_flow_in"]["status"] == "unavailable"


def test_failed_and_validated_views_are_not_solved() -> None:
    failed = {"run_id": "f", "action": "run", "status": "segment_failed", "draft_revision": REV,
              "mixed_solve": {"reason": "segment_failed", "failed_units": ["PBR-1"], "history": []}}
    view = results_view.build(failed, _document())
    assert view["label"] == "not_solved" and view["streams"] == {} and view["units"] == {}
    assert view["outcome"]["state"] == "failed"
    validated = results_view.build({"run_id": "v", "action": "validate", "status": "validated",
                                    "draft_revision": REV}, _document())
    assert validated["label"] == "not_solved" and validated["outcome"]["state"] == "validated"
    # A failed run that persisted values shows them only as last iterate.
    persisted = {**copy.deepcopy(_converged_run()), "status": "failed",
                 "solve": {"ok": False, "errors": ["x"], "failed_objects": []}}
    persisted.pop("mixed_solve")
    view = results_view.build(persisted, _document())
    assert view["label"] == "last_iterate"
    assert view["streams"]["Feed"]["mass_flow"]["label"] == results_view.LAST_ITERATE_LABEL


def test_results_route_projects_a_persisted_run_against_its_own_revision(tmp_path: Any) -> None:
    from fastapi.testclient import TestClient

    from app.main import app
    from tests.plumbing_170_support import new_workspace

    workspace_id = new_workspace()
    client = TestClient(app)
    state = draft.create_draft(workspace_id, "results route")
    directory = draft.draft_dir(workspace_id, state["draft_id"])
    run = {"run_id": "d" * 32, "action": "run", "status": "unconverged", "draft_revision": state["revision"],
           "started_at": "2026-01-01T00:00:00+00:00", "streams": {}, "units": {},
           "mixed_solve": {"reason": "max_iterations", "history": [{"max_normalized_residual": 2.0}]}}
    draft.record_run(directory, run)
    base = f"/workspaces/{workspace_id}/process/drafts/{state['draft_id']}/runs/{'d' * 32}"
    response = client.get(base + "/results")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"]["state"] == "non_converged" and body["outcome"]["residual"] == 2.0
    assert client.get(base).json()["outcome"]["state"] == "non_converged"
    assert client.get(base[:-32] + "e" * 32 + "/results").status_code == 404
    summaries = client.get(f"/workspaces/{workspace_id}/process/drafts/{state['draft_id']}/runs").json()
    assert summaries[0]["outcome"]["state"] == "non_converged"


@pytest.mark.skipif(not os.environ.get("JARVISOS_DWSIM_MCP_PATH"), reason="set JARVISOS_DWSIM_MCP_PATH to opt in to DWSIM runtime")
def test_real_dwsim_runs_store_their_outcome_and_results_view() -> None:
    from app.modules.process_stack.draft_models import (
        AddStream,
        AddUnit,
        Connect,
        DraftQuantity,
        SetStreamSpec,
        SetThermo,
        SetUnitParams,
    )
    from tests.plumbing_170_support import new_workspace

    q = DraftQuantity
    workspace_id = new_workspace()
    state = draft.create_draft(workspace_id, "181 heater")
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        SetThermo(op="set_thermo", compounds=["Water"], property_package="NRTL"),
        AddUnit(op="add_unit", id="h", type="Heater", tag="H1", x=100, y=0),
        AddStream(op="add_stream", id="f", tag="Feed", x=0, y=0),
        AddStream(op="add_stream", id="p", tag="Product", x=200, y=0),
        Connect(op="connect", stream="f", end="target", unit="h", port=0),
        Connect(op="connect", stream="p", end="source", unit="h", port=0),
        SetStreamSpec(op="set_stream_spec", stream="f", pressure=q(value=1.0, unit="bar"),
                      temperature=q(value=300, unit="K"), mass_flow=q(value=1.0, unit="kg/s"),
                      composition={"Water": 1.0}, composition_basis="mass"),
        SetUnitParams(op="set_unit_params", unit="h", mode="outlet_temperature",
                      values={"outlet_temperature": q(value=330, unit="K")}),
    ])
    run = draft.execute(workspace_id, state["draft_id"], state["revision"], "run")["run"]
    assert run["outcome"]["state"] == "converged", run.get("outcome")
    view = draft.run_results(workspace_id, state["draft_id"], run["run_id"])
    assert view["label"] == "current"
    assert view["streams"]["Product"]["temperature"]["value"] == pytest.approx(56.85, abs=1e-3)
    assert next(item for item in view["kpis"] if item["id"] == "water_input")["value"] == pytest.approx(3600.0)
