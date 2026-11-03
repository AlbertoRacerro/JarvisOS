"""Spec 184 acceptance: culture loops through the real draft -> prepare -> run path.

Each case compares the engine with an independent reference: a separately written
tanks-in-series ``solve_ivp`` model, a single well-mixed inventory, the 104
hydraulics correlations, or a root solve of the steady net-dilution balance.
Only the pinned card's rate law (``rates_at``) is shared with the engine.
"""

from __future__ import annotations

import math
import os
import time
from typing import Any

import numpy as np
import pytest
from scipy.integrate import solve_ivp
from scipy.optimize import brentq

from app.modules.process_stack import draft, dynamic_engine, pbr_unit
from app.modules.process_stack.draft_models import (
    AddStream,
    AddUnit,
    Connect,
    Delete,
    DraftQuantity,
    SetScenario,
    SetStreamCulture,
    SetStreamSpec,
    SetUnitModel,
    SetUnitParams,
)
from tests.plumbing_170_support import new_workspace, pbr_quantities
from tests.test_process_dynamic_engine import _real_scenario

PAR = 500.0
TEMPERATURE_K = 298.15
INITIAL = {"X": 0.2, "N": 0.05, "O2": 0.008}
PBR_TAGS = ("PBR", "PBR2", "PBR3", "PBR4")
TANK_TAGS = ("TankA", "TankB")


def _q(value: float, unit: str) -> DraftQuantity:
    return DraftQuantity(value=value, unit=unit)


