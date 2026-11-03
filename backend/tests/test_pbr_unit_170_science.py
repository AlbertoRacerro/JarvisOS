"""Spec 170 §9.1 scientific acceptance for ``jarvis.pbr_unit_t1``.

Expected values come from sources outside the production kernel: closed forms, the a170 survey's
recorded Q2 table, SciPy quadrature re-typed from the spec text, and the independent Radau/brentq
reference in ``scripts/benchmarks/pbr_reference/periodic_reference.py`` (values recorded below with the
command that regenerates them).
"""

from __future__ import annotations

import math
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from numpy.polynomial.legendre import leggauss
from scipy.integrate import quad

from app.modules.bio_models import cylinder
from app.modules.photobioreactor import pbr_evaluator
from app.modules.process_stack import pbr_unit
from app.modules.process_stack.mixed_runtime import JarvisUnitContext
from app.modules.process_stack.pbr_unit import Growth, PbrFailure, _Clock, certify, solve_periodic, thin_growth_rate

BACKEND = Path(__file__).resolve().parents[1]
K_D_107 = 0.004166666666666667  # 1/h, 107 synthetic fixture


def fixture_107(hrt_d: float, **changes: Any) -> Growth:
    """Spec 170 Monod version of the synthetic 107 fixture (synthetic, not a species)."""
    values: dict[str, Any] = dict(
        mu_max_h=0.0625, light_saturation=150.0, extinction=150.0, nitrogen_half_saturation=0.001,
        nitrogen_quota=0.07, oxygen_yield=1.4, temperature_form="temperature.ctmi",
        temperature_parameters=(278.15, 298.15, 308.15), loss_form="loss.first_order", loss_parameters=(K_D_107,),
        diameter=0.05, peak_par=1500.0, photoperiod_h=14.0, diffuse_fraction=0.0, temperature_mean=295.15,
        temperature_amplitude=3.0, kla_h=5.0, oxygen_saturation=0.0075, dilution_h=1.0 / (24.0 * hrt_d),
        inlet=(0.0, 0.05, 0.0075))
    values.update(changes)
    return Growth(**values)


def clock() -> _Clock:
    return _Clock(time.monotonic() + 30.0)


def solve_and_certify(model: Growth) -> tuple[Any, Any]:
    solution = solve_periodic(model, clock())
    return solution, certify(model, solution.x_star, clock())


def independent_lambda(model: Growth) -> float:
    """Λ re-typed from the spec text (half-sine PAR, sinusoidal T, CTMI, Monod light and N)."""
    t_min, t_opt, t_max = model.temperature_parameters
    rise = 12.0 - model.photoperiod_h / 2.0

    def ctmi(temperature: float) -> float:
        if not t_min < temperature < t_max:
            return 0.0
        return max(0.0, (temperature - t_max) * (temperature - t_min) ** 2 / (
            (t_opt - t_min) * ((t_opt - t_min) * (temperature - t_opt)
                               - (t_opt - t_max) * (t_opt + t_min - 2.0 * temperature))))

    def growth(hour: float) -> float:
        par = model.peak_par * math.sin(math.pi * (hour - rise) / model.photoperiod_h)
        temperature = model.temperature_mean + model.temperature_amplitude * math.sin(2 * math.pi * (hour - 9) / 24)
        n_in = model.inlet[1]
        return (model.mu_max_h * par / (model.light_saturation + par) * ctmi(temperature)
                * n_in / (model.nitrogen_half_saturation + n_in))

    daylight = quad(growth, rise, rise + model.photoperiod_h, epsabs=1e-15, epsrel=1e-13, limit=400)[0]
    return daylight / 24.0 - model.loss_parameters[0]


# ------------------------------------------------------------------------------------------- branch


def test_lambda_and_critical_hrt_match_the_spec_anchor() -> None:
    model = fixture_107(3.0)
    expected = independent_lambda(model)
    assert expected == pytest.approx(0.02361, abs=5e-6)  # spec 170 fresh evidence §3
    assert 1.0 / expected / 24.0 == pytest.approx(1.765, abs=5e-4)
    assert thin_growth_rate(model) == pytest.approx(expected, rel=1e-9)


@pytest.mark.parametrize("hrt_d", [1.5, 1.7])
def test_107_monod_fixture_washes_out_below_critical_hrt(hrt_d: float) -> None:
    solution, orbit = solve_and_certify(fixture_107(hrt_d))
    assert solution.branch == "washout" and solution.x_star == 0.0
    assert max(row[0] for row in orbit.samples) == 0.0


