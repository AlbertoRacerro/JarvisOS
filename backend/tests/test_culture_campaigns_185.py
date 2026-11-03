"""Spec 185 acceptance: semi-batch culture campaigns through the real draft -> prepare -> run path.

Each case checks the engine against an independent reference: closed-form event arithmetic, a separately
written ``solve_ivp``-plus-events model of the same ring, or a recomputation of the KPIs from the series.
Only the pinned card's rate law (``rates_at``) is shared with the engine.
"""

from __future__ import annotations

import math
import os
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest
from scipy.integrate import solve_ivp

from app.modules.process_stack import draft, dynamic_engine
from app.modules.process_stack.draft_models import (
    AddStream,
    AddUnit,
    Connect,
    Delete,
    DraftQuantity,
    SetScenario,
    SetSchedule,
    SetUnitModel,
    SetUnitParams,
)
from tests.plumbing_170_support import new_workspace, pbr_ops, pbr_quantities
from tests.test_culture_loop_184_acceptance import _loop

PAR = 500.0
TEMPERATURE_K = 298.15
O2_SAT = 0.008  # kg/m3, the PBR oxygen_saturation of the 170 fixture
DIAMETER, TUBES = 0.05, 10.0


def _q(value: float, unit: str) -> DraftQuantity:
    return DraftQuantity(value=value, unit=unit)


