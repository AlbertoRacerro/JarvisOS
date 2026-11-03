"""Focused evidence for the dynamic circulation and culture-loop contracts in spec 184."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pytest

from app.modules.process_stack import dynamic_engine
from app.modules.process_stack.draft_models import UNIT_REGISTRY
from app.modules.process_stack.dynamic_models import Scenario


def _closed_loop_topology() -> dict:
    units = [
        {"id": "pump", "tag": "Pump", "type": "Pump"},
        {"id": "pbr", "tag": "PBR", "type": "PhotobioreactorT1"},
        {"id": "tank", "tag": "Tank", "type": "HoldupTank"},
    ]
    streams = [
        {"id": "a", "tag": "a", "source": {"unit": "pump"}, "target": {"unit": "pbr"}},
        {"id": "b", "tag": "b", "source": {"unit": "pbr"}, "target": {"unit": "tank"}},
        {"id": "c", "tag": "c", "source": {"unit": "tank"}, "target": {"unit": "pump"}},
    ]
    # Unit continuity plus Q_pump_outlet = 5e-4 m3/s.
    matrix = np.asarray([
        [1, -1, 0],
        [0, 1, -1],
        [-1, 0, 1],
        [1, 0, 0],
    ], dtype=float)
    return {
        "units": units,
        "streams": streams,
        "flow_matrix": matrix.tolist(),
        "flow_rhs": [0.0, 0.0, 0.0, 5e-4],
        "circulation_rows": {3: {"tag": "Pump", "flow_m3_s": 5e-4}},
        "circulation": {"Pump": {"tag": "Pump", "flow_m3_s": 5e-4, "stream_id": "a"}},
    }


def test_holdup_tank_is_jarvis_owned_and_dynamic_only() -> None:
    tank = UNIT_REGISTRY["HoldupTank"]
    assert tank.owner == "jarvis_bio"
    assert tank.dwsim_type is None
    assert tank.native_types == ()
    assert {param.key for param in tank.params} == {
        "liquid_volume", "min_volume", "max_volume", "temperature", "oxygen_kla", "oxygen_saturation",
    }


def test_specified_closed_circulation_is_full_rank_and_exact() -> None:
    flows = dynamic_engine._solve_flows(_closed_loop_topology())
    assert flows == pytest.approx({"a": 5e-4, "b": 5e-4, "c": 5e-4}, rel=1e-12)


def test_unspecified_cycle_refuses_with_cycle_unit_names() -> None:
    topology = _closed_loop_topology()
    topology["flow_matrix"] = topology["flow_matrix"][:3]
    topology["flow_rhs"] = [0.0, 0.0, 0.0]
    topology["circulation_rows"] = {}
    topology["circulation"] = {}
    with pytest.raises(dynamic_engine.DynamicError) as exc:
        dynamic_engine._solve_flows(topology)
    assert exc.value.code == "FLOW_UNDERDETERMINED"
    assert exc.value.detail["units"] == ["PBR", "Pump", "Tank"]


def test_scenario_admits_twelve_units_and_named_circulation() -> None:
    scenario = Scenario.model_validate({
        "id": "loop", "units": [f"U{i}" for i in range(12)],
        "profiles": [{"profile_id": "p", "digest": "d"}],
        "start_utc": "2026-01-01T00:00:00Z", "end_utc": "2026-01-01T01:00:00Z",
        "output_cadence_s": 3600, "temperature_source": "unit_mean",
        "circulation": {"Pump": 5e-4},
    })
    assert scenario.circulation == {"Pump": 5e-4}


def test_velocity_mismatch_warning_is_limited_to_specified_pbr_loop(monkeypatch) -> None:
    monkeypatch.setattr(dynamic_engine.pbr_unit, "hydraulics", lambda params: {
        "velocity_seen": params["liquid_velocity"],
    })
    item = {
        "tag": "PBR", "unit": {"id": "pbr", "type": "PhotobioreactorT1"},
        "volume_m3": 1.0,
        "params": {"tube_inner_diameter": 0.1, "tube_count": 1.0, "liquid_velocity": 0.1},
    }
    topology = {
        "units": [{"id": "pbr", "tag": "PBR", "type": "PhotobioreactorT1"},
                  {"id": "pump", "tag": "Pump", "type": "Pump"}],
        "streams": [{"id": "in", "target": {"unit": "pbr"}}],
        "circulation": {"Pump": {"tag": "Pump", "flow_m3_s": 1e-3, "stream_id": "in"}},
        "culture_loops": [{"id": "PBR", "member_tags": ["PBR", "Pump"], "units": ["PBR", "Pump"]}],
    }
    _, findings = dynamic_engine._flow_hydraulics(topology, [item], {"in": 1e-3})
    assert [finding["code"] for finding in findings] == ["PBR_CIRCULATION_VELOCITY_MISMATCH"]

    topology["culture_loops"] = []
    _, findings = dynamic_engine._flow_hydraulics(topology, [item], {"in": 1e-3})
    assert findings == []


def test_closed_loop_matches_independent_scipy_tanks_in_series(monkeypatch) -> None:
    from datetime import UTC, datetime

    from scipy.integrate import solve_ivp

    from app.modules.process_stack.dynamic_engine import Snapshot

    @dataclass(frozen=True)
    class Rates:
        mu_h: float
        loss_h: float = 0.01
        nitrogen_quota: float = 0.0
        oxygen_yield: float = 0.8
        kla_h: float = 0.0
        oxygen_saturation: float = 0.01
        extinction: float = 0.0
        diameter: float = 0.05

        def rates_at(self, _par, _temp, _x, _n):
            return self.mu_h, self.loss_h

    monkeypatch.setattr(dynamic_engine, "_growth_for_unit", lambda item, dark=False: Rates(0.0 if dark else 0.02))
    start = datetime(2026, 1, 1, tzinfo=UTC).timestamp()
    duration = 10 * 86400.0
    q, vp, vt = 5e-4, 0.02, 0.01
    units = [
        {"tag": "PBR", "unit": {"id": "pbr", "type": "PhotobioreactorT1"}, "volume_m3": vp,
         "params": {"temperature_mean": 298.15}, "model": {}, "state": [0.2, 0.05, 0.008],
         "card_qualification": "synthetic"},
        {"tag": "Tank", "unit": {"id": "tank", "type": "HoldupTank"}, "volume_m3": vt,
         "params": {"temperature": 298.15}, "model": {}, "state": [0.2, 0.05, 0.008],
         "card_qualification": "shared"},
    ]
    streams = [
        {"id": "pbr_tank", "tag": "PbrTank", "source": {"unit": "pbr"}, "target": {"unit": "tank"}},
        {"id": "tank_pump", "tag": "TankPump", "source": {"unit": "tank"}, "target": {"unit": "pump"}},
        {"id": "pump_pbr", "tag": "PumpPbr", "source": {"unit": "pump"}, "target": {"unit": "pbr"}},
    ]
    topology = {
        "units": [{"id": "pbr", "tag": "PBR", "type": "PhotobioreactorT1"},
                  {"id": "tank", "tag": "Tank", "type": "HoldupTank"},
                  {"id": "pump", "tag": "Pump", "type": "Pump"}],
        "streams": streams, "flows": {stream["id"]: q for stream in streams}, "feeds": {},
        "circulation": {"Pump": {"tag": "Pump", "flow_m3_s": q, "stream_id": "pump_pbr"}},
    }
    payload = {
        "scenario": {"output_cadence_s": 3600.0, "controller_cadence_s": 3600.0,
                     "rtol": 1e-9, "atol": 1e-12, "solver_method": "BDF",
                     "downstream_cadence_s": None, "temperature_source": "unit_mean"},
        "units": units, "topology": topology, "start_epoch": start, "end_epoch": start + duration,
        "profiles": [{"times": [start, start + duration],
                       "profile": {"channels": {"par": [500.0, 500.0]}}, "par_name": "par",
                       "temp_name": "unit_mean", "ref": {"profile_id": "p", "digest": "p"}}],
        "schedule": {"events": []}, "controllers": [], "draft_id": "d", "revision": 1,
        "content_digest": "c", "scenario_id": "s",
    }
    result = dynamic_engine.run(Snapshot(payload, "snapshot"), cancelled=lambda: False,
                                progress=lambda _value: None)
    assert result.status == "succeeded", result.error

    quota, yield_o2, mu, loss = 0.0, 0.8, 0.02, 0.01

    def independent_rhs(_time, y):
        xp, np_, op, xt, nt, ot = y
        r_p = (mu - loss) * xp / 3600.0
        r_t = -loss * xt / 3600.0
        return [r_p + q / vp * (xt - xp), -quota * r_p + q / vp * (nt - np_),
                yield_o2 * r_p + q / vp * (ot - op),
                r_t + q / vt * (xp - xt), -quota * r_t + q / vt * (np_ - nt),
                yield_o2 * r_t + q / vt * (op - ot)]

    reference = solve_ivp(independent_rhs, (0.0, duration), [0.2, 0.05, 0.008, 0.2, 0.05, 0.008],
                          method="DOP853", rtol=1e-11, atol=1e-13, dense_output=True)
    assert reference.success
    actual = np.column_stack([result.series[f"{tag}_{channel}"]
                              for tag in ("PBR", "Tank") for channel in ("X", "N", "O2")])
    expected = reference.sol(result.series["t_s"]).T
    np.testing.assert_allclose(actual, expected, rtol=1e-5, atol=1e-9)


def test_flow_rebalance_keeps_specified_circulation() -> None:
    topology = _closed_loop_topology()
    flows = dynamic_engine._solve_flows(topology)
    dynamic_engine._rebalance_flows(topology, flows)
    assert flows == pytest.approx({"a": 5e-4, "b": 5e-4, "c": 5e-4}, rel=1e-12)


def test_closed_pbr_tank_loop_runs_with_dark_tank_and_loop_channels() -> None:
    from app.modules.process_stack import draft
    from app.modules.process_stack.draft_models import (
        AddStream,
        AddUnit,
        Connect,
        Disconnect,
        SetScenario,
        SetUnitParams,
    )
    from tests.plumbing_170_support import new_workspace
    from tests.test_process_dynamic_engine import _real_scenario

    workspace = new_workspace()
    state, initial = _real_scenario(workspace, 24 * 3600)
    scenario = dict(initial.payload["scenario"])
    scenario.update(
        units=["PBR", "Tank"],
        circulation={"Pump": 5e-4},
        initial={"PBR": {"X": 0.2, "N": 0.05, "O2": 0.008},
                 "Tank": {"X": 0.2, "N": 0.05, "O2": 0.008}},
    )
    state = draft.patch(workspace, state["draft_id"], state["revision"], [
        AddUnit(op="add_unit", id="tank", type="HoldupTank", tag="Tank", x=200, y=100),
        AddUnit(op="add_unit", id="pump", type="Pump", tag="Pump", x=100, y=100),
        SetUnitParams(op="set_unit_params", unit="tank", mode="dynamic", values={
            "liquid_volume": {"value": 0.01, "unit": "m3"},
            "min_volume": {"value": 0.0, "unit": "m3"},
            "max_volume": {"value": 0.01, "unit": "m3"},
            "temperature": {"value": 298.15, "unit": "K"},
        }),
        Disconnect(op="disconnect", stream="feed", end="target"),
        Connect(op="connect", stream="product", end="target", unit="tank", port=0),
        AddStream(op="add_stream", id="tank_pump", tag="TankPump", x=150, y=100),
        AddStream(op="add_stream", id="pump_pbr", tag="PumpPBR", x=100, y=50),
        Connect(op="connect", stream="tank_pump", end="source", unit="tank", port=0),
        Connect(op="connect", stream="tank_pump", end="target", unit="pump", port=0),
        Connect(op="connect", stream="pump_pbr", end="source", unit="pump", port=0),
        Connect(op="connect", stream="pump_pbr", end="target", unit="pbr", port=0),
        SetScenario(op="set_scenario", id="run", value=scenario),
    ])
    snapshot = dynamic_engine.prepare(workspace, state["draft_id"], "run")
    result = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _value: None)

    assert result.status == "succeeded", result.error
    assert result.manifest["culture_loops"][0]["id"] == "PBR"
    assert result.manifest["culture_loops"][0]["units"] == ["PBR", "Pump", "Tank"]
    assert result.manifest["culture_loops"][0]["circulation_m3_s"] == pytest.approx(5e-4)
    assert result.manifest["culture_loops"][0]["culture_residence_time_s"] == "∞, batch"
    tank_item = next(unit for unit in snapshot.payload["units"] if unit["tag"] == "Tank")
    dark_growth = dynamic_engine._growth_for_unit(tank_item, dark=True)
    assert dark_growth.rates_at(500.0, 298.15, 0.2, 0.05)[0] == 0.0
    assert result.series["Tank_X"][-1] > 0.0
    assert {"PBR_pass_rate_1_s", "PBR_X_mean", "PBR_N_mean", "PBR_biomass_kg"} <= set(result.series)
    assert np.isinf(result.series["PBR_culture_residence_time_s"]).all()
