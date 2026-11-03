"""Deterministic fidelity escalation attached to 108 study points."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.jarvis_context_models import FrozenContract
from app.modules.engineering.evaluator_contracts import (
    EngineeringEvaluator,
    EvaluationRequest,
    EvaluationResult,
    validate_evaluation_result,
)
from app.modules.engineering.evidence_contracts import FidelityTier, fidelity_rank
from app.modules.engineering.refs import EvaluationRequestRef, Quantity, StudyRef
from app.modules.engineering.studies import StudyDefinition, StudyPoint, StudyRun
from app.modules.process_stack._common import magnitude

EscalationReason = Literal[
    "decision_margin", "outside_validity_domain", "lower_fidelity_failure", "operator_request"
]


class DecisionMargin(FrozenContract):
    """Escalate when an output is within ``margin`` of a declared decision boundary."""

    output: str
    boundary: Quantity
    margin: Quantity


class EscalationPolicy(FrozenContract):
    decision_margins: tuple[DecisionMargin, ...] = ()
    operator_requested_points: tuple[int, ...] = ()
    target_fidelity: FidelityTier | None = None


class OutputDiscrepancy(FrozenContract):
    output: str
    lower: Quantity
    higher: Quantity
    delta: Quantity


class EscalatedPointEvidence(FrozenContract):
    study_point_index: int
    original_request_ref: EvaluationRequestRef
    reason: EscalationReason
    evaluator_id: str | None = None
    requested_fidelity: FidelityTier | None = None
    status: Literal["succeeded", "failed", "refused", "cancelled", "deadline_exceeded", "escalation_unavailable"]
    status_reason: str | None = None
    evaluation: EvaluationResult | None = None
    discrepancies: tuple[OutputDiscrepancy, ...] = ()


class MultifidelityStudyRun(FrozenContract):
    """Supplemental point evidence for the same Study; the primary point is preserved."""

    study_ref: StudyRef
    base_run_digest: str
    points: tuple[EscalatedPointEvidence, ...]
    content_digest: str


def _output_map(result: EvaluationResult) -> dict[str, Quantity]:
    return {item.name: item.value for item in result.outputs}


def _reason(point: StudyPoint, policy: EscalationPolicy) -> EscalationReason | None:
    if point.index in policy.operator_requested_points:
        return "operator_request"
    if point.evaluation is None or point.status != "succeeded":
        return "lower_fidelity_failure"
    outputs = _output_map(point.evaluation)
    for threshold in policy.decision_margins:
        output = outputs.get(threshold.output)
        if output is None:
            continue
        margin = magnitude(threshold.margin, output.unit)
        boundary = magnitude(threshold.boundary, output.unit)
        if abs(magnitude(output, output.unit) - boundary) <= margin:
            return "decision_margin"
    validity = point.evaluation.validity
    if validity is not None:
        # Bounds may name inputs or evaluator outputs; an unverifiable bound fails safe to escalation.
        known = {item.name: item.value for item in point.inputs} | outputs
        for bound in validity.domain:
            value = known.get(bound.variable)
            if value is None:
                return "outside_validity_domain"
            scalar = magnitude(value, value.unit)
            if bound.lower is not None and scalar < magnitude(bound.lower, value.unit):
                return "outside_validity_domain"
            if bound.upper is not None and scalar > magnitude(bound.upper, value.unit):
                return "outside_validity_domain"
    return None


def _discrepancies(lower: EvaluationResult | None, higher: EvaluationResult) -> tuple[OutputDiscrepancy, ...]:
    if lower is None or lower.status != "succeeded":
        return ()
    low = _output_map(lower)
    high = _output_map(higher)
    rows = []
    for name in sorted(low.keys() & high.keys()):
        left, right = low[name], high[name]
        right_value = magnitude(right, left.unit)
        rows.append(OutputDiscrepancy(
            output=name, lower=left, higher=right,
            delta=Quantity(value=right_value - left.value, unit=left.unit),
        ))
    return tuple(rows)


def escalate_study(
    definition: StudyDefinition,
    run: StudyRun,
    evaluators: tuple[EngineeringEvaluator, ...],
    policy: EscalationPolicy,
) -> MultifidelityStudyRun:
    """Evaluate triggered points at the next available higher fidelity, preserving base evidence."""
    if run.study_ref != definition.study_ref or run.evaluator_id != definition.evaluator_id:
        raise ValueError("study definition and run do not match")
    lower_fidelity = run.fidelity
    rows: list[EscalatedPointEvidence] = []
    candidates = []
    for evaluator in evaluators:
        descriptor = evaluator.descriptor()
        if lower_fidelity is None or fidelity_rank(descriptor.fidelity) <= fidelity_rank(lower_fidelity):
            continue
        if policy.target_fidelity is not None and fidelity_rank(descriptor.fidelity) > fidelity_rank(policy.target_fidelity):
            continue
        candidates.append((evaluator, descriptor))
    candidates.sort(key=lambda item: fidelity_rank(item[1].fidelity))

    for point in run.points:
        reason = _reason(point, policy)
        if reason is None:
            continue
        selected = None
        unavailable = None
        for evaluator, descriptor in candidates:
            availability = evaluator.availability()
            if availability.evaluator_id != descriptor.evaluator_id:
                raise ValueError("evaluator availability does not match descriptor")
            if availability.state == "available":
                selected = (evaluator, descriptor)
                break
            unavailable = availability.reason_code or availability.state
        if selected is None:
            rows.append(EscalatedPointEvidence(
                study_point_index=point.index, original_request_ref=point.request_ref, reason=reason,
                status="escalation_unavailable", status_reason=unavailable or "NO_HIGHER_FIDELITY_EVALUATOR",
            ))
            continue
        evaluator, descriptor = selected
        now = datetime.now(UTC)
        request_ref = EvaluationRequestRef(
            authority_owner=definition.study_ref.authority_owner,
            object_id=f"{point.request_ref.object_id}/fidelity-{descriptor.fidelity}",
            workspace_id=definition.study_ref.workspace_id, revision=definition.study_ref.revision,
        )
        request = EvaluationRequest(
            request_ref=request_ref, evaluator_id=descriptor.evaluator_id, subject_ref=definition.subject_ref,
            study_ref=definition.study_ref, inputs=point.inputs, backend_options=definition.backend_options,
            requested_fidelity=descriptor.fidelity, requested_at=now,
            deadline_at=now + timedelta(seconds=definition.deadline_per_point_s),
        )
        try:
            result = evaluator.evaluate(request)
            validate_evaluation_result(request, result)
        except Exception:
            rows.append(EscalatedPointEvidence(
                study_point_index=point.index, original_request_ref=point.request_ref, reason=reason,
                evaluator_id=descriptor.evaluator_id, requested_fidelity=descriptor.fidelity,
                status="failed", status_reason="MULTIFIDELITY_EVALUATION_ERROR",
            ))
            continue
        rows.append(EscalatedPointEvidence(
            study_point_index=point.index, original_request_ref=point.request_ref, reason=reason,
            evaluator_id=descriptor.evaluator_id, requested_fidelity=descriptor.fidelity,
            status=result.status, evaluation=result,
            discrepancies=_discrepancies(point.evaluation, result),
        ))
    stable_points = []
    for row in rows:
        evaluation = row.evaluation
        stable_points.append({
            "study_point_index": row.study_point_index,
            "original_request_ref": row.original_request_ref.model_dump(mode="json"),
            "reason": row.reason, "evaluator_id": row.evaluator_id,
            "requested_fidelity": row.requested_fidelity, "status": row.status,
            "status_reason": row.status_reason,
            "evaluation": ({
                "evaluator_id": evaluation.evaluator_id, "backend_version": evaluation.backend_version,
                "status": evaluation.status, "failure": evaluation.failure.model_dump(mode="json") if evaluation.failure else None,
                "fidelity": evaluation.fidelity, "outputs": [item.model_dump(mode="json") for item in evaluation.outputs],
                "validity": evaluation.validity.model_dump(mode="json") if evaluation.validity else None,
                "qualification_record_ref": evaluation.qualification_record_ref.model_dump(mode="json")
                if evaluation.qualification_record_ref else None,
                "evidence_refs": [item.model_dump(mode="json") for item in evaluation.evidence_refs],
            } if evaluation else None),
            "discrepancies": [item.model_dump(mode="json") for item in row.discrepancies],
        })
    digest_data = {
        "study_ref": definition.study_ref.model_dump(mode="json"), "base_run_digest": run.content_digest,
        "points": stable_points,
    }
    return MultifidelityStudyRun(
        study_ref=definition.study_ref, base_run_digest=run.content_digest,
        points=tuple(rows), content_digest=canonical_digest(digest_data),
    )
