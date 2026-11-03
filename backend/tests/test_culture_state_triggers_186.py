"""Spec 186 acceptance: root-found, state-triggered culture actions through the real draft -> prepare -> run path.

Each case compares the engine with an independent reference: a closed form, a matrix exponential, or a separately
written ``solve_ivp`` model with its own terminal events and its own arming rule. Only the pinned card's rate law
(``rates_at``) and the engine's prepared tube volume are shared.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest
from pydantic import ValidationError
from scipy.integrate import solve_ivp
from scipy.linalg import expm
from scipy.optimize import brentq

from app.modules.process_stack import dynamic_engine
from app.modules.process_stack.dynamic_models import ScheduleEvent
from tests.test_culture_campaigns_185 import (
    O2_SAT,
    PAR,
    TEMPERATURE_K,
    _growth,
    _ring,
    _run,
    _volumes,
)

TANK = 0.05
DOSE = {"type": "dose", "tank": "TankA", "nitrogen_kg": 1e-9}  # a harmless action: a fire without a state change


def _initial(x: float = 0.2, n: float = 0.4, o2: float = O2_SAT) -> dict[str, dict[str, float]]:
    return {tag: {"X": x, "N": n, "O2": o2} for tag in ("PBR", "TankA")}


def _event(observed: str, direction: str, threshold: float, actions: list[dict[str, Any]] | None = None, *,
           hysteresis: float = 0.0, cooldown_s: float = 0.0, max_fires: int = 1, earliest: float = 0.0
           ) -> dict[str, Any]:
    return {"type": "actions", "time_s": earliest, "actions": actions or [DOSE], "condition": {
        "observed": observed, "direction": direction, "threshold": threshold, "hysteresis": hysteresis,
        "cooldown_s": cooldown_s, "max_fires": max_fires}}


def _fires(result: dynamic_engine.EngineResult) -> list[dict[str, Any]]:
    return [entry for entry in result.manifest["event_log"] if entry.get("trigger") == "state"]


# ---------------------------------------------------------------- 1. exact crossing time

def test_loop_x_below_fires_at_the_analytic_exponential_decay_time() -> None:
    # Dark ring: mu = 0 everywhere and the loss is first order, so the well-mixed loop decays as X0*exp(-k t).
    x0, x_low = 0.4, 0.3
    event = _event("PBR.loop_X", "below", x_low, [{"type": "draw", "tank": "TankA", "volume_m3": 0.005}])
    snapshot = _ring(tube_length=1.0, tank_volume=TANK, days=3, cadence_s=3600, events=[event], dark=True, k_d=0.01,
                     initial=_initial(x0))
    result = _run(snapshot)
    growth = _growth(snapshot, "PBR")
    for x in (0.1, x0):  # the loss is first order in X, with no other rate
        mu, loss = growth.rates_at(0.0, TEMPERATURE_K, x, 0.4)
        assert mu == 0.0 and loss == pytest.approx(0.01)
    decay = growth.rates_at(0.0, TEMPERATURE_K, x0, 0.4)[1] / 3600.0
    analytic = math.log(x0 / x_low) / decay
    (fire,) = _fires(result)
    assert fire["time_s"] == pytest.approx(analytic, rel=1e-6)
    assert fire["condition"]["root_time_s"] == fire["time_s"]
    assert fire["condition"]["observed_value"] == pytest.approx(x_low, rel=1e-9)
    # The pre-event row sits at the root, between ordinary hourly samples.
    t, phase = result.series["t_s"], result.series["event_phase"]
    i = int(np.flatnonzero(phase == -1)[0])
    assert t[i] == fire["time_s"] and t[i + 1] == fire["time_s"] and phase[i + 1] == 1
    assert result.series["PBR_X_mean"][i] == pytest.approx(x_low, rel=1e-9)
    assert np.all(np.diff(t) >= 0) and set(np.arange(0, 3 * 86400 + 1, 3600)) <= set(t)


def test_unit_o2_above_fires_at_the_matrix_exponential_crossing_time() -> None:
    # Dark, no loss: only gas transfer in the PBR (the tank has no kla) and the circulation couple O2 linearly.
    q_circ, threshold = 5e-4, 0.5 * O2_SAT
    event = _event("PBR.O2", "above", threshold, hysteresis=1e-3)
    snapshot = _ring(tube_length=1.0, tank_volume=TANK, days=1, cadence_s=3600, events=[event], dark=True, k_d=0.0,
                     q_circ=q_circ, initial=_initial(o2=0.0))
    result = _run(snapshot)
    growth = _growth(snapshot, "PBR")
    v_pbr = _volumes(snapshot)["PBR"]
    a, rows = growth.kla_h / 3600.0, np.asarray([[-q_circ / v_pbr, q_circ / v_pbr], [q_circ / TANK, -q_circ / TANK]])
    rows[0, 0] -= a

    def pbr_o2(t: float) -> float:
        return float(O2_SAT + (expm(rows * t) @ np.array([-O2_SAT, -O2_SAT]))[0])

    analytic = brentq(lambda t: pbr_o2(t) - threshold, 1.0, 86400.0, xtol=1e-9, rtol=1e-14)
    (fire,) = _fires(result)
    assert fire["time_s"] == pytest.approx(analytic, rel=1e-6)


# ---------------------------------------------------------------- 2. chatter control

@pytest.fixture
def alternating_light(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hourly light/dark switching: PBR O2 oscillates every two hours (period 12 samples of 600 s)."""
    from app.modules.environment import profiles

    original = profiles.create_profile

    def alternating(workspace: str, **kwargs: Any) -> Any:
        kwargs["channels"] = {"par": [PAR if i % 2 else 0.0 for i in range(len(kwargs["timestamps"]))]}
        return original(workspace, **kwargs)

    monkeypatch.setattr(profiles, "create_profile", alternating)