# X* from scripts/benchmarks/pbr_reference/periodic_reference.py (SciPy Radau + brentq, independent optics):
#   python scripts/benchmarks/pbr_reference/periodic_reference.py
REFERENCE_X_STAR = {
    (3.0, 0.0, 0.0): 0.4548456438839151,
    (5.0, 0.0, 0.0): 0.6375285716893788,
    (3.0, 0.5, 0.0): 0.5014665503780513,
    (3.0, 0.0, 0.1): 0.5738826498337567,
}


@pytest.mark.parametrize("hrt_d", [2.0, 3.0, 5.0, 8.0])
def test_107_monod_fixture_is_productive_above_critical_hrt_and_never_trivial(hrt_d: float) -> None:
    solution, orbit = solve_and_certify(fixture_107(hrt_d))
    assert solution.branch == "productive"
    assert solution.x_star > 1e-3  # far from the trivial X = 0 fixed point
    assert all(abs(error) <= limit for error, limit in zip(orbit.residuals, orbit.tolerances, strict=True))
    assert orbit.z_identity_max <= orbit.tolerances[1]


@pytest.mark.parametrize(("key"), sorted(REFERENCE_X_STAR))
def test_periodic_biomass_matches_independent_radau_reference(key: tuple[float, float, float]) -> None:
    hrt_d, diffuse_fraction, x_in = key
    model = fixture_107(hrt_d, diffuse_fraction=diffuse_fraction, inlet=(x_in, 0.05, 0.0075))
    solution, _ = solve_and_certify(model)
    assert solution.x_star == pytest.approx(REFERENCE_X_STAR[key], rel=1e-5)


def test_numerical_tie_and_sub_resolution_branch_are_unresolved_not_washout() -> None:
    critical_hrt = 1.0 / thin_growth_rate(fixture_107(3.0)) / 24.0
    for factor in (1.0 - 5e-9, 1.0 + 5e-9):  # |Λ − D| ≤ 1e-8·D
        with pytest.raises(PbrFailure) as raised:
            solve_periodic(fixture_107(critical_hrt * factor), clock())
        assert raised.value.code == "PBR_BRANCH_UNRESOLVED"
    with pytest.raises(PbrFailure) as raised:  # Λ > D but the positive root is below X_res
        solve_periodic(fixture_107(critical_hrt * (1.0 + 1e-7)), clock())
    assert raised.value.code == "PBR_BRANCH_UNRESOLVED"
    assert "resolution" in str(raised.value)
    just_above, _ = solve_and_certify(fixture_107(critical_hrt * 1.00005))
    assert just_above.branch == "productive" and just_above.x_star > pbr_unit.X_RESOLUTION


def test_nitrogen_bound_at_or_below_resolution_refuses_before_seeding() -> None:
    model = fixture_107(3.0, nitrogen_half_saturation=1e-12, inlet=(0.0, 5e-8, 0.0075))
    assert thin_growth_rate(model) > model.dilution_h
    with pytest.raises(PbrFailure) as raised:
        solve_periodic(model, clock())
    assert raised.value.code == "PBR_BRANCH_UNRESOLVED"
    assert raised.value.detail["upper_bound_kg_m3"] == pytest.approx(5e-8 / 0.07)


def test_optical_depth_beyond_tier1_limit_is_typed_for_both_brackets() -> None:
    # The light-limited balance μ⟨f⟩(τ*) = D + k_d fixes τ* independently of k_X; fast growth at a
    # 100-day HRT puts τ* above 1000 for both the nitrogen (q > 0) and the doubling (q = 0) brackets.
    for quota, n_in in ((0.07, 50.0), (0.0, 0.05)):
        model = fixture_107(100.0, mu_max_h=5.0, nitrogen_quota=quota, inlet=(0.0, n_in, 0.0075))
        with pytest.raises(PbrFailure) as raised:
            solve_periodic(model, clock())
        assert raised.value.code == "PBR_OPTICS_OUT_OF_RANGE"
        assert raised.value.detail["optical_depth"] >= cylinder.TAU_MAX * (1 - 1e-12)


def test_doubling_cap_without_sign_change_is_no_finite_productive_state() -> None:
    # q = 0 and a vanishing k_X: the shading-limited root lies beyond 1e-3·2⁴⁰ kg m⁻³ while τ stays < 1000.
    model = fixture_107(3.0, nitrogen_quota=0.0, extinction=1e-15)
    with pytest.raises(PbrFailure) as raised:
        solve_periodic(model, _Clock(time.monotonic() + 60.0))
    assert raised.value.code == "PBR_NO_FINITE_PRODUCTIVE_STATE"
    assert "doublings" in str(raised.value)


