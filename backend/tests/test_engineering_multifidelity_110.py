from datetime import UTC, datetime

from app.modules.engineering.evaluator_contracts import (
    EvaluationFailure,
    EvaluationRequest,
    EvaluationResult,
    EvaluatorAvailability,
    EvaluatorDescriptor,
    NamedQuantity,
)
from app.modules.engineering.evidence_contracts import validity_content_digest
from app.modules.engineering.multifidelity import (
    DecisionMargin,
    EscalationPolicy,
    escalate_study,
)
from app.modules.engineering.refs import (
    DomainBound,
    DynamicModelRef,
    EvaluationResultRef,
    Quantity,
    StudyRef,
    ValidityEnvelopeRef,
)
from app.modules.engineering.studies import DesignVariable, StudyDefinition, StudyObjective, run_study

WS = "multi-test"
LOWER_ID = "multi.lower"
HIGH_ID = "multi.higher"
SUBJECT = DynamicModelRef(authority_owner="test", object_id="model", workspace_id=WS, revision="1")


class FakeEvaluator:
    def __init__(self, evaluator_id: str, fidelity: str, *, value: float = 1.0, available: bool = True,
                 fail: bool = False, raises: bool = False, domain: tuple[DomainBound, ...] = ()) -> None:
        self.evaluator_id = evaluator_id
        self.fidelity = fidelity
        self.value = value
        self.available = available
        self.fail = fail
        self.raises = raises
        self.domain = domain

    def descriptor(self) -> EvaluatorDescriptor:
        return EvaluatorDescriptor(evaluator_id=self.evaluator_id, backend_kind="specialist", backend_name="fake",
                                   backend_version="1", fidelity=self.fidelity)

    def availability(self) -> EvaluatorAvailability:
        return EvaluatorAvailability(
            evaluator_id=self.evaluator_id,
            state="available" if self.available else "not_installed",
            backend_version="1" if self.available else None,
            reason_code=None if self.available else "TOOL_BINARY_MISSING",
            checked_at=datetime.now(UTC),
        )

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        ref = EvaluationResultRef(authority_owner="test", object_id=request.request_ref.object_id,
                                  workspace_id=WS, revision="1")
        if self.raises:
            raise RuntimeError("backend crashed")
        if self.fail:
            return EvaluationResult(result_ref=ref, request_ref=request.request_ref, evaluator_id=self.evaluator_id,
                                    backend_version="1", status="failed",
                                    failure=EvaluationFailure(category="did_not_converge", backend_code="NO_CONVERGENCE"),
                                    fidelity=self.fidelity, completed_at=datetime.now(UTC))
        validity = ValidityEnvelopeRef(authority_owner="test", object_id="validity", workspace_id=WS,
                                       revision="1", domain=self.domain, qualification_status="unqualified")
        validity = validity.model_copy(update={"content_digest": validity_content_digest(validity)})
        return EvaluationResult(
            result_ref=ref, request_ref=request.request_ref, evaluator_id=self.evaluator_id, backend_version="1",
            status="succeeded", fidelity=self.fidelity,
            outputs=(NamedQuantity(name="pressure_drop", value=Quantity(value=self.value, unit="Pa")),),
            validity=validity, completed_at=datetime.now(UTC),
        )


def _definition() -> StudyDefinition:
    return StudyDefinition(
        study_ref=StudyRef(authority_owner="test", object_id="study", workspace_id=WS, revision="1"),
        evaluator_id=LOWER_ID, subject_ref=SUBJECT, method="grid",
        variables=(DesignVariable(name="x", domain=DomainBound(
            variable="x", lower=Quantity(value=1, unit="1"), upper=Quantity(value=1, unit="1")),
            step=Quantity(value=1, unit="1")),),
        objectives=(StudyObjective(output="pressure_drop", sense="minimize"),), seed=1, budget=1,
        sample_count=1,
    )


def _run():
    return run_study(_definition(), FakeEvaluator(LOWER_ID, "screening", value=10.0))


def test_reasons_include_margin_operator_and_lower_failure() -> None:
    definition = _definition()
    run = _run()
    result = escalate_study(definition, run, (FakeEvaluator(HIGH_ID, "field_resolved"),), EscalationPolicy(
        decision_margins=(DecisionMargin(output="pressure_drop", boundary=Quantity(value=10.2, unit="Pa"),
                                          margin=Quantity(value=0.5, unit="Pa")),),
    ))
    assert result.points[0].reason == "decision_margin"
    assert result.points[0].status == "succeeded"
    operator = escalate_study(definition, run, (FakeEvaluator(HIGH_ID, "field_resolved"),),
                              EscalationPolicy(operator_requested_points=(0,)))
    assert operator.points[0].reason == "operator_request"
    failed = run_study(definition, FakeEvaluator(LOWER_ID, "screening", fail=True))
    lower_failure = escalate_study(definition, failed, (FakeEvaluator(HIGH_ID, "field_resolved"),),
                                   EscalationPolicy())
    assert lower_failure.points[0].reason == "lower_fidelity_failure"