def _oscillating(condition: dict[str, Any]) -> dynamic_engine.EngineResult:
    event = _event("PBR.O2", "above", O2_SAT + 2.2e-4, **condition)
    return _run(_ring(tube_length=1.0, tank_volume=TANK, days=1, cadence_s=600, events=[event], initial=_initial()))


def _baseline_rising_crossings(level: float) -> list[tuple[float, float]]:
    """Sample intervals in which the unperturbed signal rises through ``level`` (every segment is monotone)."""
    snapshot = _ring(tube_length=1.0, tank_volume=TANK, days=1, cadence_s=600, events=[], initial=_initial())
    series = _run(snapshot).series
    t, o2 = series["t_s"], series["PBR_O2"]
    return [(float(t[i]), float(t[i + 1])) for i in range(len(t) - 1) if o2[i] < level <= o2[i + 1]]


def test_hysteresis_cooldown_and_max_fires_control_chatter_on_an_oscillating_forcing(alternating_light: None) -> None:
    level = O2_SAT + 2.2e-4
    crossings = _baseline_rising_crossings(level)
    assert len(crossings) >= 10  # without control the trigger would fire every cycle

    # No hysteresis, cooldown or cap beyond the numeric guard: one fire per rising crossing, none extra.
    free = _oscillating({"max_fires": 1000})
    assert len(_fires(free)) == len(crossings)
    for fire, (lo, hi) in zip(_fires(free), crossings, strict=True):
        assert lo <= fire["time_s"] <= hi

    # A band the signal never leaves after the first fire: exactly one fire and no re-arm.
    stuck = _oscillating({"hysteresis": 9e-4, "max_fires": 1000})
    assert len(_fires(stuck)) == 1
    history = stuck.manifest["state_triggers"][0]["history"]
    assert [item["event"] for item in history] == ["armed_at_start"]

    # A reachable band: re-arm happens at (threshold - band) and only after the signal left the band.
    banded = _oscillating({"hysteresis": 3.5e-4, "max_fires": 1000})
    fires, series = _fires(banded), banded.series
    rearms = [item for item in banded.manifest["state_triggers"][0]["history"] if item["event"] == "rearmed"]
    assert len(fires) >= 5 and len(fires) - 1 <= len(rearms) <= len(fires)
    t, o2 = np.asarray(series["t_s"]), np.asarray(series["PBR_O2"])
    for before, rearm, after in zip(fires, rearms, fires[1:], strict=False):
        assert before["time_s"] < rearm["time_s"] < after["time_s"]
        assert rearm["observed_value"] == pytest.approx(level - 3.5e-4, abs=1e-8)
        inside = (t > before["time_s"]) & (t < rearm["time_s"] - 1e-6)
        assert np.all(o2[inside] > level - 3.5e-4 - 1e-8)  # never left the band before the re-arm root

    # Cooldown: no two fires closer than cooldown_s, and fewer fires than the free-running trigger.
    cooled = _oscillating({"cooldown_s": 5 * 3600.0, "max_fires": 1000})
    times = [fire["time_s"] for fire in _fires(cooled)]
    assert 1 <= len(times) < len(crossings)
    assert all(b - a >= 5 * 3600.0 - 1e-6 for a, b in zip(times, times[1:], strict=False))

    # max_fires: exactly that many, then spent.
    capped = _oscillating({"max_fires": 3})
    assert len(_fires(capped)) == 3
    assert capped.manifest["state_triggers"][0]["status"] == "spent"
    assert [fire["condition"]["fire_index"] for fire in _fires(capped)] == [1, 2, 3]


