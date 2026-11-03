"""Direct 106 evaluator view of the same Tier-1 Process photobioreactor solver.

This is a headless scientific adapter, not an Engineering Studies catalogue entry.
The model card is selected by three exact backend options; all physical inputs
are named quantities. It delegates every calculation to the 168 unit evaluator.
"""

from __future__ import annotations

import math
import time
from datetime import UTC, datetime
from typing import Final

from app.modules.ai.jarvis_context_models import SourceRef
from app.modules.engineering.evaluator_contracts import (
    EvaluationRequest,
    EvaluationResult,
    EvaluatorAvailability,
    EvaluatorDescriptor,
    NumericalDiagnostics,
)
from app.modules.engineering.evidence_contracts import validity_content_digest
from app.modules.engineering.refs import ValidityEnvelopeRef
from app.modules.process_stack._common import (
    EvaluationRefusal,
    evaluate_with,
    import_availability,
    magnitude,
    quantities,
)
from app.modules.process_stack.pbr_unit import EVALUATOR_ID, MODEL_VERSION, PARAMETERS, PbrFailure, evaluate_pbr

_UNIT_INPUTS: Final = {
    "tube_inner_diameter": "m", "tube_length": "m", "tube_count": "1", "liquid_velocity": "m/s",
    "pump_efficiency": "percent", "baffle_friction_multiplier": "1", "oxygen_kla": "1/s",
    "oxygen_saturation": "kg/m3", "peak_par": "umol/(m**2*s)", "photoperiod": "s",
    "diffuse_fraction": "1", "temperature_mean": "K", "temperature_amplitude": "K",
    "mass_flow": "kg/s", "inlet_density": "kg/m3", "inlet_temperature": "K",
    "biomass_in": "kg/m3", "nitrogen_in": "kg/m3", "oxygen_in": "kg/m3",
}
_PIN_OPTIONS: Final = {"card_id", "card_revision", "card_digest"}
_OUTPUTS: Final = {
    "biomass_mean": "kg/m3", "volumetric_productivity": "kg/(m3*d)",
    "lambda_h": "1/h", "hrt_d": "d", "circulation_flow_m3_h": "m3/h",
    "pass_transit_time_s": "s", "circulation_to_throughflow_ratio": "1",
    "pressure_drop": "Pa", "pumping_power": "W",
}


class PbrUnitT1Evaluator:
    """Unqualified reduced-order headless view; the Process unit remains authoritative."""

    def descriptor(self) -> EvaluatorDescriptor:
        return EvaluatorDescriptor(
            evaluator_id=EVALUATOR_ID, backend_kind="dynamic_simulator", backend_name="scikit-sundae",
            backend_version=MODEL_VERSION, fidelity="reduced_order",
            capabilities=("periodic_steady", "cylinder_light", "culture_growth", "loop_hydraulics"),
            qualification_record_ref=SourceRef(
                authority_owner="repository", object_type="scientific_qualification_record",
                object_id="scripts/benchmarks/pbr_reference/pbr_unit.v1.ledger.json", revision=MODEL_VERSION),
        )

    def availability(self) -> EvaluatorAvailability:
        return import_availability(EVALUATOR_ID, "sksundae", lambda: MODEL_VERSION)

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        result = evaluate_with(self, request, lambda: self._run(request))
        if result.status != "succeeded":
            return result
        validity = ValidityEnvelopeRef(
            authority_owner="process_stack", object_id=f"{EVALUATOR_ID}.{MODEL_VERSION}",
            workspace_id=request.request_ref.workspace_id, revision=MODEL_VERSION,
            qualification_status="unqualified", domain=())
        return result.model_copy(update={"validity": validity.model_copy(
            update={"content_digest": validity_content_digest(validity)})})

    def _run(self, request: EvaluationRequest):
        if request.subject_ref.object_type != "dynamic_model":
            raise EvaluationRefusal("unsupported_request", "subject_unsupported",
                                    "PBR headless evaluation needs a dynamic_model subject")
        if set(request.backend_options) != _PIN_OPTIONS or not all(
                isinstance(value, str) and value for value in request.backend_options.values()):
            raise EvaluationRefusal("invalid_input", "pbr_card_pin_required",
                                    "card_id, card_revision and card_digest are all required")
        given = {item.name: item.value for item in request.inputs}
        if set(given) != set(_UNIT_INPUTS):
            raise EvaluationRefusal("invalid_input", "pbr_input_names_invalid",
                                    f"missing={sorted(set(_UNIT_INPUTS) - set(given))} "
                                    f"unknown={sorted(set(given) - set(_UNIT_INPUTS))}")
        values = {name: magnitude(given[name], unit) for name, unit in _UNIT_INPUTS.items()}
        density = values["inlet_density"]
        if not math.isfinite(density) or density <= 0:
            raise EvaluationRefusal("invalid_input", "pbr_inlet_density_invalid", "inlet_density must be > 0")
        unit = {"type": "PhotobioreactorT1", "tag": "PBR-headless",
                "model": dict(request.backend_options),
                "params": {name: {"si": values[name]} for name in PARAMETERS}}
        inlet = {"mass_flow_kg_s": values["mass_flow"], "density_kg_m3": density,
                 "temperature_K": values["inlet_temperature"], "vapor_fraction": 0.0,
                 "culture": {name: values[f"{name}_in"] / density for name in ("biomass", "nitrogen", "oxygen")}}
        from app.modules.process_stack.mixed_runtime import JarvisUnitContext

        deadline = time.monotonic() + max(0.0, (request.deadline_at - datetime.now(UTC)).total_seconds())
        try:
            evaluation = evaluate_pbr(unit, inlet, JarvisUnitContext(
                inlet_density_kg_m3=density, deadline=deadline, cache={},
                workspace_id=request.request_ref.workspace_id))
        except PbrFailure as exc:
            raise EvaluationRefusal("invalid_input", exc.code, str(exc)) from exc
        reported = evaluation.result["reported"]
        outputs = quantities({name: (reported[name]["value"], unit_name)
                              for name, unit_name in _OUTPUTS.items() if reported[name]["value"] is not None})
        residual = max(abs(item["residual"]) for item in evaluation.result["numerics"]["periodicity"].values())
        return outputs, NumericalDiagnostics(converged=True, final_residual=residual,
                                             iterations=evaluation.result["numerics"]["map_evaluations"])
