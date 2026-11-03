from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ParameterLifecycleState = Literal["active", "inactive", "superseded", "archived", "deleted"]
RequirementBasisKind = Literal[
    "project_objective",
    "requirement",
    "acceptance_criterion",
    "stable_constraint",
    "boundary_condition",
    "standard_regulation",
    "resource_capability_constraint",
]
RequirementGate = Literal["required", "advisory"]
CriterionOperator = Literal["<", "<=", ">", ">=", "=="]


class ModelSpecCreate(BaseModel):
    title: str = Field(min_length=1)
    engineering_question: str = Field(min_length=1)
    scope: str | None = None
    status: str = "draft"
    maturity_status: str = "draft"
    assumptions_summary: str | None = None
    inputs_summary: str | None = None
    outputs_summary: str | None = None
    raw_payload: str | None = None


class ModelSpecRead(ModelSpecCreate):
    id: str
    workspace_id: str
    schema_version: int
    created_at: str
    updated_at: str


class ModelSpecProjectUpdate(BaseModel):
    workspace_id: str
    expected_updated_at: str
    title: str | None = Field(default=None, min_length=1)
    engineering_question: str | None = Field(default=None, min_length=1)
    scope: str | None = None
    status: str | None = None
    maturity_status: str | None = None
    assumptions_summary: str | None = None
    inputs_summary: str | None = None
    outputs_summary: str | None = None
    raw_payload: str | None = None


class AssumptionCreate(BaseModel):
    statement: str = Field(min_length=1)
    scope: str | None = None
    confidence: Literal["low", "medium", "high"] | None = None
    status: Literal["proposed", "accepted", "rejected", "superseded"] = "proposed"
    source_ref: str | None = None
    notes: str | None = None


class AssumptionRead(AssumptionCreate):
    id: str
    workspace_id: str
    created_at: str
    updated_at: str


class AssumptionProjectUpdate(BaseModel):
    workspace_id: str
    expected_updated_at: str
    statement: str | None = Field(default=None, min_length=1)
    scope: str | None = None
    confidence: Literal["low", "medium", "high"] | None = None
    status: Literal["proposed", "accepted", "rejected", "superseded"] | None = None
    source_ref: str | None = None
    notes: str | None = None


class ParameterCreate(BaseModel):
    name: str = Field(min_length=1)
    symbol: str | None = None
    value: str | None = None
    unit: str = Field(min_length=1)
    value_status: Literal["candidate", "literature", "measured", "validated", "accepted"] = "candidate"
    value_min: float | None = None
    value_max: float | None = None
    source_ref: str | None = None
    confidence: float | None = None
    status: str = "draft"
    notes: str | None = None
    supersedes_parameter_id: str | None = None

    @model_validator(mode="after")
    def validate_uncertainty_bounds(self) -> "ParameterCreate":
        if self.value_min is not None and self.value_max is not None and self.value_min > self.value_max:
            raise ValueError("value_min must be less than or equal to value_max")
        if self.supersedes_parameter_id is not None and self.status != "proposed":
            raise ValueError("Parameter replacements must be created with proposed status.")
        return self


class ParameterRead(ParameterCreate):
    id: str
    workspace_id: str
    created_at: str
    updated_at: str
    lifecycle_state: ParameterLifecycleState = "active"


class ParameterUpdate(BaseModel):
    workspace_id: str
    expected_updated_at: str
    name: str | None = Field(default=None, min_length=1)
    symbol: str | None = None
    value: str | None = None
    unit: str | None = Field(default=None, min_length=1)
    value_status: Literal["candidate", "literature", "measured", "validated", "accepted"] | None = None
    value_min: float | None = None
    value_max: float | None = None
    source_ref: str | None = None
    confidence: float | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def validate_uncertainty_bounds(self) -> "ParameterUpdate":
        if self.value_min is not None and self.value_max is not None and self.value_min > self.value_max:
            raise ValueError("value_min must be less than or equal to value_max")
        return self