def test_condition_already_satisfied_at_start_does_not_fire_until_it_leaves_the_band_and_crosses_again(
        alternating_light: None) -> None:
    # O2 starts at saturation, above this threshold. It is satisfied at the earliest time: no fire at t = 0.
    low = O2_SAT - 3e-4
    result = _oscillating_at(low, hysteresis=1e-4)
    history = result.manifest["state_triggers"][0]["history"]
    assert history[0]["event"] == "satisfied_at_start" and history[0]["time_s"] == 0.0
    fires = _fires(result)
    assert all(fire["time_s"] > 0 for fire in fires)
    # Each fire is preceded by a re-arm, which needs the signal at threshold - hysteresis first.
    kinds = [item["event"] for item in history]
    assert kinds[:2] == ["satisfied_at_start", "rearmed"] or not fires
    # A band that is never left: the satisfied condition never fires at all.
    never = _oscillating_at(low, hysteresis=5e-3)
    assert _fires(never) == []
    assert [item["event"] for item in never.manifest["state_triggers"][0]["history"]] == ["satisfied_at_start"]


def _oscillating_at(threshold: float, *, hysteresis: float) -> dynamic_engine.EngineResult:
    event = _event("PBR.O2", "above", threshold, hysteresis=hysteresis, max_fires=1000)
    return _run(_ring(tube_length=1.0, tank_volume=TANK, days=1, cadence_s=600, events=[event], initial=_initial()))


def test_earliest_time_delays_arming_and_a_crossing_before_it_is_ignored() -> None:
    # Dark decay crosses 0.3 at ~28.8 h; with an earliest time of 40 h the condition is already satisfied then.
    event = _event("PBR.loop_X", "below", 0.3, earliest=40 * 3600.0)
    result = _run(_ring(tube_length=1.0, tank_volume=TANK, days=3, cadence_s=3600, events=[event], dark=True,
                        k_d=0.01, initial=_initial(0.4)))
    assert _fires(result) == []
    assert result.manifest["state_triggers"][0]["history"][0] == {
        "event": "satisfied_at_start", "time_s": 40 * 3600.0,
        "observed_value": result.manifest["state_triggers"][0]["history"][0]["observed_value"]}


# ---------------------------------------------------------------- 3. campaign equivalence

