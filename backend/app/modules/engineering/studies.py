"""Deterministic inner-loop studies over the shared evaluator protocol."""

from __future__ import annotations

import itertools
import math
from datetime import UTC, datetime, timedelta
from typing import Literal

import numpy as np
from pydantic import Field, model_validator

from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.jarvis_context_models import ContractId, FrozenContract
from app.modules.engineering.evaluator_contracts import (
    AvailabilityState,
    EngineeringEvaluator,
    EvaluationFailure,
    EvaluationRequest,
    EvaluationResult,
    EvaluationStatus,
    EvaluationSubjectRef,
    NamedQuantity,
    validate_evaluation_result,
)
from app.modules.engineering.evidence_contracts import FidelityTier
from app.modules.engineering.refs import (
    DomainBound,
    EvaluationRequestRef,
    Quantity,
    StudyRef,
    UncertaintyBound,
    VariableName,
)
from app.modules.process_stack._common import magnitude

StudyMethod = Literal["grid", "latin_hypercube", "monte_carlo", "single_objective_opt"]
StudyStatus = Literal["succeeded", "partial", "unavailable", "stopped_unhealthy", "failed"]


class DesignVariable(FrozenContract):
    name: VariableName
    domain: DomainBound
    step: Quantity | None = None
    distribution: Literal["uniform", "normal", "triangular"] = "uniform"
    uncertainty: UncertaintyBound | None = None
    mode: Quantity | None = None

    @model_validator(mode="after")
    def validate_domain(self) -> DesignVariable:
        if self.domain.variable != self.name or self.domain.lower is None or self.domain.upper is None:
            raise ValueError("study variables require matching finite lower and upper bounds")
        if self.domain.lower.unit != self.domain.upper.unit:
            raise ValueError("study variable bounds must use the same unit")
        if self.step is not None and magnitude(self.step, self.domain.lower.unit) <= 0:
            raise ValueError("grid step must be positive")
        if self.distribution == "normal" and self.uncertainty is None:
            raise ValueError("normal distributions require an uncertainty bound")
        if self.mode is not None:
            mode = magnitude(self.mode, self.domain.lower.unit)
            if not self.domain.lower.value <= mode <= self.domain.upper.value:
                raise ValueError("triangular mode must lie within the variable bounds")
        return self


class StudyObjective(FrozenContract):
    output: VariableName
    sense: Literal["minimize", "maximize"]


class StudyConstraint(FrozenContract):
    output: VariableName
    operator: Literal["le", "ge", "eq"]
    bound: Quantity


class StudyDefinition(FrozenContract):
    study_ref: StudyRef
    evaluator_id: ContractId
    subject_ref: EvaluationSubjectRef
    method: StudyMethod
    variables: tuple[DesignVariable, ...] = Field(min_length=1, max_length=32)
    fixed_inputs: tuple[NamedQuantity, ...] = Field(default=(), max_length=64)
    objectives: tuple[StudyObjective, ...] = ()
    constraints: tuple[StudyConstraint, ...] = ()
    backend_options: dict[str, str | int | float | bool] = Field(default_factory=dict, max_length=32)
    seed: int
    budget: int = Field(ge=1, le=10000)
    sample_count: int = Field(default=16, ge=1, le=10000)
    deadline_per_point_s: float = Field(default=30.0, gt=0, le=3600)
    failure_breaker_count: int = Field(default=5, ge=1, le=100)

    @model_validator(mode="after")
    def validate_study(self) -> StudyDefinition:
        names = [variable.name for variable in self.variables]
        if len(names) != len(set(names)):
            raise ValueError("study variable names must be unique")
        if set(names) & {item.name for item in self.fixed_inputs}:
            raise ValueError("design variables cannot duplicate fixed inputs")
        if len({item.name for item in self.fixed_inputs}) != len(self.fixed_inputs):
            raise ValueError("fixed input names must be unique")
        if len(names) + len(self.fixed_inputs) > 64:
            raise ValueError("a study request may contain at most 64 total inputs")
        if self.study_ref.workspace_id != self.subject_ref.workspace_id:
            raise ValueError("study and subject refs must belong to the same project")
        if self.method == "grid" and any(variable.step is None for variable in self.variables):
            raise ValueError("grid studies require a step for every variable")
        if self.method == "single_objective_opt" and len(self.objectives) != 1:
            raise ValueError("single objective optimization requires exactly one objective")
        return self


class StudyPoint(FrozenContract):
    index: int = Field(ge=0)
    inputs: tuple[NamedQuantity, ...]
    request_ref: EvaluationRequestRef
    status: EvaluationStatus
    evaluation: EvaluationResult | None = None
    failure: EvaluationFailure | None = None
    feasible: bool = False
    pareto_optimal: bool = False


