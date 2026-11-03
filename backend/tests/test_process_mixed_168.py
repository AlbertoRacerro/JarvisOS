from __future__ import annotations

import copy
import os
import time

import pytest

from app.modules.process_stack import draft, draft_compiler, mixed, mixed_runtime


def _unit(uid: str, tag: str, kind: str) -> dict:
    return {"id": uid, "kind": "unit", "tag": tag, "type": kind,
            "mode": "specified" if kind == "SpecifiedSeparator" else None,
            "params": ({"biomass_recovery": {"si": 90.0}, "concentration_factor": {"si": 10.0}}
                       if kind == "SpecifiedSeparator" else {}), "options": {}, "reactions": []}


def _stream(uid: str, tag: str, source: str | None = None,
            target: str | None = None, source_port: int = 0, target_port: int = 0) -> dict:
    return {"id": uid, "kind": "stream", "tag": tag, "type": "MaterialStream",
            "source": {"unit": source, "port": source_port} if source else None,
            "target": {"unit": target, "port": target_port} if target else None,
            "spec": {"temperature": {"si": 298.15}, "pressure": {"si": 101325.0},
                     "mass_flow": {"si": 1.0}, "composition_basis": "mass", "composition": {"Water": 1.0},
                     "culture": {"biomass": {"si": 1.0}, "salinity": {"si": 35.0}}}
            if source is None else {}}


def _tear_document() -> dict:
    objects = {
        "feed": _stream("feed", "Feed", target="m"),
        "m": _unit("m", "Mixer", "Mixer"),
        "m_j": _stream("m_j", "M_to_J", "m", "j"),
        "j": _unit("j", "Separator", "SpecifiedSeparator"),
        "j_s": _stream("j_s", "Concentrate", "j", "s", source_port=0),
        "s": _unit("s", "Splitter", "Splitter"),
        "j_b": _stream("j_b", "Clarified", "j", "s", source_port=1, target_port=0),
        "s_r": _stream("s_r", "Return", "s", "r", source_port=1),
        "r": _unit("r", "Recycle", "Recycle"),
        "r_m": _stream("r_m", "Tear", "r", "m"),
        "product": _stream("product", "Product", "s", None, source_port=0),
    }
    return {"schema_version": 1, "name": "mixed test", "compounds": ["Water"],
            "property_package": "NRTL", "objects": objects, "reactions": {}}


def _state(value: float = 0.0) -> dict:
    return {"temperature_K": 298.15, "pressure_Pa": 200000.0, "mass_flow_kg_s": 1.0,
            "mass_fractions": {"Water": value}, "vapor_fraction": 0.0,
            "culture": {field: 0.0 for field in mixed.CULTURE_FIELDS}}


def test_jarvis_evaluator_interface_keeps_separator_behavior_and_run_context() -> None:
    unit = _unit("j", "Separator", "SpecifiedSeparator")
    inlet = _state()
    inlet["culture"]["biomass"] = 0.001
    inlet["density_kg_m3"] = 1000.0
    context = mixed_runtime.JarvisUnitContext(1000.0, time.monotonic() + 10, {})
    evaluation = mixed_runtime.JARVIS_EVALUATORS["SpecifiedSeparator"](unit, inlet, context)
    expected = mixed.separator(inlet, 90.0, 10.0)
    assert evaluation.outlets == {"concentrate": expected[0], "clarified": expected[1]}
    assert evaluation.culture_generation == {}
    assert evaluation.result["evaluator"] == "jarvis.specified_separator"
    assert 0 < context.remaining_s() <= 10
    context.cache["per_run_key"] = 1
    assert context.cache["per_run_key"] == 1


def test_internal_feed_flash_exception_requires_exact_one_feed_and_one_finding() -> None:
    feed = _stream("f", "FlashFeed")
    document = {"objects": {"f": feed}}
    check = {"ready": False, "blockers": 1, "warnings": 0,
             "findings": [{"code": "STREAM_DANGLING", "severity": "blocker", "object": "FlashFeed"}]}
    allowed = draft_compiler.isolated_feed_flash_check
    assert allowed(document, "run", check, allowed=True)
    assert not allowed(document, "run", check, allowed=False)
    assert not allowed(document, "validate", check, allowed=True)
    assert not allowed({"objects": {"f": feed, "extra": _stream("extra", "Extra")}}, "run", check, allowed=True)
    assert not allowed(document, "run", {**check, "findings": [*check["findings"],
                                                         {"code": "PROPERTY_INVALID", "object": "FlashFeed"}]}, allowed=True)
    assert not allowed(document, "run", {**check, "findings": [{**check["findings"][0],
                                                                   "object": "Other"}]}, allowed=True)