def test_state_triggered_campaign_closes_balances_and_matches_independent_terminal_event_solve() -> None:
    x_high, band, fraction, recovery, factor, medium_n, max_fires = 0.4, 0.02, 0.2, 0.95, 20.0, 0.5, 5
    tube_length, q_circ, days, cadence = 1.0, 5e-4, 10, 21600
    actions = [{"type": "draw", "tank": "TankA", "loop_fraction": fraction},
               {"type": "separate", "recovery": recovery, "concentration_factor": factor, "return_to": "TankA"},
               {"type": "refill", "tank": "TankA", "to_volume_m3": TANK, "medium": {"N": medium_n, "O2": 0.0}}]
    event = _event("PBR.loop_X", "above", x_high, actions, hysteresis=band, max_fires=max_fires)
    snapshot = _ring(tube_length=tube_length, tank_volume=TANK, days=days, cadence_s=cadence, events=[event],
                     q_circ=q_circ, initial=_initial())
    result = _run(snapshot)
    series, manifest = result.series, result.manifest
    fires = _fires(result)
    assert len(fires) == max_fires
    assert all(fire["balance"]["closed"] and fire["semantics"] == "conservative_inventory_actions" for fire in fires)
    assert manifest["balances"]["asserted"] is True
    for name in ("biomass", "total_nitrogen", "oxygen"):
        assert manifest["balances"]["aggregate"][name]["residual_rel"] <= 1e-6
    assert len(manifest["campaign"]["loops"]["PBR"]["cycles"]) == max_fires

    # Provenance links the condition, the root time, the observed value and the 185 action record.
    for k, fire in enumerate(fires, start=1):
        assert fire["condition"]["fire_index"] == k and fire["condition"]["observed"] == "PBR.loop_X"
        assert fire["condition"]["observed_value"] == pytest.approx(x_high, rel=1e-8)
        assert fire["condition"]["root_time_s"] == fire["time_s"]
        assert [record["type"] for record in fire["actions"]] == ["draw", "separate", "refill"]
        assert fire["loops"]["PBR"]["x_mean_pre_kg_m3"] == pytest.approx(x_high, rel=1e-8)

    # Independent model: two well-mixed volumes in a ring, its own terminal events and its own arming rule.
    g_pbr, g_tank = _growth(snapshot, "PBR"), _growth(snapshot, "TankA")
    v_pbr = _volumes(snapshot)["PBR"]

    def rhs(_t: float, y: np.ndarray, v_tank: float) -> list[float]:
        xp, np_, xt, nt = y
        mu_p, loss_p = g_pbr.rates_at(PAR, TEMPERATURE_K, xp, np_)
        mu_t, loss_t = g_tank.rates_at(0.0, TEMPERATURE_K, xt, nt)
        rp, rt = (mu_p - loss_p) * xp / 3600.0, (mu_t - loss_t) * xt / 3600.0
        return [rp + q_circ / v_pbr * (xt - xp), -g_pbr.nitrogen_quota * rp + q_circ / v_pbr * (nt - np_),
                rt + q_circ / v_tank * (xp - xt), -g_tank.nitrogen_quota * rt + q_circ / v_tank * (np_ - nt)]

    def mean_x(y: np.ndarray, v_tank: float) -> float:
        return (y[0] * v_pbr + y[2] * v_tank) / (v_pbr + v_tank)

    t_all, phase = np.asarray(series["t_s"]), np.asarray(series["event_phase"])
    sample_rows = [i for i in range(len(t_all)) if phase[i] == 0]
    reference = np.full((len(t_all), 4), np.nan)
    y, v_tank, left, fired, armed = np.array([0.2, 0.4, 0.2, 0.4]), TANK, 0.0, 0, True
    root_times: list[float] = []
    reference[0] = y
    while True:
        def rise(t: float, state: np.ndarray, vt: float) -> float:
            return mean_x(state, vt) - x_high

        def fall(t: float, state: np.ndarray, vt: float) -> float:
            return mean_x(state, vt) - (x_high - band)

        rise.terminal, rise.direction = True, 1  # type: ignore[attr-defined]
        fall.terminal, fall.direction = True, -1  # type: ignore[attr-defined]
        ahead = [i for i in sample_rows if t_all[i] > left]
        sol = solve_ivp(rhs, (left, t_all[-1]), y, method="Radau", t_eval=[t_all[i] for i in ahead], rtol=1e-11,
                        atol=1e-14, args=(v_tank,), events=(rise if armed else fall))
        assert sol.success, sol.message
        reference[ahead[:len(sol.t)]] = sol.y.T
        if len(sol.t_events[0]) == 0:
            break
        left, y = float(sol.t_events[0][0]), sol.y_events[0][0].copy()
        if not armed:  # re-arm root
            armed = True
            continue
        root_times.append(left)
        pre = next(i for i in range(len(t_all)) if phase[i] == -1 and abs(t_all[i] - left) < 1.0)
        reference[pre] = y
        v_draw = fraction * (v_pbr + v_tank)
        v_conc = v_draw * recovery / factor
        v_after = v_tank - v_draw + (v_draw - v_conc)
        xt = (y[2] * (v_tank - v_draw) + (1 - recovery) * v_draw * y[2]) / v_after
        nt = (y[3] * v_after + medium_n * (TANK - v_after)) / TANK
        y[2], y[3], v_tank = xt * v_after / TANK, nt, TANK
        reference[pre + 1] = y
        fired += 1
        armed = mean_x(y, v_tank) <= x_high - band  # an unarmed condition waits for the band edge
        if fired == max_fires:
            armed = False
            # No more fires: advance without events by treating the rest as one plain solve.
            ahead = [i for i in sample_rows if t_all[i] > left]
            sol = solve_ivp(rhs, (left, t_all[-1]), y, method="Radau", t_eval=[t_all[i] for i in ahead],
                            rtol=1e-11, atol=1e-14, args=(v_tank,))
            reference[ahead] = sol.y.T
            break
    assert not np.isnan(reference).any()
    assert len(root_times) == max_fires
    np.testing.assert_allclose([fire["time_s"] for fire in fires], root_times, rtol=1e-6)
    actual = np.column_stack([series["PBR_X"], series["PBR_N"], series["TankA_X"], series["TankA_N"]])
    np.testing.assert_allclose(actual, reference, rtol=1e-4, atol=1e-9)
    # Restarting at a root keeps the sample grid: every output time is still present exactly once as a sample
    # row or, when it coincides with a root, as that event's pre row.
    assert set(np.arange(0, days * 86400 + 1, cadence)) <= set(t_all)