def _loop(*, q_circ: float, tube_length: float, tube_count: float = 10.0, diameter: float = 0.05,
          tank_volume: float, days: float, cadence_s: int, feed_m3_s: float | None = None,
          feed_n: float = 0.5, events: list[dict[str, Any]] | None = None,
          declared_velocity: float | None = None, extra_ops: list[Any] | None = None,
          scenario_extra: dict[str, Any] | None = None) -> tuple[dict[str, Any], dynamic_engine.Snapshot]:
    """Pump -> PBR..PBR4 -> TankA -> TankB [-> Splitter(harvest) -> Mixer(feed)] -> Pump."""
    workspace = new_workspace()
    state, _ = _real_scenario(workspace, int(days * 86400), cadence_s=cadence_s)
    document = draft.load_revision(draft.draft_dir(workspace, state["draft_id"]), state["revision"])["document"]
    pin = document["objects"]["pbr"]["model"]
    base_scenario = dict(document["scenarios"]["run"])
    velocity = q_circ / (tube_count * math.pi * diameter ** 2 / 4.0)
    params = pbr_quantities(tube_length=(tube_length, "m"), tube_count=(tube_count, "dimensionless"),
                            tube_inner_diameter=(diameter, "m"),
                            liquid_velocity=(declared_velocity or velocity, "m/s"))
    ops: list[Any] = [Delete(op="delete", id="feed"), Delete(op="delete", id="product"),
                      SetUnitParams(op="set_unit_params", unit="pbr", values=params)]
    ids = ["pbr"]
    for i, tag in enumerate(PBR_TAGS[1:], start=2):
        ids.append(f"pbr{i}")
        ops += [AddUnit(op="add_unit", id=f"pbr{i}", type="PhotobioreactorT1", tag=tag, x=100 * i, y=0),
                SetUnitParams(op="set_unit_params", unit=f"pbr{i}", values=params),
                SetUnitModel(op="set_unit_model", unit=f"pbr{i}", model=pin)]
    for i, tag in enumerate(TANK_TAGS):
        ids.append(tag.lower())
        ops += [AddUnit(op="add_unit", id=tag.lower(), type="HoldupTank", tag=tag, x=500 + 100 * i, y=0),
                SetUnitParams(op="set_unit_params", unit=tag.lower(), mode="dynamic", values={
                    "liquid_volume": _q(tank_volume, "m3"), "min_volume": _q(0.0, "m3"),
                    "max_volume": _q(tank_volume, "m3"), "temperature": _q(TEMPERATURE_K, "K")})]
    ops.append(AddUnit(op="add_unit", id="pump", type="Pump", tag="Pump", x=0, y=100))
    harvest = feed_m3_s is not None
    if harvest:
        ops += [AddUnit(op="add_unit", id="split", type="Splitter", tag="Split", x=700, y=100),
                SetUnitParams(op="set_unit_params", unit="split", mode="split_ratios", values={
                    "split_ratio_1": _q(0.5, "dimensionless"), "split_ratio_2": _q(0.5, "dimensionless")}),
                AddUnit(op="add_unit", id="mix", type="Mixer", tag="Mix", x=0, y=200)]
        ids += ["split", "mix"]
    ids.append("pump")
    ring = ids  # each unit feeds the next one; the last (Pump) feeds the first PBR
    for k, (src, dst) in enumerate(zip(ring, ring[1:] + ring[:1], strict=True)):
        port_out = 1 if src == "split" else 0
        ops += [AddStream(op="add_stream", id=f"s{k}", tag=f"S{k}", x=50 * k, y=300),
                Connect(op="connect", stream=f"s{k}", end="source", unit=src, port=port_out),
                Connect(op="connect", stream=f"s{k}", end="target", unit=dst, port=0)]
    if harvest:
        ops += [AddStream(op="add_stream", id="feed", tag="Feed", x=0, y=400),
                Connect(op="connect", stream="feed", end="target", unit="mix", port=1),
                SetStreamSpec(op="set_stream_spec", stream="feed", pressure=_q(1.0, "bar"),
                              temperature=_q(TEMPERATURE_K, "K"), mass_flow=_q(1.0, "kg/s"),
                              composition={"Water": 1.0}, composition_basis="mass"),
                SetStreamCulture(op="set_stream_culture", stream="feed", culture={
                    "biomass": _q(0.0, "kg/m3"), "nitrogen": _q(feed_n, "kg/m3"), "oxygen": _q(0.0, "kg/m3"),
                    "salinity": _q(35.0, "g/kg")}),
                AddStream(op="add_stream", id="harvest", tag="Harvest", x=700, y=400),
                Connect(op="connect", stream="harvest", end="source", unit="split", port=0)]
    scenario = {**base_scenario, "units": [*PBR_TAGS, *TANK_TAGS], "circulation": {"Pump": q_circ},
                "initial": {tag: dict(INITIAL) for tag in (*PBR_TAGS, *TANK_TAGS)},
                "rtol": 1e-9, "atol": 1e-12}
    if harvest:
        scenario["feed_flows"] = {"Feed": feed_m3_s}
    scenario.update(scenario_extra or {})
    ops += extra_ops or []
    if events:
        from app.modules.process_stack.draft_models import SetSchedule

        ops.append(SetSchedule(op="set_schedule", id="sched", value={"events": events}))
        scenario["schedule_id"] = "sched"
    ops.append(SetScenario(op="set_scenario", id="run", value=scenario))
    state = draft.patch(workspace, state["draft_id"], state["revision"], ops)
    return state, dynamic_engine.prepare(workspace, state["draft_id"], "run")


def _run(snapshot: dynamic_engine.Snapshot) -> dynamic_engine.EngineResult:
    result = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _value: None)
    assert result.status == "succeeded", result.error
    return result


def _growths(snapshot: dynamic_engine.Snapshot) -> dict[str, Any]:
    return {item["tag"]: dynamic_engine._growth_for_unit(item, dark=item["unit"]["type"] == "HoldupTank")
            for item in snapshot.payload["units"]}


def _volumes(snapshot: dynamic_engine.Snapshot) -> dict[str, float]:
    return {item["tag"]: float(item["volume_m3"]) for item in snapshot.payload["units"]}


def _loop_mean(result: dynamic_engine.EngineResult) -> np.ndarray:
    return np.asarray(result.series["PBR_X_mean"])


# ---------------------------------------------------------------- 1. circulation without dilution

