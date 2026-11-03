"""Deterministic unit coverage for spec 172's bounded dynamic contracts."""

from __future__ import annotations

import math
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest
from pydantic import TypeAdapter, ValidationError

from app.modules.process_stack import dynamic_engine
from app.modules.process_stack.draft import apply_ops, content_digest, empty_document
from app.modules.process_stack.draft_models import DraftOp
from app.modules.process_stack.dynamic_models import Scenario, Schedule, ScheduleEvent
from app.modules.process_stack.dynamics import integrate_ode


def _real_scenario(workspace_id: str, duration_s: int, *, cadence_s: int = 3600):
    from app.modules.bio_models import service as bio_models
    from app.modules.environment import profiles
    from app.modules.process_stack import draft
    from app.modules.process_stack.draft_models import SetScenario, SetUnitModel
    from tests.plumbing_170_support import pbr_ops

    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(seconds=duration_s)
    stamps = [start + timedelta(seconds=3600 * i) for i in range(duration_s // 3600 + 1)]
    profile = profiles.create_profile(
        workspace_id, name="dynamic engine fixture",
        timestamps=[stamp.isoformat().replace("+00:00", "Z") for stamp in stamps],
        channels={"par": [500.0] * len(stamps)}, resolution_minutes=60,
        provenance={"kind": "deterministic_test"},
    )
    parameter_set = bio_models.create_set(workspace_id, "dynamic engine parameters")
    for symbol, value, unit in (
        ("K_I", 150.0, "umol/(m**2*s)"), ("K_j_0", 0.001, "kg/m3"), ("k_d", 0.003, "1/hour"),
        ("a", 1.8, "1"), ("b", 0.5, "1"), ("c", 0.1, "1"), ("d", 0.01, "1"),
        ("w_ash", 0.05, "1"), ("k_X", 150.0, "m**2/kg"), ("T_min", 278.15, "K"),
        ("T_opt", 298.15, "K"), ("T_max", 318.15, "K"),
    ):
        parameter_set = bio_models.edit_set_value(
            workspace_id, parameter_set["id"], symbol,
            {"value": value, "unit": unit, "expected_unit": unit},
            parameter_set["revision"], parameter_set["digest"],
        )
    card = bio_models.create_card(
        workspace_id, "dynamic engine real card", parameter_set["id"],
        {"light": "light.monod", "optics": "optics.slab_response_average", "temperature": "temperature.ctmi",
         "nutrients": ["nutrient.monod"], "combination": "combine.liebig", "loss": "loss.first_order",
         "stoichiometry": "stoich.photoautotrophic"},
        {"value": 0.08, "unit": "1/hour"},
    )
    state = draft.create_draft(workspace_id, "dynamic engine fixture")
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], pbr_ops(model=False, flow_kg_s=0.001))
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        SetUnitModel(op="set_unit_model", unit="pbr", model={
            "card_id": card["id"], "card_revision": card["revision"], "card_digest": card["digest"]}),
        SetScenario(op="set_scenario", id="run", value={
            "units": ["PBR"], "profiles": [{"profile_id": profile["profile_id"], "digest": profile["digest"]}],
            "start_utc": start.isoformat().replace("+00:00", "Z"),
            "end_utc": end.isoformat().replace("+00:00", "Z"), "output_cadence_s": cadence_s,
            "rtol": 1e-10, "atol": 1e-12,
            "temperature_source": "unit_mean", "initial": {"PBR": {"X": 0.2, "N": 0.05, "O2": 0.008}},
        }),
    ])
    return state, dynamic_engine.prepare(workspace_id, state["draft_id"], "run")


def test_dynamic_draft_ops_are_semantic_and_legacy_documents_remain_valid() -> None:
    adapter = TypeAdapter(DraftOp)
    legacy = empty_document("legacy")
    legacy["objects"]["pbr"] = {"id": "pbr", "kind": "unit", "type": "PhotobioreactorT1", "tag": "PBR1", "x": 1, "y": 2}
    semantic = apply_ops(
        legacy,
        [
            adapter.validate_python(op)
            for op in (
                {"op": "set_schedule", "id": "feed", "value": {"events": []}},
                {
                    "op": "set_controller",
                    "id": "loop",
                    "value": {
                        "type": "pi",
                        "measurement": "X",
                        "unit": "PBR1",
                        "actuator": "feed:Feed",
                        "output_unit": "m3/s",
                        "cadence_s": 60,
                        "setpoint": 1,
                    },
                },
                {
                    "op": "set_scenario",
                    "id": "run",
                    "value": {
                        "units": ["PBR1"],
                        "profiles": [{"profile_id": "p", "digest": "d"}],
                        "start_utc": "2026-01-01T00:00:00Z",
                        "end_utc": "2026-01-01T01:00:00Z",
                        "output_cadence_s": 3600,
                        "temperature_source": "unit_mean",
                        "schedule_id": "feed",
                        "controllers": ["loop"],
                    },
                },
            )
        ],
    )
    assert legacy.get("schedules", {}) == {}
    assert semantic["schedules"]["feed"]["id"] == "feed"
    assert content_digest(semantic) != content_digest(legacy)
    moved = {**semantic, "objects": {"unit": {"kind": "unit", "tag": "PBR1", "x": 900, "y": -300}}}
    semantic_layout = {**semantic, "objects": {"unit": {"kind": "unit", "tag": "PBR1", "x": 1, "y": 2}}}
    assert content_digest(moved) == content_digest(semantic_layout)


def test_dynamic_operation_union_is_closed_and_delete_is_idempotent() -> None:
    adapter = TypeAdapter(DraftOp)
    operation = adapter.validate_python({"op": "delete_scenario", "id": "run"})
    document = apply_ops({"objects": {}, "schedules": {}, "controllers": {}, "scenarios": {}}, [operation])
    assert document["scenarios"] == {}
    with pytest.raises(ValidationError):
        adapter.validate_python({"op": "execute_python", "code": "pass"})


def test_scenario_utc_offsets_and_duration_limits_are_explicit() -> None:
    base = {
        "id": "run",
        "units": ["PBR1"],
        "profiles": [{"profile_id": "p", "digest": "d"}],
        "start_utc": "2026-10-25T00:00:00Z",
        "end_utc": "2026-10-25T02:00:00+01:00",
        "output_cadence_s": 3600,
        "temperature_source": "unit_mean",
    }
    scenario = Scenario.model_validate(base)
    start = datetime.fromisoformat(scenario.start_utc.replace("Z", "+00:00")).astimezone(UTC)
    end = datetime.fromisoformat(scenario.end_utc).astimezone(UTC)
    # Offsets define instants; a local clock's DST fold never enters the scenario time base.
    assert (end - start).total_seconds() == 3_600
    with pytest.raises(ValidationError):
        Scenario.model_validate({**base, "output_cadence_s": 30})


def test_repeated_schedule_requires_a_finite_bound() -> None:
    with pytest.raises(ValidationError):
        Schedule.model_validate(
            {
                "id": "daily",
                "events": [{"type": "harvest", "time_s": 0, "unit": "PBR1", "fraction": 0.1, "every_s": 86400}],
            }
        )


def _minimal_snapshot(**scenario_changes):
    start = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
    scenario = {
        "output_cadence_s": 60.0, "controller_cadence_s": 60.0, "rtol": 1e-9, "atol": 1e-12,
        "solver_method": "BDF", "downstream_cadence_s": None, "temperature_source": "unit_mean",
        **scenario_changes,
    }
    unit = {"tag": "PBR1", "unit": {"id": "p1"}, "params": {"temperature_mean": 298.15}, "model": {}, "volume_m3": 1.0,
            "state": [1.0, 0.2, 0.01], "card_qualification": "unqualified"}
    payload = {
        "scenario": scenario, "units": [unit], "start_epoch": start, "end_epoch": start + 120,
        "profiles": [{"times": [start, start + 120], "profile": {"channels": {"par": [0.0, 0.0]}},
                       "par_name": "par", "temp_name": "unit_mean",
                       "ref": {"profile_id": "p", "digest": "p"}}],
        "schedule": {"events": []}, "controllers": [], "draft_id": "draft", "revision": 1,
        "content_digest": "digest", "scenario_id": "run", "topology": {"units": [], "streams": [],
            "flows": {}, "feeds": {}},
    }
    feed = {"id": "feed", "tag": "Feed", "source": None, "target": {"unit": "p1"}}
    product = {"id": "product", "tag": "Product", "source": {"unit": "p1"}, "target": None}
    payload["topology"] = {"units": [], "streams": [feed, product],
                           "flows": {"feed": 1e-20, "product": 1e-20},
                           "feeds": {"feed": [1.0, 0.2, 0.01]}}
    return dynamic_engine.Snapshot(payload, "snapshot")


def test_dark_decay_is_analytic_and_runs_are_bit_identical(monkeypatch):
    class Growth:
        nitrogen_quota = 0.05
        oxygen_yield = 1.0
        kla_h = 0.0
        oxygen_saturation = 0.01
        extinction = 0.0
        diameter = 0.05

        @staticmethod
        def rates_at(_par, _temperature, _x, _n):
            return 0.0, 0.01

    monkeypatch.setattr(dynamic_engine.pbr_unit, "build_growth", lambda *_args: Growth())
    monkeypatch.setattr(dynamic_engine, "_profile_values", lambda *_args: (0.0, 298.15))
    monkeypatch.setattr(dynamic_engine, "_profile_par", lambda *_args: 0.0)
    monkeypatch.setattr(dynamic_engine, "_profile_temperature", lambda *_args: 298.15)
    snapshot = _minimal_snapshot()
    first = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _p: None)
    second = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _p: None)
    assert first.status == "succeeded", first.error
    assert first.series["PBR1_X"][-1] == pytest.approx(math.exp(-0.01 * 120 / 3600), rel=1e-7)
    assert first.series["PBR1_X"].tobytes() == second.series["PBR1_X"].tobytes()