class OutputSummary(FrozenContract):
    output: VariableName
    mean: Quantity
    standard_deviation: Quantity
    percentile_05: Quantity
    percentile_95: Quantity


class StudyRun(FrozenContract):
    study_ref: StudyRef
    definition_digest: str
    content_digest: str
    evaluator_id: ContractId
    status: StudyStatus
    availability_state: AvailabilityState
    availability_reason: str | None = None
    failure: EvaluationFailure | None = None
    points: tuple[StudyPoint, ...] = ()
    feasible_count: int = 0
    failed_count: int = 0
    infeasible_count: int = 0
    best_point_index: int | None = None
    pareto_point_indices: tuple[int, ...] = ()
    output_summaries: tuple[OutputSummary, ...] = ()
    qualification_status: str = "unqualified"
    fidelity: FidelityTier | None = None


def _float_bounds(variable: DesignVariable) -> tuple[float, float]:
    assert variable.domain.lower is not None and variable.domain.upper is not None
    return variable.domain.lower.value, magnitude(variable.domain.upper, variable.domain.lower.unit)


def _unit(variable: DesignVariable) -> str:
    assert variable.domain.lower is not None
    return variable.domain.lower.unit


def _quantities(definition: StudyDefinition, coordinates: tuple[float, ...]) -> tuple[NamedQuantity, ...]:
    design = tuple(
        NamedQuantity(name=variable.name, value=Quantity(value=value, unit=_unit(variable)))
        for variable, value in zip(definition.variables, coordinates, strict=True)
    )
    return (*definition.fixed_inputs, *design)


def _grid(definition: StudyDefinition, limit: int) -> list[tuple[float, ...]]:
    dimensions = []
    for variable in definition.variables:
        lower, upper = _float_bounds(variable)
        assert variable.step is not None
        step = magnitude(variable.step, _unit(variable))
        count = int(math.floor((upper - lower) / step + 1e-12)) + 1
        dimensions.append([min(lower + index * step, upper) for index in range(min(count, limit))])
    return list(itertools.islice(itertools.product(*dimensions), limit))


def _sample(definition: StudyDefinition, count: int) -> list[tuple[float, ...]]:
    dimension = len(definition.variables)
    if definition.method == "latin_hypercube":
        from scipy.stats import qmc

        unit_points = qmc.LatinHypercube(d=dimension, seed=definition.seed).random(count)
        return [tuple(lower + float(u) * (upper - lower)
                      for u, (lower, upper) in zip(row, map(_float_bounds, definition.variables), strict=True))
                for row in unit_points]
    rng = np.random.default_rng(definition.seed)
    points = []
    for _ in range(count):
        row = []
        for variable in definition.variables:
            lower, upper = _float_bounds(variable)
            if variable.distribution == "uniform":
                value = rng.uniform(lower, upper)
            elif variable.distribution == "triangular":
                mode = (magnitude(variable.mode, _unit(variable)) if variable.mode else (lower + upper) / 2)
                value = rng.triangular(lower, mode, upper)
            else:
                uncertainty = variable.uncertainty
                assert uncertainty is not None
                if uncertainty.absolute is not None:
                    sigma = magnitude(uncertainty.absolute, _unit(variable))
                else:
                    assert uncertainty.relative is not None
                    sigma = abs((lower + upper) / 2) * uncertainty.relative
                if sigma <= 0:
                    value = (lower + upper) / 2
                else:
                    from scipy.stats import truncnorm

                    value = truncnorm.rvs((lower - (lower + upper) / 2) / sigma,
                                          (upper - (lower + upper) / 2) / sigma,
                                          loc=(lower + upper) / 2, scale=sigma, random_state=rng)
            row.append(float(value))
        points.append(tuple(row))
    return points


def _output_map(result: EvaluationResult) -> dict[str, Quantity]:
    return {item.name: item.value for item in result.outputs}


def _feasible(definition: StudyDefinition, result: EvaluationResult) -> bool:
    if result.status != "succeeded":
        return False
    outputs = _output_map(result)
    if any(objective.output not in outputs for objective in definition.objectives):
        return False
    for constraint in definition.constraints:
        output = outputs.get(constraint.output)
        if output is None:
            return False
        actual = magnitude(output, constraint.bound.unit)
        target = constraint.bound.value
        if constraint.operator == "le" and actual > target:
            return False
        if constraint.operator == "ge" and actual < target:
            return False
        if constraint.operator == "eq" and not math.isclose(actual, target, rel_tol=1e-9, abs_tol=1e-12):
            return False
    return True


