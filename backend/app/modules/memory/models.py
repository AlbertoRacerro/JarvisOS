from typing import Literal

from pydantic import BaseModel, Field, model_validator

MemoryRecordKind = Literal["assumption", "parameter", "decision"]
MemoryStatus = Literal["proposed", "accepted", "rejected", "superseded"]
MemoryOrigin = Literal["user", "ai_proposed", "calc"]
ParameterValueStatus = Literal["candidate", "literature", "measured", "validated", "accepted"]


class MemoryProposalCreate(BaseModel):
    record_kind: MemoryRecordKind
    workspace_id: str = Field(min_length=1)
    source_ai_job_id: str | None = None
    statement: str | None = None
    scope: str | None = None
    confidence: str | float | None = None
    name: str | None = None
    symbol: str | None = None
    value: str | None = None
    unit: str = "unspecified"
    value_status: ParameterValueStatus = "candidate"
    value_min: float | None = None
    value_max: float | None = None
    source_ref: str | None = None
    supersedes_parameter_id: str | None = None
    title: str | None = None
    decision_text: str | None = None
    rationale: str | None = None
    linked_run_id: str | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def validate_parameter_fields(self) -> "MemoryProposalCreate":
        if self.record_kind != "parameter":
            if self.supersedes_parameter_id is not None:
                raise ValueError("supersedes_parameter_id is supported only for Parameter proposals.")
            return self
        if self.name is None or not self.name.strip():
            raise ValueError("name is required for parameter proposals.")
        if not self.unit.strip():
            raise ValueError("unit is required for parameter proposals.")
        if self.value_min is not None and self.value_max is not None and self.value_min > self.value_max:
            raise ValueError("value_min must be less than or equal to value_max")
        return self


class CalcParameterProposalCreate(BaseModel):
    workspace_id: str = Field(min_length=1)
    runner_job_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    symbol: str | None = None
    value: str | None = None
    unit: str = Field(default="unspecified", min_length=1)
    value_status: ParameterValueStatus = "candidate"
    value_min: float | None = None
    value_max: float | None = None
    confidence: float | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def validate_uncertainty_bounds(self) -> "CalcParameterProposalCreate":
        if self.value_min is not None and self.value_max is not None and self.value_min > self.value_max:
            raise ValueError("value_min must be less than or equal to value_max")
        return self


class MemoryRecordRead(BaseModel):
    id: str
    record_kind: MemoryRecordKind
    workspace_id: str
    status: MemoryStatus
    origin: MemoryOrigin
    source_ai_job_id: str | None = None
    promoted_at: str | None = None
    created_at: str
    updated_at: str
    title: str | None = None
    statement: str | None = None
    decision_text: str | None = None
    name: str | None = None
    source_ref: str | None = None
    notes: str | None = None
    supersedes_parameter_id: str | None = None
    scope: str | None = None
    confidence: str | float | None = None
    symbol: str | None = None
    value: str | None = None
    unit: str | None = None
    value_status: str | None = None
    value_min: float | None = None
    value_max: float | None = None
    rationale: str | None = None
    linked_run_id: str | None = None


class MemoryTransitionRead(BaseModel):
    record: MemoryRecordRead


class ParameterReplacementInvalidationRead(BaseModel):
    id: str
    source_ref: str
    replacement_ref: str
    affected_count: int
    graph_digest: str
    created_at: str


class ParameterReplacementRead(BaseModel):
    accepted_parameter: MemoryRecordRead
    superseded_parameter: MemoryRecordRead
    invalidation: ParameterReplacementInvalidationRead