def test_constant_rate_limit_matches_closed_form(monkeypatch):
    class Growth:
        nitrogen_quota = 0.0
        oxygen_yield = 0.0
        kla_h = 0.0
        oxygen_saturation = 0.0
        extinction = 0.0
        diameter = 0.05

        @staticmethod
        def rates_at(_par, _temperature, _x, _n):
            return 0.02, 0.0

    monkeypatch.setattr(dynamic_engine.pbr_unit, "build_growth", lambda *_args: Growth())
    monkeypatch.setattr(dynamic_engine, "_profile_values", lambda *_args: (0.0, 298.15))
    monkeypatch.setattr(dynamic_engine, "_profile_par", lambda *_args: 0.0)
    monkeypatch.setattr(dynamic_engine, "_profile_temperature", lambda *_args: 298.15)
    result = dynamic_engine.run(_minimal_snapshot(), cancelled=lambda: False, progress=lambda _p: None)
    assert result.status == "succeeded"
    assert result.series["PBR1_X"][-1] == pytest.approx(math.exp(0.02 * 120 / 3600), rel=1e-7)


def test_harvest_impulse_closes_hand_computed_biomass_and_nitrogen_balance(monkeypatch):
    class Growth:
        nitrogen_quota = 0.05
        oxygen_yield = 1.0
        kla_h = 0.0
        oxygen_saturation = 0.01
        extinction = 0.0
        diameter = 0.05

        @staticmethod
        def rates_at(_par, _temperature, _x, _n):
            return 0.0, 0.0

    monkeypatch.setattr(dynamic_engine.pbr_unit, "build_growth", lambda *_args: Growth())
    monkeypatch.setattr(dynamic_engine, "_profile_values", lambda *_args: (0.0, 298.15))
    monkeypatch.setattr(dynamic_engine, "_profile_par", lambda *_args: 0.0)
    monkeypatch.setattr(dynamic_engine, "_profile_temperature", lambda *_args: 298.15)
    snapshot = _minimal_snapshot()
    snapshot.payload["schedule"]["events"] = [
        {"type": "harvest", "time_s": 60.0, "unit": "PBR1", "fraction": 0.5}
    ]
    result = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _p: None)
    assert result.status == "succeeded"
    assert result.series["PBR1_X"][-1] == pytest.approx(0.5, abs=1e-8)
    assert result.manifest["event_log"][0]["impulse_inventory"] == pytest.approx([-0.5, -0.125, -0.005])
    assert result.manifest["balances"]["aggregate"]["biomass"]["residual_abs_kg"] < 1e-8
    assert result.manifest["balances"]["aggregate"]["total_nitrogen"]["residual_abs_kg"] < 1e-8


def test_sampler_failure_is_recorded_without_changing_trajectory_and_cancel_keeps_computed_rows(monkeypatch):
    class Growth:
        nitrogen_quota = 0.0
        oxygen_yield = 0.0
        kla_h = 0.0
        oxygen_saturation = 0.01
        extinction = 0.0
        diameter = 0.05

        @staticmethod
        def rates_at(_par, _temperature, _x, _n):
            return 0.0, 0.0

    monkeypatch.setattr(dynamic_engine.pbr_unit, "build_growth", lambda *_args: Growth())
    monkeypatch.setattr(dynamic_engine, "_profile_values", lambda *_args: (0.0, 298.15))
    monkeypatch.setattr(dynamic_engine, "_profile_par", lambda *_args: 0.0)
    monkeypatch.setattr(dynamic_engine, "_profile_temperature", lambda *_args: 298.15)
    snapshot = _minimal_snapshot(downstream_cadence_s=60.0)
    baseline = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _p: None)
    calls = []

    def sampler(boundary):
        calls.append(boundary["time_s"])
        if len(calls) == 1:
            raise RuntimeError("sample failed")
        return {"status": "unconverged"}

    sampled = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _p: None, sampler=sampler)
    assert calls == [60.0, 120.0]
    assert sampled.manifest["downstream_outcomes"][0]["status"] == "downstream_unconverged"
    assert sampled.series["PBR1_X"].tobytes() == baseline.series["PBR1_X"].tobytes()
    cancelled = dynamic_engine.run(snapshot, cancelled=lambda: True, progress=lambda _p: None)
    assert cancelled.status == "cancelled"
    assert len(cancelled.series["t_s"]) == 1


def test_onoff_hysteresis_and_pi_anti_windup(monkeypatch):
    unit = {"tag": "PBR1", "unit": {"id": "p1"}}
    topology = {"units": [], "streams": [{"id": "feed", "tag": "Feed", "source": None}], "flows": {}, "feeds": {}}
    state = [0.0] * 10
    onoff = {"id": "switch", "type": "onoff", "measurement": "X", "unit": "PBR1", "actuator": "feed:Feed",
             "cadence_s": 60.0, "setpoint": 1.0, "lower": 0.0, "upper": 1.0, "hysteresis": 0.2,
             "direction": "above", "output": 0.0, "_active": False, "_integral": 0.0}
    flows, log = {}, []
    for time_s, value in ((60, 0.9), (120, 1.1), (180, 1.21)):
        state[0] = value
        dynamic_engine._sample_controllers(np.asarray(state), [unit], [onoff], topology, flows,
                                           {"switch": 1.0}, time_s, log)
    assert [entry["output"] for entry in log] == [1.0, 1.0, 0.0]
    assert flows["feed"] == 0.0

    pi = {"id": "pi", "type": "pi", "measurement": "X", "unit": "PBR1", "actuator": "feed:Feed",
          "cadence_s": 60.0, "setpoint": 1.0, "lower": 0.0, "upper": 1.0, "kp": 2.0, "ki": 1.0,
          "output": 0.0, "_bias": 0.0, "_integral": 0.0}
    state[0] = 0.0
    dynamic_engine._sample_controllers(np.asarray(state), [unit], [pi], topology, flows, {"pi": 1.0}, 60, [])
    assert pi["output"] == 1.0
    assert pi["_integral"] == 0.0


def test_feed_dilution_and_setpoint_events_mutate_runtime_state():
    unit = {"tag": "PBR1", "unit": {"id": "p1"}}
    feed = {"id": "f", "tag": "Feed", "source": None, "target": {"unit": "s"}}
    splitter = {"id": "s", "tag": "Split", "type": "Splitter"}
    outlets = [{"id": "o1", "tag": "one", "source": {"unit": "s"}},
               {"id": "o2", "tag": "two", "source": {"unit": "s"}}]
    topology = {"units": [splitter], "streams": [feed, *outlets], "flows": {"f": 1.0, "o1": 0.4, "o2": 0.6},
                "feeds": {"f": [0.0, 1.0, 0.0]}}
    flows, feeds, setpoints, ctrls = topology["flows"], topology["feeds"], {"c": 1.0}, {"c": {"id": "c"}}
    state = np.zeros(10)
    args = ([unit], [], 0, topology, flows, feeds, setpoints, ctrls)
    dynamic_engine._apply_dynamic_event(state, (0, {"type": "feed_change", "time_s": 1, "stream": "Feed", "value": 2}),
                                        *args)
    dynamic_engine._apply_dynamic_event(state, (0, {"type": "dilution", "time_s": 2, "target": "splitter:Split", "value": 0.7}),
                                        *args)
    dynamic_engine._apply_dynamic_event(state, (0, {"type": "setpoint_change", "time_s": 3, "target": "c", "value": 4}),
                                        *args)
    assert flows["f"] == pytest.approx(2.0)
    assert flows["o1"] == pytest.approx(1.4)
    assert flows["o2"] == pytest.approx(0.6)
    assert setpoints["c"] == 4.0


def test_dynamic_event_preflight_rejects_bad_observation_and_values():
    with pytest.raises(ValidationError):
        ScheduleEvent.model_validate({"type": "harvest", "time_s": 0, "unit": "PBR1",
                                      "fraction": 0.1, "observed": "PBR1.X", "threshold": 1.0})
    units = [{"tag": "PBR1"}]
    topology = {"units": [], "streams": [{"id": "f", "tag": "Feed", "source": None}]}
    for event in (
        {"type": "feed_change", "time_s": 0, "stream": "Feed", "value": {"culture": {"X": -1}}},
        {"type": "setpoint_change", "time_s": 0, "target": "loop", "value": float("nan")},
        {"type": "harvest", "time_s": 0, "unit": "PBR1", "fraction": 0.1,
         "observed": "OTHER.X", "threshold": 1, "direction": "above", "hysteresis": 0.1},
    ):
        with pytest.raises(dynamic_engine.DynamicError):
            dynamic_engine._validate_events([event], units, topology, ["loop"])


