"""106 frozen engineering evaluator/adapter boundary.

One small typed boundary for process, dynamic, property, CAD, mesh, FEM, CFD
and specialist backends. It is not a universal solver schema: solver-native
case bundles travel as artifact refs plus bounded backend options, and every
failure keeps its backend-native code next to a shared category.

Availability (can it run here, now) and qualification (should its results be
trusted, 102 ledger) are separate. Numerical convergence is a diagnostic,
never validation.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated, Final, Literal, Protocol

from pydantic import Field, StringConstraints, field_validator, model_validator

from app.modules.ai.jarvis_context_models import ContractId, FrozenContract, SourceRef, UtcDatetime
from app.modules.engineering.evidence_contracts import FidelityTier, fidelity_rank
from app.modules.engineering.refs import (
    DynamicModelRef,
    EnvironmentalScenarioRef,
    EvaluationRequestRef,
    EvaluationResultRef,
    GeometryAssetRef,
    MaterialStateRef,
    MeshArtifactRef,
    PhysicsCaseRef,
    ProcessDesignEnvelopeRef,
    ProcessModelIRRef,
    PropertyBasisRef,
    Quantity,
    StudyRef,
    ValidityEnvelopeRef,
    VariableName,
)

ENGINEERING_EVALUATOR_VERSION: Final = "engineering_evaluator.v1"
EngineeringEvaluatorVersion = Literal["engineering_evaluator.v1"]

MAX_ITEMS = 64
MAX_OPTIONS = 32

BackendKind = Literal[
    "process_kernel",
    "process_simulator",
    "dynamic_simulator",
    "property_package",
    "cad_kernel",
    "mesher",
    "fem_solver",
    "cfd_solver",
    "specialist",
]
AvailabilityState = Literal["available", "not_installed", "disabled", "unhealthy", "unknown"]
EvaluationStatus = Literal["succeeded", "failed", "refused", "cancelled", "deadline_exceeded"]
FailureCategory = Literal[
    "not_available",
    "invalid_input",
    "unsupported_request",
    "outside_validity_domain",
    "resource_unavailable",
    "initialization_failed",
    "did_not_converge",
    "numerical_error",
    "solver_crash",
    "result_parse_error",
    "timeout",
    "cancelled",
    "internal_error",
]
CapabilityToken = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
BackendCode = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_.:-]{0,95}$")]
OptionValue = str | int | float | bool

# Categories that mean "not attempted": the evaluator refused before running.
REFUSAL_CATEGORIES: Final = frozenset(
    {"not_available", "invalid_input", "unsupported_request", "outside_validity_domain", "resource_unavailable"}
)
_STATUS_CATEGORY: Final[dict[str, str]] = {"cancelled": "cancelled", "deadline_exceeded": "timeout"}

EvaluationSubjectRef = (
    ProcessModelIRRef
    | DynamicModelRef
    | PropertyBasisRef
    | MaterialStateRef
    | GeometryAssetRef
    | MeshArtifactRef
    | PhysicsCaseRef
)

# Existing backend-native codes mapped onto the shared taxonomy.
_KNOWN_BACKEND_CODES: Final[dict[str, FailureCategory]] = {
    # BLUECAD tool registry / adapters
    "TOOL_UNKNOWN": "not_available",
    "TOOL_DISABLED": "not_available",
    "TOOL_BINARY_MISSING": "not_available",
    "TOOL_HASH_MISMATCH": "not_available",
    "TOOL_NOT_SUBPROCESS": "not_available",
    "TIMEOUT": "timeout",
    "PARSE_ERROR": "result_parse_error",
    "SOLVE_ERROR": "solver_crash",
    "SOLVE_DIVERGED": "did_not_converge",
    "MESH_FAIL": "solver_crash",
    "MESH_ERROR": "solver_crash",
    "MESH_GROUP_EMPTY": "invalid_input",
    "MESH_ELEMENT_ORDER_MISMATCH": "invalid_input",
    "MESH_HIGH_ORDER_INVALID": "invalid_input",
    # process kernel
    "correlation_not_qualified": "outside_validity_domain",
    "unit_dimension_unsupported": "unsupported_request",
}


def failure_category_for_code(code: str) -> FailureCategory:
    """Deterministic shared category for an existing backend-native code."""
    known = _KNOWN_BACKEND_CODES.get(code)
    if known is not None:
        return known
    if code.startswith(("unit_", "quantity_", "input_", "stream_", "flowsheet_")) or code.endswith("_invalid"):
        return "invalid_input"
    return "internal_error"


class NamedQuantity(FrozenContract):
    name: VariableName
    value: Quantity


def _unique_names(values: tuple[NamedQuantity, ...], label: str) -> tuple[NamedQuantity, ...]:
    names = [item.name for item in values]
    if len(set(names)) != len(names):
        raise ValueError(f"{label} names must be unique")
    return values


def _same_project(workspace_id: str, refs: tuple[SourceRef | None, ...], label: str) -> None:
    """Workspace-scoped refs must stay in the project; global refs (no workspace) are allowed."""
    for ref in refs:
        if ref is not None and ref.workspace_id is not None and ref.workspace_id != workspace_id:
            raise ValueError(f"{label} refs must belong to the request's project")


def _bounded_options(value: dict[str, OptionValue]) -> dict[str, OptionValue]:
    if len(value) > MAX_OPTIONS:
        raise ValueError(f"at most {MAX_OPTIONS} backend options are allowed")
    for key, item in value.items():
        if not 1 <= len(key) <= 64:
            raise ValueError("backend option keys must be 1..64 characters")
        if isinstance(item, str) and len(item) > 256:
            raise ValueError("backend option strings must be at most 256 characters")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("backend option numbers must be finite")
    return value


class EvaluatorDescriptor(FrozenContract):
    """Static identity of one evaluator adapter; the backend version is exact."""

    schema_version: EngineeringEvaluatorVersion = ENGINEERING_EVALUATOR_VERSION
    evaluator_id: ContractId
    backend_kind: BackendKind
    backend_name: str = Field(min_length=1, max_length=128)
    backend_version: str = Field(min_length=1, max_length=128)
    tool_registry_id: ContractId | None = None
    fidelity: FidelityTier
    capabilities: tuple[CapabilityToken, ...] = Field(default=(), max_length=MAX_ITEMS)
    qualification_record_ref: SourceRef | None = None


class EvaluatorAvailability(FrozenContract):
    """Truthful runtime availability; says nothing about scientific qualification."""

    evaluator_id: ContractId
    state: AvailabilityState
    checked_at: UtcDatetime
    backend_version: str | None = Field(default=None, min_length=1, max_length=128)
    reason_code: BackendCode | None = None

    @model_validator(mode="after")
    def reason_for_unavailable(self) -> EvaluatorAvailability:
        if self.state == "available":
            if self.backend_version is None or self.reason_code is not None:
                raise ValueError("available evaluators report backend_version and no reason_code")
        elif self.reason_code is None:
            raise ValueError(f"{self.state} availability requires reason_code")
        return self


class EvaluationRequest(FrozenContract):
    schema_version: EngineeringEvaluatorVersion = ENGINEERING_EVALUATOR_VERSION
    request_ref: EvaluationRequestRef
    evaluator_id: ContractId
    subject_ref: EvaluationSubjectRef = Field(discriminator="object_type")
    study_ref: StudyRef | None = None
    scenario_ref: EnvironmentalScenarioRef | None = None
    design_envelope_ref: ProcessDesignEnvelopeRef | None = None
    inputs: tuple[NamedQuantity, ...] = Field(default=(), max_length=MAX_ITEMS)
    backend_case_ref: SourceRef | None = None
    backend_options: dict[str, OptionValue] = Field(default_factory=dict)
    requested_fidelity: FidelityTier | None = None
    lease_id: ContractId | None = None
    requested_at: UtcDatetime
    deadline_at: UtcDatetime
    cancellation_id: ContractId | None = None

    @field_validator("inputs")
    @classmethod
    def check_inputs(cls, value: tuple[NamedQuantity, ...]) -> tuple[NamedQuantity, ...]:
        return _unique_names(value, "input")

    @field_validator("backend_options")
    @classmethod
    def check_options(cls, value: dict[str, OptionValue]) -> dict[str, OptionValue]:
        return _bounded_options(value)

    @model_validator(mode="after")
    def validate_request(self) -> EvaluationRequest:
        _same_project(
            self.request_ref.workspace_id,
            (self.subject_ref, self.study_ref, self.scenario_ref, self.design_envelope_ref, self.backend_case_ref),
            "request",
        )
        if self.deadline_at <= self.requested_at:
            raise ValueError("deadline_at must be after requested_at")
        return self

    def is_expired(self, now: datetime) -> bool:
        return now >= self.deadline_at


class EvaluationFailure(FrozenContract):
    category: FailureCategory
    backend_code: BackendCode
    retryable: bool = False
    message: str | None = Field(default=None, max_length=1024)


class NumericalDiagnostics(FrozenContract):
    """Solver health only. Convergence is never evidence of physical validity."""

    converged: bool | None = None
    final_residual: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)
    iterations: int | None = Field(default=None, ge=0)
    wall_time_s: float | None = Field(default=None, ge=0.0, allow_inf_nan=False)


class EvaluationResult(FrozenContract):
    schema_version: EngineeringEvaluatorVersion = ENGINEERING_EVALUATOR_VERSION
    result_ref: EvaluationResultRef
    request_ref: EvaluationRequestRef
    evaluator_id: ContractId
    backend_version: str = Field(min_length=1, max_length=128)
    status: EvaluationStatus
    failure: EvaluationFailure | None = None
    fidelity: FidelityTier
    outputs: tuple[NamedQuantity, ...] = Field(default=(), max_length=MAX_ITEMS)
    output_artifacts: tuple[SourceRef, ...] = Field(default=(), max_length=MAX_ITEMS)
    numerical: NumericalDiagnostics = NumericalDiagnostics()
    validity: ValidityEnvelopeRef | None = None
    qualification_record_ref: SourceRef | None = None
    evidence_refs: tuple[SourceRef, ...] = Field(default=(), max_length=MAX_ITEMS)
    started_at: UtcDatetime | None = None
    completed_at: UtcDatetime

    @field_validator("outputs")
    @classmethod
    def check_outputs(cls, value: tuple[NamedQuantity, ...]) -> tuple[NamedQuantity, ...]:
        return _unique_names(value, "output")

    @model_validator(mode="after")
    def status_is_consistent(self) -> EvaluationResult:
        _same_project(
            self.request_ref.workspace_id,
            (self.result_ref, self.validity, self.qualification_record_ref, *self.output_artifacts, *self.evidence_refs),
            "result",
        )
        if self.started_at is not None and self.started_at > self.completed_at:
            raise ValueError("started_at must not be after completed_at")
        if self.status == "succeeded":
            if self.failure is not None:
                raise ValueError("succeeded results carry no failure")
            if not self.outputs and not self.output_artifacts:
                raise ValueError("succeeded results require outputs or output_artifacts")
            if self.numerical.converged is False:
                raise ValueError("a non-converged run cannot be reported as succeeded")
            return self
        if self.failure is None:
            raise ValueError(f"{self.status} results require failure")
        if self.outputs or self.output_artifacts:
            raise ValueError(f"{self.status} results must not carry outputs")
        expected = _STATUS_CATEGORY.get(self.status)
        if expected is not None and self.failure.category != expected:
            raise ValueError(f"{self.status} results require failure category {expected}")
        if (self.status == "refused") != (self.failure.category in REFUSAL_CATEGORIES):
            raise ValueError("refused status and refusal failure categories must coincide")
        return self


class EvaluatorContractError(ValueError):
    """A result does not answer the request it claims to answer."""


def validate_evaluation_result(request: EvaluationRequest, result: EvaluationResult) -> None:
    if result.request_ref != request.request_ref:
        raise EvaluatorContractError("result answers a different request")
    if result.evaluator_id != request.evaluator_id:
        raise EvaluatorContractError("result comes from a different evaluator than requested")
    if result.status != "succeeded":
        return
    if result.completed_at > request.deadline_at:
        raise EvaluatorContractError("a result completed after the request deadline cannot be succeeded")
    if request.requested_fidelity is not None and fidelity_rank(result.fidelity) < fidelity_rank(request.requested_fidelity):
        raise EvaluatorContractError(
            "a result below the requested fidelity is not an answer; refuse with unsupported_request instead"
        )


class EngineeringEvaluator(Protocol):
    """Adapter boundary every process/CAD/CAE/CFD/specialist backend implements."""

    def descriptor(self) -> EvaluatorDescriptor: ...

    def availability(self) -> EvaluatorAvailability: ...

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        """Return a typed result for every outcome; expired or cancelled requests are results, not exceptions."""
        ...