@pytest.mark.parametrize("full", [False, True])
def test_flash_opts_in_and_retains_real_unready_check(full: bool, monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_path(document: dict, _client: object, **kwargs: object) -> dict:
        assert kwargs["allow_isolated_feed_flash"] is True
        assert len(document["objects"]) == 1
        return {"status": "completed", "streams": {"FlashFeed": {"mass_flow_kg_s": 1.0}},
                "dwsim_check": {"ready": False, "intentional_isolated_feed_exception": True}}

    monkeypatch.setattr(mixed_runtime, "_full" if full else "_light", fake_path)
    result = mixed_runtime._flash(_state(1.0), "FlashFeed", None, label="test", dwsim_version="10.2.9",
                                  mcp_sha256="test", property_package="NRTL", full=full, remaining_s=10)
    assert result["flash_check_exception"] == "STREAM_DANGLING"


def test_jarvis_declared_generation_closes_unit_culture_balance() -> None:
    document = _tear_document()
    # A productive Jarvis unit can declare the growth rate instead of being
    # rejected by the separator's strictly conserved biomass balance.
    document["objects"] = {key: value for key, value in document["objects"].items()
                           if key in {"feed", "j", "j_s", "j_b"}}
    document["objects"]["feed"]["target"] = {"unit": "j", "port": 0}
    document["objects"]["j_s"]["target"] = None
    document["objects"]["j_b"]["target"] = None
    inlet = _state()
    inlet["culture"]["biomass"] = 1.0
    concentrate = copy.deepcopy(inlet)
    concentrate["mass_flow_kg_s"] = 0.5
    concentrate["culture"]["biomass"] = 3.0
    clarified = copy.deepcopy(inlet)
    clarified["mass_flow_kg_s"] = 0.5
    clarified["culture"]["biomass"] = 1.0
    known = {"Feed": inlet, "Concentrate": concentrate, "Clarified": clarified}
    balance = mixed_runtime._culture_balances(document, known, {"Separator": {"biomass": 1.0}})
    assert balance["Separator"]["biomass"]["passed"]
    assert balance["Separator"]["biomass"]["generated"] == 1.0


@pytest.mark.parametrize("flow,concentration", [(1e6, 1e-6), (1e-9, 100.0)])
def test_whole_graph_tear_culture_tolerance_has_flux_units(flow: float, concentration: float) -> None:
    feed = _stream("f", "Feed")
    product = _stream("p", "Product", source="u")
    document = {"objects": {"f": feed, "p": product, "u": _unit("u", "Unit", "Heater")}}
    state = _state()
    state["mass_flow_kg_s"] = flow
    state["culture"]["biomass"] = concentration
    final = {"known": {"Feed": state, "Product": state}, "units": {}, "culture_generation": {}}
    result = mixed_runtime._whole_graph_balances(document, final, {"Tear": state})
    delta_m = mixed.tolerance("mass_flow_kg_s", flow)
    delta_c = mixed.tolerance("biomass", concentration)
    expected = flow * delta_c + concentration * delta_m + delta_m * delta_c
    baseline = 1e-9 * abs(flow * concentration) + 1e-12
    assert result["balances"]["biomass"]["tolerance"] == pytest.approx(expected + baseline)
    if flow < 1e-6:
        assert result["balances"]["biomass"]["tolerance"] < 1e-6


def test_whole_graph_salinity_tear_tolerance_converts_g_per_kg_to_kg_per_s() -> None:
    feed = _stream("f", "Feed")
    product = _stream("p", "Product", source="u")
    document = {"objects": {"f": feed, "p": product, "u": _unit("u", "Unit", "Heater")}}
    state = _state()
    state["culture"]["salinity"] = 35.0
    final = {"known": {"Feed": state, "Product": state}, "units": {}, "culture_generation": {}}
    row = mixed_runtime._whole_graph_balances(document, final, {"Tear": state})["balances"]["salinity"]
    delta_m = mixed.tolerance("mass_flow_kg_s", 1.0)
    delta_c = mixed.tolerance("salinity", 35.0)
    expected = (delta_c + 35.0 * delta_m + delta_m * delta_c) * 0.001 + 1e-9 * 0.035 + 1e-12
    assert row["tolerance"] == pytest.approx(expected)


def test_jarvis_outlet_culture_shows_evaluator_fidelity_in_operator_result() -> None:
    feed = _stream("f", "Feed", target="j")
    jarvis = _unit("j", "Separator", "SpecifiedSeparator")
    outlet = _stream("o", "Concentrate", source="j")
    document = {"objects": {"f": feed, "j": jarvis, "o": outlet}}
    state = _state()
    state["culture"]["biomass"] = 0.001
    dwsim_stream = {"phases": [{"name": "Mixture", "density_kg_m3": 1000.0}]}
    final = {"known": {"Feed": state, "Concentrate": state},
             "streams": {"Feed": dwsim_stream, "Concentrate": dwsim_stream},
             "units": {"Separator": {"fidelity": "screening — specified performance",
                                     "caveats": ["Dissolved species follow the carrier."],
                                     "evaluator": "jarvis.specified_separator", "version": 1}},
             "culture_balances": {}}
    culture_results = mixed_runtime._culture_results(document, final)
    assert culture_results["Concentrate"]["fidelity"] == "screening — specified performance"
    assert "Dissolved species follow the carrier." in culture_results["Concentrate"]["caveats"]
    assert culture_results["Concentrate"]["evaluator"] == "jarvis.specified_separator"
    assert culture_results["Feed"]["fidelity"] == mixed_runtime.culture.FIDELITY


def test_jarvis_generation_declares_field_specific_rate_units() -> None:
    unit = _unit("j", "Separator", "SpecifiedSeparator")
    outlet = _state()
    result = mixed_runtime.JarvisUnitEvaluation(
        {"concentrate": outlet, "clarified": outlet}, {},
        {"biomass": 1.0, "dic": 2.0}, {"biomass": "kg/s", "dic": "mol/s"})
    mixed_runtime._check_evaluation(unit, result)
    result.culture_generation_units["dic"] = "kg/s"
    with pytest.raises(mixed_runtime.SegmentFailure, match="JARVIS_GENERATION_INVALID"):
        mixed_runtime._check_evaluation(unit, result)


def test_culture_native_recycle_uses_seed_and_reaches_fixed_point() -> None:
    objects = {
        "f1": _stream("f1", "FeedA", target="m"),
        "f2": _stream("f2", "FeedB", target="m", target_port=1),
        "m": _unit("m", "Mixer", "Mixer"),
        "m_s": _stream("m_s", "Mixed", "m", "s"),
        "s": _unit("s", "Splitter", "Splitter"),
        "s_p": _stream("s_p", "Product", "s", None),
        "s_r": _stream("s_r", "ToRecycle", "s", "r", source_port=1),
        "r": _unit("r", "Recycle", "Recycle"),
        "r_m": _stream("r_m", "Return", "r", "m", target_port=2),
    }
    objects["f1"]["spec"]["culture"]["biomass"]["si"] = 1.0
    objects["f2"]["spec"]["culture"]["biomass"]["si"] = 3.0
    segment = {"objects": objects}
    flows = {"FeedA": 0.5, "FeedB": 0.5, "Mixed": 2.0, "Product": 1.0,
             "ToRecycle": 1.0, "Return": 1.0}
    result = {"streams": {tag: {"mass_flow_kg_s": flow, "vapor_fraction": 0.0,
                               "phases": [{"name": "Mixture", "density_kg_m3": 1000.0}]}
                          for tag, flow in flows.items()}}
    states = mixed_runtime._propagate_culture(segment, result, {})
    assert states["Return"]["biomass"] == pytest.approx(0.002, rel=2e-5)
    assert states["Product"]["biomass"] == pytest.approx(0.002, rel=2e-5)


def test_failed_latest_mixed_run_does_not_reuse_old_current_results() -> None:
    revision = "1:" + "a" * 16
    prior = {"run_id": "prior", "action": "run", "status": "completed", "draft_revision": revision,
             "materialization_fingerprint": None}
    failed = {"run_id": "failed", "action": "run", "status": "unconverged", "draft_revision": revision}
    state = draft.results_state({"revision": revision, "seq": 1}, [failed, prior], _tear_document())
    assert state["state"] == "stale"
    assert state["last_attempt"]["status"] == "unconverged"


def test_final_status_preserves_stronger_failure_and_original_nonconvergence() -> None:
    failed_balance = {"status": "failed"}
    mismatch = {"code": "light_full_mismatch"}
    assert mixed_runtime._final_status("completed", "converged", mismatch, failed_balance) == (
        "segment_failed", "light_full_mismatch")
    assert mixed_runtime._final_status("unconverged", "max_iterations", None, failed_balance) == (
        "unconverged", "max_iterations")
    assert mixed_runtime._final_status("unconverged", "wall_budget", None, failed_balance) == (
        "unconverged", "wall_budget")
    assert mixed_runtime._final_status("completed", "converged", None, failed_balance) == (
        "unconverged", "balance")


def test_jarvis_evaluator_typed_failure_keeps_unit_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    class BiologyFailure(RuntimeError):
        code = "PBR_NO_STEADY_ROOT"
        detail = {"branch": "none"}

    def failing(_unit: dict, _inlet: dict, _context: mixed_runtime.JarvisUnitContext) -> None:
        raise BiologyFailure("no productive branch")

    monkeypatch.setitem(mixed_runtime.JARVIS_EVALUATORS, "SpecifiedSeparator", failing)
    context = mixed_runtime.JarvisUnitContext(1000.0, time.monotonic() + 10, {})
    with pytest.raises(mixed_runtime.SegmentFailure) as captured:
        mixed_runtime._call_evaluator(_unit("j", "Separator", "SpecifiedSeparator"), _state(), context)
    assert captured.value.segment == "Separator"
    assert captured.value.detail == {"code": "PBR_NO_STEADY_ROOT", "error_type": "BiologyFailure",
                                     "message": "no productive branch", "detail": {"branch": "none"}}


def test_partition_consumes_cross_engine_recycle_and_keeps_jarvis_free_loop_native() -> None:
    document = _tear_document()
    part = mixed.partition(document)
    assert part["consumed"] == ["r"]
    assert part["segments"] == [["m"], ["s"]]
    assert "TEAR_CONSUMED" in {row["code"] for row in mixed.validation_findings(document)}
    assert mixed.recycle_free_cycles(document) == []


def test_recycle_free_cycle_is_reported_after_cutting_all_recycle_outputs() -> None:
    document = _tear_document()
    document["objects"].update({
        "s_m": _stream("s_m", "Bypass", "s", "m", source_port=0),
    })
    assert mixed.recycle_free_cycles(document) == [["Mixer", "Separator", "Splitter"]]


def test_native_recycle_side_loop_stays_in_its_dwsim_segment() -> None:
    document = _tear_document()
    document["objects"].update({
        "side_feed": _stream("side_feed", "SideFeed", target="h"),
        "h": _unit("h", "SideHeater", "Heater"),
        "h_r": _stream("h_r", "HeaterToRecycle", "h", "nr"),
        "nr": _unit("nr", "NativeRecycle", "Recycle"),
        "r_h": _stream("r_h", "RecycleToHeater", "nr", "h"),
        "side_product": _stream("side_product", "SideProduct", "h"),
    })
    part = mixed.partition(document)
    assert part["consumed"] == ["r"]
    assert ["nr", "h"] in part["segments"]


def test_native_recycle_connected_to_consumed_loop_segment_is_refused_as_finding() -> None:
    document = _tear_document()
    document["objects"].update({
        "side": _unit("side", "SideMixer", "Mixer"),
        "side_in": _stream("side_in", "SideIn", "s", "side"),
        "side_recycle": _unit("side_recycle", "SideRecycle", "Recycle"),
        "side_back": _stream("side_back", "SideBack", "side_recycle", "side"),
        "side_out": _stream("side_out", "SideOut", "side", "side_recycle", source_port=1),
    })
    codes = {row["code"] for row in mixed.validation_findings(document)}
    assert "NATIVE_RECYCLE_IN_JARVIS_LOOP" in codes
    assert "NATIVE_RECYCLE_IN_JARVIS_LOOP" in {row["code"] for row in draft.validate_document(document)}


def test_separator_findings_require_culture_and_screen_implausible_concentrate() -> None:
    document = _tear_document()
    document["objects"]["feed"]["spec"].pop("culture")
    codes = {row["code"] for row in mixed.validation_findings(document)}
    assert "SEPARATOR_REQUIRES_CULTURE" in codes
    document["objects"]["feed"]["spec"]["culture"] = {"biomass": {"si": 1.0}, "salinity": {"si": 35.0}}
    document["objects"]["j"]["params"]["concentration_factor"]["si"] = 300.0
    codes = {row["code"] for row in mixed.validation_findings(document)}
    assert "SEPARATOR_CONCENTRATE_IMPLAUSIBLE" in codes


def test_energy_stream_cannot_bridge_different_jarvis_levels() -> None:
    document = _tear_document()
    document["objects"]["energy"] = {
        "id": "energy", "kind": "stream", "tag": "Heat", "type": "EnergyStream",
        "source": {"unit": "m", "port": 0}, "target": {"unit": "s", "port": 0}, "spec": {},
    }
    assert "ENERGY_STREAM_CROSSES_JARVIS_LEVEL" in {
        row["code"] for row in mixed.validation_findings(document)}


def test_specified_separator_equations_and_zero_flow() -> None:
    inlet = _state()
    inlet["mass_flow_kg_s"] = 10.0
    inlet["culture"]["biomass"] = 0.001
    concentrate, clarified = mixed.separator(inlet, 90.0, 10.0)
    assert concentrate["mass_flow_kg_s"] == pytest.approx(0.9)
    assert clarified["mass_flow_kg_s"] == pytest.approx(9.1)
    assert concentrate["culture"]["biomass"] == pytest.approx(0.01)
    assert clarified["culture"]["biomass"] == pytest.approx(0.1 * 0.001 * 10 / 9.1)
    concentrate, clarified = mixed.separator(inlet | {"mass_flow_kg_s": 0.0}, 100.0, 1.000001)
    assert concentrate["mass_flow_kg_s"] == clarified["mass_flow_kg_s"] == 0
    assert concentrate["culture"]["biomass"] == clarified["culture"]["biomass"] == 0.001
    with pytest.raises(ValueError, match="liquid"):
        mixed.separator(inlet | {"vapor_fraction": 0.01}, 90, 10)
    concentrate, clarified = mixed.separator(inlet, 100.0, 1.000001)
    assert concentrate["mass_flow_kg_s"] > 0
    assert clarified["mass_flow_kg_s"] == pytest.approx(1e-5, abs=2e-11)
    assert concentrate["culture"]["biomass"] == pytest.approx(0.001000001)
    assert clarified["culture"]["biomass"] == 0


def test_heat_exchanger_first_tear_consumer_uses_large_seed_and_finding() -> None:
    document = _tear_document()
    document["objects"]["feed"]["spec"].update({"pressure": {"si": 101325.0}, "mass_flow": {"si": 2.0},
                                                    "composition": {"Water": 1.0}})
    document["objects"].update({
        "hx": _unit("hx", "HeatExchanger", "HeatExchanger"),
        "r_hx": _stream("r_hx", "TearToHX", "r", "hx"),
        "hx_m": _stream("hx_m", "HXToMixer", "hx", "m"),
    })
    document["objects"].pop("r_m")
    document["objects"]["feed"]["target"] = {"unit": "m", "port": 0}
    document["objects"]["hx_m"]["target"] = {"unit": "m", "port": 1}
    finding_codes = {item["code"] for item in mixed.validation_findings(document)}
    assert "TEAR_CONSUMER_ZERO_FLOW" in finding_codes
    part = mixed.partition(document)
    initial = mixed_runtime._seed(document, part)
    assert initial["TearToHX"]["mass_flow_kg_s"] == pytest.approx(0.002)


def test_mixed_validate_builds_jarvis_boundaries_without_solving(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    document = _tear_document()
    built: list[dict] = []
    def capture(segment, _client, **kwargs):
        assert kwargs["action"] == "validate"
        built.append(segment)
        return {"status": "validated"}
    monkeypatch.setattr(mixed_runtime, "_full", capture)
    result = mixed_runtime.run(document, action="validate", client=object(), dwsim_version="10.2.9",
                               mcp_sha256="a" * 64, run_dir=tmp_path)
    assert result["status"] == "validated"
    after_jarvis = next(segment for segment in built if "s" in segment["objects"])
    boundary = after_jarvis["objects"]["j_s"]
    assert boundary["source"] is None
    assert boundary["spec"]["mass_flow"]["si"] > 0


def test_mixed_run_hides_internal_tear_state_unless_requested(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    document = _tear_document()
    monkeypatch.setattr(mixed, "iterate", lambda *_args, **_kwargs: {
        "iterate": {"Tear": _state()}, "status": "completed", "reason": "converged", "history": [],
        "last_input": None, "last": None,
    })
    monkeypatch.setattr(mixed_runtime, "_evaluate", lambda *_args, **_kwargs: {
        "produced": {}, "elapsed_s_by_phase": {"build": 0, "solve": 0, "culture": 0},
        "max_build_seconds": 0, "streams": {}, "units": {}, "mixed_findings": [], "segments": [],
    })
    monkeypatch.setattr(mixed_runtime, "_whole_graph_balances", lambda *_args, **_kwargs: {
        "balances": [], "status": "succeeded",
    })
    monkeypatch.setattr(mixed_runtime, "_culture_results", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(mixed_runtime, "_partition_record", lambda *_args, **_kwargs: {
        "consumed": [], "segments": [], "jarvis_units": [], "levels": {},
    })
    common = {"action": "run", "client": object(), "dwsim_version": "10.2.9", "mcp_sha256": "a" * 64,
              "run_dir": tmp_path}
    normal = mixed_runtime.run(document, **common)
    sampler = mixed_runtime.run(document, **common, include_tear_state=True)
    assert "_tear_state" not in normal and "_last_attempt" not in normal
    assert sampler["_tear_state"] == {"Tear": _state()}
    assert sampler["_last_attempt"] == {"guess": None, "output": None}


def test_residual_uses_field_tolerances_and_keeps_undamped_signed_values() -> None:
    before = {"temperature_K": 300.0, "pressure_Pa": 1e5, "mass_flow_kg_s": 1.0,
              "mass_fraction.Water": 0.5, "biomass": 0.01}
    after = before | {"temperature_K": 300.01, "pressure_Pa": 1e5 + 0.1,
                      "mass_flow_kg_s": 1.00001, "mass_fraction.Water": 0.5000001,
                      "biomass": 0.0100001}
    _, _, normalized = mixed.residual(before, after)
    assert normalized["temperature_K"] == pytest.approx(1)
    assert normalized["mass_fraction.Water"] == pytest.approx(1)
    assert mixed.residual_values(before, after)["pressure_Pa"] == pytest.approx(0.1)
    assert mixed.tolerance("biomass", 0.0) == pytest.approx(1e-12)
    assert mixed.residual({"biomass": None}, {"biomass": None})[0] == 0
    assert mixed.residual({"biomass": None}, {"biomass": 0.0})[0] == float("inf")


def test_engine_neutral_controller_converges_gain_0455_in_expected_range() -> None:
    initial = {"Tear": _state(0.9)}

    def evaluate(guess: dict[str, dict], _iteration: int) -> dict[str, dict]:
        output = copy.deepcopy(guess["Tear"])
        current = output["mass_fractions"]["Water"]
        output["mass_fractions"]["Water"] = 0.455 * current + 0.545
        return {"Tear": output}

    result = mixed.iterate(initial, evaluate)
    assert result["status"] == "completed"
    assert 8 <= len(result["history"]) <= 20
    assert result["history"][-1]["max_normalized_residual"] <= 1


def test_spurious_growth_below_one_point_five_does_not_damp_and_real_growth_does() -> None:
    def run_with_residuals(values: list[float]) -> dict:
        seen = 0
        def evaluate(guess: dict[str, dict], _iteration: int) -> dict[str, dict]:
            nonlocal seen
            target = copy.deepcopy(guess["Tear"])
            current = target["mass_fractions"]["Water"]
            target["mass_fractions"]["Water"] = current + values[min(seen, len(values) - 1)] * 1e-7
            seen += 1
            return {"Tear": target}
        return mixed.iterate({"Tear": _state()}, evaluate)

    spurious = run_with_residuals([2.0, 2.8, 3.8])
    assert [row["omega"] for row in spurious["history"][:3]] == [1.0, 1.0, 1.0]
    growing = run_with_residuals([2.0, 4.0, 3.0])
    assert growing["history"][2]["omega"] == 0.5


def test_controller_damping_exhaustion_and_iteration_cap() -> None:
    seen = 0
    def growing(guess: dict[str, dict], _iteration: int) -> dict[str, dict]:
        nonlocal seen
        target = copy.deepcopy(guess["Tear"])
        target["mass_fractions"]["Water"] += 2.0 ** (seen + 1) * 1e-7
        seen += 1
        return {"Tear": target}
    exhausted = mixed.iterate({"Tear": _state()}, growing)
    assert exhausted["reason"] == "damping_exhausted"
    assert exhausted["omega"] == 0.125

    constant = mixed.iterate({"Tear": _state()}, lambda guess, _iteration: {
        "Tear": {**guess["Tear"], "mass_fractions": {"Water": guess["Tear"]["mass_fractions"]["Water"] + 2e-7}}
    })
    assert constant["reason"] == "max_iterations"
    assert len(constant["history"]) == 25


def test_controller_wall_budget_stops_before_building_an_iteration() -> None:
    called = False
    def evaluate(guess: dict[str, dict], _iteration: int) -> dict[str, dict]:
        nonlocal called
        called = True
        return guess
    result = mixed.iterate({"Tear": _state()}, evaluate,
                           before_iteration=lambda _index: "wall_budget")
    assert result["reason"] == "wall_budget"
    assert not called


def test_light_full_mismatch_and_pressure_drop_diagnosis() -> None:
    light = {"Tear": _state()}
    full = copy.deepcopy(light)
    full["Tear"]["pressure_Pa"] += 1.0
    mismatch = mixed_runtime._light_full_mismatch(light, full)
    assert mismatch and mismatch["code"] == "light_full_mismatch"
    history = [{"residuals": {"Tear": {"pressure_Pa": -20.0 - index}}} for index in range(3)]
    assert mixed_runtime._pressure_diagnosis(history) == (
        "pressure falls by 22 Pa per pass around the loop; add a pump")
    history[-1]["residuals"]["Tear"]["pressure_Pa"] = 1.0
    assert mixed_runtime._pressure_diagnosis(history) is None


def test_mixed_fingerprint_ignores_layout_but_includes_culture_and_partition() -> None:
    document = _tear_document()
    part = mixed.partition(document)
    original = mixed.fingerprint(document, part)
    moved = copy.deepcopy(document)
    moved["objects"]["m"]["x"] = 400
    assert mixed.fingerprint(moved, mixed.partition(moved)) == original
    edited = copy.deepcopy(document)
    edited["objects"]["feed"]["spec"]["culture"]["biomass"]["si"] = 2
    assert mixed.fingerprint(edited, mixed.partition(edited)) != original


@pytest.mark.parametrize("unit_result,expected", [
    ({"reported": {"Mass Flow Error": {"units": "kg/h", "value": "0"}}}, 0.0),
    ({"reported": {"Mass Flow Error": {"units": "kg/h", "value": "-36"}}}, 0.01),
    ({"reported": {}, "properties": [{"name": "Mass Flow Error", "unit": "kg/h", "value": 7.2}]}, 0.002),
    ({"reported": {"Mass Flow Error": {"units": "kg/h", "value": "NaN"}}}, None),
    ({"reported": {"Mass Flow Error": {"units": "lbm/h", "value": "1"}}}, None),
    ({}, None),
])
def test_native_recycle_mass_flow_error_parses_dwsim_string_values(unit_result: dict,
                                                                    expected: float | None) -> None:
    value = mixed_runtime._native_mass_flow_error_kg_s(unit_result)
    assert value == (pytest.approx(expected) if expected is not None else None)


def _run_document(document: dict, tmp_path, action: str = "run") -> dict:
    return mixed_runtime.run(document, action=action, client=object(), dwsim_version="10.2.9",
                             mcp_sha256="a" * 64, run_dir=tmp_path)


def test_failure_record_partition_has_the_tag_based_shape_of_a_success_record(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """B1: a failed segment must store {id, level, units: [tags]} segments, never raw id lists."""
    document = _tear_document()

    def failing_light(*_args, **_kwargs):
        raise mixed_runtime.SegmentFailure("mixed-1-0", {"code": "check_failed", "findings": [
            {"object": "Mixer", "message": "Mixer has no outlet"}]})

    monkeypatch.setattr(mixed_runtime, "_light", failing_light)
    record = _run_document(document, tmp_path)["mixed_solve"]
    assert record["status"] == "segment_failed"
    part = record["partition"]
    assert part["consumed"] == [document["objects"]["r"]["tag"]]
    assert part["segments"] == [{"id": 0, "units": ["Mixer"], "level": 0},
                                {"id": 1, "units": ["Splitter"], "level": 1}]
    assert part["jarvis_units"] == ["Separator"]
    success_shape = mixed_runtime._partition_record(document, mixed.partition(document), [
        {"id": 0, "units": ["Mixer"], "level": 0, "materialization_fingerprint": None}])
    assert set(success_shape) == set(part)
    # M7: the label maps to unit tags, and the check finding is surfaced as a message.
    assert record["failed_units"] == ["Mixer"]
    assert "Mixer has no outlet" in record["message"]


def test_non_finite_residual_is_serialised_as_null_with_an_explicit_flag(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """M2: infinity stays internal; stored and served records contain only finite numbers."""
    import json

    document = _tear_document()
    calls = {"n": 0}

    def evaluate(tear, iteration, **_kwargs):
        calls["n"] += 1
        if calls["n"] > 1:
            raise mixed_runtime.SegmentFailure("Separator", {"code": "JARVIS_UNIT_FAILED"})
        output = copy.deepcopy(tear)
        for state in output.values():
            state["culture"]["biomass"] = None  # null/value mismatch -> infinite residual
        return {"produced": output, "elapsed_s_by_phase": {"build": 0.0, "solve": 0.0, "culture": 0.0},
                "max_build_seconds": 0.0}

    monkeypatch.setattr(mixed_runtime, "_evaluate", lambda document, part, tear, **kwargs: evaluate(
        tear, kwargs["iteration"]))
    record = _run_document(document, tmp_path)["mixed_solve"]
    row = record["history"][0]
    assert row["max_normalized_residual"] is None
    assert row["non_finite"] == "inf"
    assert row["pattern_mismatch_fields"] == [f"{document['objects']['r_m']['tag']}.biomass"]
    json.dumps(record, allow_nan=False)


def test_mid_iteration_failure_carries_the_pressure_per_pass_diagnosis(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """M7: the diagnosis is attached to a segment_failed that follows three one-sided passes."""
    document = _tear_document()
    calls = {"n": 0}

    def fake(_document, _part, tear, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 4:
            raise mixed_runtime.SegmentFailure("Separator", {"code": "JARVIS_UNIT_FAILED"})
        output = copy.deepcopy(tear)
        for state in output.values():
            state["pressure_Pa"] -= 100.0
        return {"produced": output, "elapsed_s_by_phase": {"build": 0.0, "solve": 0.0, "culture": 0.0},
                "max_build_seconds": 0.0}

    monkeypatch.setattr(mixed_runtime, "_evaluate", fake)
    record = _run_document(document, tmp_path)["mixed_solve"]
    assert record["status"] == "segment_failed"
    assert record["failed_units"] == []
    assert "pressure falls by" in record["diagnosis"]


# ---- Final-review regressions (r168final) -------------------------------------------------

def _liquid(flow: float = 1.0, temperature: float = 298.15) -> dict:
    return {"temperature_K": temperature, "pressure_Pa": 101325.0, "mass_flow_kg_s": flow,
            "mass_fractions": {"Water": 1.0}, "vapor_fraction": 0.0,
            "phases": [{"name": "Mixture", "density_kg_m3": 1000.0}]}


def _plain_feed(uid: str, tag: str, target: str, **spec) -> dict:
    feed = _stream(uid, tag, target=target)
    feed["spec"] = {"pressure": {"si": 101325.0}, "composition_basis": "mass",
                    "composition": {"Water": 1.0}, **spec}
    return feed


def _document(objects: dict, compounds: list[str] | None = None) -> dict:
    return {"schema_version": 1, "name": "t", "compounds": compounds or ["Water"],
            "property_package": "NRTL", "objects": objects, "reactions": {}}


def test_m1_non_culture_inlets_dilute_and_units_they_feed_still_pass_culture_on() -> None:
    from app.modules.process_stack import culture

    document = _document({
        "f1": _stream("f1", "F1", target="m"),
        "f2": _plain_feed("f2", "F2", "p", mass_flow={"si": 1.0}, temperature={"si": 298.15}),
        "p": _unit("p", "Pump", "Pump"),
        "pm": _stream("pm", "PM", "p", "m", target_port=1),
        "m": _unit("m", "Mixer", "Mixer"),
        "out": _stream("out", "Out", "m", None),
    })
    streams = {tag: _liquid(1.0) for tag in ("F1", "F2", "PM", "Out")}
    streams["Out"] = _liquid(2.0)
    states = mixed_runtime._propagate_culture(document, {"streams": streams}, {})
    assert {"F1", "Out"} <= set(states) and "PM" not in states  # the pump outlet carries no culture
    # 167 rule: the culture-free half dilutes as zero culture, so biomass halves.
    expected, _ = culture.mix_mass_specific(
        [(1.0, {"mass_specific": states["F1"], "reasons": {}})], 2.0, 2, "Mixer")
    assert states["Out"]["biomass"] == pytest.approx(0.5 * states["F1"]["biomass"])
    assert states["Out"]["biomass"] == pytest.approx(expected["biomass"])
    assert states["Out"]["ph"] is None  # pH placeholder survives only when every inlet carries culture


def _bypass_document() -> dict:
    return _document({
        "feed": _plain_feed("feed", "Feed", "s", mass_flow={"si": 1.0}, temperature={"si": 298.15}),
        "s": _unit("s", "Splitter", "Splitter"),
        "s_h": _stream("s_h", "SH", "s", "h", source_port=0),
        "s_m": _stream("s_m", "SM", "s", "m", source_port=1, target_port=0),
        "h": _unit("h", "Heater", "Heater"),
        "h_j": _stream("h_j", "HJ", "h", "j"),
        "j": _unit("j", "Separator", "SpecifiedSeparator"),
        "j_m": _stream("j_m", "JM", "j", "m", source_port=0, target_port=1),
        "j_p": _stream("j_p", "JP", "j", None, source_port=1),
        "m": _unit("m", "Mixer", "Mixer"),
        "out": _stream("out", "Out", "m", None),
    })


def test_m3_validate_builds_cross_level_dwsim_edges_of_the_bypass_case(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    document = _bypass_document()
    document["objects"]["feed"]["spec"]["culture"] = _stream("x", "x", target="s")["spec"]["culture"]
    assert [[document["objects"][uid]["tag"] for uid in ids] for ids in mixed.partition(document)["segments"]] == [
        ["Heater", "Splitter"], ["Mixer"]]
    built: list[dict] = []

    def capture(segment, _client, **_kwargs):
        built.append(segment)
        return {"status": "validated"}

    monkeypatch.setattr(mixed_runtime, "_full", capture)
    result = _run_document(document, tmp_path, action="validate")
    assert result["status"] == "validated"
    mixer = next(segment for segment in built if "m" in segment["objects"])
    assert mixer["objects"]["s_m"]["source"] is None and mixer["objects"]["s_m"]["spec"]["mass_flow"]["si"] > 0


def test_m4_m5_feed_state_covers_every_compound_and_a_molar_only_flow() -> None:
    feed = _plain_feed("f", "F", "j", molar_flow={"si": 1000.0})
    feed["spec"]["composition"] = {"Water": 1.0}
    document = _tear_document()
    document["compounds"] = ["Water", "Ethanol"]
    state = mixed_runtime._state_from_spec(feed, document["compounds"])
    assert state["mass_fractions"] == {"Water": 1.0, "Ethanol": 0.0}
    assert state["mass_flow_kg_s"] == pytest.approx(18.01528 / 1000 * 1000.0)  # 1000 mol/s of water
    document["objects"]["feed"]["spec"].pop("mass_flow")
    document["objects"]["feed"]["spec"]["molar_flow"] = {"si": 1000.0}
    seed = mixed_runtime._seed(document, mixed.partition(document))
    assert set(seed["Tear"]["mass_fractions"]) == {"Water", "Ethanol"}
    assert seed["Tear"]["mass_flow_kg_s"] > 0
    document["objects"]["feed"]["spec"].pop("molar_flow")  # no flow in kg/s at all: still never zero
    assert mixed_runtime._seed(document, mixed.partition(document))["Tear"]["mass_flow_kg_s"] > 0


def _direct_jarvis_feed_document() -> dict:
    feed = _plain_feed("feed", "Feed", "j", molar_flow={"si": 100.0})
    feed["spec"]["culture"] = _stream("x", "x", target="j")["spec"]["culture"]
    return _document({
        "feed": feed, "j": _unit("j", "Separator", "SpecifiedSeparator"),
        "j_c": _stream("j_c", "Conc", "j", None, source_port=0),
        "j_k": _stream("j_k", "Clar", "j", None, source_port=1)})


def test_m4_jarvis_bound_feed_is_flashed_with_its_actual_spec(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    document = _direct_jarvis_feed_document()
    seen: list[dict] = []

    def fake_flash(state, tag, _client, **kwargs):
        seen.append(kwargs["spec"])
        return _liquid(2.0, temperature=310.0)

    monkeypatch.setattr(mixed_runtime, "_flash", fake_flash)
    result = mixed_runtime._evaluate(document, mixed.partition(document), {}, client=object(), full=False,
                                     run_dir=tmp_path, iteration=1, dwsim_version="10.2.9",
                                     mcp_sha256="a" * 64, remaining_s=30.0)
    assert "molar_flow" in seen[0] and "mass_flow" not in seen[0]
    assert result["known"]["Conc"]["mass_flow_kg_s"] == pytest.approx(2.0 * 0.9 / 10.0)
    assert result["known"]["Conc"]["temperature_K"] == 310.0


def test_flash_of_an_operator_feed_passes_the_actual_spec_and_all_compounds(
        monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def light(document, _client, **_kwargs):
        captured["document"] = document
        return {"streams": {"F": _liquid()}, "dwsim_check": {}}

    monkeypatch.setattr(mixed_runtime, "_light", light)
    spec = {"pressure": {"si": 1e5}, "vapor_fraction": {"si": 0.0}, "molar_flow": {"si": 5.0},
            "composition_basis": "mole", "composition": {"Water": 1.0}}
    mixed_runtime._flash({}, "F", None, label="x", dwsim_version="v", mcp_sha256="a" * 64,
                         property_package="NRTL", full=False, remaining_s=10.0,
                         compounds=["Water", "Ethanol"], spec=spec)
    assert captured["document"]["objects"]["F"]["spec"] is spec
    assert captured["document"]["compounds"] == ["Water", "Ethanol"]


def _fake_light(segment, _client, **_kwargs):
    """A conserving stand-in for one DWSIM segment: inbound spec flows split over the outlets."""
    objects = segment["objects"]
    streams = [item for item in objects.values() if item["kind"] == "stream"]
    inbound = sum(float(item["spec"]["mass_flow"]["si"]) for item in streams if item.get("source") is None)
    outbound = [item for item in streams if item.get("target") is None and item.get("source") is not None]
    results = {}
    for item in streams:
        if item.get("source") is None:
            flow = float(item["spec"]["mass_flow"]["si"])
        else:
            flow = inbound / max(1, len(outbound)) if item in outbound else inbound
        results[item["tag"]] = _liquid(flow)
    return {"status": "completed", "streams": results, "units": {},
            "elapsed_s_by_phase": {"build": 0.01, "solve": 0.01, "culture": 0.0}}


def test_m6_minor1_consumed_recycle_row_shape_and_jarvis_ownership_survive_downstream_segments(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    objects = {
        "feed": _stream("feed", "Feed", target="m"),
        "m": _unit("m", "Mixer", "Mixer"),
        "m_j": _stream("m_j", "M_to_J", "m", "j"),
        "j": _unit("j", "Separator", "SpecifiedSeparator"),
        "j_c": _stream("j_c", "Concentrate", "j", "m2", source_port=0, target_port=0),
        "j_k": _stream("j_k", "Clarified", "j", "m2", source_port=1, target_port=1),
        "m2": _unit("m2", "Mixer2", "Mixer"),
        "m2_s": _stream("m2_s", "M2_out", "m2", "s"),
        "s": _unit("s", "Splitter", "Splitter"),
        "s_p": _stream("s_p", "Product", "s", None, source_port=0),
        "s_r": _stream("s_r", "Return", "s", "r", source_port=1),
        "r": _unit("r", "Recycle", "Recycle"),
        "r_m": _stream("r_m", "Tear", "r", "m", target_port=1),
    }
    document = _document(objects)
    monkeypatch.setattr(mixed_runtime, "_light", _fake_light)
    part = mixed.partition(document)
    assert part["consumed"] == ["r"]
    tear = mixed_runtime._seed(document, part)
    result = mixed_runtime._evaluate(document, part, tear, client=object(), full=False, run_dir=tmp_path,
                                     iteration=1, dwsim_version="10.2.9", mcp_sha256="a" * 64,
                                     remaining_s=30.0)
    row = result["units"]["Recycle"]
    assert row["owner"] == "jarvis_bio" and row["label"] == "Converged by Jarvis (cross-engine tear)"
    for name, units in (("Mass Flow Error", "kg/h"), ("Temperature Error", "K"), ("Pressure Error", "Pa")):
        assert set(row["reported"][name]) == {"value", "units"} and row["reported"][name]["units"] == units
    # Jarvis outlets feeding the downstream DWSIM segment and the tear keep their Jarvis ownership.
    assert result["streams"]["Concentrate"]["owner"] == "jarvis_bio"
    assert result["streams"]["Concentrate"]["state_source"] == "jarvis_unit"
    assert result["streams"]["Tear"]["owner"] == "jarvis_bio"
    assert result["streams"]["Feed"]["owner"] == "dwsim"


def test_minor5_a_tear_straight_into_a_jarvis_unit_needs_no_density(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    objects = {
        "feed": _stream("feed", "Feed", target="m"),
        "m": _unit("m", "Mixer", "Mixer"),
        "m_s": _stream("m_s", "MS", "m", "s"),
        "s": _unit("s", "Splitter", "Splitter"),
        "s_p": _stream("s_p", "Product", "s", None, source_port=0),
        "s_r": _stream("s_r", "Return", "s", "r", source_port=1),
        "r": _unit("r", "Recycle", "Recycle"),
        "r_j": _stream("r_j", "Tear", "r", "j"),
        "j": _unit("j", "Separator", "SpecifiedSeparator"),
        "j_m": _stream("j_m", "JM", "j", "m", source_port=0, target_port=1),
        "j_k": _stream("j_k", "Clar", "j", None, source_port=1),
    }
    document = _document(objects)
    part = mixed.partition(document)
    assert part["consumed"] == ["r"]
    monkeypatch.setattr(mixed_runtime, "_light", _fake_light)
    tear = mixed_runtime._seed(document, part)
    result = mixed_runtime._evaluate(document, part, tear, client=object(), full=False, run_dir=tmp_path,
                                     iteration=1, dwsim_version="10.2.9", mcp_sha256="a" * 64,
                                     remaining_s=30.0)
    assert set(result["produced"]) == {"Tear"}


def test_minor3_wall_budget_stop_after_a_growth_restart_stores_the_best_iterate() -> None:
    def evaluate(tear, iteration):
        output = copy.deepcopy(tear)
        for state in output.values():
            state["temperature_K"] += 0.1 if iteration == 1 else 5.0
        return output

    initial = {"T": {**_state(1.0), "temperature_K": 300.0}}
    result = mixed.iterate(initial, evaluate,
                           before_iteration=lambda iteration: "wall_budget" if iteration == 3 else None)
    assert result["reason"] == "wall_budget"
    assert result["iterate"]["T"]["temperature_K"] == 300.0  # best, not the worsening iterate


def test_minor4_t_build_is_the_slowest_build_seen_and_three_seconds_only_before_the_first(
        monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    document = _document({
        "feed": _plain_feed("feed", "Feed", "h", mass_flow={"si": 1.0}, temperature={"si": 298.15}),
        "h": _unit("h", "Heater", "Heater"),
        "out": _stream("out", "Out", "h", None)})
    builds = iter([0.5, 0.2])

    def fake(_document, _part, _tear, **_kwargs):
        return {"produced": {}, "streams": {}, "units": {}, "segments": [], "culture_balances": {},
                "culture_generation": {}, "mixed_findings": [], "max_build_seconds": next(builds, 0.2),
                "elapsed_s_by_phase": {"build": 0.0, "solve": 0.0, "culture": 0.0},
                "known": {"Feed": {"mass_flow_kg_s": 1.0}, "Out": {"mass_flow_kg_s": 1.0}}}

    monkeypatch.setattr(mixed_runtime, "_evaluate", fake)
    record = _run_document(document, tmp_path)["mixed_solve"]
    assert record["budget"]["t_build_s"] == 0.5  # observed, below the 3 s first-build assumption
    # MINOR 9: a loop with no cross-engine tear is reported as such, not as a one-iteration convergence.
    assert record["culture_only"] is True


def test_minor6_culture_on_a_recycle_off_any_loop_is_blocked_at_validation() -> None:
    from app.modules.process_stack import culture

    document = _document({
        "feed": _stream("feed", "Feed", target="r"), "r": _unit("r", "Recycle", "Recycle"),
        "out": _stream("out", "Out", "r", None)})
    findings = culture.culture_findings(document)
    assert [item["code"] for item in findings if item["severity"] == "blocker"] == ["CULTURE_UNIT_UNSUPPORTED"]
    # A Recycle that closes a loop is not refused.
    assert not [item for item in culture.culture_findings(_tear_document()) if item["severity"] == "blocker"]


def test_minor7_other_partition_errors_become_a_blocking_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(_document):
        raise ValueError("partition remains cyclic after consumed tears are cut")

    monkeypatch.setattr(mixed, "partition", broken)
    findings = mixed.validation_findings(_tear_document())
    assert [item["code"] for item in findings if item["severity"] == "blocker"] == ["MIXED_PARTITION_INVALID"]


def test_native_recycle_carrier_gap_is_allowed_only_up_to_the_reported_recycle_error() -> None:
    # Real DWSIM 10.2.9: a converged native loop leaves the Mixer outlet short of its inlets by the Recycle error.
    document = {"objects": {
        "f": _stream("f", "Hot", target="m"), "m": _unit("m", "Mixer", "Mixer"),
        "t": _stream("t", "Torn", "r", "m", target_port=1), "o": _stream("o", "Mixed", "m", None),
        "r": _unit("r", "Recycle", "Recycle")}}
    hot, torn, mixed_out = _state(), _state(), _state()
    hot["mass_flow_kg_s"], torn["mass_flow_kg_s"], mixed_out["mass_flow_kg_s"] = 0.01, 0.0011111211, 0.011111211
    for state in (hot, torn, mixed_out):
        state["culture"]["biomass"] = 0.5
    known = {"Hot": hot, "Torn": torn, "Mixed": mixed_out}
    gap = mixed_out["mass_flow_kg_s"] - hot["mass_flow_kg_s"] - torn["mass_flow_kg_s"]
    with pytest.raises(mixed_runtime.SegmentFailure):
        mixed_runtime._culture_balances(document, known, {})
    with pytest.raises(mixed_runtime.SegmentFailure):
        mixed_runtime._culture_balances(document, known, {}, None, gap / 2)
    row = mixed_runtime._culture_balances(document, known, {}, None, gap)["Mixer"]["biomass"]
    assert row["passed"] and row["native_recycle_allowance"] == pytest.approx(gap * 0.5)
    mixed_out["culture"]["biomass"] = 0.6  # a real culture error is never covered by the carrier allowance
    with pytest.raises(mixed_runtime.SegmentFailure):
        mixed_runtime._culture_balances(document, known, {}, None, gap)


@pytest.mark.skipif(not os.environ.get("JARVISOS_DWSIM_MCP_PATH"), reason="set JARVISOS_DWSIM_MCP_PATH to opt in to DWSIM runtime")
def test_real_dwsim_culture_through_a_native_recycle_loop_converges() -> None:
    from app.modules.process_stack.draft_models import (
        AddStream,
        AddUnit,
        Connect,
        DraftQuantity,
        SetStreamCulture,
        SetStreamSpec,
        SetThermo,
        SetUnitParams,
    )
    from tests.plumbing_170_support import new_workspace

    q = DraftQuantity
    workspace_id = new_workspace()
    state = draft.create_draft(workspace_id, "culture native loop")
    wiring = (("f", "target", "heater", 0), ("hot", "source", "heater", 0), ("hot", "target", "mixer", 0),
              ("mixed", "source", "mixer", 0), ("mixed", "target", "split", 0), ("purge", "source", "split", 0),
              ("back", "source", "split", 1), ("back", "target", "rec", 0), ("torn", "source", "rec", 0),
              ("torn", "target", "mixer", 1))
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        SetThermo(op="set_thermo", compounds=["Water"], property_package="NRTL"),
        AddUnit(op="add_unit", id="heater", type="Heater", tag="Heater", x=150, y=0),
        AddUnit(op="add_unit", id="mixer", type="Mixer", tag="Mixer", x=250, y=0),
        AddUnit(op="add_unit", id="split", type="Splitter", tag="Split", x=450, y=0),
        AddUnit(op="add_unit", id="rec", type="Recycle", tag="Rec", x=350, y=80),
        *[AddStream(op="add_stream", id=sid, tag=sid.title(), x=x, y=0)
          for sid, x in (("f", 0), ("hot", 200), ("mixed", 400), ("purge", 500), ("back", 450), ("torn", 300))],
        *[Connect(op="connect", stream=sid, end=end, unit=unit, port=port) for sid, end, unit, port in wiring],
        SetStreamSpec(op="set_stream_spec", stream="f", pressure=q(value=1.0, unit="bar"),
                      temperature=q(value=300, unit="K"), mass_flow=q(value=0.01, unit="kg/s"),
                      composition={"Water": 1.0}, composition_basis="mass"),
        SetStreamCulture(op="set_stream_culture", stream="f", culture={
            "biomass": q(value=0.5, unit="kg/m3"), "nitrogen": q(value=0.05, unit="kg/m3"),
            "oxygen": q(value=0.008, unit="kg/m3"), "salinity": q(value=0.0, unit="g/kg")}),
        SetUnitParams(op="set_unit_params", unit="heater", mode="outlet_temperature",
                      values={"outlet_temperature": q(value=298.15, unit="K")}),
        SetUnitParams(op="set_unit_params", unit="split", mode="split_ratios", values={
            "split_ratio_1": q(value=0.9, unit="dimensionless"), "split_ratio_2": q(value=0.1, unit="dimensionless")}),
    ])
    run = draft.execute(workspace_id, state["draft_id"], state["revision"], "run")["run"]
    assert run["status"] == "completed", run.get("mixed_solve")
    assert run["streams"]["Purge"]["mass_flow_kg_s"] == pytest.approx(0.01, rel=2e-5)
    assert run["culture"]["Purge"]["values"]["biomass"]["mass_specific"] == pytest.approx(
        run["culture"]["F"]["values"]["biomass"]["mass_specific"], rel=1e-6)
