"""Deterministic linear rigid-body motion and quasi-static mooring evaluator."""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Final

from app.modules.engineering.evaluator_contracts import (
    EvaluationRequest,
    EvaluationResult,
    EvaluatorAvailability,
    EvaluatorDescriptor,
    NamedQuantity,
    NumericalDiagnostics,
    ResultCheck,
)
from app.modules.process_stack._common import EvaluationRefusal, evaluate_with, magnitude, quantities

EVALUATOR_ID: Final = "marine.rigid_body_waves"
MODEL_VERSION: Final = "1.0"


class RigidBodyWavesEvaluator:
    def descriptor(self) -> EvaluatorDescriptor:
        return EvaluatorDescriptor(
            evaluator_id=EVALUATOR_ID,
            backend_kind="specialist",
            backend_name="marine_motion",
            backend_version=MODEL_VERSION,
            fidelity="screening",
            capabilities=("rigid_body_motion", "wave_rao", "quasi_static_mooring"),
            required_checks=("FINITE_RESPONSE", "POSITIVE_STIFFNESS"),
        )

    def availability(self) -> EvaluatorAvailability:
        return EvaluatorAvailability(
            evaluator_id=EVALUATOR_ID,
            state="available",
            checked_at=datetime.now(UTC),
            backend_version=MODEL_VERSION,
        )

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        result = evaluate_with(self, request, lambda: _evaluate(request))
        if result.status != "succeeded":
            return result

        outputs = {item.name: item.value.value for item in result.outputs}
        finite = all(math.isfinite(value) for value in outputs.values())
        heave_k, surge_k, pitch_k = _stiffness_values(request)
        checks = (
            ResultCheck(
                id="FINITE_RESPONSE",
                family="numerical",
                status="pass" if finite else "fail",
                actual=max((abs(value) for value in outputs.values()), default=0.0),
                limit=0.0,
                unit="1",
                hint="All rigid-body motion outputs must be finite.",
                source="physical_limit",
            ),
            ResultCheck(
                id="POSITIVE_STIFFNESS",
                family="conservation",
                status="pass" if heave_k > 0.0 and surge_k >= 0.0 and pitch_k > 0.0 else "fail",
                actual=min(heave_k, pitch_k),
                limit=0.0,
                unit="N/m",
                hint="Heave and pitch restoring stiffness must be positive.",
                source="physical_limit",
            ),
        )
        numerical = result.numerical.model_copy(update={"converged": finite})
        return result.model_copy(update={"checks": checks, "numerical": numerical})


INPUT_UNITS: Final[Mapping[str, str]] = {
    "mass_kg": "kg",
    "added_mass_surge_kg": "kg",
    "added_mass_heave_kg": "kg",
    "pitch_inertia_kg_m2": "kg*m**2",
    "waterplane_area_m2": "m2",
    "gm_pitch_m": "m",
    "damping_surge_n_s_m": "N*s/m",
    "damping_heave_n_s_m": "N*s/m",
    "damping_pitch_n_m_s_rad": "N*m*s/rad",
    "mooring_kx_n_m": "N/m",
    "mooring_kz_n_m": "N/m",
    "wave_amplitude_m": "m",
    "wave_period_s": "s",
    "water_density_kg_m3": "kg/m3",
    "gravity_m_s2": "m/s2",
}

DEFAULTS: Final[Mapping[str, float]] = {
    "water_density_kg_m3": 1025.0,
    "gravity_m_s2": 9.80665,
}


def _stiffness_values(request: EvaluationRequest) -> tuple[float, float, float]:
    given = {item.name: item.value for item in request.inputs}

    def value(name: str, unit: str) -> float:
        if name in given:
            return magnitude(given[name], unit)
        return DEFAULTS[name]

    rho = value("water_density_kg_m3", "kg/m3")
    g = value("gravity_m_s2", "m/s2")
    mass = value("mass_kg", "kg")
    aw = value("waterplane_area_m2", "m2")
    gm = value("gm_pitch_m", "m")
    kz = value("mooring_kz_n_m", "N/m")
    kx = value("mooring_kx_n_m", "N/m")
    displaced_volume = mass / rho
    heave_k = rho * g * aw + kz
    pitch_k = rho * g * displaced_volume * gm
    return heave_k, kx, pitch_k