def _pareto(points: list[StudyPoint], objectives: tuple[StudyObjective, ...]) -> set[int]:
    usable = [point for point in points if point.feasible and point.evaluation is not None]
    vectors: dict[int, tuple[float, ...]] = {}
    units = {}
    for objective in objectives:
        first = next((_output_map(point.evaluation)[objective.output]
                      for point in usable if point.evaluation is not None
                      and objective.output in _output_map(point.evaluation)), None)
        if first is not None:
            units[objective.output] = first.unit
    for point in usable:
        assert point.evaluation is not None
        outputs = _output_map(point.evaluation)
        if all(objective.output in outputs and objective.output in units for objective in objectives):
            vectors[point.index] = tuple(
                magnitude(outputs[obj.output], units[obj.output]) * (1 if obj.sense == "minimize" else -1)
                for obj in objectives
            )
    return {index for index, candidate in vectors.items()
            if not any(other != index and all(a <= b for a, b in zip(vector, candidate, strict=True))
                       and any(a < b for a, b in zip(vector, candidate, strict=True))
                       for other, vector in vectors.items())}


def _summary(points: list[StudyPoint]) -> tuple[OutputSummary, ...]:
    names: dict[str, list[Quantity]] = {}
    for point in points:
        if point.feasible and point.evaluation is not None:
            for item in point.evaluation.outputs:
                names.setdefault(item.name, []).append(item.value)
    rows = []
    for name, values in sorted(names.items()):
        unit = values[0].unit
        magnitudes = np.asarray([magnitude(value, unit) for value in values])
        rows.append(OutputSummary(
            output=name,
            mean=Quantity(value=float(np.mean(magnitudes)), unit=unit),
            standard_deviation=Quantity(value=float(np.std(magnitudes, ddof=1 if len(values) > 1 else 0)), unit=unit),
            percentile_05=Quantity(value=float(np.percentile(magnitudes, 5)), unit=unit),
            percentile_95=Quantity(value=float(np.percentile(magnitudes, 95)), unit=unit),
        ))
    return tuple(rows)


