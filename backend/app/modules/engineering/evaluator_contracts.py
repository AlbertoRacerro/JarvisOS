"""106 frozen engineering evaluator/adapter boundary.

One small typed boundary for process, dynamic, property, CAD, mesh, FEM, CFD
and specialist backends. It is not a universal solver schema: solver-native
case bundles travel as artifact refs plus bounded backend options, and every
failure keeps its backend-native code next to a shared category.

Availability (can it run here, now) and qualification (should its results be
trusted, 102 ledger) are separate. Numerical convergence is a diagnostic,
never validation. Results declare typed checks against bounds, and a derived
verdict determines usability from declared required and advisory checks.
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
CheckId = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")]
CheckFamily = Literal["input", "numerical", "conservation", "discretisation", "plausibility", "reference"]
CheckStatus = Literal["pass", "warn", "fail", "not_evaluated"]
CHECK_SOURCE_PATTERN: Final = (
    r"^(physical_limit|(policy|constant):[A-Za-z0-9_.:-]+|value_store:[A-Za-z0-9][A-Za-z0-9_.:-]*)$"
)
CheckSource = Annotated[str, StringConstraints(min_length=1, max_length=128, pattern=CHECK_SOURCE_PATTERN)]
Verdict = Literal["usable", "usable_with_warnings", "unchecked", "not_usable"]
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
    required_checks: tuple[CheckId, ...] = Field(default=(), max_length=MAX_ITEMS)
    advisory_checks: tuple[CheckId, ...] = Field(default=(), max_length=MAX_ITEMS)

    @model_validator(mode="after")
    def validate_checks(self) -> EvaluatorDescriptor:
        if len(set(self.required_checks)) != len(self.required_checks):
            raise ValueError("required_checks ids must be unique")
        if len(set(self.advisory_checks)) != len(self.advisory_checks):
            raise ValueError("advisory_checks ids must be unique")
        overlap = set(self.required_checks) & set(self.advisory_checks)
        if overlap:
            raise ValueError(f"required_checks and advisory_checks must be disjoint: {sorted(overlap)}")
        return self


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


class ResultCheck(FrozenContract):
    id: CheckId
    family: CheckFamily
    status: CheckStatus
    actual: float | None = Field(default=None, allow_inf_nan=False)
    limit: float | None = Field(default=None, allow_inf_nan=False)
    unit: str | None = Field(default=None, min_length=1, max_length=64)
    hint: str = Field(min_length=1, max_length=240)
    source: CheckSource

    @field_validator("actual", "limit", mode="before")
    @classmethod
    def reject_bool(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("check numeric values must not be boolean")
        return value

    @field_validator("hint")
    @classmethod
    def check_hint_one_line(cls, value: str) -> str:
        if "\n" in value or "\r" in value:
            raise ValueError("hint must be a single line")
        return value

    @model_validator(mode="after")
    def validate_check(self) -> ResultCheck:
        if self.status == "not_evaluated":
            if self.actual is not None:
                raise ValueError("a not_evaluated check must not carry an actual")
            if self.limit is not None:
                raise ValueError("a not_evaluated check must not carry a limit")
        if (self.actual is None) != (self.limit is None):
            raise ValueError("numeric checks require actual and limit together")
        if self.unit is not None and self.actual is None:
            raise ValueError("unit requires actual")
        return self


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
    checks: tuple[ResultCheck, ...] = Field(default=(), max_length=MAX_ITEMS)
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

    @field_validator("checks")
    @classmethod
    def check_checks(cls, value: tuple[ResultCheck, ...]) -> tuple[ResultCheck, ...]:
        ids = [item.id for item in value]
        if len(set(ids)) != len(ids):
            raise ValueError("check ids must be unique")
        return value

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


def result_verdict(descriptor: EvaluatorDescriptor, result: EvaluationResult) -> Verdict:
    """Derive an honest verdict for an evaluation result against its descriptor."""
    if descriptor.evaluator_id != result.evaluator_id:
        raise EvaluatorContractError(
            f"evaluator mismatch: descriptor has '{descriptor.evaluator_id}' but result has '{result.evaluator_id}'"
        )
    if result.status != "succeeded" or any(check.status == "fail" for check in result.checks):
        return "not_usable"

    checks_by_id = {check.id: check for check in result.checks}

    for req_id in descriptor.required_checks:
        check = checks_by_id.get(req_id)
        if check is None or check.status == "not_evaluated":
            return "unchecked"

    if not descriptor.required_checks:
        return "unchecked"

    if any(check.status == "warn" for check in result.checks):
        return "usable_with_warnings"

    for adv_id in descriptor.advisory_checks:
        check = checks_by_id.get(adv_id)
        if check is None or check.status == "not_evaluated":
            return "usable_with_warnings"

    return "usable"


class EngineeringEvaluator(Protocol):
    """Adapter boundary every process/CAD/CAE/CFD/specialist backend implements."""

    def descriptor(self) -> EvaluatorDescriptor: ...

    def availability(self) -> EvaluatorAvailability: ...

    def evaluate(self, request: EvaluationRequest) -> EvaluationResult:
        """Return a typed result for every outcome; expired or cancelled requests are results, not exceptions."""
        ...