def _ring(*, tube_length: float, tank_volume: float, days: float, cadence_s: int, events: list[dict[str, Any]],
          q_circ: float = 5e-4, dark: bool = False, k_d: float = 0.003, initial: dict[str, dict[str, float]],
          min_volume: float = 0.0, max_volume: float | None = None) -> dynamic_engine.Snapshot:
    """Pump -> PBR -> TankA -> Pump with specified circulation and a schedule."""
    from app.modules.bio_models import service as bio_models
    from app.modules.environment import profiles

    workspace = new_workspace()
    duration = int(days * 86400)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    stamps = [start + timedelta(seconds=3600 * i) for i in range(duration // 3600 + 1)]
    profile = profiles.create_profile(
        workspace, name="185 campaign", timestamps=[s.isoformat().replace("+00:00", "Z") for s in stamps],
        channels={"par": [PAR] * len(stamps)}, resolution_minutes=60, provenance={"kind": "deterministic_test"})
    parameter_set = bio_models.create_set(workspace, "185 parameters")
    for symbol, value, unit in (
        ("K_I", 150.0, "umol/(m**2*s)"), ("K_j_0", 0.001, "kg/m3"), ("k_d", k_d, "1/hour"),
        ("a", 1.8, "1"), ("b", 0.5, "1"), ("c", 0.1, "1"), ("d", 0.01, "1"),
        ("w_ash", 0.05, "1"), ("k_X", 150.0, "m**2/kg"), ("T_min", 278.15, "K"),
        ("T_opt", 298.15, "K"), ("T_max", 318.15, "K"),
    ):
        parameter_set = bio_models.edit_set_value(
            workspace, parameter_set["id"], symbol, {"value": value, "unit": unit, "expected_unit": unit},
            parameter_set["revision"], parameter_set["digest"])
    card = bio_models.create_card(
        workspace, "185 card", parameter_set["id"],
        {"light": "light.monod", "optics": "optics.slab_response_average", "temperature": "temperature.ctmi",
         "nutrients": ["nutrient.monod"], "combination": "combine.liebig", "loss": "loss.first_order",
         "stoichiometry": "stoich.photoautotrophic"},
        {"value": 0.08, "unit": "1/hour"})
    state = draft.create_draft(workspace, "185 campaign")
    velocity = q_circ / (TUBES * math.pi * DIAMETER ** 2 / 4.0)
    ops: list[Any] = [
        *pbr_ops(model=False, flow_kg_s=0.001),
        SetUnitModel(op="set_unit_model", unit="pbr", model={
            "card_id": card["id"], "card_revision": card["revision"], "card_digest": card["digest"]}),
        Delete(op="delete", id="feed"), Delete(op="delete", id="product"),
        SetUnitParams(op="set_unit_params", unit="pbr", values=pbr_quantities(
            tube_length=(tube_length, "m"), tube_count=(TUBES, "dimensionless"),
            tube_inner_diameter=(DIAMETER, "m"), liquid_velocity=(velocity, "m/s"))),
        AddUnit(op="add_unit", id="tanka", type="HoldupTank", tag="TankA", x=300, y=0),
        SetUnitParams(op="set_unit_params", unit="tanka", mode="dynamic", values={
            "liquid_volume": _q(tank_volume, "m3"), "min_volume": _q(min_volume, "m3"),
            "max_volume": _q(max_volume or tank_volume, "m3"), "temperature": _q(TEMPERATURE_K, "K")}),
        AddUnit(op="add_unit", id="pump", type="Pump", tag="Pump", x=0, y=100),
    ]
    for k, (src, dst) in enumerate((("pbr", "tanka"), ("tanka", "pump"), ("pump", "pbr"))):
        ops += [AddStream(op="add_stream", id=f"s{k}", tag=f"S{k}", x=50 * k, y=300),
                Connect(op="connect", stream=f"s{k}", end="source", unit=src, port=0),
                Connect(op="connect", stream=f"s{k}", end="target", unit=dst, port=0)]
    ops += [SetSchedule(op="set_schedule", id="campaign", value={"events": events}),
            SetScenario(op="set_scenario", id="run", value={
                "units": ["PBR", "TankA"], "profiles": [{"profile_id": profile["profile_id"], "digest": profile["digest"]}],
                "start_utc": start.isoformat().replace("+00:00", "Z"),
                "end_utc": (start + timedelta(seconds=duration)).isoformat().replace("+00:00", "Z"),
                "output_cadence_s": cadence_s, "rtol": 1e-10, "atol": 1e-13, "temperature_source": "unit_mean",
                "par_scale": 0.0 if dark else 1.0, "circulation": {"Pump": q_circ}, "initial": initial,
                "schedule_id": "campaign"})]
    state = draft.patch(workspace, state["draft_id"], state["revision"], ops)
    return dynamic_engine.prepare(workspace, state["draft_id"], "run")


def _run(snapshot: dynamic_engine.Snapshot) -> dynamic_engine.EngineResult:
    result = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _value: None)
    assert result.status == "succeeded", result.error
    return result


def _volumes(snapshot: dynamic_engine.Snapshot) -> dict[str, float]:
    return {item["tag"]: float(item["volume_m3"]) for item in snapshot.payload["units"]}


def _growth(snapshot: dynamic_engine.Snapshot, tag: str) -> Any:
    item = next(item for item in snapshot.payload["units"] if item["tag"] == tag)
    return dynamic_engine._growth_for_unit(item, dark=item["unit"]["type"] == "HoldupTank")


def _post_rows(result: dynamic_engine.EngineResult) -> dict[int, int]:
    """Event order -> index of its post-event row."""
    phase, order = result.series["event_phase"], result.series["event_order"]
    return {int(order[i]): i for i in range(len(phase)) if phase[i] == 1}


# ---------------------------------------------------------------- 1. hand-computed fill-and-draw

def test_hand_computed_fill_and_draw_matches_closed_form_after_every_event() -> None:
    x0, n0, o0 = 0.4, 0.05, O2_SAT
    tank_volume, t_event = 0.05, 3600.0
    initial = {tag: {"X": x0, "N": n0, "O2": o0} for tag in ("PBR", "TankA")}
    events = [
        {"type": "actions", "time_s": t_event, "actions": [
            {"type": "draw", "tank": "TankA", "loop_fraction": 0.2},
            {"type": "separate", "recovery": 0.95, "concentration_factor": 20.0, "return_to": "TankA"}]},
        {"type": "actions", "time_s": t_event, "actions": [
            {"type": "refill", "tank": "TankA", "to_volume_m3": tank_volume, "medium": {"N": 0.5, "O2": O2_SAT}}]},
        {"type": "actions", "time_s": t_event, "actions": [
            {"type": "dose", "tank": "TankA", "nitrogen_kg": 0.001}]},
    ]
    # Dark, zero loss and oxygen at saturation: nothing changes between events, so every value is closed form.
    snapshot = _ring(tube_length=1.0, tank_volume=tank_volume, days=0.25, cadence_s=1800, events=events,
                     dark=True, k_d=0.0, initial=initial)
    result = _run(snapshot)
    quota = _growth(snapshot, "TankA").nitrogen_quota
    v_pbr = _volumes(snapshot)["PBR"]
    v_loop = v_pbr + tank_volume
    v_draw = 0.2 * v_loop
    v_conc = v_draw * 0.95 / 20.0
    v1 = tank_volume - v_draw + (v_draw - v_conc)
    x1 = (x0 * (tank_volume - v_draw) + 0.05 * v_draw * x0) / v1
    expected = [
        {"V": v1, "X": x1, "N": n0, "O2": o0},
        {"V": tank_volume, "X": x1 * v1 / tank_volume, "N": (n0 * v1 + 0.5 * v_conc) / tank_volume, "O2": o0},
    ]
    expected.append({**expected[1], "N": expected[1]["N"] + 0.001 / tank_volume})
    rows = _post_rows(result)
    assert sorted(rows) == [0, 1, 2]
    series = result.series
    for order, want in enumerate(expected):
        i = rows[order]
        assert series["t_s"][i] == t_event and series["t_s"][i - 1] == t_event and series["event_phase"][i - 1] == -1
        assert series["TankA_V_m3"][i] == pytest.approx(want["V"], rel=1e-12)
        for name in ("X", "N", "O2"):
            assert series[f"TankA_{name}"][i] == pytest.approx(want[name], rel=1e-12), (order, name)
        assert series["PBR_X"][i] == pytest.approx(x0, rel=1e-12)
        assert series["PBR_N"][i] == pytest.approx(n0, rel=1e-12)
    # The event ledger books the concentrate as product and the clarified liquid as returned.
    first = next(entry for entry in result.manifest["event_log"] if entry["type"] == "actions")
    deltas = first["inventory_deltas"]
    assert deltas["harvested"]["volume_m3"] == pytest.approx(v_conc, rel=1e-12)
    assert deltas["harvested"]["biomass_kg"] == pytest.approx(0.95 * v_draw * x0, rel=1e-12)
    assert deltas["harvested"]["total_nitrogen_kg"] == pytest.approx(v_conc * n0 + quota * 0.95 * v_draw * x0, rel=1e-12)
    assert deltas["returned"]["volume_m3"] == pytest.approx(v_draw - v_conc, rel=1e-12)
    assert deltas["purged"]["volume_m3"] == 0.0
    assert first["semantics"] == "conservative_inventory_actions" and first["balance"]["closed"]
    assert result.manifest["campaign"]["ledger"]["harvested"]["biomass_kg"] == pytest.approx(0.95 * v_draw * x0, rel=1e-12)
    assert result.manifest["campaign"]["medium_nitrogen_added_kg"] == pytest.approx(0.5 * v_conc + 0.001, rel=1e-12)
    assert series["harvest_X_kg"][-1] == pytest.approx(0.95 * v_draw * x0, rel=1e-12)
    assert result.manifest["balances"]["asserted"] is True


# ---------------------------------------------------------------- 2. recurring campaign vs independent model

def test_recurring_campaign_matches_independent_model_and_kpis_recompute_from_series() -> None:
    period, cycles, fraction, recovery, factor, medium_n = 2 * 86400.0, 10, 0.2, 0.95, 20.0, 0.5
    tank_volume, tube_length, q_circ = 0.05, 1.0, 5e-4
    initial = {"PBR": {"X": 0.2, "N": 0.4, "O2": O2_SAT}, "TankA": {"X": 0.2, "N": 0.4, "O2": O2_SAT}}
    events = [{"type": "actions", "time_s": period, "every_s": period, "count": cycles, "actions": [
        {"type": "draw", "tank": "TankA", "loop_fraction": fraction},
        {"type": "separate", "recovery": recovery, "concentration_factor": factor, "return_to": "TankA"},
        {"type": "refill", "tank": "TankA", "to_volume_m3": tank_volume, "medium": {"N": medium_n, "O2": 0.0}}]}]
    snapshot = _ring(tube_length=tube_length, tank_volume=tank_volume, days=cycles * 2 + 1, cadence_s=21600,
                     events=events, q_circ=q_circ, initial=initial)
    result = _run(snapshot)
    series, manifest = result.series, result.manifest
    actions = [entry for entry in manifest["event_log"] if entry["type"] == "actions"]
    assert len(actions) == cycles and all(entry["balance"]["closed"] for entry in actions)
    assert manifest["balances"]["asserted"] is True
    for name in ("biomass", "total_nitrogen", "oxygen"):
        assert manifest["balances"]["aggregate"][name]["residual_rel"] <= 1e-6

    # Independent model: two well-mixed volumes in a ring, tank volume changed only by the event arithmetic.
    g_pbr, g_tank = _growth(snapshot, "PBR"), _growth(snapshot, "TankA")
    v_pbr = _volumes(snapshot)["PBR"]

    def rhs(_t: float, y: np.ndarray, v_tank: float) -> list[float]:
        xp, np_, xt, nt = y
        mu_p, loss_p = g_pbr.rates_at(PAR, TEMPERATURE_K, xp, np_)
        mu_t, loss_t = g_tank.rates_at(0.0, TEMPERATURE_K, xt, nt)
        rp, rt = (mu_p - loss_p) * xp / 3600.0, (mu_t - loss_t) * xt / 3600.0
        return [rp + q_circ / v_pbr * (xt - xp), -g_pbr.nitrogen_quota * rp + q_circ / v_pbr * (nt - np_),
                rt + q_circ / v_tank * (xp - xt), -g_tank.nitrogen_quota * rt + q_circ / v_tank * (np_ - nt)]

    t_all = np.asarray(series["t_s"])
    phase = series["event_phase"]
    reference = np.zeros((len(t_all), 4))
    y = np.asarray([0.2, 0.4, 0.2, 0.4])
    v_tank, cursor, left = tank_volume, 0, 0.0
    harvested_ref: list[float] = []
    for k in range(1, cycles + 2):
        right = min(k * period, t_all[-1])
        mask = [i for i in range(cursor, len(t_all)) if t_all[i] <= right + 1e-9 and phase[i] != 1]
        sol = solve_ivp(rhs, (left, right), y, method="Radau", t_eval=[t_all[i] for i in mask],
                        rtol=1e-11, atol=1e-14, args=(v_tank,))
        assert sol.success, sol.message
        reference[mask] = sol.y.T
        y = sol.y[:, -1].copy()
        cursor = (mask[-1] + 1) if mask else cursor
        if k > cycles:
            break
        # draw f*V_loop at tank concentration, separate, return clarified liquid, refill to volume
        v_draw = fraction * (v_pbr + v_tank)
        xt, nt = y[2], y[3]
        v_conc = v_draw * recovery / factor
        v_after = v_tank - v_draw + (v_draw - v_conc)
        xt = (xt * (v_tank - v_draw) + (1 - recovery) * v_draw * xt) / v_after
        harvested_ref.append(recovery * v_draw * y[2])
        nt = (nt * v_after + medium_n * (tank_volume - v_after)) / tank_volume
        xt = xt * v_after / tank_volume
        y[2], y[3], v_tank = xt, nt, tank_volume
        assert phase[cursor] == 1
        reference[cursor] = y
        cursor += 1
        left = right
    actual = np.column_stack([series["PBR_X"], series["PBR_N"], series["TankA_X"], series["TankA_N"]])
    np.testing.assert_allclose(actual, reference, rtol=1e-4, atol=1e-9)

    # Cycle KPIs recomputed from the series alone.
    loop = manifest["campaign"]["loops"]["PBR"]
    pre_rows = [i for i in range(len(phase)) if phase[i] == -1]
    assert len(loop["cycles"]) == cycles == len(pre_rows)
    for cycle, i, harvest in zip(loop["cycles"], pre_rows, harvested_ref, strict=True):
        v_loop = series["PBR_volume_m3"][i]
        from_series = recovery * fraction * v_loop * series["TankA_X"][i]
        assert cycle["harvested_biomass_kg"] == pytest.approx(from_series, rel=1e-12)
        assert cycle["harvested_biomass_kg"] == pytest.approx(harvest, rel=1e-4)
        assert cycle["x_mean_before_draw_kg_m3"] == pytest.approx(series["PBR_X_mean"][i], rel=1e-12)
        assert cycle["x_mean_after_event_kg_m3"] == pytest.approx(series["PBR_X_mean"][i + 1], rel=1e-12)
        assert cycle["length_s"] == pytest.approx(period)
        assert cycle["net_volumetric_productivity_kg_m3_day"] == pytest.approx(from_series / v_loop / 2.0, rel=1e-12)
        area = math.pi * DIAMETER * tube_length * TUBES
        assert cycle["areal_productivity_kg_m2_day"] == pytest.approx(from_series / area / 2.0, rel=1e-12)
        assert cycle["concentrate_biomass_kg_m3"] == pytest.approx(
            from_series / (fraction * v_loop * recovery / factor), rel=1e-12)
    assert loop["cycles"][0]["startup"] and not any(cycle["startup"] for cycle in loop["cycles"][1:])
    assert loop["cycles"][1]["medium_volume_m3"] == pytest.approx(fraction * (v_pbr + tank_volume) * recovery / factor,
                                                                  rel=1e-12)
    pre_x = [series["PBR_X_mean"][i] for i in pre_rows][-3:]
    changes = [abs(b - a) / a for a, b in zip(pre_x, pre_x[1:], strict=False)]
    summary = loop["summary"]
    assert summary["pre_draw_x_relative_changes"] == pytest.approx(changes, rel=1e-9)
    assert summary["periodicity"] == ("periodic campaign reached" if max(changes) < 0.01 else "not yet periodic")
    last = loop["cycles"][-3:]
    assert summary["mean_last_k_net_volumetric_productivity_kg_m3_day"] == pytest.approx(
        sum(cycle["net_volumetric_productivity_kg_m3_day"] for cycle in last) / 3, rel=1e-12)


# ---------------------------------------------------------------- 3. guards

def _single(actions: list[dict[str, Any]], **extra: Any) -> dict[str, Any]:
    return {"type": "actions", "time_s": 3600.0, "actions": actions, **extra}


@pytest.mark.parametrize(("actions", "code"), [
    ([{"type": "draw", "tank": "TankA", "volume_m3": 0.06}], "EVENT_VOLUME_INVALID"),
    ([{"type": "refill", "tank": "TankA", "volume_m3": 0.02, "medium": {"N": 0.5, "O2": 0.0}}], "EVENT_VOLUME_INVALID"),
    ([{"type": "inoculate", "tank": "TankA", "volume_m3": 0.001, "culture": {"X": 1.0, "N": 0.1, "O2": 0.0}}],
     "EVENT_VOLUME_INVALID"),
    ([{"type": "separate", "recovery": 0.9, "concentration_factor": 10.0, "return_to": "none"}], "EVENT_VALUE_INVALID"),
    ([{"type": "draw", "tank": "PBR", "volume_m3": 0.001}], "EVENT_TARGET_INVALID"),
])
def test_prepare_refuses_overdraw_overfill_and_malformed_actions(actions: list[dict[str, Any]], code: str) -> None:
    initial = {tag: {"X": 0.2, "N": 0.05, "O2": O2_SAT} for tag in ("PBR", "TankA")}
    with pytest.raises(dynamic_engine.DynamicError) as exc:
        _ring(tube_length=1.0, tank_volume=0.05, days=0.25, cadence_s=3600, events=[_single(actions)],
              initial=initial)
    assert exc.value.code == code


def test_state_triggered_actions_are_refused_until_186() -> None:
    initial = {tag: {"X": 0.2, "N": 0.05, "O2": O2_SAT} for tag in ("PBR", "TankA")}
    event = _single([{"type": "draw", "tank": "TankA", "volume_m3": 0.001}], observed="PBR.X", threshold=1.0,
                    threshold_unit="kg/m3", direction="above", hysteresis=0.1)
    with pytest.raises(dynamic_engine.DynamicError) as exc:
        _ring(tube_length=1.0, tank_volume=0.05, days=0.25, cadence_s=3600, events=[event], initial=initial)
    assert exc.value.code == "EVENT_VALUE_INVALID"


def test_forced_event_imbalance_fails_the_run_with_partial_artifacts(monkeypatch: pytest.MonkeyPatch) -> None:
    initial = {tag: {"X": 0.2, "N": 0.05, "O2": O2_SAT} for tag in ("PBR", "TankA")}
    snapshot = _ring(tube_length=1.0, tank_volume=0.05, days=0.25, cadence_s=3600, initial=initial, events=[
        _single([{"type": "draw", "tank": "TankA", "volume_m3": 0.005}])])
    monkeypatch.setattr(dynamic_engine, "_EVENT_BALANCE_TEST_PERTURBATION", 1e-6)
    result = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _value: None)
    assert result.status == "failed"
    assert result.error["code"] == "EVENT_BALANCE_NOT_CLOSED"
    assert result.error["detail"]["closure"]["biomass_kg"] == pytest.approx(1e-6)
    assert result.manifest["artifact_label"] == "diagnostic_failed" and len(result.series["t_s"]) >= 2


