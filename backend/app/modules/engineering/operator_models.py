"""Small operator read projections for engineering capability discovery."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.engineering.multifidelity import EscalationPolicy


class EvaluatorRead(BaseModel):
    evaluator_id: str
    backend_kind: str | None
    backend_name: str | None
    backend_version: str | None
    fidelity: str | None
    state: str
    reason_code: str | None


class CapabilityRead(BaseModel):
    capability_id: str
    state: Literal["available", "unavailable", "not_configured"]
    reason_code: str | None


class CaseVerdictRead(BaseModel):
    id: str
    workspace_id: str
    case_id: str
    run_id: str
    quantity: str
    computed_value: float
    computed_unit: str
    published_value: float
    published_unit: str
    rel_tol: float
    deviation: float
    verdict: Literal["pass", "fail"]
    status: Literal["recorded", "accepted", "superseded"]
    rationale: str | None = None
    accepted_by: str | None = None
    accepted_at: str | None = None
    created_at: str
    updated_at: str



class EscalationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: EscalationPolicy
    evaluator_ids: list[str] = Field(default_factory=list, max_length=16)
