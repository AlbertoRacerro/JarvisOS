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
from app.modules.engineering.refs import (
    DomainBound,
    DynamicModelRef,
    EvaluationResultRef,
    Quantity,
    StudyRef,
    ValidityEnvelopeRef,
)
from app.modules.engineering.studies import (
    DesignVariable,
    StudyConstraint,
    StudyDefinition,
    StudyObjective,
    run_study,
)

EVALUATOR_ID = "study.fake"
WORKSPACE = "study-test"
SUBJECT = DynamicModelRef(authority_owner="test", object_id="subject", workspace_id=WORKSPACE, revision="1")


class FakeEvaluator:
    def descriptor(self) -> EvaluatorDescriptor:
        return EvaluatorDescriptor(evaluator_id=EVALUATOR_ID, backend_kind="specialist", backend_name="fake",
                                   backend_version="1", fidelity="screening")

    def availability(self) -> EvaluatorAvailability:
        return EvaluatorAvailability(evaluator_id=EVALUATOR_ID, state="available", backend_version="1",
                                     checked_at=datetime.now(UTC))

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        x = next(item.value.value for item in request.inputs if item.name == "x")
        result_ref = EvaluationResultRef(authority_owner="test", object_id=request.request_ref.object_id,
                                         workspace_id=WORKSPACE, revision="1")
        if x < 0.2:
            return EvaluationResult(result_ref=result_ref, request_ref=request.request_ref, evaluator_id=EVALUATOR_ID,
                                    backend_version="1", status="refused",
                                    failure=EvaluationFailure(category="invalid_input", backend_code="INPUT_EDGE"),
                                    fidelity="screening", completed_at=datetime.now(UTC))
        if x > 0.8:
            return EvaluationResult(result_ref=result_ref, request_ref=request.request_ref, evaluator_id=EVALUATOR_ID,
                                    backend_version="1", status="failed",
                                    failure=EvaluationFailure(category="did_not_converge", backend_code="NO_CONVERGENCE"),
                                    fidelity="screening", completed_at=datetime.now(UTC))
        validity = ValidityEnvelopeRef(authority_owner="test", object_id="validity", workspace_id=WORKSPACE,
                                       revision="1", qualification_status="unqualified")
        validity = validity.model_copy(update={"content_digest": validity_content_digest(validity)})
        return EvaluationResult(
            result_ref=result_ref, request_ref=request.request_ref, evaluator_id=EVALUATOR_ID,
            backend_version="1", status="succeeded", fidelity="screening", validity=validity,
            outputs=(NamedQuantity(name="cost", value=Quantity(value=x, unit="1")),
                     NamedQuantity(name="yield", value=Quantity(value=x, unit="1")),
                     NamedQuantity(name="limit", value=Quantity(value=2 * x, unit="1"))),
            completed_at=datetime.now(UTC),
        )


def _variable(lower: float = 0, upper: float = 1, step: float | None = None) -> DesignVariable:
    return DesignVariable(name="x", domain=DomainBound(variable="x", lower=Quantity(value=lower, unit="1"),
                                                         upper=Quantity(value=upper, unit="1")),
                          step=Quantity(value=step, unit="1") if step is not None else None)


def _definition(method: str = "monte_carlo", **updates) -> StudyDefinition:
    fields = {
        "study_ref": StudyRef(authority_owner="test", object_id="study", workspace_id=WORKSPACE, revision="1"),
        "evaluator_id": EVALUATOR_ID,
        "subject_ref": SUBJECT,
        "method": method,
        "variables": (_variable(),),
        "objectives": (StudyObjective(output="cost", sense="minimize"),),
        "seed": 42,
        "budget": 8,
        "sample_count": 8,
    }
    fields.update(updates)
    return StudyDefinition(**fields)


def test_seeded_monte_carlo_points_and_digest_are_reproducible() -> None:
    first = run_study(_definition(), FakeEvaluator())
    second = run_study(_definition(), FakeEvaluator())
    assert [point.inputs for point in first.points] == [point.inputs for point in second.points]
    assert first.content_digest == second.content_digest
    different = run_study(_definition(seed=43), FakeEvaluator())
    assert first.content_digest != different.content_digest


def test_seeded_latin_hypercube_repeats_exactly() -> None:
    definition = _definition(method="latin_hypercube")
    first = run_study(definition, FakeEvaluator())
    second = run_study(definition, FakeEvaluator())
    assert [point.inputs for point in first.points] == [point.inputs for point in second.points]
    assert first.content_digest == second.content_digest


def test_unavailable_evaluator_returns_a_study_record_without_points() -> None:
    class UnavailableEvaluator(FakeEvaluator):
        def availability(self) -> EvaluatorAvailability:
            return EvaluatorAvailability(evaluator_id=EVALUATOR_ID, state="disabled", reason_code="EVALUATOR_DISABLED",
                                         checked_at=datetime.now(UTC))

    run = run_study(_definition(), UnavailableEvaluator())
    assert run.status == "unavailable"
    assert run.points == ()
    assert run.failure is not None and run.failure.category == "not_available"


def test_refusals_and_failures_are_recorded_and_excluded() -> None:
    run = run_study(_definition(method="grid", variables=(_variable(step=0.2),), sample_count=6), FakeEvaluator())
    assert [point.status for point in run.points] == ["refused", "succeeded", "succeeded", "succeeded", "succeeded", "failed"]
    assert run.failed_count == 2
    assert run.feasible_count == 4
    assert run.qualification_status == "unqualified"


def test_pareto_filter_keeps_only_nondominated_feasible_points() -> None:
    objectives = (StudyObjective(output="cost", sense="minimize"),
                  StudyObjective(output="yield", sense="maximize"))
    run = run_study(_definition(method="grid", variables=(_variable(0.2, 0.8, 0.2),), objectives=objectives,
                                budget=4), FakeEvaluator())
    assert run.pareto_point_indices == (0, 1, 2, 3)


def test_constraints_drive_feasibility_and_output_uncertainty_summary() -> None:
    constraint = StudyConstraint(output="limit", operator="le", bound=Quantity(value=1.2, unit="1"))
    run = run_study(_definition(constraints=(constraint,)), FakeEvaluator())
    summary = next(item for item in run.output_summaries if item.output == "cost")
    feasible_costs = [point.evaluation.outputs[0].value.value for point in run.points
                      if point.feasible and point.evaluation is not None]
    assert run.feasible_count == len(feasible_costs)
    assert summary.mean.value == sum(feasible_costs) / len(feasible_costs)
    assert summary.percentile_05.value <= summary.mean.value <= summary.percentile_95.value
    assert all(not point.feasible for point in run.points if point.evaluation and point.status == "succeeded"
               and next(value.value.value for value in point.evaluation.outputs if value.name == "limit") > 1.2)