def test_aggregate_imbalance_of_an_action_run_fails_balance_not_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    initial = {tag: {"X": 0.2, "N": 0.05, "O2": O2_SAT} for tag in ("PBR", "TankA")}
    snapshot = _ring(tube_length=1.0, tank_volume=0.05, days=0.25, cadence_s=3600, initial=initial, events=[
        _single([{"type": "draw", "tank": "TankA", "volume_m3": 0.005}])])
    original = dynamic_engine._apply_actions

    def leaky(*args: Any, **kwargs: Any) -> np.ndarray:
        state = original(*args, **kwargs)
        args[3][-1]["impulse_inventory"][0] += 1e-6  # book a kg of biomass that never existed
        return state

    monkeypatch.setattr(dynamic_engine, "_apply_actions", leaky)
    result = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _value: None)
    assert result.status == "failed" and result.error["code"] == "BALANCE_NOT_CLOSED"


# ---------------------------------------------------------------- 4. inventory, not hydraulics

def test_pumped_loop_draw_changes_inventory_not_circulation_or_the_implied_split() -> None:
    q_circ, tank_volume, tube_length = 5e-4, 0.05, 1.0
    v_loop = 4 * TUBES * math.pi * DIAMETER ** 2 / 4.0 * tube_length + 2 * tank_volume
    feed = 0.25 / 86400 * v_loop
    _, snapshot = _loop(q_circ=q_circ, tube_length=tube_length, tank_volume=tank_volume, days=3, cadence_s=21600,
                        feed_m3_s=feed, events=[
                            {"type": "actions", "time_s": 86400.0, "actions": [
                                {"type": "draw", "tank": "TankA", "loop_fraction": 0.2}]},
                            {"type": "actions", "time_s": 2 * 86400.0, "actions": [
                                {"type": "refill", "tank": "TankA", "to_volume_m3": tank_volume,
                                 "medium": {"N": 0.5, "O2": 0.0}}]}])
    assert snapshot.payload["topology"]["implied_splitters"] == ["Split"]
    result = _run(snapshot)
    series = result.series
    t, phase, volume = np.asarray(series["t_s"]), series["event_phase"], series["PBR_volume_m3"]
    draw_post = next(i for i in range(len(t)) if t[i] == 86400.0 and phase[i] == 1)
    refill_post = next(i for i in range(len(t)) if t[i] == 2 * 86400.0 and phase[i] == 1)
    assert volume[draw_post - 1] == pytest.approx(v_loop, rel=1e-12)
    assert volume[draw_post] == pytest.approx(0.8 * v_loop, rel=1e-12)
    assert np.allclose(volume[draw_post:refill_post], 0.8 * v_loop, rtol=1e-12)
    assert volume[refill_post] == pytest.approx(v_loop, rel=1e-12)
    assert np.allclose(series["PBR_circulation_Q_m3_s"], q_circ, rtol=1e-12)
    assert np.allclose(series["feed_Feed_Q_m3_s"], feed, rtol=1e-12)
    for key in (key for key in series if key.startswith("splitter_Split_")):
        assert np.all(series[key] == series[key][0]), key
    # The draw was harvested product, so the run's harvest ledger includes it.
    draw = next(entry for entry in result.manifest["event_log"] if entry["type"] == "actions")
    assert draw["inventory_deltas"]["harvested"]["volume_m3"] == pytest.approx(0.2 * v_loop, rel=1e-12)
    assert all(record["type"] != "dilution" for record in result.manifest["event_log"])