def test_separator_rebalance_preserves_recovery_ratio():
    feed = {"id": "f", "tag": "Feed", "source": None, "target": {"unit": "sep"}}
    concentrate = {"id": "c", "tag": "Concentrate", "source": {"unit": "sep", "port": 0}}
    clarified = {"id": "r", "tag": "Return", "source": {"unit": "sep", "port": 1}}
    separator = {"id": "sep", "tag": "F301", "type": "SpecifiedSeparator",
                 "params": {"biomass_recovery": {"si": 95.0}, "concentration_factor": {"si": 20.0}}}
    topology = {"units": [separator], "streams": [feed, concentrate, clarified]}
    flows = {"f": 2.0, "c": 0.0475, "r": 0.9525}
    dynamic_engine._rebalance_flows(topology, flows)
    assert flows["c"] == pytest.approx(0.095)
    assert flows["r"] == pytest.approx(1.905)


def test_controller_series_uses_initial_output_before_first_sample():
    snapshot = _minimal_snapshot()
    controller = {"id": "loop", "output": 0.9, "_initial_output": 0.2}
    rows = [np.zeros(11), np.zeros(11)]
    series = dynamic_engine._result_series(snapshot, [0, 60], rows, snapshot.payload["units"],
                                           [controller], [{"controller": "loop", "time_s": 60, "output": 0.9}])
    assert series["controller_loop"].tolist() == [0.2, 0.9]


def test_continuous_product_outflow_is_reported_as_harvest(monkeypatch):
    class Growth:
        nitrogen_quota = oxygen_yield = kla_h = 0.0
        oxygen_saturation = extinction = 0.0
        diameter = 0.05

        @staticmethod
        def rates_at(_par, _temperature, _x, _n):
            return 0.0, 0.0

    monkeypatch.setattr(dynamic_engine.pbr_unit, "build_growth", lambda *_args: Growth())
    snapshot = _minimal_snapshot()
    snapshot.payload["topology"]["flows"] = {"feed": 0.01, "product": 0.01}
    result = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _p: None)
    assert result.status == "succeeded", result.error
    assert result.series["harvest_X_kg"][-1] == pytest.approx(1.2, rel=1e-5)
    assert result.manifest["productivity"]["harvest_kg"] == pytest.approx(1.2, rel=1e-5)
    assert result.series["feed_Feed_Q_m3_s"].tolist() == [0.01, 0.01, 0.01]


def test_conditional_run_does_not_mutate_snapshot_or_change_repeat_result(monkeypatch):
    class Growth:
        nitrogen_quota = oxygen_yield = kla_h = 0.0
        oxygen_saturation = extinction = 0.0
        diameter = 0.05

        @staticmethod
        def rates_at(_par, _temperature, _x, _n):
            return 0.0, 0.0

    monkeypatch.setattr(dynamic_engine.pbr_unit, "build_growth", lambda *_args: Growth())
    snapshot = _minimal_snapshot()
    snapshot.payload["schedule"]["events"] = [{
        "type": "harvest", "time_s": 0.0, "unit": "PBR1", "fraction": 0.2,
        "observed": "PBR1.X", "threshold": 0.5, "threshold_unit": "kg/m3",
        "direction": "above", "hysteresis": 0.1,
    }]
    original_flows = dict(snapshot.payload["topology"]["flows"])
    first = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _p: None)
    second = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _p: None)
    assert first.status == second.status == "succeeded"
    assert first.series["PBR1_X"].tolist() == second.series["PBR1_X"].tolist()
    assert first.manifest["event_log"] == second.manifest["event_log"]
    assert "_fired" not in snapshot.payload["schedule"]["events"][0]
    assert snapshot.payload["topology"]["flows"] == original_flows


def test_conditional_event_fires_at_sample_cadence_and_applies_feed_change():
    unit = {"tag": "PBR1", "unit": {"id": "p1"}}
    feed = {"id": "f", "tag": "Feed", "source": None}
    topology = {"units": [], "streams": [feed], "flows": {"f": 0.5}, "feeds": {"f": [0.0, 0.0, 0.0]}}
    state = np.zeros(10)
    state[0] = 2.0
    event = {"type": "feed_change", "time_s": 0.0, "stream": "Feed", "value": 1.5,
             "observed": "PBR1.X", "threshold": 1.0, "direction": "above", "hysteresis": 0.1}
    log, active = [], {}
    dynamic_engine._conditional_events(state, [unit], [(0, event)], active, log, 60.0,
                                       topology, topology["flows"], topology["feeds"], {}, {})
    assert topology["flows"]["f"] == 1.5
    assert log[0]["conditional"] is True
    assert log[0]["time_s"] == 60.0


def test_real_prepared_events_sort_expansions_and_preserve_stable_declared_order():
    from app.modules.process_stack import draft
    from app.modules.process_stack.draft_models import SetScenario, SetSchedule
    from tests.plumbing_170_support import new_workspace

    workspace_id = new_workspace()
    state, _snapshot = _real_scenario(workspace_id, 3600, cadence_s=60)
    events = [
        {"type": "harvest", "time_s": 60, "unit": "PBR", "fraction": 0.1},
        {"type": "harvest", "time_s": 120, "unit": "PBR", "fraction": 0.1},
        {"type": "harvest", "time_s": 30, "unit": "PBR", "fraction": 0.1},
        {"type": "harvest", "time_s": 60, "unit": "PBR", "fraction": 0.2},
    ]
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        SetSchedule(op="set_schedule", id="ordered", value={"events": events}),
        SetScenario(op="set_scenario", id="run", value={
            **_snapshot.payload["scenario"], "schedule_id": "ordered",
        }),
    ])
    snapshot = dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    expanded = snapshot.payload["schedule"]["events"]
    assert [item["time_s"] for item in expanded] == [30, 60, 60, 120]
    result = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _value: None)
    assert result.status == "succeeded", result.error
    log = result.manifest["event_log"]
    assert [item["time_s"] for item in log] == [30, 60, 60, 120]
    assert [item["declared_order"] for item in log] == [2, 0, 3, 1]
    assert [item["repeat_k"] for item in log] == [0, 0, 0, 0]


def test_real_run_applies_every_event_even_if_snapshot_events_are_unsorted():
    from dataclasses import replace

    from tests.plumbing_170_support import new_workspace

    workspace_id = new_workspace()
    _state, snapshot = _real_scenario(workspace_id, 3600, cadence_s=60)
    events = [{"type": "harvest", "time_s": t, "unit": "PBR", "fraction": 0.1,
               "declared_order": i, "repeat_k": 0} for i, t in enumerate((60, 120, 30))]
    payload = {**snapshot.payload, "schedule": {"events": events}}
    unsorted_snapshot = replace(snapshot, payload=payload) if hasattr(snapshot, "__dataclass_fields__") \
        else type(snapshot)(payload=payload, digest=snapshot.digest)
    result = dynamic_engine.run(unsorted_snapshot, cancelled=lambda: False, progress=lambda _v: None)
    assert result.status == "succeeded", result.error
    assert [item["time_s"] for item in result.manifest["event_log"]] == [30, 60, 120]


def test_downstream_enabled_without_cadence_defaults_to_daily_and_explicit_hourly_is_kept():
    from app.modules.process_stack.dynamic_models import Scenario
    from tests.plumbing_170_support import new_workspace

    _state, snapshot = _real_scenario(new_workspace(), 3600)
    base = {key: value for key, value in snapshot.payload["scenario"].items()
            if key not in {"downstream_enabled", "downstream_cadence_s"}}
    assert Scenario.model_validate({**base, "downstream_enabled": True}).downstream_cadence_s == 86400.0
    assert Scenario.model_validate(
        {**base, "downstream_enabled": True, "downstream_cadence_s": 3600}).downstream_cadence_s == 3600
    assert Scenario.model_validate(base).downstream_cadence_s is None


def test_real_conditional_event_rearms_only_after_leaving_hysteresis_band():
    from tests.plumbing_170_support import new_workspace

    workspace_id = new_workspace()
    _state, snapshot = _real_scenario(workspace_id, 3600, cadence_s=60)
    event = {"type": "feed_change", "time_s": 0.0, "stream": "Feed", "value": 0.5,
             "observed": "PBR.X", "threshold": 1.0, "threshold_unit": "kg/m3",
             "direction": "above", "hysteresis": 0.2}
    unit = snapshot.payload["units"][0]
    topology = snapshot.payload["topology"]
    flows, feeds = dict(topology["flows"]), dict(topology["feeds"])
    state = np.zeros(11)
    log, armed = [], {}
    for time_s, value in ((60, 1.1), (120, 1.05), (180, 0.9), (240, 0.79), (300, 0.95), (360, 1.0)):
        state[0] = value
        dynamic_engine._conditional_events(state, [unit], [(0, event)], armed, log, time_s,
                                           topology, flows, feeds, {}, {})
    assert [item["time_s"] for item in log if item.get("conditional")] == [60, 360]


