"""Spec 184 lane B: steady PBR circulation outputs, recycle warning and tank refusal."""

from __future__ import annotations

import math
import os
import time
from typing import Any

import pytest

from app.modules.process_stack import draft, draft_compiler, mixed, mixed_runtime, pbr_adapter, pbr_unit
from app.modules.process_stack.mixed_runtime import JarvisUnitContext


def _model() -> dict[str, Any]:
    return {
        "card": {"name": "Synthetic circulation card", "factors": {
            "light": "light.monod", "optics": "optics.slab_response_average", "temperature": "temperature.isothermal",
            "nutrients": ["nutrient.monod"], "combination": "combine.multiplicative", "loss": "loss.first_order",
            "stoichiometry": "stoich.photoautotrophic"}, "form_versions": {"light.monod": "1.0.0"}, "n_source": "NH3"},
        "set": {"id": "set", "name": "set", "revision": "r-" + "1" * 16, "digest": "2" * 64},
        "parameters": {"K_I": 150.0, "K_j_0": 0.001, "k_X": 0.0, "k_d": 0.002},
        "mu_max_h": 0.08, "nitrogen_quota": 0.05, "oxygen_yield": 1.4, "candidate_symbols": [],
    }


def _pbr() -> dict[str, Any]:
    params = {"tube_inner_diameter": 0.05, "tube_length": 10.0, "tube_count": 4.0,
              "liquid_velocity": 0.5, "pump_efficiency": 60.0, "baffle_friction_multiplier": 1.0,
              "oxygen_kla": 0.0, "oxygen_saturation": 0.008, "peak_par": 1500.0, "photoperiod": 43200.0,
              "diffuse_fraction": 0.0, "temperature_mean": 298.15, "temperature_amplitude": 0.0}
    return {"tag": "PBR", "type": "PhotobioreactorT1", "params": {key: {"si": val} for key, val in params.items()},
            "model": {"card_id": "card", "card_revision": "r-" + "a" * 16, "card_digest": "b" * 64}}


def test_pbr_reports_independent_circulation_quantities_and_process_inlet_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pbr_unit, "_resolve", lambda _workspace, _pin: _model())
    monkeypatch.setattr(pbr_unit, "hydraulics", lambda *_args, **_kwargs: {
        "reynolds_number": 100.0, "pressure_drop": 1.0, "pumping_power": 0.1})
    density = 1000.0
    area = math.pi * 0.05**2 / 4.0
    volume = 4.0 * area * 10.0
    q_inlet = volume / (3.0 * 86400.0)
    inlet = {"mass_flow_kg_s": density * q_inlet, "temperature_K": 298.15, "vapor_fraction": 0.0,
             "culture": {"biomass": 0.0, "nitrogen": 0.05 / density, "oxygen": 0.008 / density}}
    result = pbr_unit.evaluate_pbr(
        _pbr(), inlet,
        JarvisUnitContext(density, time.monotonic() + 30.0, {}, workspace_id="ws"),
    ).result
    reported = result["reported"]
    q_circ = 4.0 * 0.5 * area
    assert reported["circulation_flow_m3_h"]["value"] == pytest.approx(3600.0 * q_circ)
    assert reported["pass_transit_time_s"]["value"] == pytest.approx(10.0 / 0.5)
    assert reported["circulation_to_throughflow_ratio"]["value"] == pytest.approx(q_circ / q_inlet)
    assert "process-inlet basis" in reported["dilution_h"]["label"]
    assert "process-inlet basis" in reported["hrt_d"]["label"]
    assert "process-inlet basis" in reported["volumetric_flow_m3_h"]["label"]
    assert pbr_adapter._OUTPUTS["circulation_flow_m3_h"] == "m3/h"
    assert pbr_adapter._OUTPUTS["pass_transit_time_s"] == "s"
    assert pbr_adapter._OUTPUTS["circulation_to_throughflow_ratio"] == "1"


def _recycle_document(*, loop: bool, back_fraction: float = 0.9) -> dict[str, Any]:
    objects: dict[str, dict[str, Any]] = {
        "pbr": {"id": "pbr", "kind": "unit", **_pbr()},
        "mix": {"id": "mix", "kind": "unit", "type": "Mixer", "tag": "Mixer", "params": {}},
        "split": {"id": "split", "kind": "unit", "type": "Splitter", "tag": "Split", "params": {
            "split_ratio_1": {"si": 1.0 - back_fraction}, "split_ratio_2": {"si": back_fraction}}},
    }
    def stream(sid: str, tag: str, source: str | None, target: str | None) -> None:
        objects[sid] = {"id": sid, "kind": "stream", "type": "MaterialStream", "tag": tag,
                        "source": {"unit": source, "port": 0} if source else None,
                        "target": {"unit": target, "port": 0} if target else None, "spec": {}}
    stream("feed", "Feed", None, "mix")
    stream("mixed", "Mixed", "mix", "pbr")
    stream("product", "Product", "pbr", "split")
    stream("purge", "Purge", "split", None)
    if loop:
        objects["rec"] = {"id": "rec", "kind": "unit", "type": "Recycle", "tag": "Rec", "params": {}}
        objects["back"] = {"id": "back", "kind": "stream", "type": "MaterialStream", "tag": "Back",
                            "source": {"unit": "split", "port": 1}, "target": {"unit": "rec", "port": 0}, "spec": {}}
        objects["tear"] = {"id": "tear", "kind": "stream", "type": "MaterialStream", "tag": "Tear",
                            "source": {"unit": "rec", "port": 0}, "target": {"unit": "mix", "port": 1}, "spec": {}}
    return {"schema_version": 1, "name": "184", "compounds": ["Water"],
            "property_package": "Steam Tables (IAPWS-IF97)", "objects": objects, "reactions": {}}