# ---------------------------------------------------------------- 5. compatibility

def test_legacy_events_keep_behaviour_and_are_labelled_and_feed_changes_log_before_after() -> None:
    q_circ, tank_volume, tube_length = 5e-4, 0.05, 1.0
    v_loop = 4 * TUBES * math.pi * DIAMETER ** 2 / 4.0 * tube_length + 2 * tank_volume
    feed_1, feed_2 = 0.1 / 86400 * v_loop, 0.3 / 86400 * v_loop
    _, snapshot = _loop(q_circ=q_circ, tube_length=tube_length, tank_volume=tank_volume, days=1, cadence_s=21600,
                        feed_m3_s=feed_1, events=[
                            {"type": "harvest", "time_s": 21600.0, "unit": "TankA", "fraction": 0.1},
                            {"type": "inoculation", "time_s": 43200.0, "unit": "TankB", "value": {"X": 0.5},
                             "value_unit": "kg/m3"},
                            {"type": "feed_change", "time_s": 64800.0, "target": "feed:Feed", "value": feed_2,
                             "value_unit": "m3/s"}])
    result = _run(snapshot)
    log = {entry["type"]: entry for entry in result.manifest["event_log"]}
    assert log["harvest"]["semantics"] == "implicit_blank_refill"
    assert log["inoculation"]["semantics"] == "overwrite"
    assert log["feed_change"]["before"]["flows_m3_s"]["Feed"] == pytest.approx(feed_1, rel=1e-12)
    assert log["feed_change"]["after"]["flows_m3_s"]["Feed"] == pytest.approx(feed_2, rel=1e-12)
    assert "Harvest" in log["feed_change"]["after"]["flows_m3_s"]
    assert "event_phase" not in result.series  # legacy events keep a single row
    assert result.manifest["balances"]["asserted"] is False and "campaign" not in result.manifest
    # Legacy harvest is still a constant-volume (1 - f) scaling of the tank state.
    t = np.asarray(result.series["t_s"])
    i = int(np.flatnonzero(t == 21600.0)[0])
    assert np.all(result.series["TankA_V_m3"] == tank_volume)
    assert result.series["TankA_harvest_X_kg"][i] > 0


