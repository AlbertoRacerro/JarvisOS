"""Dissolved oxygen build-up along a plug-flow tube (in-house screening balance).

The classic tubular design rule L = u·(C_max − C_in)/R_O2 (Molina Grima et al. 2001):
constant volumetric oxygen production, no gas exchange inside the tube. Exceeding the
chosen outlet limit is a design outcome reported by the validity point, not a refusal.
"""

from __future__ import annotations

from datetime import UTC, datetime
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
    ValidityPoint,
    evaluate_card,
    quantities,
    required_inputs,
)

EVALUATOR_ID: Final = "bioprocess.tube_oxygen_buildup"
MODEL_VERSION: Final = "1.0"
CARD_CHECKS: Final = ("VALIDITY_RANGE", "FINITE_OUTPUTS")
ASSUMPTIONS_HINT: Final = "Plug flow, constant oxygen production rate, no transfer to gas in the tube."


class TubeOxygenBuildupEvaluator:
    """Outlet oxygen fraction and concentration, residence time, and longest tube under the limit."""

    def descriptor(self) -> EvaluatorDescriptor:
        return EvaluatorDescriptor(
            evaluator_id=EVALUATOR_ID, backend_kind="specialist", backend_name="oxygen_tube",
            backend_version=MODEL_VERSION, fidelity="screening",
            capabilities=("tube_oxygen_balance", "plug_flow"),
            required_checks=CARD_CHECKS,
        )

    def availability(self) -> EvaluatorAvailability:
        return EvaluatorAvailability(
            evaluator_id=EVALUATOR_ID, state="available", checked_at=datetime.now(UTC),
            backend_version=MODEL_VERSION,
        )

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        return evaluate_card(self, request, lambda: _tube_oxygen(request))


def _tube_oxygen(request: EvaluationRequest) -> tuple[tuple[NamedQuantity, ...], NumericalDiagnostics, ValidityPoint]:
    x = required_inputs(request, {
        "velocity": "m/s", "oxygen_production_rate": "mol/(m**3*s)", "saturation_concentration": "mol/m**3",
        "inlet_saturation_fraction": "1", "max_saturation_fraction": "1", "length": "m",
    })
    bad = sorted(
        name for name in ("velocity", "oxygen_production_rate", "saturation_concentration", "length")
        if x[name] <= 0.0
    )
    if bad:
        raise EvaluationRefusal("invalid_input", "input_non_positive", f"must be positive: {bad}")
    inlet, maximum = x["inlet_saturation_fraction"], x["max_saturation_fraction"]
    if inlet < 0.0:
        raise EvaluationRefusal("invalid_input", "input_negative", "inlet_saturation_fraction must be non-negative")
    if maximum <= inlet:
        raise EvaluationRefusal(
            "invalid_input", "saturation_fractions_invalid",
            "max_saturation_fraction must be greater than inlet_saturation_fraction",
        )
    velocity, rate = x["velocity"], x["oxygen_production_rate"]
    c_sat, length = x["saturation_concentration"], x["length"]
    outlet_fraction = inlet + rate * length / (velocity * c_sat)
    return quantities({
        "outlet_saturation_fraction": (outlet_fraction, "1"),
        "outlet_concentration": (outlet_fraction * c_sat, "mol/m**3"),
        "residence_time": (length / velocity, "s"),
        "max_length": (velocity * (maximum - inlet) * c_sat / rate, "m"),
    }), NumericalDiagnostics(converged=True), ValidityPoint(outlet_fraction, maximum, "1", ASSUMPTIONS_HINT)
