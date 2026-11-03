from __future__ import annotations

from typing import Any

import pytest

from app.modules.process_stack import dynamic_downstream


def _snapshot() -> Any:
    pbr = {"id": "pbr", "kind": "unit", "type": "PhotobioreactorT1", "tag": "PBR", "params": {}}
    heater = {"id": "heater", "kind": "unit", "type": "Heater", "tag": "Heater", "params": {}}
    product = {"id": "product", "kind": "stream", "type": "MaterialStream", "tag": "CultureOut",
               "source": {"unit": "pbr", "port": "out"}, "target": {"unit": "heater", "port": "in"},
               "spec": {"temperature": {"si": 300.0}, "pressure": {"si": 101325.0}}}
    outlet = {"id": "outlet", "kind": "stream", "type": "MaterialStream", "tag": "Product",
              "source": {"unit": "heater", "port": "out"}, "target": None, "spec": {}}
    feed = {"id": "feed", "kind": "stream", "type": "MaterialStream", "tag": "Feed",
            "source": None, "target": {"unit": "pbr", "port": "in"},
            "spec": {"mass_flow": {"si": 0.01}, "culture": {"biomass": {"si": 0.1}}}}

    class Snapshot:
        payload = {"document": {"schema_version": 1, "name": "downstream", "compounds": ["Water"],
                                 "property_package": "NRTL", "objects": {x["id"]: x for x in
                                 (pbr, heater, product, outlet, feed)}, "reactions": {}},
                   "units": [{"unit": pbr}], "topology": {"streams": [feed], "flows": {"feed": 1e-5}},
                   "start_epoch": 1798761600.0}

    return Snapshot()


def _boundary() -> dict[str, Any]:
    return {"time_s": 86400.0, "streams": {"CultureOut": {
        "flow_m3_s": 0.001, "concentrations": {"X": 0.2, "N": 0.1, "O2": 0.01}}}}


def test_downstream_timeout_is_a_sample_outcome_not_an_exception() -> None:
    sampler = dynamic_downstream.build_sampler(_snapshot(), client_factory=lambda: object(),
        runner=lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("late")))
    assert sampler is not None
    outcome = sampler(_boundary())
    assert outcome["status"] == "downstream_unconverged"
    assert outcome["reason"] == "timeout"
    assert outcome["time_s"] == 86400.0
    assert outcome["retry_skipped"]["reason"] == "initial solve raised before a retry guess was available"
    assert outcome["time_utc"] == "2027-01-02T00:00:00Z"