def test_closed_four_pbr_two_tank_loop_matches_tanks_in_series_and_well_mixed_inventory() -> None:
    # tau_loop = V_loop / Q_circ ~ 0.44 s, so mu_max * tau_loop ~ 1e-5 (spec 184 acceptance 1).
    q_circ = 1e-2
    _, snapshot = _loop(q_circ=q_circ, tube_length=0.05, tank_volume=2.5e-4, days=10, cadence_s=21600)
    result = _run(snapshot)
    loop = result.manifest["culture_loops"][0]
    volumes, growths = _volumes(snapshot), _growths(snapshot)
    order = [*PBR_TAGS, *TANK_TAGS]
    v_loop = sum(volumes.values())
    mu_max_s = growths["PBR"].mu_max_h / 3600.0
    assert loop["volume_m3"] == pytest.approx(v_loop, rel=1e-12)
    assert loop["circulation_m3_s"] == pytest.approx(q_circ, rel=1e-12)
    assert loop["culture_residence_time_s"] == "∞, batch"
    assert mu_max_s * v_loop / q_circ <= 1.1e-5

    # The culture grows from the inoculum and never washes out.
    x_mean = _loop_mean(result)
    assert x_mean[-1] > 1.5 * x_mean[0]
    assert np.all(np.diff(x_mean) > -1e-9)

    # Aggregate biomass and total-N balances close.
    for name in ("biomass", "total_nitrogen"):
        assert result.manifest["balances"]["aggregate"][name]["residual_rel"] <= max(1e-6, 100 * 1e-9)

    # Independent tanks-in-series model of the same ring (Pump is algebraic).
    def ring_rhs(_t: float, y: np.ndarray) -> list[float]:
        out: list[float] = []
        for k, tag in enumerate(order):
            x, n, o = y[3 * k:3 * k + 3]
            prev = (k - 1) % len(order)
            px, pn, po = y[3 * prev:3 * prev + 3]
            g = growths[tag]
            mu, loss = g.rates_at(0.0 if tag in TANK_TAGS else PAR, TEMPERATURE_K, x, n)
            r = (mu - loss) * x / 3600.0
            d = q_circ / volumes[tag]
            out += [r + d * (px - x), -g.nitrogen_quota * r + d * (pn - n),
                    g.oxygen_yield * r + g.kla_h * (g.oxygen_saturation - o) / 3600.0 + d * (po - o)]
        return out

    y0 = [INITIAL[c] for _ in order for c in ("X", "N", "O2")]
    t = np.asarray(result.series["t_s"])
    ref = solve_ivp(ring_rhs, (0.0, t[-1]), y0, method="Radau", t_eval=t, rtol=1e-11, atol=1e-14)
    assert ref.success, ref.message
    actual = np.column_stack([result.series[f"{tag}_{c}"] for tag in order for c in ("X", "N", "O2")])
    np.testing.assert_allclose(actual, ref.y.T, rtol=1e-5, atol=1e-10)

    # Fast circulation: a single well-mixed inventory with illuminated fraction f.
    f = loop["illuminated_fraction"]
    g = growths["PBR"]

    def inventory_rhs(_t: float, y: np.ndarray) -> list[float]:
        x, n = y
        mu, loss = g.rates_at(PAR, TEMPERATURE_K, x, n)
        r = (f * mu - loss) * x / 3600.0
        return [r, -g.nitrogen_quota * r]

    single = solve_ivp(inventory_rhs, (0.0, t[-1]), [INITIAL["X"], INITIAL["N"]], method="Radau",
                       t_eval=t, rtol=1e-11, atol=1e-14)
    assert single.success
    np.testing.assert_allclose(x_mean, single.y[0], rtol=1e-3)
    np.testing.assert_allclose(result.series["PBR_N_mean"], single.y[1], rtol=1e-3, atol=1e-6)


# ---------------------------------------------------------------- 2. circulation drives hydraulics only

