"""Pure-fluid thermophysical properties owned by CoolProp (103 role: properties)."""

from __future__ import annotations

import math
from typing import Final

from app.modules.engineering.evaluator_contracts import (
    EvaluationRequest,
    EvaluationResult,
    EvaluatorAvailability,
    EvaluatorDescriptor,
    NamedQuantity,
    NumericalDiagnostics,
)
from app.modules.process_stack._common import (
    EvaluationRefusal,
    descriptor_version,
    evaluate_with,
    import_availability,
    option,
    quantities,
    required_inputs,
)

EVALUATOR_ID: Final = "coolprop.pure_fluid_properties"
# Closed set: pure/pseudo-pure fluids the culture-loop models use; brine and mixtures are not qualified here (103).
FLUIDS: Final = ("Water", "CO2", "Nitrogen", "Oxygen", "Air")
_OUTPUTS: Final = (
    ("density", "D", "kg/m3"),
    ("dynamic_viscosity", "V", "Pa*s"),
    ("thermal_conductivity", "L", "W/(m*K)"),
    ("specific_heat_cp", "C", "J/(kg*K)"),
)


class CoolPropPropertyEvaluator:
    """Single-phase properties at (T, p); two-phase and out-of-EOS-range states are refused."""

    def descriptor(self) -> EvaluatorDescriptor:
        return EvaluatorDescriptor(
            evaluator_id=EVALUATOR_ID, backend_kind="property_package", backend_name="CoolProp",
            backend_version=descriptor_version("CoolProp"), fidelity="steady_state_detailed",
            capabilities=("pure_fluid_properties", "single_phase"),
        )

    def availability(self) -> EvaluatorAvailability:
        return import_availability(EVALUATOR_ID, "CoolProp.CoolProp", lambda: descriptor_version("CoolProp"))

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        return evaluate_with(self, request, lambda: _properties(request))


def _properties(request: EvaluationRequest) -> tuple[tuple[NamedQuantity, ...], NumericalDiagnostics]:
    from CoolProp.CoolProp import PhaseSI, PropsSI

    if request.subject_ref.object_type not in {"material_state", "property_basis"}:
        raise EvaluationRefusal("unsupported_request", "subject_unsupported", "subject must be a material state")
    fluid = option(request, "fluid", FLUIDS, "Water")
    state = required_inputs(request, {"temperature": "K", "pressure": "Pa"})
    temperature, pressure = state["temperature"], state["pressure"]
    # CoolProp extrapolates silently beyond its EOS limits; the limits are the validity domain.
    t_min, t_max, p_max = (PropsSI(key, fluid) for key in ("Tmin", "Tmax", "pmax"))
    if not (t_min <= temperature <= t_max and 0.0 < pressure <= p_max):
        raise EvaluationRefusal(
            "outside_validity_domain", "coolprop_eos_range",
            f"{fluid} EOS range T=[{t_min}, {t_max}] K, p<= {p_max} Pa",
        )
    phase = PhaseSI("T", temperature, "P", pressure, fluid)
    if phase in {"twophase", "unknown", ""}:
        raise EvaluationRefusal("outside_validity_domain", "coolprop_phase_unsupported", f"phase {phase!r}")
    try:
        values = {name: (float(PropsSI(key, "T", temperature, "P", pressure, fluid)), unit)
                  for name, key, unit in _OUTPUTS}
    except ValueError as exc:  # CoolProp reports unsupported state/property combinations this way
        raise EvaluationRefusal("outside_validity_domain", "coolprop_state_invalid", str(exc)) from exc
    if not all(math.isfinite(value) for value, _unit in values.values()):
        raise EvaluationRefusal("numerical_error", "coolprop_non_finite", f"non-finite property at {phase}")
    return quantities(values), NumericalDiagnostics(converged=True)
