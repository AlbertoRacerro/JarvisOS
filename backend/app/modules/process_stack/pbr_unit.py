"""Jarvis-native Tier-1 photobioreactor ``PhotobioreactorT1`` (spec 170, evaluator ``jarvis.pbr_unit_t1``).

A continuously diluted, well-mixed tube loop is solved to its 24-hour periodic steady state.

State y = (X, N, O₂) in kg m⁻³, time t in hours from midnight, dilution D_h = 3600·Q/V in h⁻¹:

- μ_g = μ_max·⟨f_I⟩(I₀(t), X)·f_T(T(t))·f_N(N);  r_X = (μ_g − k_d(t))·X
- dX/dt = r_X + D_h·(X_in − X)
- dN/dt = −q·r_X + D_h·(N_in − N)
- dO₂/dt = Y_O2·r_X − kLa_h·(O₂ − O₂sat) + D_h·(O₂_in − O₂)

⟨f_I⟩ is the Monod response averaged over the tube cross-section with true cylindrical Beer–Lambert
light (``bio_models.cylinder``). The reduced periodic problem uses the exact structure of the system:

- Z = N + qX obeys dZ/dt = D_h·(Z_in − Z), so seeding N₀ = Z_in − qX₀ keeps N = Z_in − qX on every
  trajectory, and X is a scalar root.
- O₂ is linear with constant decay kLa_h + D_h and no feedback, so O₂(24) = α·O₂(0) + β with
  α = exp(−24·(kLa_h + D_h)); O₂* = β/(1 − α).
- X is a scalar periodic root F(X₀) = Φ₂₄(X₀) − X₀ found by a deterministic Brent iteration inside a
  physically derived bracket. Each map evaluation integrates the full system from (X₀, N slaved, O₂*),
  so the final evaluation is the certification integration, whose N, O₂, Z-identity and positivity
  checks remain independent of the root.

The 107 day profile, light factor and nitrogen factor come from ``photobioreactor.pbr_core`` (shared core).
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final

import numpy as np

from app.modules.bio_models import cylinder, forms
from app.modules.photobioreactor.pbr_core import (
    cardinal_temperature,
    light_response,
    nitrogen_factor,
    synthetic_day_inputs,
)
from app.modules.process_stack.dynamics import OdeSolution, integrate_ode

if TYPE_CHECKING:  # pragma: no cover - imported lazily to keep the 168 runtime import one-directional
    from app.modules.process_stack.mixed_runtime import JarvisUnitContext, JarvisUnitEvaluation

EVALUATOR_ID: Final = "jarvis.pbr_unit_t1"
MODEL_VERSION: Final = "pbr_unit.v1"
FIDELITY: Final = "T1 · unqualified · periodic steady state, cylinder light, no energy balance"
LIGHT_CONVENTION: Final = "beam ⟂ axis + isotropic diffuse, scalar-irradiance convention"

# Numerical constants fixed under MODEL_VERSION.
RTOL: Final = 1e-11
ATOL: Final = 1e-14  # kg m⁻³
SAMPLES_PER_HOUR: Final = 20
X_RESOLUTION: Final = 1e-6  # kg m⁻³
BRENT_MAX_EVALUATIONS: Final = 80
MAX_DOUBLINGS: Final = 40
DOUBLING_START: Final = 1e-3  # kg m⁻³
BRACKET_WIDTH_REL: Final = 1e-10
RESIDUAL_REL: Final = 1e-9
PERIODIC_REL: Final = 1e-9
NEGATIVE_TOLERANCE: Final = 1e-12  # kg m⁻³
ALLOWANCE_FLOOR: Final = 1e-12  # kg m⁻³
LAMBDA_EPSREL: Final = 1e-10
LAMBDA_EPSABS: Final = 1e-12  # h⁻¹·h per quadrature segment
TIE_REL: Final = 1e-8
WALL_CAP_S: Final = 5.0
HRT_RANGE_D: Final = (0.1, 100.0)
TEMPERATURE_DIFFERS_K: Final = 5.0
VAPOR_LIMIT: Final = 1e-6
_EPS: Final = 2.220446049250313e-16

PARAMETERS: Final = (
    "tube_inner_diameter", "tube_length", "tube_count", "liquid_velocity", "pump_efficiency",
    "baffle_friction_multiplier", "oxygen_kla", "oxygen_saturation", "peak_par", "photoperiod",
    "diffuse_fraction", "temperature_mean", "temperature_amplitude",
)

CAVEATS: Final = (
    "No energy balance; outlet temperature equals inlet.",
    f"Light: {LIGHT_CONVENTION}.",
    "Well-mixed (0-D) loop; no axial O₂ buildup along tubes, so maximum DO is a loop-mean value.",
    "Every tube receives full declared I₀; no array shading or wall losses (174).",
    "Q is carrier volume; biomass volume is neglected.",
    "Carbon and phosphorus assumed externally supplied and non-limiting; elemental C/P balances are not modeled.",
    "pH not modeled; photosynthetic DIC uptake would raise it.",
    "Biomass loss returns its N quota to dissolved N and consumes O₂ at Y_O2.",
    "O₂ saturation is the declared constant, independent of temperature and salinity.",
    "The model card's phosphorus stoichiometry is disclosed but not applied as Tier-1 phosphorus consumption.",
    "Temperature follows the declared diel profile at all hours (107 applied it only in daylight).",
    "Pressure drop is a screening estimate: straight smooth tubes × baffle multiplier; not an outlet pressure.",
)


class PbrFailure(ValueError):
    """A typed PBR unit failure; 168 records ``code`` and ``detail`` with the unit tag."""

    def __init__(self, code: str, message: str, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message[:600])
        self.code = code
        self.detail = _finite_detail(detail or {})


def _finite_detail(value: Any) -> Any:
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, dict):
        return {key: _finite_detail(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_detail(item) for item in value]
    return value


# ---------------------------------------------------------------------------------------------- model


@dataclass(frozen=True)
class Growth:
    """Card-resolved biology plus the unit's light, temperature and transport inputs (hourly units)."""

    mu_max_h: float
    light_saturation: float  # K_I, µmol m⁻² s⁻¹
    extinction: float  # k_X, m² kg⁻¹
    nitrogen_half_saturation: float  # K_j_0, kg m⁻³
    nitrogen_quota: float  # q, kg N per kg total dry biomass
    oxygen_yield: float  # Y_O2, kg O₂ per kg
    temperature_form: str
    temperature_parameters: tuple[float, ...]
    loss_form: str
    loss_parameters: tuple[float, ...]
    diameter: float  # m
    peak_par: float
    photoperiod_h: float
    diffuse_fraction: float
    temperature_mean: float
    temperature_amplitude: float
    kla_h: float
    oxygen_saturation: float
    dilution_h: float
    inlet: tuple[float, float, float]  # X_in, N_in, O₂_in in kg m⁻³

    @property
    def z_in(self) -> float:
        return self.inlet[1] + self.nitrogen_quota * self.inlet[0]

    def environment(self, hour: float) -> tuple[float, float]:
        return synthetic_day_inputs(hour, self.photoperiod_h, self.peak_par, self.temperature_mean,
                                    self.temperature_amplitude)

    def thermal(self, temperature: float) -> float:
        if self.temperature_form == "temperature.isothermal":
            return 1.0
        if self.temperature_form == "temperature.ctmi":
            return cardinal_temperature(temperature, *self.temperature_parameters)
        reference, activation = self.temperature_parameters
        return forms.temperature_arrhenius_ref(temperature, reference, activation)

    def loss(self, par: float) -> float:
        if self.loss_form == "loss.first_order":
            return self.loss_parameters[0]
        dark_threshold, light_loss, dark_loss = self.loss_parameters
        return light_loss if par > dark_threshold else dark_loss

    def _monod(self, irradiance: np.ndarray) -> np.ndarray:
        return light_response(irradiance, self.light_saturation, 0.0)

    def light(self, par: float, biomass: float, diffuse_fraction: float | None = None) -> float:
        if par <= 0.0:
            return 0.0
        tau = cylinder.optical_depth(self.extinction, biomass, self.diameter)
        if tau <= 0.0:
            return light_response(par, self.light_saturation, 0.0)
        if tau > cylinder.TAU_MAX:
            raise _optics_failure(self, biomass)
        return cylinder.response_average_tau(
            self._monod, par, tau, self.diffuse_fraction if diffuse_fraction is None else diffuse_fraction
        )

    def rates(self, hour: float, biomass: float, nitrogen: float) -> tuple[float, float]:
        par, temperature = self.environment(hour)
        return self.rates_at(par, temperature, biomass, nitrogen)

    def rates_at(self, par: float, temperature: float, biomass: float, nitrogen: float,
                 diffuse_fraction: float | None = None) -> tuple[float, float]:
        """Evaluate the reviewed hourly biological rates at explicit environmental inputs."""
        thermal = self.thermal(temperature)
        growth = 0.0
        if par > 0.0 and thermal > 0.0:
            growth = (self.mu_max_h * self.light(par, biomass, diffuse_fraction) * thermal
                      * nitrogen_factor(nitrogen, self.nitrogen_half_saturation))
        return growth, self.loss(par)

    def thin_net_growth(self, hour: float) -> float:
        par, temperature = self.environment(hour)
        thermal = self.thermal(temperature)
        growth = 0.0
        if par > 0.0 and thermal > 0.0:
            growth = (self.mu_max_h * light_response(par, self.light_saturation, 0.0) * thermal
                      * nitrogen_factor(self.inlet[1], self.nitrogen_half_saturation))
        return growth - self.loss(par)

    def breakpoints(self) -> list[float]:
        """Sunrise, sunset, I₀ = I_dark crossings and CTMI T_min/T_max crossings within [0, 24] h."""
        points = {0.0, 24.0}
        sunrise = 12.0 - self.photoperiod_h / 2.0
        sunset = 12.0 + self.photoperiod_h / 2.0
        points.update({sunrise, sunset})
        if self.loss_form == "loss.light_dark" and self.peak_par > 0.0:
            threshold = self.loss_parameters[0]
            if 0.0 < threshold < self.peak_par:
                offset = self.photoperiod_h * math.asin(threshold / self.peak_par) / math.pi
                points.update({sunrise + offset, sunset - offset})
        if self.temperature_form == "temperature.ctmi" and self.temperature_amplitude > 0.0:
            for cardinal in (self.temperature_parameters[0], self.temperature_parameters[2]):
                ratio = (cardinal - self.temperature_mean) / self.temperature_amplitude
                if -1.0 < ratio < 1.0:
                    shift = 24.0 * math.asin(ratio) / (2.0 * math.pi)
                    points.update({(9.0 + shift) % 24.0, (21.0 - shift) % 24.0})
        return sorted(point for point in points if 0.0 <= point <= 24.0)