def _evaluate(request: EvaluationRequest) -> tuple[tuple[NamedQuantity, ...], NumericalDiagnostics]:
    given = {item.name: item.value for item in request.inputs}
    unknown = sorted(set(given) - set(INPUT_UNITS))
    missing = sorted(set(INPUT_UNITS) - set(given) - set(DEFAULTS))
    if unknown or missing:
        raise EvaluationRefusal("invalid_input", "input_names_invalid", f"missing={missing} unknown={unknown}")

    values: dict[str, float] = {}
    for name, unit in INPUT_UNITS.items():
        if name in given:
            values[name] = magnitude(given[name], unit)
        else:
            values[name] = DEFAULTS[name]

    positive = (
        "mass_kg",
        "pitch_inertia_kg_m2",
        "waterplane_area_m2",
        "gm_pitch_m",
        "wave_period_s",
        "water_density_kg_m3",
        "gravity_m_s2",
    )
    nonnegative = (
        "added_mass_surge_kg",
        "added_mass_heave_kg",
        "damping_surge_n_s_m",
        "damping_heave_n_s_m",
        "damping_pitch_n_m_s_rad",
        "mooring_kx_n_m",
        "mooring_kz_n_m",
        "wave_amplitude_m",
    )
    bad_positive = sorted(name for name in positive if not math.isfinite(values[name]) or values[name] <= 0.0)
    bad_nonnegative = sorted(
        name for name in nonnegative if not math.isfinite(values[name]) or values[name] < 0.0
    )
    if bad_positive:
        raise EvaluationRefusal("invalid_input", "input_non_positive", f"must be positive: {bad_positive}")
    if bad_nonnegative:
        raise EvaluationRefusal("invalid_input", "input_negative", f"must be non-negative: {bad_nonnegative}")

    rho = values["water_density_kg_m3"]
    g = values["gravity_m_s2"]
    mass = values["mass_kg"]
    aw = values["waterplane_area_m2"]
    amplitude = values["wave_amplitude_m"]
    period = values["wave_period_s"]
    omega = 2.0 * math.pi / period
    wave_number = omega * omega / g
    displaced_volume = mass / rho

    surge_total_mass = mass + values["added_mass_surge_kg"]
    surge_stiffness = values["mooring_kx_n_m"]
    surge_damping = values["damping_surge_n_s_m"]
    surge_force = rho * g * displaced_volume * wave_number * amplitude
    surge_h = 1.0 / complex(surge_stiffness - omega * omega * surge_total_mass, omega * surge_damping)
    surge_rao = abs(surge_h * surge_force) / amplitude if amplitude > 0.0 else 0.0
    surge_amplitude = abs(surge_h * surge_force)

    heave_total_mass = mass + values["added_mass_heave_kg"]
    heave_stiffness = rho * g * aw + values["mooring_kz_n_m"]
    heave_damping = values["damping_heave_n_s_m"]
    heave_force = rho * g * aw * amplitude
    heave_h = 1.0 / complex(heave_stiffness - omega * omega * heave_total_mass, omega * heave_damping)
    heave_rao = abs(heave_h * heave_force) / amplitude if amplitude > 0.0 else 0.0
    heave_amplitude = abs(heave_h * heave_force)
    heave_natural_period = 2.0 * math.pi * math.sqrt(heave_total_mass / heave_stiffness)

    pitch_inertia = values["pitch_inertia_kg_m2"]
    pitch_stiffness = rho * g * displaced_volume * values["gm_pitch_m"]
    pitch_damping = values["damping_pitch_n_m_s_rad"]
    pitch_moment = rho * g * displaced_volume * values["gm_pitch_m"] * wave_number * amplitude
    pitch_h = 1.0 / complex(pitch_stiffness - omega * omega * pitch_inertia, omega * pitch_damping)
    pitch_rao = abs(pitch_h * pitch_moment) / amplitude if amplitude > 0.0 else 0.0
    pitch_amplitude = abs(pitch_h * pitch_moment)
    pitch_natural_period = 2.0 * math.pi * math.sqrt(pitch_inertia / pitch_stiffness)

    heave_acceleration = abs(heave_h * heave_force) * omega * omega
    mooring_tension = surge_stiffness * surge_amplitude

    public = quantities({
        "heave_rao": (heave_rao, "1"),
        "surge_rao": (surge_rao, "1"),
        "pitch_rao": (pitch_rao, "rad/m"),
        "heave_amplitude": (heave_amplitude, "m"),
        "surge_amplitude": (surge_amplitude, "m"),
        "pitch_amplitude": (pitch_amplitude, "rad"),
        "heave_natural_period": (heave_natural_period, "s"),
        "pitch_natural_period": (pitch_natural_period, "s"),
        "heave_acceleration": (heave_acceleration, "m/s^2"),
        "mooring_tension": (mooring_tension, "N"),
    })
    return public, NumericalDiagnostics(converged=True)