class ParameterLifecycleCommand(BaseModel):
    workspace_id: str
    action: Literal["activate", "deactivate", "archive", "delete"]
    expected_lifecycle_state: ParameterLifecycleState
    expected_updated_at: str
    reason: str | None = Field(default=None, max_length=500)


class RequirementCreate(BaseModel):
    statement: str = Field(min_length=1)
    rationale: str | None = None
    status: Literal["draft", "active", "retired"] = "draft"
    notes: str | None = None
    basis_kind: RequirementBasisKind = "requirement"
    reconciliation_gate: RequirementGate = "advisory"
    criterion_output_name: str | None = None
    criterion_operator: CriterionOperator | None = None
    criterion_expected_value: str | None = None
    criterion_expected_unit: str | None = None
    criterion_rule_version: str | None = None

    @model_validator(mode="after")
    def validate_criterion_metadata(self) -> "RequirementCreate":
        fields = (
            self.criterion_output_name,
            self.criterion_operator,
            self.criterion_expected_value,
            self.criterion_expected_unit,
            self.criterion_rule_version,
        )
        if self.basis_kind != "acceptance_criterion" and any(value is not None for value in fields):
            raise ValueError("Typed criterion metadata is only valid for acceptance criteria.")
        if self.basis_kind == "acceptance_criterion" and any(value is not None for value in fields):
            if any(value is None for value in fields):
                raise ValueError("Typed acceptance criteria require output, operator, value, unit, and rule version together.")
        return self


class RequirementUpdate(BaseModel):
    statement: str | None = Field(default=None, min_length=1)
    rationale: str | None = None
    status: Literal["draft", "active", "retired"] | None = None
    notes: str | None = None
    basis_kind: RequirementBasisKind | None = None
    reconciliation_gate: RequirementGate | None = None
    criterion_output_name: str | None = None
    criterion_operator: CriterionOperator | None = None
    criterion_expected_value: str | None = None
    criterion_expected_unit: str | None = None
    criterion_rule_version: str | None = None


class RequirementProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str
    expected_updated_at: str
    statement: str | None = Field(default=None, min_length=1)
    rationale: str | None = None
    notes: str | None = None
    basis_kind: RequirementBasisKind | None = None
    reconciliation_gate: RequirementGate | None = None
    criterion_output_name: str | None = None
    criterion_operator: CriterionOperator | None = None
    criterion_expected_value: str | None = None
    criterion_expected_unit: str | None = None
    criterion_rule_version: str | None = None


class RequirementRead(RequirementCreate):
    id: str
    workspace_id: str
    schema_version: int
    created_at: str
    updated_at: str


class SimulationRunCreate(BaseModel):
    model_version_id: str | None = None
    run_label: str | None = None
    status: str = "planned"
    input_payload: str | None = None
    parameter_payload: str | None = None
    output_payload: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    notes: str | None = None
    project_knowledge_revision_id: str | None = None


class SimulationRunRead(SimulationRunCreate):
    id: str
    workspace_id: str
    created_at: str


class DecisionCreate(BaseModel):
    title: str = Field(min_length=1)
    decision_text: str = Field(min_length=1)
    rationale: str | None = None
    status: str = "draft"
    linked_run_id: str | None = None
    notes: str | None = None
    basis_lifecycle_state: Literal["active", "retired"] = "active"


class DecisionRead(DecisionCreate):
    id: str
    workspace_id: str
    created_at: str
    updated_at: str


class DecisionProjectUpdate(BaseModel):
    workspace_id: str
    expected_updated_at: str
    title: str | None = Field(default=None, min_length=1)
    decision_text: str | None = Field(default=None, min_length=1)
    rationale: str | None = None
    linked_run_id: str | None = None
    notes: str | None = None
    basis_lifecycle_state: Literal["active", "retired"] | None = None