# ------------------------------------------------------------------------------------------ numerics


class _Clock:
    def __init__(self, deadline: float) -> None:
        self.deadline = deadline

    def check(self) -> None:
        if time.monotonic() >= self.deadline:
            raise TimeoutError(
                "The photobioreactor periodic solve exceeded its wall-time cap (5 s or the remaining Process "
                "Run budget). Retry the Run; if it repeats, shorten the recycle loop or simplify the case.")


def _schedule(growth: Growth) -> list[list[float]]:
    points = growth.breakpoints()
    grids = []
    for start, stop in zip(points, points[1:], strict=False):
        if stop - start <= 1e-12:
            continue
        count = max(2, math.ceil((stop - start) * SAMPLES_PER_HOUR) + 1)
        grids.append([start + (stop - start) * index / (count - 1) for index in range(count - 1)] + [stop])
    return grids


def _integrate(rhs: Callable[[float, tuple[float, ...]], list[float]], state: list[float],
               grids: list[list[float]], clock: _Clock) -> list[tuple[float, ...]]:
    """Integrate one day segment by segment (CVODE restarts at each light/temperature boundary)."""
    samples: list[tuple[float, ...]] = [tuple(state)]
    current: tuple[float, ...] = tuple(state)
    for grid in grids:
        clock.check()
        solution: OdeSolution = integrate_ode(rhs, current, grid, rtol=RTOL, atol=ATOL)
        if not solution.success:
            raise PbrFailure(
                "PBR_PERIODIC_STEADY_FAILED",
                "The 24-hour culture integration failed inside the periodic solver (CVODE: "
                f"{solution.message[:160]}). Check the light, temperature and model-card values for extreme "
                "rates.", {"segment_h": [grid[0], grid[-1]]})
        samples.extend(solution.states[1:])
        current = solution.states[-1]
    return samples


def _full_rhs(growth: Growth) -> Callable[[float, tuple[float, ...]], list[float]]:
    q, y_o2, kla, o_sat = growth.nitrogen_quota, growth.oxygen_yield, growth.kla_h, growth.oxygen_saturation
    x_in, n_in, o_in = growth.inlet
    dilution = growth.dilution_h

    def rhs(hour: float, state: tuple[float, ...]) -> list[float]:
        biomass, nitrogen, oxygen = state[0], state[1], state[2]
        growth_rate, loss = growth.rates(hour, biomass, nitrogen)
        r_x = (growth_rate - loss) * biomass
        transfer = kla * (oxygen - o_sat)
        return [r_x + dilution * (x_in - biomass),
                -q * r_x + dilution * (n_in - nitrogen),
                y_o2 * r_x - transfer + dilution * (o_in - oxygen),
                biomass, nitrogen, oxygen, r_x, transfer]

    return rhs


def thin_growth_rate(growth: Growth) -> float:
    """Λ = (1/24)∫₀²⁴ [μ_g(thin limit, N = N_in) − k_d] dt in h⁻¹ by deterministic adaptive quadrature."""
    from scipy.integrate import quad

    points = growth.breakpoints()
    total = 0.0
    for start, stop in zip(points, points[1:], strict=False):
        if stop - start <= 1e-12:
            continue
        value, _ = quad(growth.thin_net_growth, start, stop, epsabs=LAMBDA_EPSABS, epsrel=LAMBDA_EPSREL, limit=200)
        total += value
    return total / 24.0


@dataclass
class _Root:
    x: float
    bracket: tuple[float, float]
    residual: float
    evaluations: int