@pytest.mark.parametrize(("loop", "expected"), [(True, 1), (False, 0)])
def test_synthetic_183_shape_warns_only_for_fast_pbr_process_recycle(loop: bool, expected: int,
                                                                      monkeypatch: pytest.MonkeyPatch,
                                                                      tmp_path) -> None:
    document = _recycle_document(loop=loop)
    monkeypatch.setattr(pbr_unit, "_resolve", lambda _workspace, _pin: {"mu_max_h": 0.08})

    def candidate(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"produced": {"tear": {"mass_flow_kg_s": 1.0}}, "streams": {},
                "units": {"PBR": {"reported": {"dilution_h": {"value": 1.0}}}}, "segments": [],
                "known": {}, "culture_balances": {}, "culture_generation": {},
                "culture_generation_allowance": {}, "mixed_findings": [], "max_build_seconds": 0.0,
                "elapsed_s_by_phase": {"build": 0.0, "solve": 0.0, "culture": 0.0}}

    def iterate(initial: dict[str, Any], evaluate: Any, **_kwargs: Any) -> dict[str, Any]:
        evaluate(initial, 1)
        return {"status": "completed", "reason": None, "history": [{"iteration": 1}],
                "iterate": initial, "last_input": initial, "diagnostics": {}}

    monkeypatch.setattr(mixed_runtime, "_seed", lambda *_args: {})
    monkeypatch.setattr(mixed_runtime, "_evaluate", candidate)
    monkeypatch.setattr(mixed, "iterate", iterate)
    monkeypatch.setattr(mixed_runtime, "_whole_graph_balances", lambda *_args: {"status": "calculated", "balances": {}})
    monkeypatch.setattr(mixed_runtime, "_final_status", lambda status, *_args: (status, None))
    monkeypatch.setattr(mixed_runtime, "_light_full_mismatch", lambda *_args: None)
    monkeypatch.setattr(mixed_runtime, "_culture_results", lambda *_args: {})
    run = mixed_runtime.run(document, action="run", client=object(), dwsim_version="10.2.9",
                            mcp_sha256="a" * 64, run_dir=tmp_path, workspace_id="ws")
    findings = [item for item in run.get("mixed_findings", []) if item["code"] == "PBR_LOOP_AS_PROCESS_RECYCLE"]
    assert run["status"] == "completed"
    assert len(findings) == expected
    if expected:
        assert findings[0]["object"] == "PBR"
        assert "dynamic culture loop" in findings[0]["message"]


def test_holdup_tank_steady_run_and_materialize_refuse_before_validation_or_dwsim(monkeypatch: pytest.MonkeyPatch) -> None:
    document = {"objects": {"tank": {"id": "tank", "kind": "unit", "type": "HoldupTank", "tag": "Tank"}}}
    monkeypatch.setattr(draft, "draft_dir", lambda *_args: object())
    monkeypatch.setattr(draft, "load_revision", lambda *_args: {"document": document})
    monkeypatch.setattr(draft, "validate_document", lambda *_args: pytest.fail("steady Run must refuse before validation"))
    with pytest.raises(draft.DraftError) as run_error:
        draft.execute("ws", "draft", "revision", "run")
    assert run_error.value.code == "HOLDUP_TANK_DYNAMIC_ONLY"
    assert run_error.value.detail["units"] == ["Tank"]

    class NoDwsim:
        def call(self, *_args: Any, **_kwargs: Any) -> Any:
            pytest.fail("materializer must refuse before contacting DWSIM")

    with pytest.raises(draft_compiler.MaterializationError) as materialize_error:
        draft_compiler.materialize(document, action="run", client=NoDwsim(), dwsim_version="10.2.9",
                                   mcp_sha256="a" * 64, label="must-not-materialize")
    assert materialize_error.value.code == "HOLDUP_TANK_DYNAMIC_ONLY"


@pytest.mark.skipif(not os.environ.get("JARVISOS_DWSIM_MCP_PATH"),
                    reason="set JARVISOS_DWSIM_MCP_PATH to opt in to DWSIM runtime")
@pytest.mark.parametrize(("tube_length_m", "expected"), [(100.0, False), (10.0, True)])
def test_real_dwsim_pbr_culture_recycle_warns_only_above_ten_mu_max(tube_length_m: float, expected: bool) -> None:
    from app.modules.process_stack.draft_models import SetSolver, SetUnitParams
    from tests.plumbing_170_support import pbr_quantities
    from tests.test_tear_solver_183 import _real_pbr_recycle

    # 183 shape at 0.9 return (mu_max 0.08 1/h). With the 183 100 m tubes D_in = 0.184 1/h = 2.3 mu_max: no
    # warning. With 10 m tubes D_in is ten times larger, above 10 mu_max, and the loop reads as circulation.
    workspace_id, state = _real_pbr_recycle(0.9)
    # The default 25-iteration direct substitution needs ~119 iterations at 0.9 return (183); Broyden converges.
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        SetUnitParams(op="set_unit_params", unit="pbr", values=pbr_quantities(tube_length=(tube_length_m, "m"))),
        SetSolver(op="set_solver", solver={"method": "broyden", "seed_mode": "feed", "wall_s": 600})])
    run = draft.execute(workspace_id, state["draft_id"], state["revision"], "run")["run"]
    assert run["status"] == "completed", run.get("mixed_solve")
    dilution = run["units"]["PBR"]["reported"]["dilution_h"]["value"]
    assert (dilution > 10 * 0.08) is expected, dilution
    findings = [item for item in run.get("mixed_findings", []) if item["code"] == "PBR_LOOP_AS_PROCESS_RECYCLE"]
    assert [item["object"] for item in findings] == (["PBR"] if expected else [])
