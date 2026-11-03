"""145 frozen typed local decision contracts.

A decision is advisory: it may select one of a bounded candidate set and emit
typed bool/enum/score outputs, or abstain. It can never carry permission,
admission, egress, provider or sensitivity authority; output names that collide
with those authorities are refused at the contract boundary.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated, Final, Literal

from pydantic import Field, StrictBool, StringConstraints, field_validator, model_validator

from app.modules.ai.jarvis_context_models import ContractId, FrozenContract, SourceRef, UtcDatetime
from app.modules.ai.routing.invariants import CRITICAL_PERMISSION_FIELDS
from app.modules.local_ai.classification.contracts import MODEL_NON_AUTHORITY_BOUNDARIES
from app.modules.local_ai.resource_contracts import RuntimeResourceSnapshot

DECISION_CONTRACTS_VERSION: Final = "decision_contracts.v1"
DecisionContractsVersion = Literal["decision_contracts.v1"]

MAX_CANDIDATES = 64
MAX_OUTPUTS = 16
MAX_ENUM_VALUES = 32
MAX_REFS = 32

OutputName = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
DecisionType = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_.]{0,95}$")]
ReasonCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
EnumValue = Annotated[str, StringConstraints(min_length=1, max_length=64)]
ConstraintValue = str | int | float | bool
DecisionOutputKind = Literal["bool", "enum", "score"]

_AUTHORITY_NAME_TOKENS = frozenset(
    {
        "permission",
        "permissions",
        "permit",
        "permitted",
        "allow",
        "allowed",
        "authorize",
        "authorized",
        "authorization",
        "approve",
        "approved",
        "grant",
        "granted",
        "admit",
        "admitted",
    }
)
FORBIDDEN_DECISION_OUTPUT_NAMES = frozenset(CRITICAL_PERMISSION_FIELDS) | frozenset(MODEL_NON_AUTHORITY_BOUNDARIES)


class DecisionContractError(ValueError):
    """A decision result does not satisfy the request it answers."""


def _require_non_authority_name(name: str) -> str:
    if name in FORBIDDEN_DECISION_OUTPUT_NAMES or _AUTHORITY_NAME_TOKENS.intersection(name.split("_")):
        raise ValueError(f"decision output {name!r} names an authority the model cannot own")
    return name


class DecisionOutputSpec(FrozenContract):
    name: OutputName
    kind: DecisionOutputKind
    enum_values: tuple[EnumValue, ...] = Field(default=(), max_length=MAX_ENUM_VALUES)

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        return _require_non_authority_name(value)

    @model_validator(mode="after")
    def enum_values_for_enum_only(self) -> DecisionOutputSpec:
        if self.kind == "enum":
            if len(self.enum_values) < 2 or len(set(self.enum_values)) != len(self.enum_values):
                raise ValueError("enum outputs require at least two unique enum_values")
        elif self.enum_values:
            raise ValueError("only enum outputs may declare enum_values")
        return self


class DecisionRequest(FrozenContract):
    schema_version: DecisionContractsVersion = DECISION_CONTRACTS_VERSION
    decision_id: ContractId
    decision_type: DecisionType
    candidate_set: tuple[ContractId, ...] = Field(default=(), max_length=MAX_CANDIDATES)
    output_specs: tuple[DecisionOutputSpec, ...] = Field(default=(), max_length=MAX_OUTPUTS)
    constraints: dict[str, ConstraintValue] = Field(default_factory=dict)
    resource_snapshot: RuntimeResourceSnapshot | None = None
    evidence_refs: tuple[SourceRef, ...] = Field(default=(), max_length=MAX_REFS)
    requested_at: UtcDatetime
    deadline_at: UtcDatetime

    @field_validator("constraints")
    @classmethod
    def check_constraints(cls, value: dict[str, ConstraintValue]) -> dict[str, ConstraintValue]:
        if len(value) > MAX_REFS:
            raise ValueError(f"at most {MAX_REFS} constraints are allowed")
        for key, item in value.items():
            if not 1 <= len(key) <= 64 or (isinstance(item, str) and len(item) > 256):
                raise ValueError("constraint keys must be 1..64 and string values at most 256 characters")
            if isinstance(item, float) and not math.isfinite(item):
                raise ValueError("constraint numbers must be finite")
        return value

    @model_validator(mode="after")
    def validate_request(self) -> DecisionRequest:
        if not self.candidate_set and not self.output_specs:
            raise ValueError("decision requires a candidate_set or output_specs")
        if len(set(self.candidate_set)) != len(self.candidate_set):
            raise ValueError("candidate_set entries must be unique")
        names = [spec.name for spec in self.output_specs]
        if len(set(names)) != len(names):
            raise ValueError("output_specs names must be unique")
        if self.deadline_at <= self.requested_at:
            raise ValueError("deadline_at must be after requested_at")
        return self

    def is_expired(self, now: datetime) -> bool:
        return now >= self.deadline_at


class _DecisionOutputBase(FrozenContract):
    name: OutputName

    @field_validator("name")
    @classmethod
    def check_name(cls, value: str) -> str:
        return _require_non_authority_name(value)


class BoolDecisionOutput(_DecisionOutputBase):
    kind: Literal["bool"] = "bool"
    value: StrictBool


class EnumDecisionOutput(_DecisionOutputBase):
    kind: Literal["enum"] = "enum"
    value: EnumValue


class ScoreDecisionOutput(_DecisionOutputBase):
    kind: Literal["score"] = "score"
    value: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)


DecisionOutput = Annotated[
    BoolDecisionOutput | EnumDecisionOutput | ScoreDecisionOutput,
    Field(discriminator="kind"),
]


class DecisionResult(FrozenContract):
    """``model_ref`` names the exact model revision (or deterministic rule) that decided."""

    schema_version: DecisionContractsVersion = DECISION_CONTRACTS_VERSION
    decision_id: ContractId
    outcome: Literal["decided", "abstained"]
    selected_candidate: ContractId | None = None
    outputs: tuple[DecisionOutput, ...] = Field(default=(), max_length=MAX_OUTPUTS)
    model_ref: str = Field(min_length=1, max_length=256)
    calibration_ref: SourceRef | None = None
    evidence_refs: tuple[SourceRef, ...] = Field(default=(), max_length=MAX_REFS)
    reason_code: ReasonCode
    decided_at: UtcDatetime

    @model_validator(mode="after")
    def abstention_is_empty(self) -> DecisionResult:
        if self.outcome == "abstained" and (self.selected_candidate is not None or self.outputs):
            raise ValueError("abstained decisions carry no selection or outputs")
        return self


def validate_decision_result(request: DecisionRequest, result: DecisionResult) -> None:
    """Refuse results that do not answer exactly the bounded question asked."""
    if result.decision_id != request.decision_id:
        raise DecisionContractError("result answers a different decision_id")
    if result.outcome == "abstained":
        return
    if request.candidate_set:
        if result.selected_candidate not in request.candidate_set:
            raise DecisionContractError("selected_candidate is not in the request candidate_set")
    elif result.selected_candidate is not None:
        raise DecisionContractError("request has no candidate_set to select from")
    specs = {spec.name: spec for spec in request.output_specs}
    outputs = {output.name: output for output in result.outputs}
    if len(outputs) != len(result.outputs) or set(outputs) != set(specs):
        raise DecisionContractError("outputs must answer every requested output spec exactly once")
    for name, output in outputs.items():
        spec = specs[name]
        if output.kind != spec.kind:
            raise DecisionContractError(f"output {name!r} kind does not match its spec")
        if spec.kind == "enum" and output.value not in spec.enum_values:
            raise DecisionContractError(f"output {name!r} value is not an allowed enum value")