def run_study(definition: StudyDefinition, evaluator: EngineeringEvaluator) -> StudyRun:
    """Run a bounded deterministic study. Evaluator errors become per-point evidence."""
    definition_digest = canonical_digest(definition.model_dump(mode="json"))
    try:
        descriptor = evaluator.descriptor()
        availability = evaluator.availability()
    except Exception as exc:
        failure = EvaluationFailure(category="internal_error", backend_code="STUDY_PREFLIGHT_ERROR",
                                    message=type(exc).__name__)
        return StudyRun(
            study_ref=definition.study_ref, definition_digest=definition_digest,
            content_digest=canonical_digest({"definition": definition_digest, "preflight_error": type(exc).__name__}),
            evaluator_id=definition.evaluator_id, status="failed", availability_state="unknown", failure=failure,
        )
    if descriptor.evaluator_id != definition.evaluator_id or availability.evaluator_id != definition.evaluator_id:
        raise ValueError("study evaluator id does not match evaluator descriptor and availability")
    if availability.state != "available":
        failure = EvaluationFailure(category="not_available", backend_code=availability.reason_code or "STUDY_UNAVAILABLE")
        stable = {"definition": definition_digest, "availability": availability.model_dump(mode="json")}
        return StudyRun(
            study_ref=definition.study_ref, definition_digest=definition_digest,
            content_digest=canonical_digest(stable), evaluator_id=definition.evaluator_id,
            status="unavailable", availability_state=availability.state,
            availability_reason=availability.reason_code, failure=failure,
        )

    count = min(definition.sample_count, definition.budget)
    if definition.method == "grid":
        coordinates = _grid(definition, definition.budget)
    elif definition.method == "single_objective_opt":
        coordinates = []
    else:
        coordinates = _sample(definition, count)

    points: list[StudyPoint] = []
    consecutive_failures = 0

    def evaluate(coordinate: tuple[float, ...]) -> float:
        nonlocal consecutive_failures
        if len(points) >= definition.budget or consecutive_failures >= definition.failure_breaker_count:
            return math.inf
        index = len(points)
        request_ref = EvaluationRequestRef(
            authority_owner=definition.study_ref.authority_owner,
            object_id=f"{definition.study_ref.object_id}/point-{index}",
            workspace_id=definition.study_ref.workspace_id,
            revision=definition.study_ref.revision,
        )
        inputs = _quantities(definition, coordinate)
        requested_at = datetime.now(UTC)
        result = None
        failure = None
        point_status: EvaluationStatus = "failed"
        try:
            request = EvaluationRequest(
                request_ref=request_ref, evaluator_id=definition.evaluator_id,
                subject_ref=definition.subject_ref, study_ref=definition.study_ref,
                inputs=inputs, backend_options=definition.backend_options,
                requested_at=requested_at,
                deadline_at=requested_at + timedelta(seconds=definition.deadline_per_point_s),
            )
            result = evaluator.evaluate(request)
            if datetime.now(UTC) > request.deadline_at or result.completed_at > request.deadline_at:
                failure = EvaluationFailure(category="timeout", backend_code="STUDY_POINT_DEADLINE",
                                            message="evaluation completed after its study point deadline")
                result = None
                point_status = "deadline_exceeded"
            else:
                validate_evaluation_result(request, result)
        except Exception as exc:
            result = None
            failure = EvaluationFailure(category="internal_error", backend_code="STUDY_EVALUATION_ERROR",
                                        message=type(exc).__name__)
            point_status = "failed"
        if result is not None:
            failure = result.failure
            feasible = _feasible(definition, result)
            status = result.status
        else:
            feasible = False
            status = point_status
        point = StudyPoint(index=index, inputs=inputs, request_ref=request_ref, status=status,
                           evaluation=result, failure=failure, feasible=feasible)
        points.append(point)
        consecutive_failures = consecutive_failures + 1 if status != "succeeded" else 0
        if definition.objectives and feasible and result is not None:
            outputs = _output_map(result)
            objective = definition.objectives[0]
            value = outputs.get(objective.output)
            if value is not None:
                score = magnitude(value, value.unit)
                return score if objective.sense == "minimize" else -score
        return math.inf

    if definition.method == "single_objective_opt":
        from scipy.optimize import minimize

        bounds = [_float_bounds(variable) for variable in definition.variables]
        initial = tuple((low + high) / 2 for low, high in bounds)

        def score_candidate(coordinate: np.ndarray) -> float:
            return evaluate(tuple(float(value) for value in coordinate))

        minimize(score_candidate, np.asarray(initial), method="Powell", bounds=bounds,
                 options={"maxfev": definition.budget, "maxiter": definition.budget, "xtol": 1e-4, "ftol": 1e-4})
    else:
        for coordinate in coordinates:
            if consecutive_failures >= definition.failure_breaker_count:
                break
            evaluate(tuple(coordinate))

    pareto = _pareto(points, definition.objectives) if len(definition.objectives) > 1 else set()
    points = [point.model_copy(update={"pareto_optimal": point.index in pareto}) for point in points]
    eligible = [point for point in points if point.feasible and point.evaluation is not None]
    best = None
    if definition.objectives and len(definition.objectives) == 1 and eligible:
        objective = definition.objectives[0]
        assert eligible[0].evaluation is not None
        unit = _output_map(eligible[0].evaluation)[objective.output].unit
        def objective_value(point: StudyPoint) -> float:
            assert point.evaluation is not None
            output = _output_map(point.evaluation)[objective.output]
            return magnitude(output, unit) * (1 if objective.sense == "minimize" else -1)

        best = min(eligible, key=lambda point: (objective_value(point), point.index)).index
    validities = [point.evaluation.validity for point in points if point.evaluation is not None]
    qualifications = [validity.qualification_status if validity else "unqualified" for validity in validities]
    qualification_order = {"unqualified": 0, "candidate": 1, "calibrated": 2, "benchmarked": 3, "qualified": 4}
    qualification = min(qualifications, key=lambda value: qualification_order[value]) if qualifications else "unqualified"
    fidelities = [point.evaluation.fidelity for point in points if point.evaluation is not None]
    fidelity_order: tuple[FidelityTier, ...] = (
        "screening", "reduced_order", "steady_state_detailed", "dynamic_detailed", "field_resolved"
    )
    fidelity = min(fidelities, key=fidelity_order.index) if fidelities else None
    failed = sum(point.status != "succeeded" for point in points)
    infeasible = sum(point.status == "succeeded" and not point.feasible for point in points)
    status: StudyStatus = "stopped_unhealthy" if consecutive_failures >= definition.failure_breaker_count else (
        "succeeded" if failed == 0 else "partial" if eligible else "failed")
    stable_points = [{
        "index": point.index, "inputs": [item.model_dump(mode="json") for item in point.inputs],
        "status": point.status, "failure": point.failure.model_dump(mode="json") if point.failure else None,
        "feasible": point.feasible, "outputs": [item.model_dump(mode="json") for item in point.evaluation.outputs]
        if point.evaluation else [], "pareto_optimal": point.pareto_optimal,
    } for point in points]
    content_digest = canonical_digest({"definition": definition_digest, "points": stable_points})
    return StudyRun(
        study_ref=definition.study_ref, definition_digest=definition_digest, content_digest=content_digest,
        evaluator_id=definition.evaluator_id, status=status, availability_state=availability.state,
        points=tuple(points), feasible_count=len(eligible), failed_count=failed, infeasible_count=infeasible,
        best_point_index=best,
        pareto_point_indices=tuple(sorted(pareto)), output_summaries=_summary(points),
        qualification_status=qualification, fidelity=fidelity,
    )