# ---------------------------------------------------------------- 6. performance

# The limit is stated for the qualification host (spec 185 acceptance 6); hosted CI runners are several times slower
# and the run is dominated by the shared 170 cylinder-optics rate law, so it runs with the real-DWSIM qualification.
@pytest.mark.skipif(not os.environ.get("JARVISOS_DWSIM_MCP_PATH"),
                    reason="qualification-host timing; set JARVISOS_DWSIM_MCP_PATH to opt in")
def test_sixty_day_zephyr_scale_campaign_runs_under_one_second_per_simulated_day(record_property) -> None:
    # Four 30 m x 10-tube modules and two 1 m3 tanks circulated at 5e-4 m3/s; every 48 h draw 20 %, separate, refill.
    events = [{"type": "actions", "time_s": 2 * 86400.0, "every_s": 2 * 86400.0, "end_s": 60 * 86400.0, "actions": [
        {"type": "draw", "tank": "TankA", "loop_fraction": 0.2},
        {"type": "separate", "recovery": 0.95, "concentration_factor": 20.0, "return_to": "TankA"},
        {"type": "refill", "tank": "TankA", "to_volume_m3": 1.0, "medium": {"N": 0.5, "O2": 0.0}}]}]
    _, snapshot = _loop(q_circ=5e-4, tube_length=30.0, tank_volume=1.0, days=60, cadence_s=3600, events=events)
    started = time.perf_counter()
    result = _run(snapshot)
    wall = time.perf_counter() - started
    record_property("wall_s_per_simulated_day", wall / 60.0)
    print(f"185 performance: 60 simulated days in {wall:.2f} s ({wall / 60.0:.3f} s/day)")
    assert len(result.manifest["campaign"]["loops"]["PBR"]["cycles"]) == 30
    assert wall / 60.0 < 1.0