def test_real_prepare_admission_limits_have_typed_codes():
    from app.modules.process_stack import draft
    from app.modules.process_stack.draft_models import SetScenario, SetSchedule
    from tests.plumbing_170_support import new_workspace

    workspace_id = new_workspace()
    state, snapshot = _real_scenario(workspace_id, 3600, cadence_s=3600)
    base = snapshot.payload["scenario"]

    def reject(value: dict, code: str, schedule: dict | None = None) -> None:
        nonlocal state
        ops: list[Any] = [SetScenario(op="set_scenario", id="run", value=value)]
        if schedule is not None:
            ops.insert(0, SetSchedule(op="set_schedule", id="bounded", value=schedule))
        state = draft.patch(workspace_id, state["draft_id"], state["revision"], ops)
        with pytest.raises(dynamic_engine.DynamicError) as raised:
            dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
        assert raised.value.code == code

    start = datetime.fromisoformat(base["start_utc"].replace("Z", "+00:00"))
    reject({**base, "end_utc": (start + timedelta(days=121)).isoformat().replace("+00:00", "Z")},
           "DURATION_LIMIT")
    reject({**base, "end_utc": (start + timedelta(days=30)).isoformat().replace("+00:00", "Z"),
            "output_cadence_s": 60}, "OUTPUT_LIMIT")
    repeated = {"type": "inoculation", "time_s": 0, "unit": "PBR", "value": 0.2,
                "value_unit": "kg/m3", "every_s": 1, "count": 600}
    reject({**base, "schedule_id": "bounded"}, "EVENT_LIMIT", {"events": [repeated, repeated]})
    revision = draft.load_revision(draft.draft_dir(workspace_id, state["draft_id"]), state["revision"])
    invalid_document = revision["document"]
    invalid_document["scenarios"]["run"]["controllers"] = [f"c{i}" for i in range(17)]
    stored = draft._write_revision(draft.draft_dir(workspace_id, state["draft_id"]), invalid_document,
                                   parent=state["revision"], actor="test", ops=[])
    state = {**state, "revision": stored["revision"]}
    with pytest.raises(dynamic_engine.DynamicError) as raised:
        dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    assert raised.value.code == "CONTROLLER_LIMIT"
    reject({**base, "end_utc": (start + timedelta(days=120)).isoformat().replace("+00:00", "Z"),
            "downstream_enabled": True, "downstream_cadence_s": 3600}, "DWSIM_SAMPLE_LIMIT")


def test_real_prepare_rejects_unsupported_measurements_and_actuators():
    from app.modules.process_stack import draft
    from app.modules.process_stack.draft_models import SetController, SetScenario
    from tests.plumbing_170_support import new_workspace

    workspace_id = new_workspace()
    state, snapshot = _real_scenario(workspace_id, 3600)
    controller = {"id": "unsafe", "type": "pi", "measurement": "CO2", "unit": "PBR",
                  "actuator": "feed:Feed", "output_unit": "m3/s", "cadence_s": 60,
                  "setpoint": 0.1, "lower": 0, "upper": 0.001, "output": 0}
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        SetController(op="set_controller", id="unsafe", value=controller),
        SetScenario(op="set_scenario", id="run", value={**snapshot.payload["scenario"],
                                                          "controllers": ["unsafe"]}),
    ])
    with pytest.raises(dynamic_engine.DynamicError) as measurement:
        dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    assert measurement.value.code == "UNSUPPORTED_MEASUREMENT"
    controller["measurement"] = "X"
    controller["actuator"] = "thermal:PBR"
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        SetController(op="set_controller", id="unsafe", value=controller),
    ])
    with pytest.raises(dynamic_engine.DynamicError) as actuator:
        dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    assert actuator.value.code == "UNSUPPORTED_ACTUATOR"


def test_real_profile_gap_coverage_and_unused_null_channels():
    from app.modules.environment import profiles
    from app.modules.process_stack import draft
    from app.modules.process_stack.draft_models import SetScenario
    from tests.plumbing_170_support import new_workspace

    workspace_id = new_workspace()
    state, snapshot = _real_scenario(workspace_id, 3600)
    start = datetime.fromisoformat(snapshot.payload["scenario"]["start_utc"].replace("Z", "+00:00"))
    stamps = [start + timedelta(hours=index) for index in range(2)]

    def bind(channels: dict[str, list[float | None]], temperature: str = "unit_mean") -> Any:
        nonlocal state
        profile = profiles.create_profile(
            workspace_id, name="dynamic gap fixture",
            timestamps=[stamp.isoformat().replace("+00:00", "Z") for stamp in stamps],
            channels=channels, resolution_minutes=60, provenance={"kind": "deterministic_test"},
        )
        scenario = {**snapshot.payload["scenario"], "temperature_source": temperature,
                    "profiles": [{"profile_id": profile["profile_id"], "digest": profile["digest"]}]}
        state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
            SetScenario(op="set_scenario", id="run", value=scenario),
        ])
        return profile

    bind({"par": [100.0, None], "sea_temperature": [280.0, 281.0]})
    with pytest.raises(dynamic_engine.DynamicError) as raised:
        dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    assert raised.value.code == "PROFILE_GAP"
    assert raised.value.detail == {"channel": "par", "time_utc": stamps[1].isoformat().replace("+00:00", "Z")}

    bind({"par": [100.0, 100.0], "sea_temperature": [280.0, None]}, "sea_temperature")
    with pytest.raises(dynamic_engine.DynamicError) as raised:
        dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    assert raised.value.code == "PROFILE_GAP"
    assert raised.value.detail["channel"] == "sea_temperature"
    assert raised.value.detail["time_utc"] == stamps[1].isoformat().replace("+00:00", "Z")

    bind({"par": [100.0, 100.0], "sea_temperature": [None, None]})
    unused_null = dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    assert unused_null.payload["profiles"][0]["profile"]["channels"]["sea_temperature"] == [None, None]

    short = profiles.create_profile(
        workspace_id, name="short profile",
        timestamps=[stamps[0].isoformat().replace("+00:00", "Z")], channels={"par": [100.0]},
        resolution_minutes=None,
        provenance={"kind": "deterministic_test"},
    )
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        SetScenario(op="set_scenario", id="run", value={
            **snapshot.payload["scenario"],
            "profiles": [{"profile_id": short["profile_id"], "digest": short["digest"]}],
        }),
    ])
    with pytest.raises(dynamic_engine.DynamicError) as raised:
        dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    assert raised.value.code == "PROFILE_COVERAGE"


def test_real_temperature_is_linear_while_par_uses_interval_hold():
    from app.modules.environment import profiles
    from app.modules.process_stack import draft
    from app.modules.process_stack.draft_models import SetScenario
    from tests.plumbing_170_support import new_workspace

    workspace_id = new_workspace()
    state, original = _real_scenario(workspace_id, 3600)
    start = datetime.fromisoformat(original.payload["scenario"]["start_utc"].replace("Z", "+00:00"))
    stamps = [start + timedelta(hours=index) for index in range(3)]
    profile = profiles.create_profile(
        workspace_id, name="dynamic temporal fixture",
        timestamps=[stamp.isoformat().replace("+00:00", "Z") for stamp in stamps],
        channels={"par": [10.0, 20.0, 30.0], "sea_temperature": [280.0, 300.0, 320.0]},
        resolution_minutes=60, provenance={"kind": "deterministic_test"},
    )
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        SetScenario(op="set_scenario", id="run", value={
            **original.payload["scenario"], "temperature_source": "sea_temperature",
            "profiles": [{"profile_id": profile["profile_id"], "digest": profile["digest"]}],
        }),
    ])
    snapshot = dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    assert dynamic_engine._profile_par(snapshot, 1800) == 20.0
    assert dynamic_engine._profile_temperature(snapshot, 1800) == pytest.approx(290.0)


def test_real_scenario_elapsed_integration_is_unchanged_across_european_dst():
    from app.modules.environment import profiles
    from app.modules.process_stack import draft
    from app.modules.process_stack.draft_models import SetScenario
    from tests.plumbing_170_support import new_workspace

    workspace_id = new_workspace()
    state, original = _real_scenario(workspace_id, 3600, cadence_s=3600)
    utc_start = datetime(2026, 10, 24, tzinfo=UTC)
    stamps = [utc_start + timedelta(hours=index) for index in range(49)]
    profile = profiles.create_profile(
        workspace_id, name="DST elapsed fixture",
        timestamps=[stamp.isoformat().replace("+00:00", "Z") for stamp in stamps],
        channels={"par": [100.0] * len(stamps)}, resolution_minutes=60,
        provenance={"kind": "deterministic_test"},
    )
    scenario = {
        **original.payload["scenario"],
        "start_utc": "2026-10-24T00:00:00Z", "end_utc": "2026-10-26T00:00:00Z",
        "output_cadence_s": 3600,
        "profiles": [{"profile_id": profile["profile_id"], "digest": profile["digest"]}],
    }
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        SetScenario(op="set_scenario", id="run", value=scenario),
    ])
    utc_snapshot = dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    utc_result = dynamic_engine.run(utc_snapshot, cancelled=lambda: False, progress=lambda _value: None)
    assert utc_result.status == "succeeded", utc_result.error
    scenario["start_utc"] = "2026-10-24T02:00:00+02:00"
    scenario["end_utc"] = "2026-10-26T01:00:00+01:00"
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        SetScenario(op="set_scenario", id="run", value=scenario),
    ])
    local_snapshot = dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    local_result = dynamic_engine.run(local_snapshot, cancelled=lambda: False, progress=lambda _value: None)
    assert local_result.status == "succeeded", local_result.error
    assert local_result.series["t_s"].tolist() == utc_result.series["t_s"].tolist()
    for key in ("PBR_X", "PBR_N", "PBR_O2"):
        assert np.array_equal(local_result.series[key], utc_result.series[key])