# ---------------------------------------------------------------- 4. runtime volume guard

def test_runtime_overdraw_and_overfill_fail_with_the_typed_error_while_prepare_accepts_the_schedule() -> None:
    draw = [{"type": "draw", "tank": "TankA", "volume_m3": 0.06}]  # more than the tank holds
    fill = [{"type": "refill", "tank": "TankA", "volume_m3": 0.01, "medium": {"N": 0.5, "O2": 0.0}}]  # above max
    for actions in (draw, fill):
        snapshot = _ring(tube_length=1.0, tank_volume=TANK, days=3, cadence_s=3600, dark=True, k_d=0.01,
                         events=[_event("PBR.loop_X", "below", 0.3, actions)], initial=_initial(0.4))
        result = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _value: None)
        assert result.status == "failed"
        assert result.error["code"] == "EVENT_VOLUME_INVALID_RUNTIME"
        assert result.error["detail"]["tank"] == "TankA"
        assert result.manifest["artifact_label"] == "diagnostic_failed"
        assert result.manifest["state_triggers"][0]["fires"] == 1


def test_time_triggered_action_after_a_state_trigger_is_guarded_at_run_time() -> None:
    state = _event("PBR.loop_X", "below", 0.3, [{"type": "draw", "tank": "TankA", "volume_m3": 0.01}])
    late = {"type": "actions", "time_s": 2 * 86400.0, "actions": [
        {"type": "draw", "tank": "TankA", "volume_m3": 0.045}]}  # fits the full tank, not the one left by the fire
    snapshot = _ring(tube_length=1.0, tank_volume=TANK, days=3, cadence_s=3600, dark=True, k_d=0.01,
                     events=[state, late], initial=_initial(0.4))
    result = dynamic_engine.run(snapshot, cancelled=lambda: False, progress=lambda _value: None)
    assert result.status == "failed" and result.error["code"] == "EVENT_VOLUME_INVALID_RUNTIME"


# ---------------------------------------------------------------- 5. validation

_GOOD = {"observed": "PBR.loop_X", "direction": "above", "threshold": 1.0, "hysteresis": 0.1, "cooldown_s": 60.0,
         "max_fires": 2}
_ACTIONS = [{"type": "dose", "tank": "TankA", "nitrogen_kg": 1.0}]


def _model(condition: dict[str, Any] | None = None, **extra: Any) -> ScheduleEvent:
    return ScheduleEvent.model_validate({"type": "actions", "time_s": 5.0, "actions": _ACTIONS,
                                         "condition": {**_GOOD, **(condition or {})}, **extra})


def test_valid_condition_round_trips_and_defaults_are_conservative() -> None:
    event = _model()
    assert event.condition is not None and event.condition.max_fires == 2
    minimal = ScheduleEvent.model_validate({"type": "actions", "time_s": 0, "actions": _ACTIONS, "condition": {
        "observed": "TankA.X", "direction": "below", "threshold": 0.1}})
    assert minimal.condition is not None
    assert (minimal.condition.hysteresis, minimal.condition.cooldown_s, minimal.condition.max_fires) == (0, 0, 1)