def _seeded_orbit(growth: Growth, x0: float, rhs: Callable[[float, tuple[float, ...]], list[float]],
                  grids: list[list[float]], clock: _Clock) -> tuple[float, list[tuple[float, ...]]]:
    """Integrate the full system from (X₀, N slaved, O₂*(X₀)); returns O₂* and the day's samples.

    O₂ is affine in its seed with α = exp(−24·(kLa_h + D_h)), so one integration from O₂ = 0 gives
    β and O₂* = β/(1 − α) exactly; the second integration starts on the O₂ periodic orbit. The map and
    the certification therefore integrate the identical initial state, so the certified X periodicity
    residual is exactly the root residual F(X*) and never a second, independent CVODE error budget.
    (A one-state X integration accumulated ~1e-9 relative global error at rtol 1e-11 on the 107
    fixture, which is the certification tolerance itself.)
    """
    n0 = growth.z_in - growth.nitrogen_quota * x0
    beta = _integrate(rhs, [x0, n0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0], grids, clock)[-1][2]
    oxygen_star = beta / -math.expm1(-24.0 * (growth.kla_h + growth.dilution_h))
    return oxygen_star, _integrate(rhs, [x0, n0, oxygen_star, 0.0, 0.0, 0.0, 0.0, 0.0], grids, clock)


class _Map:
    """F(X₀) = Φ₂₄,X(X₀) − X₀ with N slaved; records every evaluation for the sign-change check."""

    def __init__(self, growth: Growth, clock: _Clock) -> None:
        self.growth, self.clock = growth, clock
        self.rhs = _full_rhs(growth)
        self.grids = _schedule(growth)
        self.points: list[tuple[float, float]] = []

    def __call__(self, x0: float) -> float:
        self.clock.check()
        final = _seeded_orbit(self.growth, x0, self.rhs, self.grids, self.clock)[1][-1][0]
        value = final - x0
        self.points.append((x0, value))
        return value


def _residual_tolerance(x: float) -> float:
    return RESIDUAL_REL * max(abs(x), X_RESOLUTION)


def _brent(func: _Map, low: float, f_low: float, high: float, f_high: float) -> _Root:
    """Brent's method (zeroin) with the spec 170 stopping rule; F(low) > 0 ≥ F(high)."""
    a, fa, b, fb = low, f_low, high, f_high
    c, fc = a, fa
    d = e = b - a
    evaluations = 0
    while True:
        if fb * fc > 0.0:
            c, fc = a, fa
            d = e = b - a
        if abs(fc) < abs(fb):
            a, b, c = b, c, b
            fa, fb, fc = fb, fc, fb
        width = abs(c - b)
        if width <= BRACKET_WIDTH_REL * max(abs(b), X_RESOLUTION) or abs(fb) <= _residual_tolerance(b):
            return _Root(b, (min(b, c), max(b, c)), fb, evaluations)
        if evaluations >= BRENT_MAX_EVALUATIONS:
            raise PbrFailure(
                "PBR_PERIODIC_STEADY_FAILED",
                f"The periodic root did not converge within {BRENT_MAX_EVALUATIONS} 24-hour map evaluations "
                f"(bracket {min(b, c):.6g}–{max(b, c):.6g} kg/m³, residual {fb:.3g} kg/m³). The case is close to "
                "a washout or nutrient limit; change HRT, inlet nitrogen or light.",
                {"bracket_kg_m3": [min(b, c), max(b, c)], "residual_kg_m3": fb})
        midpoint = 0.5 * (c - b)
        tolerance = 2.0 * _EPS * abs(b) + 0.5 * BRACKET_WIDTH_REL * max(abs(b), X_RESOLUTION)
        if abs(e) >= tolerance and abs(fa) > abs(fb):
            s = fb / fa
            if a == c:
                p, q = 2.0 * midpoint * s, 1.0 - s
            else:
                q, r = fa / fc, fb / fc
                p = s * (2.0 * midpoint * q * (q - r) - (b - a) * (r - 1.0))
                q = (q - 1.0) * (r - 1.0) * (s - 1.0)
            if p > 0.0:
                q = -q
            else:
                p = -p
            if 2.0 * p < min(3.0 * midpoint * q - abs(tolerance * q), abs(e * q)):
                e, d = d, p / q
            else:
                d = e = midpoint
        else:
            d = e = midpoint
        a, fa = b, fb
        b = b + d if abs(d) > tolerance else b + math.copysign(tolerance, midpoint)
        fb = func(b)
        evaluations += 1


def _sign_pattern_ok(points: list[tuple[float, float]]) -> bool:
    """Evaluated points must show exactly one sign change, from positive to negative F."""
    signs = [1 if value > _residual_tolerance(x) else -1 if value < -_residual_tolerance(x) else 0
             for x, value in sorted(points)]
    nonzero = [sign for sign in signs if sign]
    changes = sum(1 for left, right in zip(nonzero, nonzero[1:], strict=False) if left != right)
    return changes <= 1 and (not nonzero or nonzero[0] == 1 or all(sign == -1 for sign in nonzero)) and (
        changes == 1 or 0 in signs or not nonzero)


def _tau_cap(growth: Growth) -> float:
    return math.inf if growth.extinction <= 0.0 else cylinder.TAU_MAX / (growth.extinction * growth.diameter)


def _optics_failure(growth: Growth, biomass: float) -> PbrFailure:
    tau = cylinder.optical_depth(growth.extinction, biomass, growth.diameter)
    return PbrFailure(
        "PBR_OPTICS_OUT_OF_RANGE",
        f"The productive state needs optical depth τ = k_X·X·D above the Tier-1 limit {cylinder.TAU_MAX:g} "
        f"(τ ≈ {tau:.4g} at {biomass:.4g} kg/m³). The culture would be optically far too dense for the T1 light "
        "model; reduce tube diameter, raise dilution or check k_X.",
        {"optical_depth": tau, "limit": cylinder.TAU_MAX, "biomass_kg_m3": biomass})


@dataclass
class _Solution:
    branch: str
    lambda_h: float
    x_star: float
    bracket: tuple[float, float]
    map_evaluations: int
    root_residual: float


def _no_finite(growth: Growth, lambda_h: float, reason: str, detail: dict[str, Any] | None = None) -> PbrFailure:
    return PbrFailure(
        "PBR_NO_FINITE_PRODUCTIVE_STATE",
        f"No finite productive periodic state exists for these inputs: {reason} Λ = {lambda_h:.6g} 1/h, "
        f"D = {growth.dilution_h:.6g} 1/h. Use a model card with nutrient (q > 0) or self-shading (k_X > 0) "
        "feedback, or raise dilution.",
        {"lambda_h": lambda_h, "dilution_h": growth.dilution_h, **(detail or {})})


def _unresolved(growth: Growth, lambda_h: float, reason: str, detail: dict[str, Any] | None = None) -> PbrFailure:
    return PbrFailure(
        "PBR_BRANCH_UNRESOLVED",
        f"The growth branch cannot be resolved: {reason} Λ = {lambda_h:.6g} 1/h, D = {growth.dilution_h:.6g} 1/h, "
        f"resolution {X_RESOLUTION:g} kg/m³. Move HRT away from the washout boundary (critical HRT ≈ "
        f"{(1.0 / lambda_h / 24.0) if lambda_h > 0 else math.inf:.4g} d) or change light/nitrogen.",
        {"lambda_h": lambda_h, "dilution_h": growth.dilution_h, "resolution_kg_m3": X_RESOLUTION, **(detail or {})})


def _linear_fixed_point(growth: Growth, clock: _Clock) -> tuple[float, int]:
    """q = k_X = 0: X(24) = α·X(0) + β, so F is affine; two map evaluations give X* = β/(1 − α).

    β = F(0). The second point is the estimate from the quadrature Λ, α = exp(24·(Λ − D_h)), so the
    secant is taken next to the answer and the certification integration starts where the affine
    coefficients were measured.
    """
    func = _Map(growth, clock)
    beta = func(0.0)
    estimate = beta / -math.expm1(24.0 * (thin_growth_rate(growth) - growth.dilution_h))
    slope = (func(estimate) - beta) / estimate  # α − 1 < 0
    return -beta / slope, 2