def test_unavailable_and_high_fidelity_failure_are_recorded() -> None:
    definition, run = _definition(), _run()
    unavailable = escalate_study(definition, run, (FakeEvaluator(HIGH_ID, "field_resolved", available=False),),
                                 EscalationPolicy(operator_requested_points=(0,)))
    assert unavailable.points[0].status == "escalation_unavailable"
    failed = escalate_study(definition, run, (FakeEvaluator(HIGH_ID, "field_resolved", fail=True),),
                            EscalationPolicy(operator_requested_points=(0,)))
    assert failed.points[0].status == "failed"
    assert failed.points[0].evaluation is not None and failed.points[0].evaluation.failure is not None


def test_supplement_is_linked_and_preserves_fidelity_validity_and_qualification() -> None:
    definition, run = _definition(), _run()
    base = run.points[0].evaluation
    assert base is not None and base.validity is not None
    high = escalate_study(definition, run, (FakeEvaluator(HIGH_ID, "field_resolved", value=12),),
                          EscalationPolicy(operator_requested_points=(0,)))
    point = high.points[0]
    assert high.study_ref == run.study_ref == definition.study_ref
    assert point.original_request_ref == run.points[0].request_ref
    assert point.evaluation is not None
    assert point.evaluation.fidelity == "field_resolved"
    assert point.evaluation.validity is not None
    assert point.evaluation.validity.qualification_status == "unqualified"
    assert run.points[0].evaluation == base
    assert point.discrepancies[0].delta == Quantity(value=2, unit="Pa")


def test_multifidelity_content_digest_repeats() -> None:
    definition, run = _definition(), _run()
    policy = EscalationPolicy(operator_requested_points=(0,))
    evaluator = FakeEvaluator(HIGH_ID, "field_resolved", value=12)
    first = escalate_study(definition, run, (evaluator,), policy)
    second = escalate_study(definition, run, (evaluator,), policy)
    assert first.content_digest == second.content_digest


def test_outside_validity_domain_escalates_and_in_domain_points_are_not_escalated() -> None:
    definition = _definition()
    inside = run_study(definition, FakeEvaluator(LOWER_ID, "screening", domain=(DomainBound(
        variable="x", lower=Quantity(value=0, unit="1"), upper=Quantity(value=2, unit="1")),)))
    higher = (FakeEvaluator(HIGH_ID, "field_resolved"),)
    assert escalate_study(definition, inside, higher, EscalationPolicy()).points == ()
    outside = run_study(definition, FakeEvaluator(LOWER_ID, "screening", domain=(DomainBound(
        variable="x", lower=Quantity(value=2, unit="1"), upper=Quantity(value=3, unit="1")),)))
    assert escalate_study(definition, outside, higher, EscalationPolicy()).points[0].reason == "outside_validity_domain"
    unverifiable = run_study(definition, FakeEvaluator(LOWER_ID, "screening", domain=(DomainBound(
        variable="reynolds_number", lower=Quantity(value=0, unit="1"), upper=Quantity(value=2000, unit="1")),)))
    assert escalate_study(definition, unverifiable, higher, EscalationPolicy()).points[0].reason == "outside_validity_domain"
    by_output = run_study(definition, FakeEvaluator(LOWER_ID, "screening", domain=(DomainBound(
        variable="pressure_drop", lower=Quantity(value=0, unit="Pa"), upper=Quantity(value=5, unit="Pa")),)))
    assert escalate_study(definition, by_output, higher, EscalationPolicy()).points == ()


def test_target_fidelity_caps_escalation_and_backend_exception_is_recorded() -> None:
    definition, run = _definition(), _run()
    capped = escalate_study(definition, run, (FakeEvaluator(HIGH_ID, "field_resolved"),), EscalationPolicy(
        operator_requested_points=(0,), target_fidelity="reduced_order"))
    assert capped.points[0].status == "escalation_unavailable"
    assert capped.points[0].status_reason == "NO_HIGHER_FIDELITY_EVALUATOR"
    crashed = escalate_study(definition, run, (FakeEvaluator(HIGH_ID, "field_resolved", raises=True),),
                             EscalationPolicy(operator_requested_points=(0,)))
    assert crashed.points[0].status == "failed"
    assert crashed.points[0].status_reason == "MULTIFIDELITY_EVALUATION_ERROR"
    assert run.points[0].evaluation is not None