@pytest.mark.parametrize("condition", [
    {"hysteresis": -0.1}, {"cooldown_s": -1.0}, {"max_fires": 0}, {"max_fires": 1.5}, {"max_fires": 10_000},
    {"direction": "sideways"}, {"threshold": float("nan")}, {"threshold": float("inf")}, {"threshold": "1"},
    {"observed": "PBR.Y"}, {"observed": "PBR"}, {"observed": ".X"}, {"observed": "PBR.loop_O2"},
    {"unexpected": 1},
])
def test_model_rejects_bad_condition_fields(condition: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _model(condition)


@pytest.mark.parametrize("extra", [
    {"every_s": 10.0, "count": 2}, {"count": 2}, {"end_s": 100.0},
    {"observed": "PBR.X", "threshold": 1.0},
])
def test_model_rejects_conditions_that_mix_with_repetition_or_the_172_fields(extra: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _model(**extra)


def test_model_rejects_a_condition_on_a_legacy_event_type() -> None:
    with pytest.raises(ValidationError):
        ScheduleEvent.model_validate({"type": "harvest", "time_s": 0, "unit": "TankA", "fraction": 0.1,
                                      "condition": _GOOD})


@pytest.mark.parametrize("observed", ["Ghost.X", "Ghost.loop_X", "Pump.X"])
def test_prepare_rejects_unknown_observables_with_a_typed_error(observed: str) -> None:
    with pytest.raises(dynamic_engine.DynamicError) as exc:
        _ring(tube_length=1.0, tank_volume=TANK, days=1, cadence_s=3600, initial=_initial(),
              events=[_event(observed, "above", 1.0)])
    assert exc.value.code == "UNSUPPORTED_MEASUREMENT"


def test_prepare_still_validates_the_targets_of_a_conditional_action() -> None:
    with pytest.raises(dynamic_engine.DynamicError) as exc:
        _ring(tube_length=1.0, tank_volume=TANK, days=1, cadence_s=3600, initial=_initial(),
              events=[_event("PBR.X", "above", 1.0, [{"type": "draw", "tank": "PBR", "volume_m3": 0.001}])])
    assert exc.value.code == "EVENT_TARGET_INVALID"


# ---------------------------------------------------------------- 6. no state trigger, no change

def test_a_schedule_without_conditions_has_no_trigger_output() -> None:
    result = _run(_ring(tube_length=1.0, tank_volume=TANK, days=1, cadence_s=3600, initial=_initial(), events=[
        {"type": "actions", "time_s": 3600.0, "actions": [{"type": "draw", "tank": "TankA", "volume_m3": 0.005}]}]))
    assert "state_triggers" not in result.manifest and _fires(result) == []


# ---------------------------------------------------------------- 7. the integrator seam

def test_integrate_ode_stops_at_a_directional_root_and_reports_it() -> None:
    from app.modules.process_stack.dynamics import OdeInputError, integrate_ode

    def rhs(_t: float, y: tuple[float, ...]) -> list[float]:
        return [1.0, -y[1]]

    # y1 = exp(-t) falls through 0.1 at ln(10); y0 = t rises through 3 later.
    def roots(_t: float, y: tuple[float, ...]) -> list[float]:
        return [y[0] - 3.0, y[1] - 0.1]

    kwargs = {"rtol": 1e-10, "atol": 1e-12, "roots": roots}
    for grid in ([0.0, 1.0, 4.0], [0.0, 4.0]):  # a two-point grid takes the solver's internal-step workaround
        solved = integrate_ode(rhs, [0.0, 1.0], grid, root_directions=[1, -1], **kwargs)
        assert solved.success and solved.root_indices == (1,)
        assert solved.root_time == pytest.approx(math.log(10), rel=1e-7) and solved.times[-1] == solved.root_time
        assert solved.times[:-1] == tuple(g for g in grid if g < math.log(10))
        assert solved.root_state is not None and solved.root_state[0] == pytest.approx(math.log(10), rel=1e-7)
    later = integrate_ode(rhs, [0.0, 1.0], [0.0, 1.0, 4.0], root_directions=[1, 1], **kwargs)  # falling root ignored
    assert later.root_indices == (0,) and later.root_time == pytest.approx(3.0, rel=1e-7)
    none = integrate_ode(rhs, [0.0, 1.0], [0.0, 1.0, 4.0], root_directions=[-1, 1], **kwargs)
    assert none.root_time is None and none.times == (0.0, 1.0, 4.0) and none.root_indices == ()
    with pytest.raises(OdeInputError):
        integrate_ode(rhs, [0.0, 1.0], [0.0, 4.0], roots=roots, root_directions=[])
    with pytest.raises(OdeInputError):
        integrate_ode(rhs, [0.0, 1.0], [0.0, 4.0], roots=roots, root_directions=[2, 0])
