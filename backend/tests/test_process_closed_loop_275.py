"""Focused tests for Spec 275 (CLOSED-LOOP-ENERGY-1):
Closed circulation loops converge or block with a typed finding.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from app.modules.process_stack import draft, mixed, mixed_runtime
from app.modules.process_stack.draft_models import (
    AddStream,
    AddUnit,
    Connect,
    DraftQuantity,
    SetSolver,
    SetThermo,
    SetUnitModel,
    SetUnitParams,
)
from tests.plumbing_170_support import new_workspace, pbr_quantities


def _make_closed_loop_fixture(
    workspace_id: str,
    *,
    with_heat_sink: bool = True,
    sink_type: str = "Cooler",
    sink_mode: str = "outlet_temperature",
) -> tuple[dict[str, Any], str]:
    """Build minimal closed loop: Pump -> PhotobioreactorT1 -> [Cooler/Heater] -> Recycle -> Pump."""
    state = draft.create_draft(workspace_id, "closed loop 275")
    draft_id = state["draft_id"]
    revision = state["revision"]

    ops: list[Any] = [
        SetThermo(op="set_thermo", compounds=["Water"], property_package="NRTL"),
        AddUnit(op="add_unit", id="pump", type="Pump", tag="Pump", x=0, y=0),
        AddUnit(op="add_unit", id="pbr", type="PhotobioreactorT1", tag="PBR", x=100, y=0),
        AddUnit(op="add_unit", id="rec", type="Recycle", tag="Rec", x=300, y=0),
        AddStream(op="add_stream", id="s_pump_pbr", tag="PumpToPBR", x=50, y=0),
        Connect(op="connect", stream="s_pump_pbr", end="source", unit="pump", port=0),
        Connect(op="connect", stream="s_pump_pbr", end="target", unit="pbr", port=0),
        SetUnitParams(
            op="set_unit_params",
            unit="pump",
            mode="pressure_increase",
            values={"pressure_increase": DraftQuantity(value=50000.0, unit="Pa")},
        ),
        SetUnitParams(
            op="set_unit_params",
            unit="pbr",
            mode="periodic_steady",
            values=pbr_quantities(
                temperature_mean=(25.0, "degC"),
                temperature_amplitude=(0.0, "K"),
            ),
        ),
    ]

    # Pin a fake model card on PBR so validation passes
    fake_pin = {"card_id": "card-c275", "card_revision": "r-" + "a" * 16, "card_digest": "b" * 64}
    ops.append(SetUnitModel(op="set_unit_model", unit="pbr", model=fake_pin))

    if with_heat_sink:
        sink_unit_type = "Cooler" if sink_type == "Cooler" else "Heater"
        ops.append(AddUnit(op="add_unit", id="sink", type=sink_unit_type, tag="Sink", x=200, y=0))
        ops.append(AddStream(op="add_stream", id="s_pbr_sink", tag="PBRToSink", x=150, y=0))
        ops.append(AddStream(op="add_stream", id="s_sink_rec", tag="SinkToRec", x=250, y=0))
        ops.append(AddStream(op="add_stream", id="s_rec_pump", tag="RecToPump", x=350, y=0))

        ops.append(Connect(op="connect", stream="s_pbr_sink", end="source", unit="pbr", port=0))
        ops.append(Connect(op="connect", stream="s_pbr_sink", end="target", unit="sink", port=0))
        ops.append(Connect(op="connect", stream="s_sink_rec", end="source", unit="sink", port=0))
        ops.append(Connect(op="connect", stream="s_sink_rec", end="target", unit="rec", port=0))
        ops.append(Connect(op="connect", stream="s_rec_pump", end="source", unit="rec", port=0))
        ops.append(Connect(op="connect", stream="s_rec_pump", end="target", unit="pump", port=0))

        if sink_mode == "outlet_temperature":
            ops.append(
                SetUnitParams(
                    op="set_unit_params",
                    unit="sink",
                    mode="outlet_temperature",
                    values={
                        "outlet_temperature": DraftQuantity(value=298.15, unit="K"),
                        "pressure_drop": DraftQuantity(value=50000.0, unit="Pa"),
                    },
                )
            )
        elif sink_mode == "heat_removed":
            ops.append(
                SetUnitParams(
                    op="set_unit_params",
                    unit="sink",
                    mode="heat_removed",
                    values={
                        "heat_duty": DraftQuantity(value=500.0, unit="W"),
                        "pressure_drop": DraftQuantity(value=50000.0, unit="Pa"),
                    },
                )
            )
    else:
        ops.append(AddStream(op="add_stream", id="s_pbr_rec", tag="PBRToRec", x=200, y=0))
        ops.append(AddStream(op="add_stream", id="s_rec_pump", tag="RecToPump", x=350, y=0))
        ops.append(Connect(op="connect", stream="s_pbr_rec", end="source", unit="pbr", port=0))
        ops.append(Connect(op="connect", stream="s_pbr_rec", end="target", unit="rec", port=0))
        ops.append(Connect(op="connect", stream="s_rec_pump", end="source", unit="rec", port=0))
        ops.append(Connect(op="connect", stream="s_rec_pump", end="target", unit="pump", port=0))

    # Solver setting with explicit seed for the recycle stream
    solver_settings = {
        "method": "direct_substitution",
        "max_iterations": 25,
        "seeds": {
            "Rec": {
                "mass_flow_kg_s": 0.05,
                "temperature_K": 298.15,
                "pressure_Pa": 101325.0,
                "biomass": 0.2,
                "nitrogen": 0.05,
                "oxygen": 0.0,
                "salinity": 35.0,
            }
        },
    }
    ops.append(SetSolver(op="set_solver", solver=solver_settings))

    state = draft.patch(workspace_id, draft_id, revision, ops)
    doc = draft.load_revision(draft.draft_dir(workspace_id, draft_id), state["revision"])["document"]
    return doc, draft_id


# ---------------------------------------------------------------------------
# Acceptance Criterion 2: Pre-run loop energy-closure check
# ---------------------------------------------------------------------------


def test_ac2_unclosed_loop_is_blocked_with_loop_energy_unclosed() -> None:
    """AC2: minimal closed loop without heat sink / temperature specification is blocked

    by validate_document with typed blocker finding LOOP_ENERGY_UNCLOSED naming the loop.
    """
    workspace_id = new_workspace()
    doc, _ = _make_closed_loop_fixture(workspace_id, with_heat_sink=False)

    findings = draft.validate_document(doc, workspace_id)
    blockers = [f for f in findings if f["severity"] == "blocker"]
    energy_findings = [f for f in blockers if f["code"] == "LOOP_ENERGY_UNCLOSED"]

    assert len(energy_findings) == 1
    finding = energy_findings[0]
    assert finding["severity"] == "blocker"
    assert finding["code"] == "LOOP_ENERGY_UNCLOSED"

    # Message must name the loop units
    for expected_unit in ("PBR", "Pump", "Rec"):
        assert expected_unit in finding["message"]


def test_ac2_loop_with_heat_sink_is_not_blocked() -> None:
    """A closed loop with a Cooler or temperature-fixing Heater does NOT emit LOOP_ENERGY_UNCLOSED."""
    workspace_id = new_workspace()
    # With Cooler
    doc_cooler, _ = _make_closed_loop_fixture(workspace_id, with_heat_sink=True, sink_type="Cooler")
    findings_cooler = draft.validate_document(doc_cooler, workspace_id)
    assert not any(f["code"] == "LOOP_ENERGY_UNCLOSED" for f in findings_cooler)

    # With Heater in outlet_temperature mode
    doc_heater, _ = _make_closed_loop_fixture(
        workspace_id, with_heat_sink=True, sink_type="Heater", sink_mode="outlet_temperature"
    )
    findings_heater = draft.validate_document(doc_heater, workspace_id)
    assert not any(f["code"] == "LOOP_ENERGY_UNCLOSED" for f in findings_heater)


def test_loop_energy_unclosed_various_heat_adding_modes() -> None:
    """Test that Heater in heat_added, heat_added_removed, and positive temperature_change

    modes without heat sink emits LOOP_ENERGY_UNCLOSED.
    """
    def _doc_with_heater(mode: str, tc_value: float = 5.0) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "name": "heater loop",
            "compounds": ["Water"],
            "property_package": "NRTL",
            "objects": {
                "h": {
                    "id": "h",
                    "kind": "unit",
                    "type": "Heater",
                    "tag": "H1",
                    "mode": mode,
                    "params": (
                        {"temperature_change": {"value": tc_value, "si": tc_value}}
                        if mode == "temperature_change"
                        else {"heat_duty": {"value": 1000.0, "si": 1000.0}}
                    ),
                    "options": {},
                    "reactions": [],
                },
                "r": {
                    "id": "r",
                    "kind": "unit",
                    "type": "Recycle",
                    "tag": "R1",
                    "mode": None,
                    "params": {},
                    "options": {},
                    "reactions": [],
                },
                "s1": {
                    "id": "s1",
                    "kind": "stream",
                    "tag": "S1",
                    "type": "MaterialStream",
                    "source": {"unit": "h", "port": 0},
                    "target": {"unit": "r", "port": 0},
                    "spec": {},
                },
                "s2": {
                    "id": "s2",
                    "kind": "stream",
                    "tag": "S2",
                    "type": "MaterialStream",
                    "source": {"unit": "r", "port": 0},
                    "target": {"unit": "h", "port": 0},
                    "spec": {},
                },
            },
            "reactions": {},
        }

    # heat_added mode: blocked
    f_added = mixed.loop_energy_findings(_doc_with_heater("heat_added"))
    assert any(item["code"] == "LOOP_ENERGY_UNCLOSED" for item in f_added)

    # heat_added_removed mode: blocked
    f_added_removed = mixed.loop_energy_findings(_doc_with_heater("heat_added_removed"))
    assert any(item["code"] == "LOOP_ENERGY_UNCLOSED" for item in f_added_removed)

    # positive temperature_change mode: blocked
    f_tc_pos = mixed.loop_energy_findings(_doc_with_heater("temperature_change", tc_value=5.0))
    assert any(item["code"] == "LOOP_ENERGY_UNCLOSED" for item in f_tc_pos)

    # non-positive temperature_change mode: NOT blocked
    f_tc_neg = mixed.loop_energy_findings(_doc_with_heater("temperature_change", tc_value=-5.0))
    assert not any(item["code"] == "LOOP_ENERGY_UNCLOSED" for item in f_tc_neg)


# ---------------------------------------------------------------------------
# Acceptance Criterion 1: Closed-loop converges < 25 iterations, mass balance <= 1e-9
# ---------------------------------------------------------------------------


def test_ac1_feedless_closed_loop_runner_with_fixed_fake_evaluation(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """AC1 (plumbing only): `_evaluate` is replaced by a fixed fake, so this does not prove the physics.

    It proves that the runner accepts a feedless closed loop seeded from the solver settings, stops
    in fewer than 25 tear iterations once the fake's fixed point repeats, and keeps the carrier-mass
    balance bookkeeping at zero in and zero out.
    """
    workspace_id = new_workspace()
    doc, _ = _make_closed_loop_fixture(workspace_id, with_heat_sink=True, sink_type="Cooler")

    # Mock DWSIM evaluations offline for Pump and Cooler
    # Pump: adds 50 kPa and +0.05 K shaft work heating
    # Cooler: fixes outlet temperature to 298.15 K and takes -50 kPa pressure drop
    def fake_evaluate(document, part, tear, **kwargs):
        inlet_tear = tear.get("SinkToRec", next(iter(tear.values())))
        # Recycle passes inlet through
        p_rec = float(inlet_tear["pressure_Pa"])
        t_rec = float(inlet_tear["temperature_K"])
        flow = float(inlet_tear["mass_flow_kg_s"])
        culture = dict(inlet_tear.get("culture", {}))

        # Pump segment:
        p_pump_out = p_rec + 50000.0
        t_pump_out = t_rec + 0.05  # shaft work heating

        # PBR passes temperature through (T1 has no energy balance)
        p_pbr_out = p_pump_out - 1000.0  # slight hydraulic loss
        t_pbr_out = t_pump_out
        # PBR periodic steady culture output (biomass converges to target)
        culture_out = dict(culture)
        culture_out["biomass"] = 0.25

        # Cooler segment: sets fixed outlet temperature 298.15 K and drops pressure
        t_cooler_out = 298.15
        p_cooler_out = 101325.0

        produced_state = {
            "temperature_K": t_cooler_out,
            "pressure_Pa": p_cooler_out,
            "mass_flow_kg_s": flow,
            "mass_fractions": {"Water": 1.0},
            "vapor_fraction": 0.0,
            "phases": [{"name": "Mixture", "density_kg_m3": 1000.0}],
            "culture": culture_out,
        }
        known_streams = {
            "SinkToRec": copy.deepcopy(produced_state),
            "RecToPump": copy.deepcopy(produced_state),
            "PumpToPBR": {**copy.deepcopy(produced_state), "temperature_K": t_pump_out, "pressure_Pa": p_pump_out},
            "PBRToSink": {**copy.deepcopy(produced_state), "temperature_K": t_pbr_out, "pressure_Pa": p_pbr_out},
        }
        return {
            "produced": {"RecToPump": produced_state},
            "streams": known_streams,
            "units": {
                "Pump": {"owner": "dwsim"},
                "PBR": {"owner": "jarvis_bio"},
                "Sink": {"owner": "dwsim"},
                "Rec": {"owner": "dwsim"},
            },
            "known": known_streams,
            "culture_generation": {"PBR": {"biomass": 1e-6, "nitrogen": -1e-7, "oxygen": 1e-6}},
            "culture_generation_allowance": {"PBR": {"biomass": 1e-5, "nitrogen": 1e-6, "oxygen": 1e-5}},
            "culture_balances": {},
            "mixed_findings": [],
            "segments": [],
            "max_build_seconds": 0.01,
            "elapsed_s_by_phase": {"build": 0.01, "solve": 0.01, "culture": 0.01},
        }

    monkeypatch.setattr(mixed_runtime, "_evaluate", fake_evaluate)

    result = mixed_runtime.run(
        doc,
        action="run",
        client=object(),
        dwsim_version="10.2.9",
        mcp_sha256="a" * 64,
        run_dir=tmp_path,
        workspace_id=workspace_id,
    )
    mixed_solve = result["mixed_solve"]

    # Must converge in fewer than 25 iterations
    assert mixed_solve["status"] == "completed"
    assert mixed_solve["reason"] == "converged"
    assert len(mixed_solve["history"]) < 25

    # Mass balance closure within 1e-9 relative tolerance
    balances = mixed_solve["balances"]
    carrier = balances["carrier_mass"]
    assert carrier["passed"] is True
    # In a closed loop without feeds/products, in = out = 0, residual = 0
    assert abs(carrier["residual"]) <= 1e-9 * max(abs(carrier["in"]), abs(carrier["out"]), 1e-12)


# ---------------------------------------------------------------------------
# Acceptance Criterion 3: Runtime monotonic temperature runaway / property diagnosis
# ---------------------------------------------------------------------------


def test_ac3_temperature_diagnosis_detects_monotonic_rise() -> None:
    """AC3 unit: _temperature_diagnosis detects monotonic temperature rise around loop."""
    # 3 iterations with positive temperature residuals (+0.5, +0.6, +0.7 K per pass)
    history = [
        {"residuals": {"Rec": {"temperature_K": 0.5 + 0.1 * idx}}}
        for idx in range(3)
    ]
    diag = mixed_runtime._temperature_diagnosis(history)
    assert diag is not None
    assert "temperature rises by 0.7 K per pass around the loop" in diag
    assert "add a heat sink" in diag


def test_ac3_temperature_diagnosis_none_when_temperature_falls_or_oscillates() -> None:
    """_temperature_diagnosis is None when temperature does not monotonically rise."""
    # Negative residuals (cooling)
    history_cooling = [
        {"residuals": {"Rec": {"temperature_K": -0.5 - 0.1 * idx}}}
        for idx in range(3)
    ]
    assert mixed_runtime._temperature_diagnosis(history_cooling) is None

    # Oscillating residuals (+, -, +)
    history_oscillating = [
        {"residuals": {"Rec": {"temperature_K": 0.5}}},
        {"residuals": {"Rec": {"temperature_K": -0.2}}},
        {"residuals": {"Rec": {"temperature_K": 0.3}}},
    ]
    assert mixed_runtime._temperature_diagnosis(history_oscillating) is None


def test_ac3_runtime_solve_monotonic_temperature_runaway_reports_diagnosis(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """AC3 runtime: an unclosed loop undergoing monotonic temperature rise reports

    a clear typed diagnosis naming the loop temperature rise.
    """
    workspace_id = new_workspace()
    doc, _ = _make_closed_loop_fixture(workspace_id, with_heat_sink=False)

    call_count = {"n": 0}

    def rising_evaluate(document, part, tear, **kwargs):
        call_count["n"] += 1
        output = copy.deepcopy(tear)
        for state in output.values():
            # Pump adds 0.8 K per pass without a heat sink
            state["temperature_K"] += 0.8
        return {
            "produced": output,
            "streams": {},
            "units": {},
            "known": {},
            "culture_generation": {},
            "culture_generation_allowance": {},
            "mixed_findings": [],
            "segments": [],
            "max_build_seconds": 0.01,
            "elapsed_s_by_phase": {"build": 0.01, "solve": 0.01, "culture": 0.01},
        }

    monkeypatch.setattr(mixed_runtime, "_evaluate", rising_evaluate)

    # Limit to 4 iterations to simulate runaway stop
    result = mixed_runtime.run(
        doc,
        action="run",
        client=object(),
        dwsim_version="10.2.9",
        mcp_sha256="a" * 64,
        run_dir=tmp_path,
        max_iterations=4,
        workspace_id=workspace_id,
    )
    mixed_solve = result["mixed_solve"]
    assert mixed_solve["status"] != "completed"
    assert "diagnosis" in mixed_solve
    assert "temperature rises by 0.8 K per pass around the loop" in mixed_solve["diagnosis"]


def test_ac3_property_range_failure_maps_to_typed_temperature_diagnosis(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """AC3 runtime: property-range failure during tear evaluation maps to a clear

    typed loop diagnosis next to the original message.
    """
    workspace_id = new_workspace()
    doc, _ = _make_closed_loop_fixture(workspace_id, with_heat_sink=False)

    call_count = {"n": 0}

    def runaway_then_property_failure(document, part, tear, **kwargs):
        call_count["n"] += 1
        if call_count["n"] >= 3:
            # Simulates CoolProp temperature out of domain / PBR property range failure
            raise mixed_runtime.SegmentFailure(
                "PBR",
                {
                    "code": "PBR_HYDRAULICS_OUTSIDE_DOMAIN",
                    "error_type": "PbrFailure",
                    "message": "CoolProp error: temperature 380 K is outside domain bounds",
                },
            )
        output = copy.deepcopy(tear)
        for state in output.values():
            state["temperature_K"] += 1.5
        return {
            "produced": output,
            "streams": {},
            "units": {},
            "known": {},
            "culture_generation": {},
            "culture_generation_allowance": {},
            "mixed_findings": [],
            "segments": [],
            "max_build_seconds": 0.01,
            "elapsed_s_by_phase": {"build": 0.01, "solve": 0.01, "culture": 0.01},
        }

    monkeypatch.setattr(mixed_runtime, "_evaluate", runaway_then_property_failure)

    result = mixed_runtime.run(
        doc,
        action="run",
        client=object(),
        dwsim_version="10.2.9",
        mcp_sha256="a" * 64,
        run_dir=tmp_path,
        workspace_id=workspace_id,
    )
    mixed_solve = result["mixed_solve"]
    assert mixed_solve["status"] == "segment_failed"
    # Diagnosis must name the loop temperature rise
    assert "diagnosis" in mixed_solve
    assert mixed_solve["diagnosis"] == (
        "loop temperature left the property range; add a heat sink or temperature specification")
    # The original failure text is kept; the diagnosis lives only in its own field
    assert "outside domain bounds" in mixed_solve["message"]


def test_ac3_free_text_does_not_count_as_property_range_failure() -> None:
    assert not mixed_runtime._is_temperature_out_of_range("temperature is too high, out of range")
    assert not mixed_runtime._is_temperature_out_of_range({"code": "X", "message": "temperature outside domain"})
    assert mixed_runtime._is_temperature_out_of_range({"code": "PBR_HYDRAULICS_OUTSIDE_DOMAIN"})


def test_temperature_diagnosis_none_when_loop_converges_from_below() -> None:
    # Shrinking residuals across the observed tail (even slow convergence like 0.8, 0.7, 0.6)
    for sequence in ((0.8, 0.7, 0.6), (0.8, 0.4, 0.2)):
        history = [{"residuals": {"Rec": {"temperature_K": value}}} for value in sequence]
        assert mixed_runtime._temperature_runaway_rise(history) is None
        assert mixed_runtime._temperature_diagnosis(history) is None


def test_temperature_diagnosis_reports_persistent_or_rising_residuals() -> None:
    # Persistent positive residuals (e.g. constant work input without heat sink)
    history_persistent = [{"residuals": {"Rec": {"temperature_K": value}}} for value in (0.5, 0.5, 0.5)]
    assert mixed_runtime._temperature_runaway_rise(history_persistent) == 0.5
    diag_p = mixed_runtime._temperature_diagnosis(history_persistent)
    assert diag_p is not None and "temperature rises by 0.5 K per pass around the loop" in diag_p

    # Rising positive residuals
    history_rising = [{"residuals": {"Rec": {"temperature_K": value}}} for value in (0.5, 0.6, 0.7)]
    assert mixed_runtime._temperature_runaway_rise(history_rising) == 0.7
    diag_r = mixed_runtime._temperature_diagnosis(history_rising)
    assert diag_r is not None and "temperature rises by 0.7 K per pass around the loop" in diag_r


def _fed_recycle_document(*, sink: dict[str, Any] | None = None, purge: bool = True) -> dict[str, Any]:
    def unit(uid: str, utype: str, **extra: Any) -> dict[str, Any]:
        return {"id": uid, "kind": "unit", "type": utype, "tag": uid.upper(), "mode": None,
                "params": {}, "options": {}, "reactions": [], **extra}

    def stream(sid: str, source: str | None, target: str | None) -> dict[str, Any]:
        return {"id": sid, "kind": "stream", "tag": sid.upper(), "type": "MaterialStream", "spec": {},
                "source": {"unit": source, "port": 0} if source else None,
                "target": {"unit": target, "port": 0} if target else None}

    objects = {u["id"]: u for u in (unit("mix", "Mixer"), unit("pump", "Pump"), unit("pbr", "PhotobioreactorT1"),
                                    unit("split", "Splitter"))}
    if sink is not None:
        objects["sink"] = unit("sink", sink.pop("type"), **sink)
    chain = ["mix", "pump", "pbr"] + (["sink"] if sink is not None else []) + ["split"]
    streams = [stream("feed", None, "mix")]
    streams += [stream(f"c{i}", a, b) for i, (a, b) in enumerate(zip(chain, chain[1:], strict=False))]
    streams.append(stream("rec", "split", "mix"))
    if purge:
        streams.append(stream("product", "split", None))
    objects.update({item["id"]: item for item in streams})
    return {"schema_version": 1, "name": "fed recycle", "compounds": ["Water"], "property_package": "NRTL",
            "objects": objects, "reactions": {}}


def test_ac2_fed_recycle_loop_with_purge_is_not_blocked() -> None:
    """A feed enters and a product leaves: the through-flow carries the heat, so no blocker."""
    assert mixed.loop_energy_findings(_fed_recycle_document()) == []


def test_ac2_closed_fixture_is_still_blocked() -> None:
    workspace_id = new_workspace()
    doc, _ = _make_closed_loop_fixture(workspace_id, with_heat_sink=False)
    assert [f["code"] for f in mixed.loop_energy_findings(doc)] == ["LOOP_ENERGY_UNCLOSED"]


def _closed_heater_loop(**heater: Any) -> dict[str, Any]:
    doc = _fed_recycle_document(sink=dict({"type": "Heater"}, **heater), purge=False)
    del doc["objects"]["feed"]
    return doc


def test_negative_heater_duty_is_a_sink_not_heat_adding() -> None:
    unit = {"type": "Heater", "mode": "heat_added", "params": {"heat_duty": {"si": -500.0, "value": 0.5, "unit": "kW"}}}
    assert mixed._is_heat_or_work_adding(unit) is False
    unit["params"]["heat_duty"] = {"si": 500.0, "value": -1.0, "unit": "kW"}
    assert mixed._is_heat_or_work_adding(unit) is True
    # A closed loop with only a heat-removing Heater plus a pump still has a heater that is not heat-adding,
    # but the pump adds work: the loop stays blocked unless the Heater fixes a temperature.
    assert mixed._is_heat_or_work_adding({"type": "Pump"}) is True


def test_temperature_fixing_reactors_close_the_loop_energy() -> None:
    for utype, mode, blocked in (("CSTR", "isothermic", False), ("PFR", "outlet_temperature", False),
                                 ("PFR", "adiabatic", True)):
        doc = _closed_heater_loop()
        doc["objects"]["sink"].update({"type": utype, "mode": mode, "params": {}})
        assert bool(mixed.loop_energy_findings(doc)) is blocked, (utype, mode)
    # Heater with no mode is temperature-fixing (its default); a heat_added Heater is not
    assert mixed.loop_energy_findings(_closed_heater_loop(mode=None)) == []
    assert mixed.loop_energy_findings(_closed_heater_loop(mode="heat_added", params={})) != []


def test_closed_loop_without_feed_needs_a_solver_seed(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    workspace_id = new_workspace()
    doc, _ = _make_closed_loop_fixture(workspace_id, with_heat_sink=True)
    doc["solver"]["seeds"] = {"Rec": {"pressure_Pa": 101325.0}}

    def fail(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("must not evaluate without a seed")

    monkeypatch.setattr(mixed_runtime, "_evaluate", fail)
    result = mixed_runtime.run(doc, action="run", client=object(), dwsim_version="10.2.9",
                               mcp_sha256="a" * 64, run_dir=tmp_path, workspace_id=workspace_id)
    mixed_solve = result["mixed_solve"]
    assert mixed_solve["status"] == "segment_failed"
    assert mixed_solve["reason"] == "initial_guess"
    assert mixed_solve["message"] == (
        "A closed loop without a feed needs a solver seed for Rec (mass_flow_kg_s, temperature_K)")


def test_feedless_seed_uses_explicit_values_and_leaves_unseeded_culture_none() -> None:
    workspace_id = new_workspace()
    doc, _ = _make_closed_loop_fixture(workspace_id, with_heat_sink=True)
    seeded = mixed_runtime._seed(doc, mixed.partition(doc), mixed_runtime.tear_solver.parse_settings(doc["solver"]))
    state = seeded["RecToPump"]
    assert state["mass_flow_kg_s"] == 0.05 and state["temperature_K"] == 298.15
    assert state["culture"]["biomass"] == 0.2
    assert any(value is None for value in state["culture"].values())
    assert 0.0 not in [value for name, value in state["culture"].items() if name not in doc["solver"]["seeds"]["Rec"]]


def test_feedless_seed_multi_compound_nondefault_phase_and_composition(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Explicit solver seeds for mass_fractions and vapor_fraction reach the tear state in a feedless loop."""
    workspace_id = new_workspace()
    doc, _ = _make_closed_loop_fixture(workspace_id, with_heat_sink=True)
    doc["compounds"] = ["Water", "Ethanol"]
    explicit_seed = {
        "mass_flow_kg_s": 0.05,
        "temperature_K": 310.15,
        "pressure_Pa": 105000.0,
        "mass_fractions": {"Water": 0.7, "Ethanol": 0.3},
        "vapor_fraction": 0.25,
        "biomass": 0.15,
    }
    settings = mixed_runtime.tear_solver.Settings(seeds={"Rec": explicit_seed})
    part = mixed.partition(doc)
    seeded = mixed_runtime._seed(doc, part, settings)
    state = seeded["RecToPump"]
    assert state["mass_fractions"] == {"Water": 0.7, "Ethanol": 0.3}
    assert state["vapor_fraction"] == 0.25
    assert state["mass_flow_kg_s"] == 0.05
    assert state["temperature_K"] == 310.15
    assert state["pressure_Pa"] == 105000.0
    # Culture merge behavior: seeded field is applied, unseeded fields stay None
    assert state["culture"]["biomass"] == 0.15
    assert state["culture"]["salinity"] is None
    assert state["culture"]["nitrogen"] is None

    # Prove that the seeded nondefault phase and composition reach the tear state in solve evaluation
    captured_tear: dict[str, Any] = {}

    def mock_eval(document, part, tear, **kwargs):
        captured_tear.update(copy.deepcopy(tear))
        return {
            "produced": copy.deepcopy(tear),
            "streams": {},
            "units": {},
            "known": {},
            "culture_generation": {},
            "culture_generation_allowance": {},
            "mixed_findings": [],
            "segments": [],
            "max_build_seconds": 0.01,
            "elapsed_s_by_phase": {"build": 0.01, "solve": 0.01, "culture": 0.01},
        }

    monkeypatch.setattr(mixed_runtime, "_evaluate", mock_eval)
    result = mixed_runtime.run(
        doc,
        action="run",
        client=object(),
        dwsim_version="10.2.9",
        mcp_sha256="a" * 64,
        run_dir=tmp_path,
        workspace_id=workspace_id,
        initial_tear=seeded,
        include_tear_state=True,
    )
    assert captured_tear["RecToPump"]["mass_fractions"] == {"Water": 0.7, "Ethanol": 0.3}
    assert captured_tear["RecToPump"]["vapor_fraction"] == 0.25
    assert result["_tear_state"]["RecToPump"]["mass_fractions"] == {"Water": 0.7, "Ethanol": 0.3}
    assert result["_tear_state"]["RecToPump"]["vapor_fraction"] == 0.25


@pytest.mark.parametrize(
    ("mass_flow", "temperature"),
    [
        (True, 298.15),
        (False, 298.15),
        (0.05, True),
        (0.05, False),
        (True, True),
    ],
)
def test_feedless_seed_rejects_booleans_for_required_numbers(mass_flow: Any, temperature: Any) -> None:
    """Booleans for required mass_flow_kg_s or temperature_K seed numbers must be rejected."""
    workspace_id = new_workspace()
    doc, _ = _make_closed_loop_fixture(workspace_id, with_heat_sink=True)
    settings = mixed_runtime.tear_solver.Settings(
        seeds={"Rec": {"mass_flow_kg_s": mass_flow, "temperature_K": temperature}}
    )
    part = mixed.partition(doc)
    with pytest.raises(mixed_runtime.SegmentFailure) as exc_info:
        mixed_runtime._seed(doc, part, settings)
    assert exc_info.value.segment == "initial_guess"
    assert "A closed loop without a feed needs a solver seed for Rec (mass_flow_kg_s, temperature_K)" in str(
        exc_info.value.detail
    )