def test_dynamic_draft_ops_keep_cas_and_restore_semantics():
    from app.modules.process_stack import draft
    from app.modules.process_stack.draft_models import SetController, SetScenario, SetSchedule
    from tests.plumbing_170_support import new_workspace

    workspace_id = new_workspace()
    state, snapshot = _real_scenario(workspace_id, 3600)
    original_revision = state["revision"]
    scenario = {**snapshot.payload["scenario"], "schedule_id": "schedule", "controllers": ["control"]}
    added = draft.patch(workspace_id, state["draft_id"], original_revision, [
        SetSchedule(op="set_schedule", id="schedule", value={"events": []}),
        SetController(op="set_controller", id="control", value={
            "type": "onoff", "measurement": "X", "unit": "PBR", "actuator": "feed:Feed",
            "output_unit": "m3/s", "cadence_s": 60, "setpoint": 0.5,
            "lower": 0.0, "upper": 0.001, "output": 0.0,
        }),
        SetScenario(op="set_scenario", id="run", value=scenario),
    ])
    with pytest.raises(draft.DraftError) as stale:
        draft.patch(workspace_id, state["draft_id"], original_revision, [
            SetSchedule(op="set_schedule", id="late", value={"events": []}),
        ])
    assert stale.value.code == "revision_conflict"
    restored = draft.restore(workspace_id, state["draft_id"], added["revision"], original_revision)
    assert restored["revision"] != original_revision
    assert draft.current_digest(workspace_id, state["draft_id"]) == snapshot.payload["content_digest"]


@pytest.mark.parametrize(("rate", "expected"), [(float("nan"), "STATE_NONFINITE"), (-1e6, "STATE_NEGATIVE")])
def test_real_engine_state_failures_keep_typed_diagnostic_series(monkeypatch, rate: float, expected: str):
    from types import SimpleNamespace

    from app.modules.process_stack import dynamics
    from tests.plumbing_170_support import new_workspace

    class Growth:
        nitrogen_quota = oxygen_yield = kla_h = 0.0
        oxygen_saturation = extinction = 0.0
        diameter = 0.05

        @staticmethod
        def rates_at(_par, _temperature, _x, _n):
            return rate, 0.0

    workspace_id = new_workspace()
    _state, snapshot = _real_scenario(workspace_id, 3600, cadence_s=3600)
    monkeypatch.setattr(dynamic_engine.pbr_unit, "build_growth", lambda *_args: Growth())

    def invalid_solver(rhs: Any, initial: Any, grid: Any, **_kwargs: Any) -> Any:
        derivative = np.asarray(rhs(float(grid[0]), tuple(initial)), dtype=np.float64)
        final = np.asarray(initial, dtype=np.float64) + derivative * (float(grid[-1]) - float(grid[0]))
        return SimpleNamespace(success=True, message="injected state", times=np.asarray(grid),
                               states=np.vstack((initial, final)))

    monkeypatch.setattr(dynamics, "integrate_ode", invalid_solver)
    result = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _value: None)
    assert result.status == "failed"
    assert result.error and result.error["code"] == expected
    assert len(result.series["t_s"]) == 1
    assert "PBR_X" in result.series
    assert result.manifest["artifact_label"] == "diagnostic_failed"
    assert result.manifest["channels"]["PBR_X"] == "kg/m3"
    assert result.manifest["balances"]["status"] == "partial"


def test_real_engine_sampler_retry_preserves_biology_and_cancel_boundary(monkeypatch):
    from app.modules.process_stack import draft, dynamic_downstream, mixed_runtime
    from app.modules.process_stack.draft_models import AddStream, AddUnit, Connect, SetScenario
    from tests.plumbing_170_support import new_workspace

    workspace_id = new_workspace()
    state, initial = _real_scenario(workspace_id, 3600, cadence_s=3600)
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        AddUnit(op="add_unit", id="heater", type="Heater", tag="Heater", x=300, y=0),
        AddStream(op="add_stream", id="heater_out", tag="HeaterOut", x=400, y=0),
        Connect(op="connect", stream="product", end="target", unit="heater", port=0),
        Connect(op="connect", stream="heater_out", end="source", unit="heater", port=0),
        SetScenario(op="set_scenario", id="run", value={
            **initial.payload["scenario"], "downstream_cadence_s": 3600,
        }),
    ])
    snapshot = dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    monkeypatch.setattr(mixed_runtime, "_seed", lambda *_args, **_kwargs: None)
    calls: list[dict[str, Any]] = []

    def runner(_document: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        if kwargs.get("initial_tear") is not None:
            return {"status": "failed", "mixed_solve": {"reason": "max_iterations", "history": [
                {"max_normalized_residual": 0.25}]}}
        return {"status": "failed", "_last_attempt": {
            "guess": {"Tear": {"mass_flow_kg_s": 2.0}},
            "output": {"Tear": {"mass_flow_kg_s": 4.0}}},
            "mixed_solve": {"reason": "max_iterations", "history": [
                {"max_normalized_residual": 0.5}]}}

    sampler = dynamic_downstream.build_sampler(snapshot, client_factory=lambda: object(), runner=runner)
    assert sampler is not None
    sampled = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _value: None, sampler=sampler)
    baseline = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _value: None)
    assert sampled.status == baseline.status == "succeeded"
    assert len(calls) == 2
    outcome = sampled.manifest["downstream_outcomes"][0]
    assert outcome["status"] == "downstream_unconverged"
    assert outcome["residual"] == 0.25 and outcome["time_s"] == 3600
    for channel in ("PBR_X", "PBR_N", "PBR_O2"):
        assert sampled.series[channel].tobytes() == baseline.series[channel].tobytes()

    timeout_calls = 0

    def timeout_runner(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal timeout_calls
        timeout_calls += 1
        raise TimeoutError("injected DWSIM timeout")

    timeout_sampler = dynamic_downstream.build_sampler(
        snapshot, client_factory=lambda: object(), runner=timeout_runner,
    )
    assert timeout_sampler is not None
    timed_out = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _value: None,
                                   sampler=timeout_sampler)
    assert timed_out.status == "succeeded" and timeout_calls == 1
    assert timed_out.manifest["downstream_outcomes"][0]["reason"] == "timeout"
    for channel in ("PBR_X", "PBR_N", "PBR_O2"):
        assert timed_out.series[channel].tobytes() == baseline.series[channel].tobytes()

    cancelled = [False]

    def cancel_sampler(_boundary: dict[str, Any]) -> dict[str, Any]:
        cancelled[0] = True
        return {"status": "succeeded", "streams": {}}

    stopped = dynamic_engine.run(snapshot, cancelled=lambda: cancelled[0], progress=lambda _value: None,
                                 sampler=cancel_sampler)
    assert stopped.status == "cancelled"
    assert stopped.manifest["artifact_label"] == "diagnostic_cancelled"


def test_real_prepare_refuses_coupled_pbrs_with_different_card_or_parameter_digest():
    from app.modules.bio_models import service as bio_models
    from app.modules.process_stack import draft
    from app.modules.process_stack.draft_models import (
        AddStream,
        AddUnit,
        Connect,
        Disconnect,
        SetScenario,
        SetUnitModel,
        SetUnitParams,
    )
    from tests.plumbing_170_support import new_workspace, pbr_quantities, pin_of

    workspace_id = new_workspace()
    state, snapshot = _real_scenario(workspace_id, 3600)
    model = snapshot.payload["units"][0]["model"]
    card = bio_models.create_card(
        workspace_id, "dynamic distinct card", model["set"]["id"],
        model["card"]["factors"], {"value": 0.08, "unit": "1/hour"},
    )
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        AddUnit(op="add_unit", id="pbr2", type="PhotobioreactorT1", tag="PBR2", x=300, y=0),
        SetUnitParams(op="set_unit_params", unit="pbr2", values=pbr_quantities()),
        SetUnitModel(op="set_unit_model", unit="pbr2", model=pin_of(card)),
        AddStream(op="add_stream", id="between", tag="Between", x=200, y=0),
        Disconnect(op="disconnect", stream="product", end="source"),
        Connect(op="connect", stream="between", end="source", unit="pbr", port=0),
        Connect(op="connect", stream="between", end="target", unit="pbr2", port=0),
        Connect(op="connect", stream="product", end="source", unit="pbr2", port=0),
        SetScenario(op="set_scenario", id="run", value={
            **snapshot.payload["scenario"], "units": ["PBR", "PBR2"],
            "initial": {"PBR": {"X": 0.2, "N": 0.05, "O2": 0.008},
                        "PBR2": {"X": 0.2, "N": 0.05, "O2": 0.008}},
        }),
    ])
    with pytest.raises(dynamic_engine.DynamicError) as raised:
        dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    assert raised.value.code == "TOPOLOGY_CARD_MISMATCH"
    assert set(raised.value.detail["units"]) == {"PBR", "PBR2"}


def test_integrator_two_point_grid_returns_only_requested_times():
    solved = integrate_ode(lambda _t, _y: [1.0], [0.0], [0.0, 2.0])
    assert solved.success, solved.message
    assert solved.times == (0.0, 2.0)
    assert solved.states[0] == (0.0,)
    assert solved.states[-1][0] == pytest.approx(2.0)


def test_par_is_held_through_interval_end_and_unit_mean_temperature_is_local():
    snapshot = _minimal_snapshot()
    start = snapshot.payload["start_epoch"]
    snapshot.payload["profiles"][0].update(
        times=[start + 60.0, start + 120.0],
        profile={"channels": {"par": [11.0, 22.0]}},
    )
    snapshot.payload["units"].append({"tag": "PBR2", "params": {"temperature_mean": 310.0}})
    assert dynamic_engine._profile_par(snapshot, 30.0) == 11.0
    assert dynamic_engine._profile_par(snapshot, 60.0) == 11.0
    assert dynamic_engine._profile_par(snapshot, 61.0) == 22.0
    assert dynamic_engine._profile_temperature(snapshot, 10.0, 1) == 310.0