def test_circulation_scaling_changes_hydraulics_not_biology() -> None:
    base_q, days = 1e-2, 4
    runs = {}
    for scale in (0.25, 1.0, 4.0):
        _, snapshot = _loop(q_circ=base_q * scale, tube_length=0.05, tank_volume=2.5e-4, days=days,
                            cadence_s=21600, declared_velocity=base_q / (10 * math.pi * 0.05 ** 2 / 4.0))
        runs[scale] = (snapshot, _run(snapshot))
    snapshot, _ = runs[1.0]
    pbr = next(item for item in snapshot.payload["units"] if item["tag"] == "PBR")
    area = pbr["params"]["tube_count"] * math.pi * pbr["params"]["tube_inner_diameter"] ** 2 / 4.0
    v_loop = sum(_volumes(snapshot).values())
    mu_max_s = _growths(snapshot)["PBR"].mu_max_h / 3600.0
    for scale, (_, result) in runs.items():
        q = base_q * scale
        u = q / area
        expected = pbr_unit.hydraulics({**pbr["params"], "liquid_velocity": u})
        assert result.series["PBR_velocity_m_s"][-1] == pytest.approx(u, rel=1e-12)
        assert result.series["PBR_reynolds_number"][-1] == pytest.approx(expected["reynolds_number"], rel=1e-9)
        if expected.get("pressure_drop") is not None:
            assert result.series["PBR_pressure_drop"][-1] == pytest.approx(expected["pressure_drop"], rel=1e-9)
            assert result.series["PBR_pumping_power"][-1] == pytest.approx(expected["pumping_power"], rel=1e-9)
        assert result.series["PBR_pass_transit_time_s"][-1] == pytest.approx(v_loop / q, rel=1e-12)
        assert result.series["PBR_pass_rate_1_s"][-1] == pytest.approx(q / _volumes(snapshot)["PBR"], rel=1e-12)
        mismatch = [f for f in result.manifest["findings"] if f["code"] == "PBR_CIRCULATION_VELOCITY_MISMATCH"]
        assert bool(mismatch) == (scale != 1.0)
    # Loop-mean biomass changes by less than the derived bound and by less than 1e-3.
    reference = _loop_mean(runs[1.0][1])
    elapsed = days * 86400.0
    for scale in (0.25, 4.0):
        tau = v_loop / (base_q * scale)
        bound = min(1e-3, 10 * mu_max_s * tau * mu_max_s * elapsed)
        relative = np.max(np.abs(_loop_mean(runs[scale][1]) - reference) / reference)
        assert relative < bound, (scale, relative, bound)


# ---------------------------------------------------------------- 3. net throughput sets dilution

def _steady_root(snapshot: dynamic_engine.Snapshot, f: float, d_net_h: float, n_in: float) -> float:
    g = _growths(snapshot)["PBR"]

    def residual(x: float) -> float:
        mu, loss = g.rates_at(PAR, TEMPERATURE_K, x, max(0.0, n_in - g.nitrogen_quota * x))
        return f * mu - loss - d_net_h

    return brentq(residual, 1e-6, 0.999 * n_in / g.nitrogen_quota if g.nitrogen_quota else 50.0, xtol=1e-14)


@pytest.mark.parametrize("d_net_per_day", [0.1, 0.25, 0.5])
def test_net_throughput_sets_dilution_and_steady_biomass(d_net_per_day: float) -> None:
    q_circ, tank_volume, tube_length = 5e-4, 2.5e-3, 1.0
    v_pbr = 10 * math.pi * 0.05 ** 2 / 4.0 * tube_length
    v_loop = 4 * v_pbr + 2 * tank_volume
    feed = d_net_per_day / 86400.0 * v_loop
    _, snapshot = _loop(q_circ=q_circ, tube_length=tube_length, tank_volume=tank_volume, days=90,
                        cadence_s=86400, feed_m3_s=feed, feed_n=0.5)
    result = _run(snapshot)
    loop = result.manifest["culture_loops"][0]
    assert loop["net_dilution_1_s"] == pytest.approx(d_net_per_day / 86400.0, rel=1e-9)
    assert loop["circulation_m3_s"] == pytest.approx(q_circ, rel=1e-12)
    assert loop["culture_residence_time_s"] == pytest.approx(86400.0 / d_net_per_day, rel=1e-9)
    x_star = _steady_root(snapshot, loop["illuminated_fraction"], d_net_per_day / 24.0, 0.5)
    x_mean = _loop_mean(result)
    assert x_mean[-1] == pytest.approx(x_star, rel=2e-3)
    assert abs(x_mean[-1] - x_mean[-2]) / x_mean[-1] < 1e-4  # settled


