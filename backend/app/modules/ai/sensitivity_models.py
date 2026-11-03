from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.packs import default_workspace_slug
from app.modules.ai.context_builder import DEFAULT_CONTEXT_BUDGET_CHARS, MAX_CONTEXT_BLOCKS
from app.modules.ai.models import ContextPackSelectionRequest

SensitivityLevel = Literal["S0", "S1", "S2", "S3", "S4"]
DerivativeLevel = Literal["S0", "S1", "S2"]
ALLOWED_SOURCE_KINDS = frozenset(
    {"decision", "assumption", "parameter", "requirement", "evidence"}
)
# Spec 159: a derivative may also descend from the operator's own conversation
# turn. Interactions are lineage-only: they are never labelled, never selected
# into a context pack and never fed to the canonical sanitizer.
DERIVATIVE_SOURCE_KINDS = ALLOWED_SOURCE_KINDS | {"interaction"}


def _normalize_source_ref(value: str, kinds: frozenset[str] = ALLOWED_SOURCE_KINDS) -> str:
    cleaned = value.strip()
    if ":" not in cleaned:
        raise ValueError("source reference must use <kind>:<id>")
    kind, record_id = cleaned.split(":", 1)
    if kind not in kinds or not record_id.strip():
        raise ValueError("source reference has an unsupported kind or empty id")
    return f"{kind}:{record_id.strip()}"


class SensitivityLabelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(min_length=1)
    subject_ref: str = Field(min_length=3)
    level: SensitivityLevel

    @field_validator("subject_ref")
    @classmethod
    def validate_subject_ref(cls, value: str) -> str:
        return _normalize_source_ref(value)


class SensitivityLabelRead(BaseModel):
    id: str
    workspace_id: str
    subject_ref: str
    content_digest: str
    level: SensitivityLevel
    classification_source: str
    policy_version: str
    actor: str
    prior_label_id: str | None
    created_at: str
    current: bool
    stale_reason: str | None = None


class SanitizedDerivativeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(min_length=1)
    source_refs: list[str] = Field(min_length=1, max_length=20)
    content: str = Field(min_length=1, max_length=DEFAULT_CONTEXT_BUDGET_CHARS)
    effective_level: DerivativeLevel
    transformations: list[str] = Field(min_length=1, max_length=50)

    @field_validator("source_refs")
    @classmethod
    def validate_source_refs(cls, value: list[str]) -> list[str]:
        cleaned = [_normalize_source_ref(item, DERIVATIVE_SOURCE_KINDS) for item in value]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("source_refs must not contain duplicates")
        return cleaned

    @field_validator("transformations")
    @classmethod
    def validate_transformations(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value]
        if any(not item for item in cleaned):
            raise ValueError("transformations must contain non-empty descriptions")
        return cleaned


class SanitizedDerivativeApprove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer_notes: str | None = Field(default=None, max_length=2000)


class SanitizedDerivativeRead(BaseModel):
    id: str
    workspace_id: str
    source_refs: list[str]
    source_digests: dict[str, str]
    content: str
    content_digest: str
    effective_level: DerivativeLevel
    transformations: list[str]
    policy_version: str
    status: Literal["draft", "approved", "revoked", "stale"]
    actor: str
    reviewer: str | None
    reviewed_at: str | None
    stale_reason: str | None
    created_at: str
    updated_at: str


class SensitivityContextPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(default_factory=default_workspace_slug)
    budget_chars: int = Field(default=DEFAULT_CONTEXT_BUDGET_CHARS, ge=1)
    selection: ContextPackSelectionRequest = Field(default_factory=ContextPackSelectionRequest)

    @field_validator("selection")
    @classmethod
    def validate_selection_kinds(
        cls,
        value: ContextPackSelectionRequest,
    ) -> ContextPackSelectionRequest:
        unsupported = sorted(set(value.kinds) - ALLOWED_SOURCE_KINDS)
        if unsupported:
            raise ValueError(f"unsupported context kinds: {unsupported}")
        return value


class ManualContextPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(default_factory=default_workspace_slug)
    budget_chars: int = Field(default=DEFAULT_CONTEXT_BUDGET_CHARS, ge=1)
    context_blocks: list[dict[str, Any]] = Field(default_factory=list, max_length=MAX_CONTEXT_BLOCKS)


class SensitivityContextPreviewResponse(BaseModel):
    blocks: list[dict[str, Any]]
    context_digest: str | None
    included_sources_manifest: list[dict[str, Any]]
    withheld_sources_manifest: list[dict[str, Any]]
    dropped_sources_manifest: list[dict[str, Any]]
    char_count: int
    estimated_token_count: int
    included_count: int
    withheld_count: int
    dropped_count: int
    budget_chars: int