def test_engine_uses_segment_par_at_the_right_endpoint(monkeypatch):
    seen = []

    class Growth:
        nitrogen_quota = oxygen_yield = kla_h = 0.0
        oxygen_saturation = extinction = 0.0
        diameter = 0.05

        @staticmethod
        def rates_at(par, _temperature, _x, _n):
            seen.append(par)
            return 0.0, 0.0

    monkeypatch.setattr(dynamic_engine.pbr_unit, "build_growth", lambda *_args: Growth())
    snapshot = _minimal_snapshot()
    start = snapshot.payload["start_epoch"]
    snapshot.payload["profiles"][0].update(
        times=[start + 60.0, start + 120.0], profile={"channels": {"par": [11.0, 22.0]}}
    )
    result = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _p: None)
    assert result.status == "succeeded", result.error
    assert result.series["par"].tolist() == [11.0, 11.0, 22.0]
    assert seen and set(seen) == {11.0, 22.0}


def test_cancelled_result_uses_named_one_dimensional_channels(monkeypatch):
    class Growth:
        nitrogen_quota = oxygen_yield = kla_h = 0.0
        oxygen_saturation = extinction = 0.0
        diameter = 0.05

        @staticmethod
        def rates_at(_par, _temperature, _x, _n):
            return 0.0, 0.0

    monkeypatch.setattr(dynamic_engine.pbr_unit, "build_growth", lambda *_args: Growth())
    monkeypatch.setattr(dynamic_engine, "_profile_par", lambda *_args: 0.0)
    monkeypatch.setattr(dynamic_engine, "_profile_temperature", lambda *_args: 298.15)
    calls = iter((False, True))
    result = dynamic_engine.run(_minimal_snapshot(), cancelled=lambda: next(calls), progress=lambda _p: None)
    assert result.status == "cancelled"
    assert "state" not in result.series
    assert all(value.ndim == 1 for value in result.series.values())
    assert {"t_s", "par", "temperature", "PBR1_X", "PBR1_N", "PBR1_O2"} <= result.series.keys()


def test_sampler_receives_product_concentration_from_algebraic_splitter():
    units = [{"tag": "PBR1", "unit": {"id": "p1"}, "params": {}, "model": {}, "volume_m3": 1,
              "state": [2.0, 0.3, 0.1], "growth": type("Growth", (), {"nitrogen_quota": 0.05})()}]
    splitter = {"id": "s1", "type": "Splitter", "tag": "Split"}
    topology = {
        "units": [splitter],
        "streams": [
            {"id": "in", "tag": "In", "source": {"unit": "p1"}, "target": {"unit": "s1"}},
            {"id": "out", "tag": "Out", "source": {"unit": "s1"}, "target": None},
        ],
        "flows": {"in": 1.0, "out": 1.0}, "feeds": {},
    }
    boundary = dynamic_engine._boundary_streams(topology, np.array([2.0, 0.3, 0.1, 0, 0, 0, 0, 0, 0, 0]),
                                                 units, topology["flows"], topology["feeds"])
    assert boundary["Out"]["X_kg_m3"] == 2.0
    assert boundary["Out"]["N_kg_m3"] == 0.3
    assert boundary["Out"]["O2_kg_m3"] == 0.1


def test_separator_conserves_biomass_across_concentrate_and_clarified_outlets():
    unit = {"id": "sep", "type": "SpecifiedSeparator", "tag": "Sep", "params": {
        "biomass_recovery": {"si": 95.0}, "concentration_factor": {"si": 20.0}}}
    streams = [
        {"id": "in", "tag": "In", "source": {"unit": "p1"}, "target": {"unit": "sep"}},
        {"id": "conc", "tag": "Conc", "source": {"unit": "sep", "port": 0}, "target": None},
        {"id": "clar", "tag": "Clar", "source": {"unit": "sep", "port": 1}, "target": None},
    ]
    pbr = {"tag": "PBR", "unit": {"id": "p1"}, "params": {}, "model": {}, "volume_m3": 1,
           "state": [2.0, 0.3, 0.1], "growth": type("Growth", (), {"nitrogen_quota": 0.05})()}
    topology = {"units": [unit], "streams": streams,
                "flows": {"in": 1.0, "conc": 0.0475, "clar": 0.9525}, "feeds": {}}
    state = np.array([2.0, 0.3, 0.1, 0.0, 0.0, 0.0, 0.0])
    boundary = dynamic_engine._boundary_streams(topology, state, [pbr], topology["flows"], {})
    biomass_out = boundary["Conc"]["flow_m3_s"] * boundary["Conc"]["X_kg_m3"]
    biomass_out += boundary["Clar"]["flow_m3_s"] * boundary["Clar"]["X_kg_m3"]
    assert biomass_out == pytest.approx(2.0)


def test_topology_solves_specified_separator_volumetric_outlets():
    pbr = {"id": "p1", "kind": "unit", "type": "PhotobioreactorT1", "tag": "PBR"}
    separator = {"id": "sep", "kind": "unit", "type": "SpecifiedSeparator", "tag": "Sep",
                 "params": {"biomass_recovery": {"si": 95.0}, "concentration_factor": {"si": 20.0}}}
    feed = {"id": "feed", "kind": "stream", "type": "MaterialStream", "tag": "Feed", "source": None,
            "target": {"unit": "p1"}, "spec": {"culture": {}}}
    product = {"id": "product", "kind": "stream", "type": "MaterialStream", "tag": "Product",
               "source": {"unit": "p1"}, "target": {"unit": "sep"}, "spec": {}}
    concentrate = {"id": "conc", "kind": "stream", "type": "MaterialStream", "tag": "Conc",
                   "source": {"unit": "sep", "port": 0}, "target": None, "spec": {}}
    clarified = {"id": "clar", "kind": "stream", "type": "MaterialStream", "tag": "Clar",
                 "source": {"unit": "sep", "port": 1}, "target": None, "spec": {}}
    result = dynamic_engine._topology(
        {"objects": {item["id"]: item for item in (pbr, separator, feed, product, concentrate, clarified)}},
        [{"unit": pbr, "tag": "PBR"}], {"feed_flows": {"Feed": 1.0}},
    )
    assert result["flows"]["conc"] == pytest.approx(0.0475)
    assert result["flows"]["clar"] == pytest.approx(0.9525)


def test_pump_and_recycle_are_identity_nodes_for_culture_concentrations():
    pbr = {"tag": "PBR", "unit": {"id": "p1"}, "params": {}, "model": {}, "volume_m3": 1,
           "state": [0.8, 0.03, 0.006], "growth": type("Growth", (), {"nitrogen_quota": 0.04})()}
    direct_stream = {"id": "direct", "tag": "Direct", "source": {"unit": "p1"},
                     "target": {"unit": "p1"}}
    direct = {"units": [], "streams": [direct_stream], "flows": {"direct": 1.0}, "feeds": {}}
    direct_runtime = dynamic_engine._network_runtime(direct, direct["flows"], [pbr])
    direct_values = dynamic_engine._network_concentrations(
        direct, direct_runtime, tuple([0.8, 0.03, 0.006, 0.0, 0.0, 0.0, 0.0]), [pbr], direct["flows"])

    pump = {"id": "pump", "tag": "Pump", "type": "Pump"}
    recycle = {"id": "recycle", "tag": "Recycle", "type": "Recycle"}
    streams = [
        {"id": "to_pump", "tag": "ToPump", "source": {"unit": "p1"}, "target": {"unit": "pump"}},
        {"id": "to_recycle", "tag": "ToRecycle", "source": {"unit": "pump"}, "target": {"unit": "recycle"}},
        {"id": "return", "tag": "Return", "source": {"unit": "recycle"}, "target": {"unit": "p1"}},
    ]
    chained = {"units": [pump, recycle], "streams": streams,
               "flows": {"to_pump": 1.0, "to_recycle": 1.0, "return": 1.0}, "feeds": {}}
    chained_runtime = dynamic_engine._network_runtime(chained, chained["flows"], [pbr])
    chained_values = dynamic_engine._network_concentrations(
        chained, chained_runtime, tuple([0.8, 0.03, 0.006, 0.0, 0.0, 0.0, 0.0]), [pbr], chained["flows"])
    assert chained_values["stream:return"] == direct_values["stream:direct"]


def test_downstream_boundary_missing_conventional_state_is_refused():
    boundary = {"CultureOut": {"tag": "CultureOut", "spec": {"temperature": {"si": 298.15}}}}
    with pytest.raises(dynamic_engine.DynamicError) as raised:
        dynamic_engine._validate_downstream_boundary_specs(boundary, {})
    assert raised.value.code == "DOWNSTREAM_BOUNDARY_STATE_UNDECLARED"
    assert raised.value.detail["streams"] == {"CultureOut": ["pressure", "composition"]}


def test_tiny_negative_roundoff_is_clipped_and_recorded():
    state = np.array([-1e-12, 0.2, 0.01, 0.0, 0.0, 0.0, 0.0])
    units = [{"tag": "PBR"}]
    clips: list[dict] = []
    dynamic_engine._valid_state(state, units, [type("Growth", (), {"extinction": 0, "diameter": 0})()])
    dynamic_engine._clip_tiny_negatives(state, units, clips, 60.0)
    assert state[0] == 0.0
    assert clips == [{"time_s": 60.0, "unit": "PBR", "channel": "X", "value_before": -1e-12,
                      "value_after": 0.0}]