def test_net_dilution_above_maximum_net_growth_washes_out() -> None:
    q_circ, tank_volume, tube_length = 5e-4, 2.5e-3, 1.0
    v_loop = 4 * 10 * math.pi * 0.05 ** 2 / 4.0 * tube_length + 2 * tank_volume
    _, probe = _loop(q_circ=q_circ, tube_length=tube_length, tank_volume=tank_volume, days=1, cadence_s=3600,
                     feed_m3_s=1e-7)
    g = _growths(probe)["PBR"]
    f = 4 * 10 * math.pi * 0.05 ** 2 / 4.0 * tube_length / v_loop
    mu0, loss0 = g.rates_at(PAR, TEMPERATURE_K, 1e-9, 0.5)
    d_max_h = f * mu0 - loss0
    assert d_max_h > 0
    d_h = 2.0 * d_max_h
    _, snapshot = _loop(q_circ=q_circ, tube_length=tube_length, tank_volume=tank_volume, days=20,
                        cadence_s=86400, feed_m3_s=d_h / 3600.0 * v_loop, feed_n=0.5)
    x_mean = _loop_mean(_run(snapshot))
    assert np.all(np.diff(x_mean) < 0)
    assert x_mean[-1] < 1e-2 * x_mean[0]


def test_feed_change_inside_pumped_loop_keeps_circulation_and_moves_harvest() -> None:
    q_circ, tank_volume, tube_length = 5e-4, 2.5e-3, 1.0
    v_loop = 4 * 10 * math.pi * 0.05 ** 2 / 4.0 * tube_length + 2 * tank_volume
    feed_1, feed_2 = 0.1 / 86400 * v_loop, 0.4 / 86400 * v_loop
    _, snapshot = _loop(q_circ=q_circ, tube_length=tube_length, tank_volume=tank_volume, days=4,
                        cadence_s=21600, feed_m3_s=feed_1, events=[{
                            "time_s": 2 * 86400, "type": "feed_change", "target": "feed:Feed",
                            "value": feed_2, "value_unit": "m3/s"}])
    result = _run(snapshot)
    assert snapshot.payload["topology"]["implied_splitters"] == ["Split"]
    t = np.asarray(result.series["t_s"])
    circ = np.asarray(result.series["PBR_circulation_Q_m3_s"])
    dilution = np.asarray(result.series["PBR_net_dilution_1_s"])
    assert np.allclose(circ, q_circ, rtol=1e-12)
    assert np.allclose(dilution[t < 2 * 86400], feed_1 / v_loop, rtol=1e-9)
    assert np.allclose(dilution[t > 2 * 86400], feed_2 / v_loop, rtol=1e-9)
    for name in ("biomass", "total_nitrogen"):
        assert result.manifest["balances"]["aggregate"][name]["residual_rel"] <= 1e-6


def test_dilution_event_on_circulation_implied_splitter_is_refused() -> None:
    with pytest.raises(dynamic_engine.DynamicError) as exc:
        _loop(q_circ=5e-4, tube_length=1.0, tank_volume=2.5e-3, days=1, cadence_s=3600, feed_m3_s=1e-7,
              events=[{"time_s": 3600, "type": "dilution", "target": "splitter:Split", "value": 0.3,
                       "value_unit": "1"}])
    assert exc.value.code == "SPLIT_IMPLIED_BY_CIRCULATION"