# ------------------------------------------------------------------------------- structure and edges


def test_closed_form_oxygen_equals_brute_force_full_map_fixed_point() -> None:
    model = fixture_107(3.0, kla_h=0.05)  # α = exp(−24·(kLa + D)) ≈ 0.2, so iteration is informative
    solution, orbit = solve_and_certify(model)
    rhs, grids = pbr_unit._full_rhs(model), pbr_unit._schedule(model)
    state = [solution.x_star, orbit.initial[1], 0.0]
    for _ in range(60):
        state = list(pbr_unit._integrate(rhs, [*state, 0, 0, 0, 0, 0], grids, clock())[-1][:3])
        state[0], state[1] = orbit.initial[0], orbit.initial[1]  # X and N are already periodic
    assert state[2] == pytest.approx(orbit.initial[2], rel=1e-9, abs=1e-15)


def test_nonnegative_inlet_x_gives_unique_positive_state_from_any_bracket_side() -> None:
    model = fixture_107(0.5, inlet=(0.3, 0.05, 0.0075))
    assert thin_growth_rate(model) < model.dilution_h  # would wash out without inlet biomass
    solution, orbit = solve_and_certify(model)
    func = pbr_unit._Map(model, clock())
    below, above = func(0.5 * solution.x_star), func(min(1.5 * solution.x_star, 0.3 + 0.05 / 0.07))
    assert below > 0.0 > above  # one sign change around the certified root
    assert solution.branch == "productive" and 0.0 < solution.x_star


def test_no_feedback_linear_case_has_closed_form_in_daylight_and_refuses_growth_without_bound() -> None:
    model = fixture_107(0.5, extinction=0.0, nitrogen_quota=0.0, inlet=(0.05, 0.05, 0.0075))
    lam, dilution = independent_lambda(model), model.dilution_h
    assert lam < dilution
    solution, orbit = solve_and_certify(model)
    assert solution.map_evaluations == 2
    assert all(abs(error) <= limit for error, limit in zip(orbit.residuals, orbit.tolerances, strict=True))
    # Mean balance on the periodic orbit: D·(X_in − X̄) + (1/24)∫(μ_g − k_d)X dt = 0, with X̄ > X_in
    # because growth exceeds loss in this case only through inlet biomass.
    assert orbit.samples[-1][3] / 24.0 > 0.0
    with pytest.raises(PbrFailure) as raised:
        solve_periodic(fixture_107(5.0, extinction=0.0, nitrogen_quota=0.0, inlet=(0.05, 0.05, 0.0075)), clock())
    assert raised.value.code == "PBR_NO_FINITE_PRODUCTIVE_STATE"
    with pytest.raises(PbrFailure) as raised:
        solve_periodic(fixture_107(5.0, extinction=0.0, nitrogen_quota=0.0), clock())
    assert raised.value.code == "PBR_NO_FINITE_PRODUCTIVE_STATE"  # only an unstable washout exists


def test_dark_period_matches_cstr_decay_with_feedback_and_washes_out_without_inlet_biomass() -> None:
    dark = fixture_107(3.0, peak_par=0.0, inlet=(0.2, 0.05, 0.0075))
    assert thin_growth_rate(dark) == pytest.approx(-K_D_107, rel=1e-12)
    solution, orbit = solve_and_certify(dark)
    dilution = dark.dilution_h
    assert solution.x_star == pytest.approx(dilution * 0.2 / (dilution + K_D_107), rel=1e-9)
    assert orbit.initial[1] == pytest.approx(0.05 + 0.07 * (0.2 - solution.x_star), rel=1e-12)
    assert solve_periodic(fixture_107(3.0, peak_par=0.0), clock()).branch == "washout"


def test_zero_nutrient_inlet_washes_out_or_decays_and_keeps_nitrogen_nonnegative() -> None:
    assert solve_periodic(fixture_107(3.0, inlet=(0.0, 0.0, 0.0075)), clock()).branch == "washout"
    solution, orbit = solve_and_certify(fixture_107(3.0, inlet=(0.2, 0.0, 0.0075)))
    assert solution.x_star < 0.2
    assert min(row[1] for row in orbit.samples) >= -pbr_unit.NEGATIVE_TOLERANCE