def test_two_pbr_splitter_mixer_recycle_closes_mass_and_nitrogen_balances():
    from app.modules.process_stack import draft
    from app.modules.process_stack.draft_models import (
        AddStream,
        AddUnit,
        Connect,
        Disconnect,
        DraftQuantity,
        SetScenario,
        SetUnitModel,
        SetUnitParams,
    )
    from tests.plumbing_170_support import new_workspace, pbr_quantities

    workspace_id = new_workspace()
    state, _ = _real_scenario(workspace_id, 24 * 3600)
    directory = draft.draft_dir(workspace_id, state["draft_id"])
    head = draft._head(directory)
    document = draft.load_revision(directory, head["revision"])["document"]
    pin = document["objects"]["pbr"]["model"]
    scenario = dict(document["scenarios"]["run"])
    scenario["units"] = ["PBR", "PBR2"]
    scenario["initial"] = {
        "PBR": {"X": 0.2, "N": 0.05, "O2": 0.008},
        "PBR2": {"X": 0.2, "N": 0.05, "O2": 0.008},
    }
    ops = [
        AddUnit(op="add_unit", id="pbr2", type="PhotobioreactorT1", tag="PBR2", x=300, y=100),
        AddUnit(op="add_unit", id="split", type="Splitter", tag="Split", x=220, y=0),
        AddUnit(op="add_unit", id="mixer", type="Mixer", tag="Mixer", x=100, y=100),
        AddStream(op="add_stream", id="recycle", tag="RecycleFlow", x=260, y=80),
        AddStream(op="add_stream", id="harvest", tag="Harvest", x=280, y=-20),
        AddStream(op="add_stream", id="p2out", tag="PBR2Out", x=150, y=120),
        AddStream(op="add_stream", id="mixout", tag="MixedFeed", x=60, y=80),
        Disconnect(op="disconnect", stream="feed", end="target"),
        Connect(op="connect", stream="feed", end="target", unit="mixer", port=0),
        Connect(op="connect", stream="product", end="target", unit="split", port=0),
        Connect(op="connect", stream="harvest", end="source", unit="split", port=0),
        Connect(op="connect", stream="recycle", end="source", unit="split", port=1),
        Connect(op="connect", stream="recycle", end="target", unit="pbr2", port=0),
        Connect(op="connect", stream="p2out", end="source", unit="pbr2", port=0),
        Connect(op="connect", stream="p2out", end="target", unit="mixer", port=1),
        Connect(op="connect", stream="mixout", end="source", unit="mixer", port=0),
        Connect(op="connect", stream="mixout", end="target", unit="pbr", port=0),
        SetUnitParams(op="set_unit_params", unit="pbr2", values=pbr_quantities()),
        SetUnitModel(op="set_unit_model", unit="pbr2", model=pin),
        SetUnitParams(op="set_unit_params", unit="split", mode="split_ratios", values={
            "split_ratio_1": DraftQuantity(value=0.5, unit="dimensionless"),
            "split_ratio_2": DraftQuantity(value=0.5, unit="dimensionless"),
        }),
        SetScenario(op="set_scenario", id="run", value=scenario),
    ]
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], ops)
    snapshot = dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    result = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _progress: None)
    assert result.status == "succeeded", result.error
    for species in ("biomass", "total_nitrogen"):
        assert result.manifest["balances"]["aggregate"][species]["residual_rel"] <= 1e-6


def test_real_24_hour_run_matches_independent_170_rate_reference():
    from app.modules.process_stack import pbr_unit
    from tests.plumbing_170_support import new_workspace

    workspace_id = new_workspace()
    _state, snapshot = _real_scenario(workspace_id, 24 * 3600)
    result = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _p: None)
    assert result.status == "succeeded", result.error
    assert result.manifest["balances"]["sign_conventions"]["oxygen_transfer"] == (
        "positive means oxygen absorption into liquid"
    )
    for terms in result.manifest["balances"]["aggregate"].values():
        assert terms["residual_rel"] <= 1e-6, terms
    unit = snapshot.payload["units"][0]
    growth = pbr_unit.build_growth(unit["params"], unit["model"], (0.0, 0.05, 0.0), 0.0)
    topology = snapshot.payload["topology"]
    feed = topology["feeds"][next(iter(topology["feeds"]))]
    inlet = np.asarray([feed["X"], feed["N"], feed["O2"]])
    flow = next(iter(topology["flows"].values()))
    dilution = flow / unit["volume_m3"]
    expected = np.asarray(unit["state"], dtype=np.float64)

    def reference_rhs(elapsed: float, state: tuple[float, ...]) -> list[float]:
        x, nitrogen, oxygen = state
        mu, loss = growth.rates_at(500.0, unit["params"]["temperature_mean"], x, nitrogen)
        rx = (mu - loss) * x / 3600.0
        transfer = growth.kla_h * (growth.oxygen_saturation - oxygen) / 3600.0
        return [rx + dilution * (inlet[0] - x), -growth.nitrogen_quota * rx + dilution * (inlet[1] - nitrogen),
                growth.oxygen_yield * rx + transfer + dilution * (inlet[2] - oxygen)]

    for index in range(24):
        step = integrate_ode(reference_rhs, expected, [index * 3600.0, (index + 1) * 3600.0],
                             rtol=snapshot.payload["scenario"]["rtol"], atol=snapshot.payload["scenario"]["atol"])
        assert step.success, step.message
        expected = np.asarray(step.states[-1])
        actual = np.asarray([result.series[f"PBR_{name}"][index + 1] for name in ("X", "N", "O2")])
        assert np.max(np.abs(actual - expected) / np.maximum(np.abs(expected), 1e-12)) <= 1e-6, (
            index, actual, expected, result.series["par"][index + 1], flow, inlet.tolist(), dilution
        )


def test_prepare_refuses_tampered_real_profile_digest():
    from app.core.paths import build_paths
    from tests.plumbing_170_support import new_workspace

    workspace_id = new_workspace()
    state, snapshot = _real_scenario(workspace_id, 3600)
    digest = snapshot.payload["profiles"][0]["ref"]["digest"]
    artifact = build_paths().environment_profiles_dir(workspace_id) / f"{digest[7:]}.json"
    artifact.write_bytes(artifact.read_bytes() + b" ")
    with pytest.raises(dynamic_engine.DynamicError) as raised:
        dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    assert raised.value.code == "PROFILE_DIGEST_INVALID"