def solve_periodic(growth: Growth, clock: _Clock) -> _Solution:
    """Branch rule + bracketed root of the 24-hour map (spec 170 capability 4)."""
    x_in, n_in, _ = growth.inlet
    q, dilution = growth.nitrogen_quota, growth.dilution_h
    lambda_h = thin_growth_rate(growth)
    no_feedback = q == 0.0 and growth.extinction == 0.0
    tie = abs(lambda_h - dilution) <= TIE_REL * dilution
    if x_in == 0.0 and tie:
        raise _unresolved(growth, lambda_h, "thin-culture growth and dilution are numerically tied.")
    if no_feedback:
        if x_in > 0.0:
            if lambda_h >= dilution:
                raise _no_finite(growth, lambda_h, "growth does not depend on biomass (q = 0 and k_X = 0) and "
                                 "Λ ≥ D, so biomass grows without bound.")
            x_star, evaluations = _linear_fixed_point(growth, clock)
            return _Solution("productive", lambda_h, x_star, (x_star, x_star), evaluations, 0.0)
        if lambda_h > dilution:
            raise _no_finite(growth, lambda_h, "growth does not depend on biomass (q = 0 and k_X = 0) and Λ > D, "
                             "so the only periodic state is an unstable washout.")
        return _Solution("washout", lambda_h, 0.0, (0.0, 0.0), 0, 0.0)
    if x_in == 0.0 and lambda_h < dilution:
        return _Solution("washout", lambda_h, 0.0, (0.0, 0.0), 0, 0.0)

    func = _Map(growth, clock)
    cap = _tau_cap(growth)
    low = 0.0 if x_in > 0.0 else X_RESOLUTION
    if q > 0.0:
        high = x_in + n_in / q
        if x_in == 0.0 and high <= X_RESOLUTION:
            raise _unresolved(growth, lambda_h, f"the nitrogen-limited upper bound {high:.3g} kg/m³ is at or below "
                              "the resolution.", {"upper_bound_kg_m3": high})
    else:
        high = max(2.0 * x_in, DOUBLING_START)
    f_low = func(low)
    if f_low <= 0.0 or (x_in == 0.0 and f_low <= _residual_tolerance(low)):
        raise _unresolved(growth, lambda_h, f"F({low:.3g} kg/m³) = {f_low:.3g} kg/m³ is not positive, so no "
                          "positive branch is resolved above the solver resolution.",
                          {"bracket_kg_m3": [low, high], "residual_kg_m3": f_low})
    if q > 0.0:
        if high > cap:
            if func(cap) > _residual_tolerance(cap):
                raise _optics_failure(growth, cap)
            high = cap
        f_high = func(high) if func.points[-1][0] != high else func.points[-1][1]
        if f_high > _residual_tolerance(high):
            raise _unresolved(growth, lambda_h, "the nitrogen-limited upper bracket does not change sign.",
                              {"bracket_kg_m3": [low, high], "residual_kg_m3": f_high})
    else:
        doublings = 0
        while True:
            if high > cap:
                high = cap
            f_high = func(high)
            if f_high <= _residual_tolerance(high):
                break
            if high >= cap:
                raise _optics_failure(growth, high)
            low, f_low = high, f_high
            doublings += 1
            if doublings > MAX_DOUBLINGS:
                raise _no_finite(growth, lambda_h, f"no sign change after {MAX_DOUBLINGS} bracket doublings.",
                                 {"bracket_kg_m3": [low, high], "residual_kg_m3": f_high})
            high *= 2.0
    if abs(f_high) <= _residual_tolerance(high):
        root = _Root(high, (low, high), f_high, 0)
    else:
        root = _brent(func, low, f_low, high, f_high)
    if not _sign_pattern_ok(func.points):
        raise _unresolved(growth, lambda_h, "the evaluated 24-hour map shows more than one sign change, so a "
                          "unique physical root cannot be certified.",
                          {"evaluations": [[x, value] for x, value in sorted(func.points)]})
    if x_in == 0.0 and root.x <= X_RESOLUTION * (1.0 + 1e-9):
        raise _unresolved(growth, lambda_h, "the positive root lies at the solver resolution.",
                          {"bracket_kg_m3": list(root.bracket), "residual_kg_m3": root.residual})
    return _Solution("productive", lambda_h, root.x, root.bracket, len(func.points), root.residual)


@dataclass
class _Orbit:
    initial: tuple[float, float, float]
    samples: list[tuple[float, ...]]
    residuals: tuple[float, float, float]
    tolerances: tuple[float, float, float]
    z_identity_max: float


def certify(growth: Growth, x_star: float, clock: _Clock) -> _Orbit:
    """O₂* from the affine O₂ map, then one full three-state certification integration."""
    n_star = growth.z_in - growth.nitrogen_quota * x_star
    oxygen_star, samples = _seeded_orbit(growth, x_star, _full_rhs(growth), _schedule(growth), clock)
    initial = (x_star, n_star, oxygen_star)
    if not all(math.isfinite(value) for row in samples for value in row):
        raise PbrFailure("PBR_NONPHYSICAL_STATE", "The certified culture trajectory is not finite; check the model "
                         "card values and the unit inputs for extreme magnitudes.")
    minimum = [min(row[index] for row in samples) for index in range(3)]
    if min(minimum) < -NEGATIVE_TOLERANCE:
        names = [name for name, value in zip(("biomass", "nitrogen", "oxygen"), minimum, strict=True)
                 if value < -NEGATIVE_TOLERANCE]
        hint = ("Dissolved oxygen falls below zero (night-time respiration exceeds oxygen supply); raise "
                "oxygen_kla or the inlet dissolved oxygen." if "oxygen" in names else
                "Raise inlet nitrogen or reduce biomass loss.")
        raise PbrFailure(
            "PBR_NONPHYSICAL_STATE",
            f"The periodic culture state becomes negative ({', '.join(names)}), which T1 does not model "
            f"(minimum O₂ {minimum[2]:.3g} kg/m³). {hint}",
            {"minimum_kg_m3": dict(zip(("biomass", "nitrogen", "oxygen"), minimum, strict=True))})
    final = samples[-1][:3]
    residuals = (final[0] - initial[0], final[1] - initial[1], final[2] - initial[2])
    z_in = growth.z_in
    tolerances = (PERIODIC_REL * max(x_star, X_RESOLUTION), PERIODIC_REL * max(z_in, X_RESOLUTION),
                  PERIODIC_REL * max(abs(oxygen_star), growth.oxygen_saturation))
    z_max = max(abs(row[1] + growth.nitrogen_quota * row[0] - z_in) for row in samples)
    failed = [name for name, value, limit in zip(("biomass", "nitrogen", "oxygen"), residuals, tolerances,
                                                  strict=True) if abs(value) > limit]
    if failed or z_max > tolerances[1]:
        raise PbrFailure(
            "PBR_PERIODIC_STEADY_FAILED",
            "The 24-hour periodic state did not certify (" + (", ".join(failed) or "N + qX identity")
            + " residual above tolerance). The case is numerically stiff or near a washout limit; adjust HRT or "
            "light.",
            {"residuals_kg_m3": dict(zip(("biomass", "nitrogen", "oxygen"), residuals, strict=True)),
             "tolerances_kg_m3": dict(zip(("biomass", "nitrogen", "oxygen"), tolerances, strict=True)),
             "z_identity_max_kg_m3": z_max, "z_tolerance_kg_m3": tolerances[1]})
    max_tau = cylinder.optical_depth(growth.extinction, max(row[0] for row in samples), growth.diameter)
    if max_tau > cylinder.TAU_MAX:
        raise _optics_failure(growth, max(row[0] for row in samples))
    return _Orbit(initial, samples, residuals, tolerances, z_max)