def test_nonconvergence_retries_from_midpoint_and_reports_missing_separately_from_zero() -> None:
    calls: list[dict[str, Any]] = []

    def run(document: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if len(calls) == 1:
            assert kwargs["include_tear_state"] is True
            return {"status": "failed", "_last_attempt": {
                "guess": {"Tear": {"mass_flow_kg_s": 2.0, "culture": {"X": 2.0}}},
                "output": {"Tear": {"mass_flow_kg_s": 4.0, "culture": {"X": 6.0}}}}}
        assert kwargs["initial_tear"]["Tear"]["mass_flow_kg_s"] == 3.0
        assert kwargs["initial_tear"]["Tear"]["culture"]["X"] == 4.0
        assert kwargs["include_tear_state"] is True
        assert document["objects"]["product"]["source"] is None
        assert document["objects"]["product"]["spec"]["culture"]["biomass"]["si"] == 0.2
        assert document["objects"]["product"]["spec"]["mass_flow"]["si"] == pytest.approx(1.0)
        return {"status": "completed", "streams": {
            "CultureOut": {"mass_flow_kg_s": 1.0, "temperature_K": 300.0},
            "Product": {"mass_flow_kg_s": 0.0, "temperature_K": None}},
            "units": {"Heater": {"properties": {"DeltaQ": 2.5}}}}

    sampler = dynamic_downstream.build_sampler(_snapshot(), client_factory=lambda: object(), runner=run)
    assert sampler is not None
    result = sampler(_boundary())
    assert result["status"] == "succeeded"
    assert result["time_s"] == 86400.0
    # The solved outlet keeps its reported zero; the boundary feed is reported as Jarvis input, not a result.
    assert result["streams"] == {"Product": {"mass_flow_kg_s": 0.0, "temperature_K": None,
                                             "provenance": "dwsim_solved", "owner": "dwsim"}}
    assert result["units"] == {"Heater": {"properties": {"DeltaQ": 2.5}, "provenance": "dwsim_solved"}}
    boundary = result["boundary_inputs"]["CultureOut"]
    assert boundary["provenance"] == "jarvis_t1_snapshot"
    assert boundary["flow_m3_s"] == pytest.approx(0.001)
    assert boundary["mass_flow_kg_s"] == pytest.approx(1.0)
    assert boundary["concentrations_kg_m3"] == {"biomass": 0.2, "nitrogen": 0.1, "oxygen": 0.01}
    assert result["boundary_density_sources"]["CultureOut"]["rho_kg_m3"] == pytest.approx(1000.0)
    assert result["boundary_density_sources"]["CultureOut"]["source"] == "declared_mass_flow_over_volumetric_flow"


def test_double_nonconvergence_records_residual_and_timestamp(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dynamic_downstream.mixed_runtime, "_seed", lambda *_args, **_kwargs: None)
    calls: list[dict[str, Any]] = []

    def run(*_args: Any, **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if kwargs.get("initial_tear") is not None:
            assert kwargs["include_tear_state"] is True
            return {"status": "failed", "mixed_solve": {"reason": "max_iterations", "history": [
                {"max_normalized_residual": 0.25}]}}
        return {"status": "failed", "_last_attempt": {
            "guess": {"Tear": {"mass_flow_kg_s": 2.0}},
            "output": {"Tear": {"mass_flow_kg_s": 4.0}}},
            "mixed_solve": {"reason": "max_iterations", "history": [{"max_normalized_residual": 0.5}]}}

    sampler = dynamic_downstream.build_sampler(_snapshot(), client_factory=lambda: object(), runner=run)
    assert sampler is not None
    result = sampler(_boundary())
    assert result["status"] == "downstream_unconverged"
    assert result["residual"] == 0.25
    assert result["time_s"] == 86400.0
    assert result["time_utc"] == "2027-01-02T00:00:00Z"
    assert len(calls) == 2
    assert "retry_skipped" not in result
    assert result["diagnostics"] == {"solve_status": "failed", "solve_reason": "max_iterations", "iterations": 1}


def test_segment_failure_is_not_reported_as_nonconvergence_and_keeps_its_cause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dynamic_downstream.mixed_runtime, "_seed", lambda *_args, **_kwargs: None)

    def run(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"status": "segment_failed", "mixed_solve": {
            "status": "segment_failed", "reason": "segment_failed", "failed_segment": "mixed-1-0",
            "failed_units": ["Mixer", "Split"], "message": "x" * 900, "history": []}}

    sampler = dynamic_downstream.build_sampler(_snapshot(), client_factory=lambda: object(), runner=run)
    assert sampler is not None
    result = sampler(_boundary())
    assert result["status"] == "downstream_unconverged" and result["reason"] == "segment_failed"
    assert result["residual"] is None and result["retry_skipped"] == {"reason": "no_retry_guess"}
    diagnostics = result["diagnostics"]
    assert diagnostics["failed_segment"] == "mixed-1-0" and diagnostics["failed_units"] == ["Mixer", "Split"]
    assert diagnostics["solve_status"] == "segment_failed" and len(diagnostics["message"]) == 500


def test_successful_sampler_flags_assumed_density() -> None:
    snapshot = _snapshot()
    snapshot.payload["document"]["objects"]["feed"]["spec"].pop("mass_flow")
    sampler = dynamic_downstream.build_sampler(
        snapshot, client_factory=lambda: object(),
        runner=lambda *_args, **_kwargs: {"status": "completed", "streams": {}},
    )
    assert sampler is not None
    result = sampler(_boundary())
    assert result["status"] == "succeeded"
    assert result["density_assumed"] is True
    assert result["density_assumption_reason"] == "water_carrier_default_1000_kg_m3"
    assert result["boundary_density_sources"]["CultureOut"]["density_assumed"] is True


def test_boundary_state_feed_does_not_invent_thermodynamic_defaults() -> None:
    original = {"tag": "CultureOut", "spec": {}}
    state = {"flow_m3_s": 0.001, "concentrations": {"X": 0.2, "N": 0.1, "O2": 0.01}}
    feed = dynamic_downstream._state_feed(original, state, 1000.0, {})
    assert "temperature" not in feed["spec"]
    assert "pressure" not in feed["spec"]
    assert "composition" not in feed["spec"]


def test_solved_stream_missing_from_dwsim_is_none_not_an_echoed_boundary() -> None:
    sampler = dynamic_downstream.build_sampler(
        _snapshot(), client_factory=lambda: object(),
        runner=lambda *_args, **_kwargs: {"status": "completed", "streams": {
            "CultureOut": {"mass_flow_kg_s": 1.0}}},
    )
    assert sampler is not None
    result = sampler(_boundary())
    assert result["status"] == "succeeded"
    assert result["streams"] == {"Product": None}
    assert "CultureOut" not in result["streams"]
    assert result["boundary_inputs"]["CultureOut"]["provenance"] == "jarvis_t1_snapshot"