def test_controller_on_circulation_implied_splitter_is_refused() -> None:
    from app.modules.process_stack.draft_models import SetController

    controller = SetController(op="set_controller", id="bleed", value={
        "type": "pi", "measurement": "X", "unit": "PBR", "actuator": "splitter:Split", "output_unit": "1",
        "cadence_s": 3600, "setpoint": 1.0})
    with pytest.raises(dynamic_engine.DynamicError) as exc:
        _loop(q_circ=5e-4, tube_length=1.0, tank_volume=2.5e-3, days=1, cadence_s=3600, feed_m3_s=1e-7,
              extra_ops=[controller], scenario_extra={"controllers": ["bleed"]})
    assert exc.value.code == "SPLIT_IMPLIED_BY_CIRCULATION"


def test_pumped_loop_harvest_is_continuity_not_an_inventory_draw() -> None:
    # The implied split is a steady hydraulic bleed equal to the net boundary feed. It never removes a
    # fraction of the inventory: loop volumes are fixed and the split ratio is not an actuator. Semi-batch
    # draw/refill is a separate dynamic inventory operation on tanks (spec 185), not a Splitter control.
    q_circ, tank_volume, tube_length = 5e-4, 2.5e-3, 1.0
    v_loop = 4 * 10 * math.pi * 0.05 ** 2 / 4.0 * tube_length + 2 * tank_volume
    feed = 0.25 / 86400 * v_loop
    _, snapshot = _loop(q_circ=q_circ, tube_length=tube_length, tank_volume=tank_volume, days=2,
                        cadence_s=21600, feed_m3_s=feed)
    solved = dynamic_engine._solve_flows(snapshot.payload["topology"])
    flows = {stream["tag"]: solved[stream["id"]] for stream in snapshot.payload["topology"]["streams"]}
    assert flows["Harvest"] == pytest.approx(feed, rel=1e-12)
    result = _run(snapshot)
    assert sum(_volumes(snapshot).values()) == pytest.approx(v_loop, rel=1e-12)
    assert result.manifest["culture_loops"][0]["volume_m3"] == pytest.approx(v_loop, rel=1e-12)
    # The bleed never changes the inventory: tank and loop volumes stay exactly constant (185 tracks them).
    for tag in TANK_TAGS:
        assert np.all(result.series[f"{tag}_V_m3"] == tank_volume)
    assert np.allclose(result.series["PBR_volume_m3"], v_loop, rtol=1e-12)
    assert "event_phase" not in result.series


def test_inconsistent_overdetermined_flow_balance_is_rejected_not_least_squared() -> None:
    # A feed enters a circulation-fixed closed loop that has no outlet: every stream is determined
    # (full column rank) but volume cannot be conserved. Least squares would return a best fit that
    # silently loses the feed; the solve must refuse it with the conservation residual instead.
    with pytest.raises(dynamic_engine.DynamicError) as exc:
        _loop(q_circ=5e-4, tube_length=1.0, tank_volume=2.5e-3, days=1, cadence_s=3600, feed_m3_s=1e-5,
              extra_ops=[Delete(op="delete", id="harvest")])
    assert exc.value.code == "FLOW_BALANCE_UNSOLVED"
    assert exc.value.detail["reason"] == "conservation_residual"
    assert exc.value.detail["residual_rel"] > 1e-3
    assert exc.value.detail["streams"]


def test_flow_solve_rejects_contradictory_circulation_rows_directly() -> None:
    _, snapshot = _loop(q_circ=5e-4, tube_length=1.0, tank_volume=2.5e-3, days=1, cadence_s=3600)
    topology = dict(snapshot.payload["topology"])
    matrix = np.asarray(topology["flow_matrix"])
    circ_row = int(next(iter(topology["circulation_rows"])))
    other = matrix[circ_row].copy()
    other_stream = int(np.flatnonzero(other)[0])
    # A second pump on the same loop asks for twice the circulation of the first.
    duplicate = np.zeros(matrix.shape[1])
    duplicate[(other_stream + 1) % matrix.shape[1]] = 1.0
    topology["flow_matrix"] = [*topology["flow_matrix"], duplicate.tolist()]
    topology["flow_rhs"] = [*topology["flow_rhs"], 1e-3]
    with pytest.raises(dynamic_engine.DynamicError) as exc:
        dynamic_engine._solve_flows(topology)
    assert exc.value.code == "FLOW_BALANCE_UNSOLVED"
    assert exc.value.detail["reason"] == "conservation_residual"
    assert exc.value.detail["residual_rel"] > 0.1
    assert dynamic_engine._solve_flows(snapshot.payload["topology"])  # the consistent original still solves


