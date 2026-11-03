"""107 PBR-EVALUATOR-1: lumped day/night tubular photobioreactor evaluator.

Model form (reduced order, one well-mixed liquid volume; every coefficient is an input). Time is
hours from local midnight; solar noon is at 12 h.

* incident PAR ``I0(h) = peak_par * sin(pi * (h - sunrise) / photoperiod)`` during daylight, 0 at night;
* local light ``I(z) = I0 * exp(-specific_light_extinction * X * z)`` across the tube diameter
  (Beer-Lambert, one-side-lit slab approximation of the tube); gross growth is the depth average of
  the local light response (8-point Gauss-Legendre), so self-shading acts locally (a Bechet type II
  model, as in Lima et al. 2022 for N. gaditana). Light response (backend option ``light_response``):
  ``monod``: ``mu_max * I / (K_s + I)``; ``haldane`` (default): ``mu_max * I / (K_s + I + I**2 / K_i)``;
* temperature (option ``temperature_response``): ``cardinal`` (default) uses the cardinal
  temperature model with inflexion (Rosso 1993) over a prescribed diel liquid-temperature sinusoid;
  ``isothermal`` applies kinetics exactly at their sourced reference temperature and refuses any
  scenario whose liquid temperature leaves it by more than ``ISOTHERMAL_TOLERANCE_K``. There is no
  heat balance: liquid temperature is prescribed (see ``MODEL_GAPS``);
* nitrogen (option ``nitrogen_response``): ``monod`` (default) multiplies growth by ``N / (K_N + N)``;
  ``replete`` assumes non-limiting nitrogen and refuses the run if dissolved nitrogen is exhausted.
  Both use a fixed biomass N quota, so dissolved plus biomass nitrogen is conserved except for
  harvest/replenishment (net biomass loss returns its nitrogen to solution);
* first-order biomass loss ``k_d * X`` day and night (lumped death/respiration);
* dissolved O2: ``Y_O2 * dX/dt`` (net production, net consumption when biomass is lost) minus
  ``kLa * (O2 - O2_sat)`` degassing; cumulative degassed O2 is integrated so the O2 balance closes;
* daily semi-continuous harvest at ``harvest_hour``: a fraction of the culture is replaced by
  medium (``medium_nitrogen``, O2 at saturation), applied between CVODE segments so CVODE stays
  the single time/state owner;
* loop hydraulics through the 104 CoolProp (pure water, a screening stand-in for seawater) and
  fluids evaluators (straight smooth tube only).

Every biological/physical coefficient must carry a ``basis_ref`` (its provenance) or the request is
refused: the evaluator never supplies default biology. A successful run means the model ran, not that
it is qualified; every succeeded result carries an ``unqualified`` validity envelope and qualification
lives in the 102 ledger.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Final

from app.modules.ai.jarvis_context_models import SourceRef
from app.modules.engineering.evaluator_contracts import (
    EvaluationRequest,
    EvaluationResult,
    EvaluatorAvailability,
    EvaluatorDescriptor,
    NamedQuantity,
    NumericalDiagnostics,
)
from app.modules.engineering.evidence_contracts import validity_content_digest
from app.modules.engineering.refs import (
    DomainBound,
    EvaluationRequestRef,
    MaterialStateRef,
    Quantity,
    ValidityEnvelopeRef,
)
from app.modules.photobioreactor.pbr_core import cardinal_temperature as _cardinal_temperature
from app.modules.photobioreactor.pbr_core import light_response, nitrogen_factor, synthetic_day_inputs
from app.modules.process_stack._common import (
    EvaluationRefusal,
    descriptor_version,
    evaluate_with,
    import_availability,
    magnitude,
    option,
    quantities,
)
from app.modules.process_stack.correlations import PIPE_EVALUATOR_ID, PipePressureDropEvaluator
from app.modules.process_stack.dynamics import integrate_ode
from app.modules.process_stack.properties import EVALUATOR_ID as PROPERTY_EVALUATOR_ID
from app.modules.process_stack.properties import CoolPropPropertyEvaluator

EVALUATOR_ID: Final = "photobioreactor.day_night"
MODEL_VERSION: Final = "pbr_day_night.v2"
MAX_DAYS: Final = 366
ISOTHERMAL_TOLERANCE_K: Final = 0.5
_SAMPLES_PER_HOUR: Final = 4
_GAUSS_POINTS: Final = 8
_NEGATIVE_TOLERANCE: Final = 1e-9

OPTIONS: Final[Mapping[str, tuple[str, ...]]] = {
    "light_response": ("haldane", "monod"),
    "temperature_response": ("cardinal", "isothermal"),
    "nitrogen_response": ("monod", "replete"),
}
# Coefficients that describe the organism or a physical property: provenance (basis_ref) is mandatory.
SOURCED_ALWAYS: Final[Mapping[str, str]] = {
    "max_specific_growth_rate": "1/h",
    "light_saturation_constant": "umol/(m**2*s)",
    "specific_light_extinction": "m**2/kg",
    "biomass_loss_rate": "1/h",
    "biomass_nitrogen_fraction": "1",
    "oxygen_yield": "1",
    "oxygen_kla": "1/h",
    "oxygen_saturation": "kg/m3",
}
SOURCED_BY_OPTION: Final[Mapping[tuple[str, str], Mapping[str, str]]] = {
    ("light_response", "haldane"): {"light_inhibition_constant": "umol/(m**2*s)"},
    ("temperature_response", "cardinal"): {"temperature_min": "K", "temperature_opt": "K", "temperature_max": "K"},
    ("temperature_response", "isothermal"): {"kinetics_reference_temperature": "K"},
    ("nitrogen_response", "monod"): {"nitrogen_half_saturation": "kg/m3"},
}
# Design, operation, scenario and initial-state variables chosen by the study.
DESIGN: Final[Mapping[str, str]] = {
    "initial_biomass": "kg/m3",
    "initial_nitrogen": "kg/m3",
    "initial_dissolved_oxygen": "kg/m3",
    "peak_par": "umol/(m**2*s)",
    "photoperiod": "h",
    "temperature_mean": "K",
    "temperature_amplitude": "K",
    "duration": "d",
    "tube_inner_diameter": "m",
    "loop_length": "m",
    "liquid_velocity": "m/s",
    "pump_efficiency": "1",
    "harvest_fraction": "1",
    "harvest_hour": "h",
    "medium_nitrogen": "kg/m3",
}
# What this model does not represent; recorded with every qualification record for this version.
MODEL_GAPS: Final[tuple[str, ...]] = (
    "heat_balance: liquid temperature is prescribed; no solar/ambient/pump heat balance or cooling duty",
    "seawater_properties: loop hydraulics use pure-water CoolProp properties at the mean temperature",
    "tube_light_geometry: one-side-lit slab of depth = tube diameter; no circular/multi-side/diffuse light",
    "tube_light_geometry: resolved in jarvis.pbr_unit_t1",
    "photoacclimation_and_light_dark_cycling: no pigment adaptation or flashing-light effect",
    "oxygen_inhibition: dissolved O2 does not inhibit growth; only reported as saturation ratio",
    "carbon_ph: no CO2/bicarbonate/pH balance; carbon assumed non-limiting",
    "nutrient_quota: fixed biomass N quota (no Droop/storage); phosphorus not modelled",
    "loss_temperature_dependence: biomass loss rate is temperature independent",
    "hydraulic_losses: straight smooth tube only; no bends, fittings, degasser or manifold losses",
    "storage: no post-harvest storage/degradation model",
)


class PbrDayNightEvaluator:
    def descriptor(self) -> EvaluatorDescriptor:
        return EvaluatorDescriptor(
            evaluator_id=EVALUATOR_ID,
            backend_kind="dynamic_simulator",
            backend_name="scikit-sundae",
            backend_version=f"{MODEL_VERSION}+sksundae-{descriptor_version('sksundae')}",
            fidelity="reduced_order",
            capabilities=(
                "day_night_light",
                "self_shading",
                "photoinhibition",
                "cardinal_temperature",
                "nitrogen_limitation",
                "oxygen_degassing",
                "semi_continuous_harvest",
                "loop_hydraulics",
            ),
            qualification_record_ref=SourceRef(
                authority_owner="repository",
                object_type="scientific_qualification_record",
                object_id="scripts/qualification/107/pbr_day_night.v2.ledger.json",
                revision=MODEL_VERSION,
            ),
        )

    def availability(self) -> EvaluatorAvailability:
        stack = (PipePressureDropEvaluator(), CoolPropPropertyEvaluator())
        missing = next((item.availability() for item in stack if item.availability().state != "available"), None)
        if missing is not None:
            return missing.model_copy(update={"evaluator_id": EVALUATOR_ID})
        return import_availability(EVALUATOR_ID, "sksundae", lambda: self.descriptor().backend_version)

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        result = evaluate_with(self, request, lambda: _simulate(request))
        if result.status != "succeeded":
            return result
        return result.model_copy(update={"validity": _validity(request)})


def _validity(request: EvaluationRequest) -> ValidityEnvelopeRef:
    """Structural domain of this model version; always ``unqualified`` (availability is not qualification)."""
    envelope = ValidityEnvelopeRef(
        authority_owner="photobioreactor",
        object_id=f"{EVALUATOR_ID}.{MODEL_VERSION}",
        workspace_id=request.request_ref.workspace_id,
        revision=MODEL_VERSION,
        qualification_status="unqualified",
        domain=(
            DomainBound(
                variable="duration",
                lower=Quantity(value=0.0, unit="d"),
                upper=Quantity(value=float(MAX_DAYS), unit="d"),
            ),
            DomainBound(
                variable="photoperiod", lower=Quantity(value=0.0, unit="h"), upper=Quantity(value=24.0, unit="h")
            ),
        ),
    )
    return envelope.model_copy(update={"content_digest": validity_content_digest(envelope)})


def _modes(request: EvaluationRequest) -> dict[str, str]:
    unknown = sorted(set(request.backend_options) - set(OPTIONS))
    if unknown:
        raise EvaluationRefusal("unsupported_request", "option_unsupported", f"unsupported options {unknown}")
    modes = {}
    for name, allowed in OPTIONS.items():
        only = request.model_copy(
            update={"backend_options": {key: value for key, value in request.backend_options.items() if key == name}}
        )
        modes[name] = option(only, name, allowed, allowed[0])
    return modes


def _inputs(request: EvaluationRequest) -> tuple[dict[str, float], dict[str, str]]:
    if request.subject_ref.object_type != "dynamic_model":
        raise EvaluationRefusal("unsupported_request", "subject_unsupported", "subject must be a dynamic model")
    modes = _modes(request)
    sourced = dict(SOURCED_ALWAYS)
    for choice in modes.items():
        sourced.update(SOURCED_BY_OPTION.get(choice, {}))
    given = {item.name: item.value for item in request.inputs}
    expected = {**sourced, **DESIGN}
    unknown, missing = sorted(set(given) - set(expected)), sorted(set(expected) - set(given))
    if unknown or missing:
        raise EvaluationRefusal("invalid_input", "input_names_invalid", f"missing={missing} unknown={unknown}")
    unsourced = sorted(name for name in sourced if given[name].basis_ref is None)
    if unsourced:
        raise EvaluationRefusal(
            "invalid_input", "parameter_provenance_missing", f"coefficients without basis_ref: {unsourced}"
        )
    x = {name: magnitude(given[name], unit) for name, unit in expected.items()}
    non_negative = [name for name in expected if name != "temperature_amplitude"]
    if any(x[name] < 0.0 for name in non_negative):
        raise EvaluationRefusal(
            "invalid_input", "input_negative", f"negative: {sorted(name for name in non_negative if x[name] < 0.0)}"
        )
    positive = [
        name
        for name in (
            "max_specific_growth_rate",
            "light_saturation_constant",
            "light_inhibition_constant",
            "specific_light_extinction",
            "nitrogen_half_saturation",
            "oxygen_kla",
            "oxygen_saturation",
            "tube_inner_diameter",
            "loop_length",
            "liquid_velocity",
            "pump_efficiency",
            "duration",
            "initial_biomass",
        )
        if name in x
    ]
    if any(x[name] <= 0.0 for name in positive):
        raise EvaluationRefusal(
            "invalid_input",
            "input_non_positive",
            f"must be positive: {sorted(name for name in positive if x[name] <= 0.0)}",
        )
    if modes["temperature_response"] == "cardinal":
        if not x["temperature_min"] < x["temperature_opt"] < x["temperature_max"]:
            raise EvaluationRefusal("invalid_input", "temperature_cardinals_invalid", "need T_min < T_opt < T_max")
    elif (
        abs(x["temperature_mean"] - x["kinetics_reference_temperature"]) + abs(x["temperature_amplitude"])
        > ISOTHERMAL_TOLERANCE_K
    ):
        raise EvaluationRefusal(
            "outside_validity_domain",
            "temperature_outside_kinetics_reference",
            f"isothermal kinetics hold only within {ISOTHERMAL_TOLERANCE_K} K of their reference",
        )
    if not (
        0.0 < x["photoperiod"] <= 24.0
        and 0.0 <= x["harvest_hour"] < 24.0
        and x["harvest_fraction"] < 1.0
        and x["pump_efficiency"] <= 1.0
        and x["biomass_nitrogen_fraction"] < 1.0
    ):
        raise EvaluationRefusal(
            "invalid_input",
            "input_range_invalid",
            "photoperiod in (0, 24] h, harvest_hour in [0, 24) h, fractions/efficiency < 1",
        )
    if x["duration"] > MAX_DAYS:
        raise EvaluationRefusal("unsupported_request", "duration_too_long", f"at most {MAX_DAYS} days")
    return x, modes


def _rhs(x: Mapping[str, float], modes: Mapping[str, str]) -> Callable[[float, tuple[float, ...]], list[float]]:
    """State: biomass X, dissolved N, dissolved O2, cumulative degassed O2 (all kg/m3); time in h."""
    import numpy as np

    nodes, weights = np.polynomial.legendre.leggauss(_GAUSS_POINTS)
    depths = 0.5 * (nodes + 1.0)  # fraction of the diameter
    half_weights = 0.5 * weights
    sunrise = 12.0 - x["photoperiod"] / 2.0
    inhibition = 1.0 / x["light_inhibition_constant"] if modes["light_response"] == "haldane" else 0.0
    cardinal = modes["temperature_response"] == "cardinal"
    nitrogen_limited = modes["nitrogen_response"] == "monod"

    def growth(hour: float, biomass: float) -> float:
        day_hour = hour % 24.0
        if not sunrise < day_hour < sunrise + x["photoperiod"]:
            return 0.0
        surface, _ = synthetic_day_inputs(
            day_hour, x["photoperiod"], x["peak_par"], x["temperature_mean"], x["temperature_amplitude"]
        )
        optical_depth = x["specific_light_extinction"] * max(biomass, 0.0) * x["tube_inner_diameter"]
        light = surface * np.exp(-optical_depth * depths)
        local = light_response(light, x["light_saturation_constant"], inhibition)
        thermal = 1.0
        if cardinal:
            _, temperature = synthetic_day_inputs(
                day_hour, x["photoperiod"], x["peak_par"], x["temperature_mean"], x["temperature_amplitude"]
            )
            thermal = _cardinal_temperature(
                temperature, x["temperature_min"], x["temperature_opt"], x["temperature_max"]
            )
        return float(x["max_specific_growth_rate"] * thermal * np.dot(half_weights, local))

    def rhs(hour: float, y: tuple[float, ...]) -> list[float]:
        biomass, nitrogen, oxygen, _ = y
        gross = growth(hour, biomass)
        if nitrogen_limited:
            gross *= nitrogen_factor(nitrogen, x["nitrogen_half_saturation"])
        d_biomass = (gross - x["biomass_loss_rate"]) * biomass
        degassing = x["oxygen_kla"] * (oxygen - x["oxygen_saturation"])
        return [
            d_biomass,
            -x["biomass_nitrogen_fraction"] * d_biomass,
            x["oxygen_yield"] * d_biomass - degassing,
            degassing,
        ]

    return rhs


def _hydraulics(request: EvaluationRequest, x: Mapping[str, float]) -> dict[str, float]:
    """Loop pressure drop and pump power through the 104 evaluators (water at mean temperature)."""
    now = datetime.now(UTC)
    ref = request.request_ref

    def sub_request(evaluator_id: str, suffix: str, inputs: Mapping[str, tuple[float, str]]) -> EvaluationRequest:
        return EvaluationRequest(
            request_ref=EvaluationRequestRef(
                authority_owner=ref.authority_owner,
                object_id=f"{ref.object_id}/{suffix}",
                workspace_id=ref.workspace_id,
                revision=ref.revision,
            ),
            evaluator_id=evaluator_id,
            subject_ref=MaterialStateRef(
                authority_owner="photobioreactor",
                object_id=f"{ref.object_id}/culture",
                workspace_id=ref.workspace_id,
                revision=ref.revision,
            ),
            inputs=quantities(inputs),
            requested_at=now,
            deadline_at=request.deadline_at,
        )

    def outputs(result: EvaluationResult) -> dict[str, float]:
        if result.status != "succeeded":
            failure = result.failure
            raise EvaluationRefusal(
                failure.category if failure else "internal_error",
                f"stack:{failure.backend_code}" if failure else "stack:failed",
                f"{result.evaluator_id}: {failure.message if failure else result.status}",
            )
        return {item.name: item.value.value for item in result.outputs}

    water = outputs(
        CoolPropPropertyEvaluator().evaluate(
            sub_request(
                PROPERTY_EVALUATOR_ID,
                "water",
                {"temperature": (x["temperature_mean"], "K"), "pressure": (101325.0, "Pa")},
            )
        )
    )
    pipe = outputs(
        PipePressureDropEvaluator().evaluate(
            sub_request(
                PIPE_EVALUATOR_ID,
                "loop",
                {
                    "density": (water["density"], "kg/m3"),
                    "dynamic_viscosity": (water["dynamic_viscosity"], "Pa*s"),
                    "velocity": (x["liquid_velocity"], "m/s"),
                    "diameter": (x["tube_inner_diameter"], "m"),
                    "length": (x["loop_length"], "m"),
                    "roughness": (0.0, "m"),
                },
            )
        )
    )
    flow = x["liquid_velocity"] * math.pi * x["tube_inner_diameter"] ** 2 / 4.0
    return {
        "reynolds_number": pipe["reynolds_number"],
        "pressure_drop": pipe["pressure_drop"],
        "pumping_power": pipe["pressure_drop"] * flow / x["pump_efficiency"],
    }


def _segments(x: Mapping[str, float]) -> list[float]:
    end = x["duration"] * 24.0
    boundaries = [0.0]
    harvest = x["harvest_hour"] if x["harvest_hour"] > 0.0 else 24.0
    while harvest < end:
        boundaries.append(harvest)
        harvest += 24.0
    boundaries.append(end)
    return boundaries


def _simulate(request: EvaluationRequest) -> tuple[tuple[NamedQuantity, ...], NumericalDiagnostics]:
    x, modes = _inputs(request)
    hydraulics = _hydraulics(request, x)
    rhs = _rhs(x, modes)
    boundaries = _segments(x)
    end = boundaries[-1]

    state: tuple[float, ...] = (x["initial_biomass"], x["initial_nitrogen"], x["initial_dissolved_oxygen"], 0.0)
    harvested = [0.0, 0.0, 0.0]  # biomass, dissolved N, dissolved O2 removed with harvested broth
    fed_nitrogen = fed_oxygen = 0.0
    minimum = list(state[:3])
    maximum_oxygen = state[2]
    biomass_time_integral = 0.0
    evaluations = 0
    for start, stop in zip(boundaries, boundaries[1:], strict=False):
        count = max(2, math.ceil((stop - start) * _SAMPLES_PER_HOUR) + 1)
        grid = [start + (stop - start) * i / (count - 1) for i in range(count)]
        solution = integrate_ode(rhs, state, grid, rtol=1e-8, atol=1e-10)
        evaluations += solution.diagnostics.iterations or 0
        if not solution.success:
            raise EvaluationRefusal("did_not_converge", "cvode_failed", solution.message)
        states = solution.states
        step = grid[1] - grid[0]
        biomass_time_integral += step * sum(0.5 * (a[0] + b[0]) for a, b in zip(states, states[1:], strict=False))
        minimum = [min(minimum[i], *(row[i] for row in states)) for i in range(3)]
        maximum_oxygen = max(maximum_oxygen, *(row[2] for row in states))
        state = states[-1]
        if stop < end:  # harvest and replenish between segments; CVODE restarts from the new state
            fraction = x["harvest_fraction"]
            harvested = [total + fraction * value for total, value in zip(harvested, state[:3], strict=True)]
            fed_nitrogen += fraction * x["medium_nitrogen"]
            fed_oxygen += fraction * x["oxygen_saturation"]
            state = (
                state[0] * (1.0 - fraction),
                state[1] * (1.0 - fraction) + fraction * x["medium_nitrogen"],
                state[2] * (1.0 - fraction) + fraction * x["oxygen_saturation"],
                state[3],
            )
    if modes["nitrogen_response"] == "replete" and minimum[1] <= _NEGATIVE_TOLERANCE:
        raise EvaluationRefusal(
            "outside_validity_domain",
            "nitrogen_exhausted",
            "dissolved nitrogen was exhausted; the nitrogen-replete assumption does not hold",
        )
    if minimum[0] < -_NEGATIVE_TOLERANCE or minimum[1] < -_NEGATIVE_TOLERANCE:
        raise EvaluationRefusal("numerical_error", "negative_state", "integrated biomass or nitrogen became negative")
    if minimum[2] < -_NEGATIVE_TOLERANCE:
        raise EvaluationRefusal(
            "outside_validity_domain",
            "oxygen_depleted",
            "dissolved O2 reached zero; biomass loss is not O2-limited in this model",
        )

    biomass, nitrogen, oxygen, degassed = state
    quota = x["biomass_nitrogen_fraction"]
    produced = biomass + harvested[0] - x["initial_biomass"]  # net biomass formed over the run
    nitrogen_in = x["initial_nitrogen"] + quota * x["initial_biomass"] + fed_nitrogen
    nitrogen_out = nitrogen + quota * biomass + harvested[1] + quota * harvested[0]
    oxygen_in = x["initial_dissolved_oxygen"] + fed_oxygen + x["oxygen_yield"] * produced
    oxygen_out = oxygen + harvested[2] + degassed
    days = x["duration"]
    nitrogen_error, oxygen_error = abs(nitrogen_in - nitrogen_out), abs(oxygen_in - oxygen_out)
    return quantities(
        {
            "final_biomass": (biomass, "kg/m3"),
            "mean_biomass": (biomass_time_integral / end, "kg/m3"),
            "harvested_biomass": (harvested[0], "kg/m3"),
            "volumetric_productivity": (produced / days, "kg/(m**3*d)"),
            "final_nitrogen": (nitrogen, "kg/m3"),
            "min_nitrogen": (minimum[1], "kg/m3"),
            "nitrogen_balance_error": (nitrogen_error, "kg/m3"),
            "final_dissolved_oxygen": (oxygen, "kg/m3"),
            "max_dissolved_oxygen": (maximum_oxygen, "kg/m3"),
            "max_oxygen_saturation_ratio": (maximum_oxygen / x["oxygen_saturation"], "1"),
            "degassed_oxygen": (degassed, "kg/m3"),
            "oxygen_balance_error": (oxygen_error, "kg/m3"),
            "reynolds_number": (hydraulics["reynolds_number"], "1"),
            "pressure_drop": (hydraulics["pressure_drop"], "Pa"),
            "pumping_power": (hydraulics["pumping_power"], "W"),
        }
    ), NumericalDiagnostics(converged=True, iterations=evaluations, final_residual=max(nitrogen_error, oxygen_error))