# ------------------------------------------------------------------------------------ unit evaluator


def _quantity(value: float | None, units: str, label: str) -> dict[str, Any]:
    return {"value": value, "units": units, "label": label}


def _finding(severity: str, code: str, tag: str, message: str, field: str | None = None) -> dict[str, Any]:
    item = {"severity": severity, "code": code, "object": tag, "message": message, "source": "jarvis"}
    if field:
        item["field"] = field
    return item


def _parameters(unit: dict[str, Any]) -> dict[str, float]:
    params = unit.get("params") or {}
    values: dict[str, float] = {}
    missing = []
    for key in PARAMETERS:
        raw = (params.get(key) or {}).get("si")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
            missing.append(key)
        else:
            values[key] = float(raw)
    if missing:
        raise PbrFailure("PBR_PARAMETER_INVALID", "The photobioreactor is missing required inputs: "
                         f"{', '.join(missing)}. Set every Geometry, Operation and Light & environment value.",
                         {"fields": missing})
    bad = []
    if not 0.005 <= values["tube_inner_diameter"] <= 0.5:
        bad.append("tube_inner_diameter")
    for key in ("tube_length", "liquid_velocity", "oxygen_saturation", "photoperiod"):
        if values[key] <= 0.0:
            bad.append(key)
    if values["tube_count"] < 1 or values["tube_count"] != int(values["tube_count"]):
        bad.append("tube_count")
    if not 0.0 < values["pump_efficiency"] <= 100.0:
        bad.append("pump_efficiency")
    if values["baffle_friction_multiplier"] < 1.0:
        bad.append("baffle_friction_multiplier")
    if values["oxygen_kla"] < 0.0 or values["peak_par"] < 0.0:
        bad.extend(key for key in ("oxygen_kla", "peak_par") if values[key] < 0.0)
    if values["photoperiod"] > 86400.0:
        bad.append("photoperiod")
    if not 0.0 <= values["diffuse_fraction"] <= 1.0:
        bad.append("diffuse_fraction")
    if values["temperature_amplitude"] < 0.0 or values["temperature_mean"] - values["temperature_amplitude"] <= 0.0:
        bad.append("temperature_amplitude")
    if bad:
        raise PbrFailure("PBR_PARAMETER_INVALID", f"Photobioreactor inputs are outside their domain: {', '.join(bad)}. "
                         "Correct them in the inspector.", {"fields": bad})
    return values


def _model_values(model: dict[str, Any]) -> dict[str, Any]:
    card, values = model["card"], model["parameters"]
    factors = card["factors"]
    temperature_form, loss_form = factors["temperature"], factors["loss"]
    try:
        if temperature_form == "temperature.ctmi":
            temperature_parameters: tuple[float, ...] = (values["T_min"], values["T_opt"], values["T_max"])
            forms.temperature_ctmi(values["T_opt"], *temperature_parameters)
        elif temperature_form == "temperature.arrhenius_ref":
            temperature_parameters = (values["T_ref"], values["E_a"])
            forms.temperature_arrhenius_ref(values["T_ref"], *temperature_parameters)
        else:
            temperature_parameters = ()
        if loss_form == "loss.first_order":
            loss_parameters: tuple[float, ...] = (forms.loss_first_order(values["k_d"]),)
        else:
            forms.loss_light_dark(0.0, values["I_dark"], values["m_L"], values["m_D"])
            loss_parameters = (values["I_dark"], values["m_L"], values["m_D"])
        forms.light_monod(0.0, values["K_I"])
        forms.nutrient_monod(0.0, values["K_j_0"])
        if values["k_X"] < 0.0 or not math.isfinite(values["k_X"]):
            raise forms.FormRefusal("k_X must be >= 0")
    except forms.FormRefusal as exc:
        raise PbrFailure("PBR_MODEL_CARD_UNAVAILABLE", f"The pinned model card's parameter set is invalid for T1: {exc}. "
                         "Correct the set in the Biology model library and pin the new revision.") from exc
    return {"temperature_parameters": temperature_parameters, "loss_parameters": loss_parameters}


def build_growth(params: dict[str, float], model: dict[str, Any], inlet: tuple[float, float, float],
                 dilution_h: float) -> Growth:
    values = model["parameters"]
    resolved = _model_values(model)
    return Growth(
        mu_max_h=float(model["mu_max_h"]), light_saturation=values["K_I"], extinction=values["k_X"],
        nitrogen_half_saturation=values["K_j_0"], nitrogen_quota=float(model["nitrogen_quota"]),
        oxygen_yield=float(model["oxygen_yield"]),
        temperature_form=model["card"]["factors"]["temperature"],
        temperature_parameters=resolved["temperature_parameters"],
        loss_form=model["card"]["factors"]["loss"], loss_parameters=resolved["loss_parameters"],
        diameter=params["tube_inner_diameter"], peak_par=params["peak_par"],
        photoperiod_h=params["photoperiod"] / 3600.0, diffuse_fraction=params["diffuse_fraction"],
        temperature_mean=params["temperature_mean"], temperature_amplitude=params["temperature_amplitude"],
        kla_h=3600.0 * params["oxygen_kla"], oxygen_saturation=params["oxygen_saturation"],
        dilution_h=dilution_h, inlet=inlet)


CORRELATION_FIDELITY: Final = "Literature correlation (Colebrook/Clamond, screening)"
CFD_FIDELITY: Final = "CFD reduced model (scientifically unqualified)"


def _pinned_reduced_model(pin: dict[str, Any], workspace_id: str | None) -> Any:
    from app.core.database import open_sqlite_connection
    from app.modules.bluecad.cfd_pipe.reduced_model import PublicationRefused
    from app.modules.bluecad.cfd_pipe.studio_publication import reduced_model_of_source
    from app.modules.studio.service import published_source

    def unavailable(reason: str) -> PbrFailure:
        return PbrFailure("PBR_HYDRAULICS_MODEL_UNAVAILABLE", f"The pinned CFD reduced-model hydraulics are unavailable: "
                          f"{reason}. Re-pin a published reduced model in the unit's Hydraulics setting, or clear the "
                          "pin to use the literature correlation; no fallback was applied.")

    if not workspace_id:
        raise unavailable("the Process workspace is unavailable")
    with open_sqlite_connection() as connection:
        source = published_source(connection, workspace_id, str(pin["studio_version_id"]))
    if source is None:
        raise unavailable(f"Studio version {pin['studio_version_id']} is missing or its source no longer matches its digest")
    if hashlib.sha256(source.encode("utf-8")).hexdigest() != pin["source_sha256"]:
        raise unavailable("the Studio source digest differs from the pinned source_sha256")
    try:
        model = reduced_model_of_source(source)
    except PublicationRefused as exc:
        raise unavailable(str(exc)) from exc
    if model.version_id != pin["reduced_model_version_id"]:
        raise unavailable("the carried reduced model version differs from the pinned reduced_model_version_id")
    return model