def test_night_oxygen_depletion_without_aeration_is_typed_nonphysical() -> None:
    model = fixture_107(3.0, kla_h=0.0, inlet=(0.2, 0.05, 0.0), peak_par=0.0)
    solution = solve_periodic(model, clock())
    with pytest.raises(PbrFailure) as raised:
        certify(model, solution.x_star, clock())
    assert raised.value.code == "PBR_NONPHYSICAL_STATE"
    assert "oxygen_kla" in str(raised.value)


@pytest.mark.parametrize("hrt_d", [0.1, 100.0])
def test_extreme_hrt_certifies_without_relaxed_tolerances(hrt_d: float) -> None:
    model = fixture_107(hrt_d, inlet=(0.05, 0.05, 0.0075))
    solution, orbit = solve_and_certify(model)
    assert solution.branch == "productive"
    assert orbit.tolerances[0] == pbr_unit.PERIODIC_REL * max(solution.x_star, pbr_unit.X_RESOLUTION)
    assert all(abs(error) <= limit for error, limit in zip(orbit.residuals, orbit.tolerances, strict=True))


def test_periodic_solution_is_bitwise_deterministic_across_cold_processes() -> None:
    script = (f"import sys; sys.path.insert(0, {str(BACKEND)!r}); from tests.test_pbr_unit_170_science import "
              "fixture_107, solve_and_certify; s, o = solve_and_certify(fixture_107(3.0, diffuse_fraction=0.3)); "
              "print(repr((s.x_star, s.bracket, s.map_evaluations, o.initial, o.residuals)))")
    runs = {subprocess.run([sys.executable, "-c", script], cwd=BACKEND, capture_output=True, text=True,
                           check=True).stdout for _ in range(2)}
    solution, orbit = solve_and_certify(fixture_107(3.0, diffuse_fraction=0.3))
    assert runs == {repr((solution.x_star, solution.bracket, solution.map_evaluations, orbit.initial,
                          orbit.residuals)) + "\n"}


# ---------------------------------------------------------------------------------- unit conversions


class _Resolved:
    """Monkeypatched 169 resolver output carrying the 107 fixture values in their pinned units."""

    @staticmethod
    def model() -> dict[str, Any]:
        return {
            "card": {"name": "Synthetic 107 Monod", "factors": {
                "light": "light.monod", "optics": "optics.slab_response_average", "temperature": "temperature.ctmi",
                "nutrients": ["nutrient.monod"], "combination": "combine.multiplicative", "loss": "loss.first_order",
                "stoichiometry": "stoich.photoautotrophic"}, "form_versions": {"light.monod": "1.0.0"},
                "n_source": "HNO3"},
            "set": {"id": "set-1", "name": "Synthetic", "revision": "r-" + "1" * 16, "digest": "2" * 64},
            "parameters": {"K_I": 150.0, "K_j_0": 0.001, "k_X": 150.0, "k_d": K_D_107,
                           "T_min": 278.15, "T_opt": 298.15, "T_max": 308.15},
            "mu_max_h": 0.0625, "nitrogen_quota": 0.07, "oxygen_yield": 1.4, "candidate_symbols": [],
        }


def _unit(**si: float) -> dict[str, Any]:
    values = {"tube_inner_diameter": 0.05, "tube_length": 100.0, "tube_count": 4.0, "liquid_velocity": 0.5,
              "pump_efficiency": 60.0, "baffle_friction_multiplier": 1.0, "oxygen_kla": 5.0 / 3600.0,
              "oxygen_saturation": 0.0075, "peak_par": 1500.0, "photoperiod": 14.0 * 3600.0,
              "diffuse_fraction": 0.0, "temperature_mean": 295.15, "temperature_amplitude": 3.0}
    values.update(si)
    return {"tag": "PBR-1", "type": "PhotobioreactorT1", "params": {key: {"si": value} for key, value in values.items()},
            "model": {"card_id": "card-1", "card_revision": "r-" + "a" * 16, "card_digest": "b" * 64}}