def test_real_30_day_hourly_run_meets_host_budget_and_api_publishes_artifacts():
    import shutil

    from fastapi.testclient import TestClient

    from app.main import app
    from app.modules.process_stack import draft, dynamic_engine
    from tests.plumbing_170_support import new_workspace

    workspace_id = new_workspace()
    state, snapshot = _real_scenario(workspace_id, 30 * 86400)
    started = time.perf_counter()
    result = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _p: None)
    elapsed = time.perf_counter() - started
    print(f"30-day dynamic T1 wall time: {elapsed:.3f}s")
    assert result.status == "succeeded", result.error
    assert len(result.series["t_s"]) == 721
    assert elapsed < 30.0
    checks = iter((False, True))
    cancelled = dynamic_engine.run(snapshot, cancelled=lambda: next(checks), progress=lambda _p: None)
    assert cancelled.status == "cancelled"
    assert "state" not in cancelled.series and all(values.ndim == 1 for values in cancelled.series.values())

    # A separate real-engine job exercises the workspace API and immutable artifact reads.
    api_state, api_snapshot = _real_scenario(workspace_id, 24 * 3600)
    base = f"/workspaces/{workspace_id}/process/drafts/{api_state['draft_id']}/dynamic"
    with TestClient(app) as client:
        long_base = f"/workspaces/{workspace_id}/process/drafts/{state['draft_id']}/dynamic"
        long_job = client.post(long_base + "/runs", json={"scenario_id": "run"})
        assert long_job.status_code == 202, long_job.text
        long_job_id = long_job.json()["job_id"]
        cancelled_request = client.post(long_base + f"/runs/{long_job_id}/cancel")
        assert cancelled_request.status_code == 200, cancelled_request.text
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            long_status = client.get(long_base + f"/runs/{long_job_id}")
            if long_status.json()["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        assert long_status.json()["status"] == "cancelled", long_status.json()
        if long_status.json().get("artifacts"):
            long_manifest = client.get(long_base + f"/runs/{long_job_id}/manifest").json()
            assert long_manifest["artifact_label"] == "diagnostic_cancelled"

        created = client.post(base + "/runs", json={"scenario_id": "run"})
        assert created.status_code == 202, created.text
        job_id = created.json()["job_id"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            status = client.get(base + f"/runs/{job_id}")
            assert status.status_code == 200, status.text
            if status.json()["status"] in {"succeeded", "failed", "cancelled"}:
                break
            time.sleep(0.02)
        assert status.json()["status"] == "succeeded", status.json()
        manifest = client.get(base + f"/runs/{job_id}/manifest")
        series = client.get(base + f"/runs/{job_id}/series?channels=t_s,PBR_X&max_points=100")
        assert manifest.status_code == 200 and manifest.json()["fidelity"] == "T1"
        manifest_body = manifest.json()
        assert manifest_body["snapshot_digest"] == api_snapshot.digest
        assert manifest_body["draft_content_digest"] == api_snapshot.payload["content_digest"]
        assert {"card_id", "card_revision", "card_digest", "parameter_set_digest"} <= set(
            manifest_body["units"][0]
        )
        assert {"profile_id", "digest", "resolution_minutes", "consumed_channels", "temporal_rule"} <= set(
            manifest_body["profiles"][0]
        )
        assert series.status_code == 200 and len(series.json()["channels"]["t_s"]) <= 100
        assert status.json()["current"] is True
        revision = api_state["revision"]
        moved = client.post(f"/workspaces/{workspace_id}/process/drafts/{api_state['draft_id']}/patch", json={
            "expected_revision": revision, "ops": [{"op": "move", "id": "pbr", "x": 140, "y": 20}],
        })
        assert moved.status_code == 200, moved.text
        assert client.get(base + f"/runs/{job_id}").json()["current"] is True
        semantic = client.post(f"/workspaces/{workspace_id}/process/drafts/{api_state['draft_id']}/patch", json={
            "expected_revision": moved.json()["revision"],
            "ops": [{"op": "set_scenario", "id": "run", "value": {
                **api_snapshot.payload["scenario"], "par_scale": 1.5,
            }}],
        })
        assert semantic.status_code == 200, semantic.text
        assert client.get(base + f"/runs/{job_id}").json()["current"] is False
        shutil.rmtree(draft.draft_dir(workspace_id, api_state["draft_id"]))
        assert client.get(base + f"/runs/{job_id}").json()["current"] is False


def _loop_ops(*, heater_first: bool):
    from app.modules.process_stack.draft_models import AddStream, AddUnit, Connect, DraftQuantity, SetUnitParams

    ops = [
        AddUnit(op="add_unit", id="mixer", type="Mixer", tag="Mixer", x=250, y=0),
        AddUnit(op="add_unit", id="split", type="Splitter", tag="Split", x=450, y=0),
        AddStream(op="add_stream", id="mixed", tag="Mixed", x=400, y=0),
        AddStream(op="add_stream", id="purge", tag="Purge", x=500, y=-50),
        AddStream(op="add_stream", id="back", tag="Back", x=450, y=80),
        Connect(op="connect", stream="mixed", end="source", unit="mixer", port=0),
        Connect(op="connect", stream="mixed", end="target", unit="split", port=0),
        Connect(op="connect", stream="purge", end="source", unit="split", port=0),
        Connect(op="connect", stream="back", end="source", unit="split", port=1),
        Connect(op="connect", stream="back", end="target", unit="mixer", port=1),
        SetUnitParams(op="set_unit_params", unit="split", mode="split_ratios", values={
            "split_ratio_1": DraftQuantity(value=0.9, unit="dimensionless"),
            "split_ratio_2": DraftQuantity(value=0.1, unit="dimensionless")}),
    ]
    if not heater_first:
        return ops + [Connect(op="connect", stream="product", end="target", unit="mixer", port=0)]
    return ops + [
        AddUnit(op="add_unit", id="heater", type="Heater", tag="Heater", x=150, y=0),
        AddStream(op="add_stream", id="hot", tag="Hot", x=200, y=0),
        Connect(op="connect", stream="product", end="target", unit="heater", port=0),
        Connect(op="connect", stream="hot", end="source", unit="heater", port=0),
        Connect(op="connect", stream="hot", end="target", unit="mixer", port=0),
        SetUnitParams(op="set_unit_params", unit="heater", mode="outlet_temperature",
                      values={"outlet_temperature": DraftQuantity(value=298.15, unit="K")}),
    ]


def test_real_prepare_refuses_t1_algebraic_cycle_but_admits_dwsim_owned_downstream_loop():
    from app.modules.process_stack import draft
    from tests.plumbing_170_support import new_workspace

    workspace_id = new_workspace()
    state, _ = _real_scenario(workspace_id, 3600)
    inside = draft.patch(workspace_id, state["draft_id"], state["revision"], _loop_ops(heater_first=False))
    with pytest.raises(dynamic_engine.DynamicError) as raised:
        dynamic_engine.prepare(workspace_id, inside["draft_id"], "run")
    assert raised.value.code == "ALGEBRAIC_CYCLE"

    workspace_id = new_workspace()
    state, _ = _real_scenario(workspace_id, 3600)
    downstream = draft.patch(workspace_id, state["draft_id"], state["revision"], _loop_ops(heater_first=True))
    snapshot = dynamic_engine.prepare(workspace_id, downstream["draft_id"], "run")
    assert [unit["tag"] for unit in snapshot.payload["units"]] == ["PBR"]


def test_real_prepare_refuses_downstream_sampling_of_a_recycle_free_dwsim_loop():
    from app.modules.process_stack import draft
    from app.modules.process_stack.draft_models import (
        AddStream,
        AddUnit,
        Connect,
        Disconnect,
        SetScenario,
    )
    from tests.plumbing_170_support import new_workspace

    workspace_id = new_workspace()
    state, snapshot = _real_scenario(workspace_id, 3600)
    sampled = {**snapshot.payload["scenario"], "downstream_enabled": True, "downstream_cadence_s": 3600}
    state = draft.patch(workspace_id, state["draft_id"], state["revision"],
                        [*_loop_ops(heater_first=True), SetScenario(op="set_scenario", id="run", value=sampled)])
    with pytest.raises(dynamic_engine.DynamicError) as raised:
        dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    assert raised.value.code == "DOWNSTREAM_RECYCLE_REQUIRED"
    assert raised.value.detail == {"loops": [["Mixer", "Split"]]}

    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        AddUnit(op="add_unit", id="rec", type="Recycle", tag="Rec", x=350, y=80),
        AddStream(op="add_stream", id="torn", tag="Torn", x=300, y=80),
        Disconnect(op="disconnect", stream="back", end="target"),
        Connect(op="connect", stream="back", end="target", unit="rec", port=0),
        Connect(op="connect", stream="torn", end="source", unit="rec", port=0),
        Connect(op="connect", stream="torn", end="target", unit="mixer", port=1),
    ])
    admitted = dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    assert [unit["tag"] for unit in admitted.payload["units"]] == ["PBR"]


@pytest.mark.skipif(not os.environ.get("JARVISOS_DWSIM_MCP_PATH"), reason="set JARVISOS_DWSIM_MCP_PATH to opt in to DWSIM runtime")
def test_real_dwsim_recycle_sample_reports_solved_downstream_streams_not_echoed_boundary():
    from app.modules.process_stack import draft
    from app.modules.process_stack.draft_models import AddStream, AddUnit, Connect, Disconnect, SetScenario
    from app.modules.process_stack.dynamic_downstream import build_sampler
    from tests.plumbing_170_support import new_workspace

    workspace_id = new_workspace()
    state, snapshot = _real_scenario(workspace_id, 86400)
    sampled = {**snapshot.payload["scenario"], "downstream_enabled": True, "downstream_cadence_s": 43200}
    state = draft.patch(workspace_id, state["draft_id"], state["revision"], [
        *_loop_ops(heater_first=True), SetScenario(op="set_scenario", id="run", value=sampled),
        AddUnit(op="add_unit", id="rec", type="Recycle", tag="Rec", x=350, y=80),
        AddStream(op="add_stream", id="torn", tag="Torn", x=300, y=80),
        Disconnect(op="disconnect", stream="back", end="target"),
        Connect(op="connect", stream="back", end="target", unit="rec", port=0),
        Connect(op="connect", stream="torn", end="source", unit="rec", port=0),
        Connect(op="connect", stream="torn", end="target", unit="mixer", port=1),
    ])
    snapshot = dynamic_engine.prepare(workspace_id, state["draft_id"], "run")
    sampler = build_sampler(snapshot)
    assert sampler is not None
    run = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _v: None, sampler=sampler)
    plain = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _v: None)
    assert run.status == plain.status == "succeeded"
    for channel in ("PBR_X", "PBR_N", "PBR_O2"):
        assert np.array_equal(run.series[channel], plain.series[channel])
    outcomes = run.manifest["downstream_outcomes"]
    assert [item["time_s"] for item in outcomes] == [43200.0, 86400.0]
    for outcome in outcomes:
        assert outcome["status"] == "succeeded", outcome
        boundary = outcome["boundary_inputs"]["Product"]
        assert boundary["provenance"] == "jarvis_t1_snapshot"
        assert "Product" not in outcome["streams"]
        solved = outcome["streams"]
        assert set(solved) == {"Hot", "Mixed", "Purge", "Back", "Torn"}
        assert all(item is not None and item["provenance"] == "dwsim_solved" for item in solved.values())
        feed = boundary["mass_flow_kg_s"]
        # Steady state through the 0.9/0.1 split: Purge carries the feed, Mixed carries feed/0.9 -- values
        # DWSIM can only report by actually solving the loop, never by echoing the boundary input.
        assert solved["Hot"]["mass_flow_kg_s"] == pytest.approx(feed, rel=1e-4)
        assert solved["Hot"]["temperature_K"] == pytest.approx(298.15, abs=0.05)
        assert solved["Purge"]["mass_flow_kg_s"] == pytest.approx(feed, rel=1e-3)
        assert solved["Mixed"]["mass_flow_kg_s"] == pytest.approx(feed / 0.9, rel=1e-3)
        assert solved["Back"]["mass_flow_kg_s"] == pytest.approx(feed / 9, rel=1e-2)
        assert {"Heater", "Mixer", "Split", "Rec"} <= set(outcome["units"])
        assert outcome["solve"]["status"] == "completed"
        assert all(row["passed"] for row in outcome["solve"]["balances"].values())