def hydraulics(params: dict[str, float], deadline_s: float = 30.0, pin: dict[str, Any] | None = None,
               workspace_id: str | None = None) -> dict[str, Any]:
    """Re, ΔP and circulation power through the 104 stack (CoolProp water at T_mean and 1 atm), or a pinned CFD reduced model."""
    from app.modules.engineering.evaluator_contracts import EvaluationRequest
    from app.modules.process_stack._common import quantities
    from app.modules.process_stack.correlations import PIPE_EVALUATOR_ID, PipePressureDropEvaluator
    from app.modules.process_stack.properties import EVALUATOR_ID as PROPERTY_EVALUATOR_ID
    from app.modules.process_stack.properties import CoolPropPropertyEvaluator

    now = datetime.now(UTC)

    def request(evaluator_id: str, suffix: str, inputs: dict[str, tuple[float, str]]) -> Any:
        return EvaluationRequest.model_validate({
            "request_ref": {"authority_owner": "process", "object_type": "evaluation_request",
                            "object_id": f"pbr-unit/{suffix}", "workspace_id": "process", "revision": "1"},
            "evaluator_id": evaluator_id,
            "subject_ref": {"authority_owner": "process", "object_type": "material_state",
                            "object_id": "pbr-unit/culture", "workspace_id": "process", "revision": "1"},
            "inputs": [item.model_dump(mode="json") for item in quantities(inputs)],
            "requested_at": now - timedelta(seconds=1), "deadline_at": now + timedelta(seconds=deadline_s),
        })

    water = CoolPropPropertyEvaluator().evaluate(request(
        PROPERTY_EVALUATOR_ID, "water", {"temperature": (params["temperature_mean"], "K"), "pressure": (101325.0, "Pa")}))
    if water.status != "succeeded":
        raise PbrFailure("PBR_HYDRAULICS_UNAVAILABLE", "Water properties for the loop hydraulics are unavailable at the "
                         "declared mean temperature; check temperature_mean.")
    props = {item.name: item.value.value for item in water.outputs}
    if pin:
        return _cfd_hydraulics(params, props, pin, workspace_id)
    pipe = PipePressureDropEvaluator().evaluate(request(PIPE_EVALUATOR_ID, "loop", {
        "density": (props["density"], "kg/m3"), "dynamic_viscosity": (props["dynamic_viscosity"], "Pa*s"),
        "velocity": (params["liquid_velocity"], "m/s"), "diameter": (params["tube_inner_diameter"], "m"),
        "length": (params["tube_length"], "m"), "roughness": (0.0, "m")}))
    reynolds = params["liquid_velocity"] * params["tube_inner_diameter"] * props["density"] / props["dynamic_viscosity"]
    if pipe.status != "succeeded":
        code = pipe.failure.backend_code if pipe.failure else "failed"
        if code == "transitional_regime":
            return {"reynolds_number": reynolds, "pressure_drop": None, "pumping_power": None, "regime": code}
        raise PbrFailure("PBR_HYDRAULICS_UNAVAILABLE", "The loop's screening hydraulic calculation failed "
                         f"({code}); check tube geometry, liquid velocity and mean temperature, then Run again.",
                         {"backend_code": code})
    outputs = {item.name: item.value.value for item in pipe.outputs}
    pressure_drop = outputs["pressure_drop"] * params["baffle_friction_multiplier"]
    area = math.pi * params["tube_inner_diameter"] ** 2 / 4.0
    power = pressure_drop * params["liquid_velocity"] * area * params["tube_count"] / (params["pump_efficiency"] / 100.0)
    return {"reynolds_number": outputs["reynolds_number"], "pressure_drop": pressure_drop, "pumping_power": power,
            "regime": "laminar" if outputs["reynolds_number"] < 2040 else "turbulent"}


def _cfd_hydraulics(params: dict[str, float], props: dict[str, float], pin: dict[str, Any],
                    workspace_id: str | None) -> dict[str, Any]:
    from app.modules.bluecad.cfd_pipe.reduced_model import evaluate

    model = _pinned_reduced_model(pin, workspace_id)
    from app.modules.workspaces.design_components import pinned_reduced_model_obsolescence

    obsolete = pinned_reduced_model_obsolescence(str(workspace_id), str(pin["studio_version_id"]), model.geometry_digest)
    reynolds = params["liquid_velocity"] * params["tube_inner_diameter"] * props["density"] / props["dynamic_viscosity"]
    verdict = evaluate(model, reynolds=reynolds, diameter_m=params["tube_inner_diameter"], density_kg_m3=props["density"],
                       dynamic_viscosity_pa_s=props["dynamic_viscosity"], out_of_range=pin.get("out_of_range", "refuse"))
    pressure_drop = power = None
    if verdict.status != "refused" and verdict.pressure_gradient_pa_m is not None:
        pressure_drop = verdict.pressure_gradient_pa_m * params["tube_length"] * params["baffle_friction_multiplier"]
        area = math.pi * params["tube_inner_diameter"] ** 2 / 4.0
        power = pressure_drop * params["liquid_velocity"] * area * params["tube_count"] / (params["pump_efficiency"] / 100.0)
    return {"reynolds_number": reynolds, "pressure_drop": pressure_drop, "pumping_power": power,
            "regime": verdict.regime or "unclassified", "cfd": {
                "fidelity": CFD_FIDELITY, "studio_version_id": pin["studio_version_id"],
                "reduced_model_version_id": model.version_id, "geometry_digest": model.geometry_digest,
                "status": verdict.status, "violations": list(verdict.violations), "obsolete_reasons": obsolete,
                "relative_uncertainty": verdict.relative_uncertainty, "out_of_range": pin.get("out_of_range", "refuse")}}


_FAILURE_INPUT_CODES = {"biomass": "PBR_REQUIRES_BIOMASS", "nitrogen": "PBR_REQUIRES_NITROGEN",
                        "oxygen": "PBR_REQUIRES_OXYGEN"}


def _inlet_culture(inlet: dict[str, Any], density: float) -> tuple[float, float, float]:
    culture = inlet.get("culture")
    if not isinstance(culture, dict):
        raise PbrFailure("PBR_REQUIRES_CULTURE_INLET", "The photobioreactor inlet carries no culture. Connect it "
                         "downstream of a culture feed (set Culture medium on the feed stream).")
    values = []
    for field, code in _FAILURE_INPUT_CODES.items():
        raw = culture.get(field)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
            raise PbrFailure(code, f"The photobioreactor inlet has no specified dissolved {field} value. Set "
                             f"{field} on the culture feed; an explicit 0 is valid, an unknown value is not.")
        if float(raw) < 0.0:
            raise PbrFailure("PBR_NONPHYSICAL_STATE", f"The photobioreactor inlet {field} is negative; correct the "
                             "culture feed.")
        values.append(float(raw) * density)
    return values[0], values[1], values[2]


def _cache_key(unit: dict[str, Any], params: dict[str, float], pin: dict[str, Any], set_digest: str,
               inlet: dict[str, Any], density: float) -> str:
    payload = [MODEL_VERSION, set_digest, pin, sorted(params.items()), unit.get("tag"), inlet, repr(density)]
    if unit.get("hydraulics_model"):
        payload.append({"hydraulics_model": unit["hydraulics_model"]})
    text = json.dumps(payload, sort_keys=True, default=repr, allow_nan=True)
    return "pbr_unit.v1:" + hashlib.sha256(text.encode()).hexdigest()