def test_evaluator_unit_conversions_and_balances_close_against_independent_steady_balance(monkeypatch) -> None:
    monkeypatch.setattr(pbr_unit, "_resolve", lambda workspace_id, pin: _Resolved.model())
    density, hrt_d = 1000.0, 3.0
    volume = 4 * math.pi * 0.05**2 / 4 * 100.0
    flow = density * volume / (hrt_d * 86400.0)  # kg/s carrier
    inlet = {"mass_flow_kg_s": flow, "temperature_K": 295.15, "pressure_Pa": 101325.0, "vapor_fraction": 0.0,
             "culture": {"biomass": 0.0, "nitrogen": 0.05 / density, "oxygen": 0.0075 / density}}
    context = JarvisUnitContext(inlet_density_kg_m3=density, deadline=time.monotonic() + 30.0, cache={},
                                workspace_id="ws")
    evaluation = pbr_unit.evaluate_pbr(_unit(), inlet, context)
    reported = evaluation.result["reported"]
    # D_s = Q/V, D_h = 3600·D_s, HRT = 1/D_s; kLa and photoperiod enter in hours.
    assert reported["dilution_h"]["value"] == pytest.approx(1.0 / (24.0 * hrt_d), rel=1e-12)
    assert reported["hrt_d"]["value"] == pytest.approx(hrt_d, rel=1e-12)
    assert reported["volume_m3"]["value"] == pytest.approx(volume, rel=1e-15)
    q_m3_s = flow / density
    outlet = evaluation.outlets["outlet"]["culture"]
    x_out, n_out, o_out = (outlet[name] * density for name in ("biomass", "nitrogen", "oxygen"))
    x_in, n_in, o_in = 0.0, 0.05, 0.0075
    generation, allowance = evaluation.culture_generation, evaluation.culture_generation_allowance
    # Independent steady balance in kg/s: in − out + generation = V·drift/86 400 ≤ allowance.
    for name, c_in, c_out in (("biomass", x_in, x_out), ("nitrogen", n_in, n_out), ("oxygen", o_in, o_out)):
        residual = q_m3_s * (c_in - c_out) + generation[name]
        assert abs(residual) <= allowance[name] * (1 + 1e-6) + 1e-15
        assert allowance[name] <= volume * (pbr_unit.PERIODIC_REL * 1.0 + pbr_unit.ALLOWANCE_FLOOR) / 86400.0
    # Nitrogen: total N conserved, so generation_N = −q·generation_X exactly.
    assert generation["nitrogen"] == pytest.approx(-0.07 * generation["biomass"], rel=1e-14)
    # Hourly productivity ↔ kg/day: P = 24·D_h·(X̄ − X_in); net production 24·V·r̄ = 86 400·Q·(X̄ − X_in).
    assert reported["volumetric_productivity"]["value"] == pytest.approx(
        86400.0 * q_m3_s / volume * (x_out - x_in), rel=1e-12)
    assert reported["net_biomass_production"]["value"] == pytest.approx(86400.0 * generation["biomass"], rel=1e-12)
    assert reported["net_biomass_production"]["value"] == pytest.approx(86400.0 * q_m3_s * (x_out - x_in), rel=1e-8)
    # O₂ balance: Y·r̄ = gas transfer + D·(Ō₂ − O₂_in) per volume; transfer sign = degassing.
    transfer_kg_d = reported["oxygen_gas_transfer"]["value"]
    assert 1.4 * reported["net_biomass_production"]["value"] == pytest.approx(
        transfer_kg_d + 86400.0 * q_m3_s * (o_out - o_in), rel=1e-8)
    assert transfer_kg_d > 0.0  # supersaturated daytime culture degasses
    assert evaluation.result["branch"] == "productive"


def test_evaluator_with_inlet_biomass_separates_net_productivity_from_outlet_throughput(monkeypatch) -> None:
    monkeypatch.setattr(pbr_unit, "_resolve", lambda workspace_id, pin: _Resolved.model())
    density = 1000.0
    volume = 4 * math.pi * 0.05**2 / 4 * 100.0
    flow = density * volume / (3.0 * 86400.0)
    inlet = {"mass_flow_kg_s": flow, "temperature_K": 295.15, "vapor_fraction": 0.0,
             "culture": {"biomass": 0.1 / density, "nitrogen": 0.05 / density, "oxygen": 0.0075 / density}}
    context = JarvisUnitContext(inlet_density_kg_m3=density, deadline=time.monotonic() + 30.0, cache={},
                                workspace_id="ws")
    reported = pbr_unit.evaluate_pbr(_unit(), inlet, context).result["reported"]
    x_mean = reported["biomass_mean"]["value"]
    assert x_mean > 0.1  # daily mean X̄ (the midnight X* is checked against the reference above)
    q_m3_d = 86400.0 * flow / density
    assert reported["net_biomass_production"]["value"] == pytest.approx(q_m3_d * (x_mean - 0.1), rel=1e-8)
    assert reported["outlet_biomass_throughput"]["value"] == pytest.approx(q_m3_d * x_mean, rel=1e-12)