# ---------------------------------------------------------------- 7. performance

def test_thirty_day_zephyr_scale_loop_runs_under_one_second_per_simulated_day(record_property) -> None:
    # Four 30 m x 10-tube modules and two 20 L tanks circulated at 5e-4 m3/s.
    _, snapshot = _loop(q_circ=5e-4, tube_length=30.0, tank_volume=0.02, days=30, cadence_s=3600)
    started = time.perf_counter()
    result = _run(snapshot)
    wall = time.perf_counter() - started
    record_property("wall_s_per_simulated_day", wall / 30.0)
    print(f"184 performance: 30 simulated days in {wall:.2f} s ({wall / 30.0:.3f} s/day)")
    assert result.series["PBR_X_mean"][-1] > 0
    assert wall / 30.0 < 1.0


# ---------------------------------------------------------------- 6. real DWSIM downstream of a culture loop

@pytest.mark.skipif(not os.environ.get("JARVISOS_DWSIM_MCP_PATH"), reason="set JARVISOS_DWSIM_MCP_PATH to opt in to DWSIM runtime")
def test_real_dwsim_samples_continuous_harvest_leaving_a_culture_loop() -> None:
    from app.modules.process_stack.dynamic_downstream import build_sampler

    q_circ, tank_volume, tube_length = 5e-4, 2.5e-3, 1.0
    v_loop = 4 * 10 * math.pi * 0.05 ** 2 / 4.0 * tube_length + 2 * tank_volume
    feed = 0.25 / 86400 * v_loop
    heater = [
        AddUnit(op="add_unit", id="heater", type="Heater", tag="Heater", x=800, y=400),
        AddStream(op="add_stream", id="hot", tag="Hot", x=900, y=400),
        Connect(op="connect", stream="harvest", end="target", unit="heater", port=0),
        Connect(op="connect", stream="hot", end="source", unit="heater", port=0),
        SetUnitParams(op="set_unit_params", unit="heater", mode="outlet_temperature",
                      values={"outlet_temperature": _q(310.15, "K")}),
    ]
    _, snapshot = _loop(q_circ=q_circ, tube_length=tube_length, tank_volume=tank_volume, days=1, cadence_s=3600,
                        feed_m3_s=feed, extra_ops=heater,
                        scenario_extra={"downstream_enabled": True, "downstream_cadence_s": 43200})
    sampler = build_sampler(snapshot)
    assert sampler is not None
    run = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _v: None, sampler=sampler)
    plain = _run(snapshot)
    assert run.status == "succeeded", run.error
    for tag in (*PBR_TAGS, *TANK_TAGS):
        assert np.array_equal(run.series[f"{tag}_X"], plain.series[f"{tag}_X"])
    outcomes = run.manifest["downstream_outcomes"]
    assert [item["time_s"] for item in outcomes] == [43200.0, 86400.0]
    for outcome in outcomes:
        assert outcome["status"] == "succeeded", outcome
        boundary = outcome["boundary_inputs"]["Harvest"]
        assert boundary["provenance"] == "jarvis_t1_snapshot"
        hot = outcome["streams"]["Hot"]
        assert hot["provenance"] == "dwsim_solved"
        assert hot["temperature_K"] == pytest.approx(310.15, abs=0.05)
        assert hot["mass_flow_kg_s"] == pytest.approx(boundary["mass_flow_kg_s"], rel=1e-4)
        assert outcome["solve"]["status"] == "completed"