def _resolve(workspace_id: str | None, pin: dict[str, Any]) -> dict[str, Any]:
    from app.modules.bio_models import service

    if not all(isinstance(pin.get(key), str) and pin.get(key) for key in ("card_id", "card_revision", "card_digest")):
        raise PbrFailure("PBR_REQUIRES_MODEL_CARD", "The photobioreactor has no pinned biological model card. Open "
                         "Biology and pin a card (create one in the Biology model library if none exist).")
    if not workspace_id:
        raise PbrFailure("PBR_MODEL_CARD_UNAVAILABLE", "The Process workspace for the model card is unavailable; "
                         "reopen the draft and run again.")
    try:
        return service.resolve_growth_model(workspace_id, pin["card_id"], pin["card_revision"], pin["card_digest"])
    except service.BioModelError as exc:
        code = exc.code if exc.code.startswith("PBR_MODEL_CARD_") else "PBR_MODEL_CARD_UNAVAILABLE"
        raise PbrFailure(code, str(exc), dict(exc.detail)) from exc


def evaluate_pbr(unit: dict[str, Any], inlet: dict[str, Any], context: JarvisUnitContext) -> JarvisUnitEvaluation:
    """168 Jarvis-unit evaluator for ``PhotobioreactorT1``."""
    from app.modules.process_stack.mixed_runtime import JarvisUnitEvaluation

    if context.validation:
        outlet = copy.deepcopy(inlet)
        outlet["owner"] = "jarvis_bio"
        return JarvisUnitEvaluation(outlets={"outlet": outlet},
                                    result={"owner": "jarvis_bio", "calculated": False, "evaluator": EVALUATOR_ID,
                                            "model_version": MODEL_VERSION, "findings": []})
    started = time.monotonic()
    clock = _Clock(min(started + WALL_CAP_S, context.deadline))
    tag = str(unit.get("tag", "PBR"))
    if (unit.get("options") or {}).get("optics_model", "t1") == "t2":
        raise PbrFailure("OPTICS_T2_REQUIRES_DYNAMIC_PROFILE",
                         "T2 optics requires a dynamic run with DNI and DHI profile channels.")
    density = float(context.inlet_density_kg_m3)
    if not math.isfinite(density) or density <= 0.0:
        raise PbrFailure("PBR_INLET_DENSITY_UNAVAILABLE", "The photobioreactor inlet has no solved liquid density "
                         "(it is fed directly by a recycle tear). Place a Mixer or Heater between the Recycle and "
                         "the PBR so DWSIM supplies the inlet state.")
    vapor = inlet.get("vapor_fraction")
    if isinstance(vapor, (int, float)) and math.isfinite(vapor) and vapor > VAPOR_LIMIT:
        raise PbrFailure("CULTURE_PHASE_NOT_LIQUID", f"The photobioreactor inlet is not liquid (vapor fraction "
                         f"{vapor:.3g}). Cool or pressurise the culture before the PBR.")
    flow = float(inlet.get("mass_flow_kg_s") or 0.0)
    if not math.isfinite(flow) or flow <= 0.0:
        raise PbrFailure("PBR_NO_THROUGHFLOW", "The photobioreactor has no carrier flow through it, so dilution and "
                         "residence time are undefined. Give the culture feed a positive mass flow.")
    params = _parameters(unit)
    pin = dict(unit.get("model") or {})
    model = _resolve(getattr(context, "workspace_id", None), pin)
    culture_inlet = _inlet_culture(inlet, density)
    key = _cache_key(unit, params, pin, model["set"]["digest"], inlet, density)
    cached = context.cache.get(key) if isinstance(context.cache, dict) else None
    if cached is not None:
        return copy.deepcopy(cached)

    volume = params["tube_count"] * math.pi * params["tube_inner_diameter"] ** 2 * params["tube_length"] / 4.0
    q_m3_s = flow / density
    dilution_s = q_m3_s / volume
    dilution_h = 3600.0 * dilution_s
    hrt_d = 1.0 / dilution_s / 86400.0
    circulation = params["liquid_velocity"] * params["tube_count"] * math.pi * params["tube_inner_diameter"] ** 2 / 4.0
    growth = build_growth(params, model, culture_inlet, dilution_h)
    solution = solve_periodic(growth, clock)
    orbit = certify(growth, solution.x_star, clock)
    clock.check()
    hydro = hydraulics(params, deadline_s=max(1.0, clock.deadline - time.monotonic()),
                       pin=unit.get("hydraulics_model") or None, workspace_id=getattr(context, "workspace_id", None))
    clock.check()

    final = orbit.samples[-1]
    x_mean, n_mean, o_mean, r_mean, transfer_mean = (final[index] / 24.0 for index in range(3, 8))
    x_in, n_in, o_in = culture_inlet
    q, y_o2 = growth.nitrogen_quota, growth.oxygen_yield
    generation = {"biomass": volume * r_mean / 3600.0,
                  "nitrogen": -q * volume * r_mean / 3600.0,
                  "oxygen": volume * (y_o2 * r_mean - transfer_mean) / 3600.0}
    allowance = {name: volume * (abs(residual) + ALLOWANCE_FLOOR) / 86400.0
                 for name, residual in zip(("biomass", "nitrogen", "oxygen"), orbit.residuals, strict=True)}
    outlet = copy.deepcopy(inlet)
    outlet["owner"] = "jarvis_bio"
    outlet["culture"] = dict(outlet["culture"])
    outlet["culture"].update(biomass=x_mean / density, nitrogen=n_mean / density, oxygen=o_mean / density)

    x_series = [row[0] for row in orbit.samples]
    n_series = [row[1] for row in orbit.samples]
    o_series = [row[2] for row in orbit.samples]
    cfd = hydro.get("cfd")
    reported = {
        "lambda_h": _quantity(solution.lambda_h, "1/h", "Thin-culture growth rate Λ"),
        "dilution_h": _quantity(dilution_h, "1/h", "Dilution rate D (process-inlet basis)"),
        "hrt_d": _quantity(hrt_d, "d", "Hydraulic residence time (process-inlet basis)"),
        "volume_m3": _quantity(volume, "m³", "Liquid volume"),
        "volumetric_flow_m3_h": _quantity(3600.0 * q_m3_s, "m³/h", "Volumetric flow Q (process-inlet basis)"),
        "circulation_flow_m3_h": _quantity(3600.0 * circulation, "m³/h", "Internal circulation flow"),
        "pass_transit_time_s": _quantity(params["tube_length"] / params["liquid_velocity"], "s",
                                          "Internal circulation pass transit time"),
        "circulation_to_throughflow_ratio": _quantity(circulation / q_m3_s, "1",
                                                       "Internal circulation / process throughflow"),
        "biomass_mean": _quantity(x_mean, "kg/m³", "Mean biomass X̄"),
        "volumetric_productivity": _quantity(24.0 * dilution_h * (x_mean - x_in), "kg/(m³·d)",
                                             "Net volumetric biomass productivity"),
        "net_biomass_production": _quantity(24.0 * volume * r_mean, "kg/d", "Net biomass production rate"),
        "outlet_biomass_throughput": _quantity(86400.0 * q_m3_s * x_mean, "kg/d", "Outlet biomass throughput"),
        "nitrogen_mean": _quantity(n_mean, "kg/m³", "Mean dissolved N"),
        "nitrogen_min": _quantity(min(n_series), "kg/m³", "Minimum dissolved N"),
        "oxygen_mean": _quantity(o_mean, "kg/m³", "Mean dissolved O₂"),
        "oxygen_max": _quantity(max(o_series), "kg/m³", "Maximum dissolved O₂ (loop mean)"),
        "oxygen_saturation_ratio_max": _quantity(max(o_series) / growth.oxygen_saturation, "1",
                                                 "Maximum O₂ saturation ratio (loop mean, not a degasser design value)"),
        "oxygen_gas_transfer": _quantity(24.0 * volume * transfer_mean, "kg/d",
                                         "Net O₂ gas transfer (+ degassing, − absorption)"),
        "optical_depth_max": _quantity(cylinder.optical_depth(growth.extinction, max(x_series), growth.diameter),
                                       "1", "Maximum optical depth τ = k_X·X·D (T1 limit 1000)"),
        "reynolds_number": _quantity(hydro["reynolds_number"], "1", "Reynolds number"),
        "pressure_drop": _quantity(hydro["pressure_drop"], "Pa", "Tube pressure drop" if cfd else "Tube pressure drop (screening)"),
        "pumping_power": _quantity(hydro["pumping_power"], "W", "Circulation pumping power" if cfd else "Circulation pumping power (screening)"),
    }
    findings = []
    if solution.branch == "washout":
        findings.append(_finding("warning", "PBR_WASHOUT", tag,
                                 f"Washout: thin-culture growth Λ = {solution.lambda_h:.4g} 1/h does not exceed dilution "
                                 f"D = {dilution_h:.4g} 1/h, so no biomass is retained. Lengthen HRT above "
                                 f"{(1.0 / solution.lambda_h / 24.0) if solution.lambda_h > 0 else math.inf:.3g} d or "
                                 "improve light/nitrogen."))
    if not HRT_RANGE_D[0] <= hrt_d <= HRT_RANGE_D[1]:
        findings.append(_finding("warning", "PBR_HRT_OUT_OF_RANGE", tag,
                                 f"Hydraulic residence time {hrt_d:.3g} d (process-inlet basis) is outside the 0.1–100 d "
                                 "screening range of the T1 model."))
    if model.get("candidate_symbols"):
        findings.append(_finding("info", "PBR_PARAMETER_UNVERIFIED", tag,
                                 "Model-card values not yet verified (candidate): "
                                 + ", ".join(model["candidate_symbols"]) + "."))
    inlet_temperature = inlet.get("temperature_K")
    if isinstance(inlet_temperature, (int, float)) and abs(params["temperature_mean"] - inlet_temperature) > TEMPERATURE_DIFFERS_K:
        findings.append(_finding("info", "PBR_TEMPERATURE_DECLARED_DIFFERS", tag,
                                 f"Declared culture mean temperature differs from the inlet by "
                                 f"{abs(params['temperature_mean'] - inlet_temperature):.3g} K; T1 has no energy balance."))
    if cfd and cfd["status"] == "refused":
        findings.append(_finding("warning", "PBR_HYDRAULICS_MODEL_REFUSED", tag,
                                 "The pinned CFD reduced model refused this operating point, so no pressure drop or pumping "
                                 "power is reported: " + "; ".join(cfd["violations"]) + ". Change velocity or diameter, "
                                 "choose warn mode, or clear the pin to use the literature correlation."))
    elif cfd and cfd["status"] == "out_of_range_warned":
        findings.append(_finding("warning", "PBR_HYDRAULICS_MODEL_OUT_OF_RANGE", tag,
                                 "Operating point is outside the pinned CFD reduced model's validity (no uncertainty is "
                                 "claimed): " + "; ".join(cfd["violations"]) + "."))
    elif hydro["pressure_drop"] is None:
        findings.append(_finding("warning", "PBR_REYNOLDS_TRANSITIONAL", tag,
                                 f"Reynolds number {hydro['reynolds_number']:.4g} is in the laminar–turbulent transition; "
                                 "no screening pressure drop or pumping power is reported. Change velocity or diameter."))
    if cfd and cfd.get("obsolete_reasons"):
        findings.append(_finding("warning", "PBR_HYDRAULICS_MODEL_OBSOLETE", tag,
                                 "Reduced model obsolete, rerun the study: " + "; ".join(cfd["obsolete_reasons"])
                                 + ". The pinned version is still used until you re-pin or clear it."))
    if q_m3_s >= circulation:
        findings.append(_finding("warning", "PBR_FEED_EXCEEDS_CIRCULATION", tag,
                                 "Process throughflow exceeds internal circulation."))
    card, parameter_set = model["card"], model["set"]
    caveats = list(CAVEATS)
    caveats.append(f"N source assumed: {card['n_source']}; inlet N speciation unverified; O₂ yield depends on this "
                   "assumption.")
    if str(card["factors"].get("optics", "")).startswith("optics.slab_"):
        caveats.append("Card optics replaced by unit geometry (cylinder).")
    if cfd and cfd["status"] != "in_range":
        caveats.append("Loop hydraulics: " + ("refused, outside" if cfd["status"] == "refused" else "evaluated outside")
                       + " the pinned CFD reduced model's validity (" + "; ".join(cfd["violations"]) + ")"
                       + ("." if cfd["status"] == "refused" else "; no uncertainty."))
    if cfd and cfd.get("obsolete_reasons"):
        caveats.append("Loop hydraulics use a pinned CFD reduced model that is obsolete (" + "; ".join(cfd["obsolete_reasons"])
                       + "); the pin is kept until re-pinned or cleared.")
    hydraulics_block = cfd or {"fidelity": CORRELATION_FIDELITY}
    result = {
        "owner": "jarvis_bio", "calculated": True, "evaluator": EVALUATOR_ID, "model_version": MODEL_VERSION,
        "fidelity": FIDELITY, "hydraulics_model": hydraulics_block, "branch": solution.branch, "caveats": caveats, "findings": findings,
        "model_pin": {"card_id": pin["card_id"], "card_name": card.get("name"), "card_revision": pin["card_revision"],
                      "card_digest": pin["card_digest"], "set_id": parameter_set["id"],
                      "set_name": parameter_set.get("name"), "set_revision": parameter_set["revision"],
                      "set_digest": parameter_set["digest"], "form_versions": dict(card["form_versions"]),
                      "n_source": card["n_source"]},
        "reported": reported,
        "numerics": {
            "map_evaluations": solution.map_evaluations,
            "bracket": {"value": list(solution.bracket), "units": "kg/m³"},
            "root_residual": {"value": solution.root_residual, "units": "kg/m³"},
            "periodicity": {name: {"residual": residual, "tolerance": limit, "units": "kg/m³"}
                            for name, residual, limit in zip(("biomass", "nitrogen", "oxygen"), orbit.residuals,
                                                             orbit.tolerances, strict=True)},
            "z_identity_max": {"value": orbit.z_identity_max, "units": "kg/m³"},
            "generation_allowance": {name: {"value": value, "units": "kg/s"} for name, value in allowance.items()},
            "wall_time_s": time.monotonic() - started,
        },
    }
    evaluation = JarvisUnitEvaluation(outlets={"outlet": outlet}, result=result, culture_generation=generation,
                                      culture_generation_units={name: "kg/s" for name in generation},
                                      culture_generation_allowance=allowance)
    if isinstance(context.cache, dict):
        context.cache[key] = copy.deepcopy(evaluation)
    return evaluation


def warm_imports() -> None:
    """Pay cold imports (SUNDIALS, CoolProp, fluids, SciPy quad) and build the optics table at startup."""
    import CoolProp.CoolProp  # noqa: F401
    import fluids  # noqa: F401
    import sksundae.cvode  # noqa: F401
    from scipy.integrate import quad  # noqa: F401

    cylinder.response_average_tau(lambda irradiance: irradiance / (1.0 + irradiance), 1.0, 1.0, 0.5)