# ---------------------------------------------------------------------------------------------- optics


def test_q2_golden_table_cylinder_versus_107_slab_matches_a170_survey() -> None:
    """Spec 170 §5 golden table (supersedes record §10.1 Q2): probe constants Ks 100, Ki 2000, κ = 200·X, D 0.06 m."""
    nodes, weights = leggauss(8)
    depth = (nodes + 1.0) / 2.0

    def response(irradiance: np.ndarray) -> np.ndarray:
        return irradiance / (100.0 + irradiance + irradiance**2 / 2000.0)

    table: dict[str, list[float]] = {}
    for diffuse_fraction, name in ((0.0, "beam"), (1.0, "diffuse_3d"), (0.5, "mixed")):
        rows = []
        for biomass in (0.2, 0.5, 1.0, 2.0, 4.0):
            for surface in (200.0, 800.0, 1800.0):
                slab = float(np.dot(weights / 2.0, response(surface * np.exp(-200.0 * biomass * 0.06 * depth))))
                cylinder_value = cylinder.response_average_tau(response, surface, 200.0 * biomass * 0.06,
                                                               diffuse_fraction)
                rows.append(100.0 * (cylinder_value / slab - 1.0))
        table[name] = rows
    # a170 §4.4: beam −0.3 % to +39.2 % (10/15 above 20 %), 3-D +5.0 % to +118.5 % (12/15), 50/50 +3.4 % to +115 %.
    assert (round(min(table["beam"]), 1), round(max(table["beam"]), 1)) == (-0.3, 39.2)
    assert sum(value > 20.0 for value in table["beam"]) == 10
    assert (round(min(table["diffuse_3d"]), 1), round(max(table["diffuse_3d"]), 1)) == (5.0, 118.5)
    assert sum(value > 20.0 for value in table["diffuse_3d"]) == 12
    assert (round(min(table["mixed"]), 1), round(max(table["mixed"]), 0)) == (3.4, 115.0)


@pytest.mark.parametrize("tau", [100.0, 300.0, 1000.0])
def test_thick_limit_trends_have_order_one_over_tau_corrections(tau: float) -> None:
    beam = cylinder.response_average_tau(lambda irradiance: irradiance, 1.0, tau, 0.0)
    diffuse = cylinder.response_average_tau(lambda irradiance: irradiance, 1.0, tau, 1.0)
    assert abs(beam * math.pi * tau / 4.0 - 1.0) <= 2.0 / tau
    assert abs(diffuse * tau - 1.0) <= 2.0 / tau


def test_optics_is_monotone_and_smooth_in_surface_light_and_optical_depth() -> None:
    def monod(irradiance: np.ndarray) -> np.ndarray:
        return irradiance / (150.0 + irradiance)

    taus = np.geomspace(1e-4, 1000.0, 400)
    for fraction in (0.0, 0.4, 1.0):
        values = np.array([cylinder.response_average_tau(monod, 800.0, tau, fraction) for tau in taus])
        assert np.all(np.diff(values) < 0.0)
        assert values[0] == pytest.approx(800.0 / 950.0, rel=1e-3)
        # No node switching: relative change under a 1e-7 relative perturbation of τ is O(1e-7).
        for tau in (1e-3, 0.7, 31.0, 999.0):
            base = cylinder.response_average_tau(monod, 800.0, tau, fraction)
            bumped = cylinder.response_average_tau(monod, 800.0, tau * (1 + 1e-7), fraction)
            assert abs(bumped - base) <= 2e-7 * base
    surfaces = np.linspace(1.0, 2500.0, 200)
    values = np.array([cylinder.response_average_tau(monod, surface, 40.0, 0.3) for surface in surfaces])
    assert np.all(np.diff(values) > 0.0)
    assert np.all(np.diff(values, 2) < 0.0)  # concave in I₀ like the Monod response itself


def test_thin_limit_is_continuous_at_tau_zero() -> None:
    def monod(irradiance: np.ndarray) -> np.ndarray:
        return irradiance / (150.0 + irradiance)

    at_zero = cylinder.response_average_tau(monod, 800.0, 0.0, 0.5)
    assert cylinder.response_average_tau(monod, 800.0, 1e-12, 0.5) == pytest.approx(at_zero, rel=1e-11)


def test_107_model_gaps_point_to_the_cylinder_unit() -> None:
    assert "tube_light_geometry: resolved in jarvis.pbr_unit_t1" in pbr_evaluator.MODEL_GAPS
