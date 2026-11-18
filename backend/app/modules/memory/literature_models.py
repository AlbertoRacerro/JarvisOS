from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.modules.engineering.evaluator_contracts import ResultCheck, Verdict

LiteratureSourceKind = Literal["paper", "book", "report", "standard", "dataset", "web", "other"]
LiteratureState = Literal["raw", "review", "accepted"]
LiteratureEntryKind = Literal["claim", "datum"]
LiteratureLocatorKind = Literal["page", "line", "section"]
LiteratureUsedByKind = Literal["parameter", "assumption", "artifact"]


class StrictLiteratureModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LiteratureSourceCreate(StrictLiteratureModel):
    title: str = Field(min_length=1, max_length=500)
    source_kind: LiteratureSourceKind
    state: LiteratureState = "raw"
    artifact_id: str | None = Field(default=None, min_length=1)
    citation: str | None = Field(default=None, max_length=4000)
    publisher: str | None = Field(default=None, max_length=500)
    published_year: int | None = Field(default=None, ge=0, le=9999)
    request_key: str | None = Field(default=None, min_length=1, max_length=128)


class LiteratureBackingAttach(StrictLiteratureModel):
    artifact_id: str = Field(min_length=1)


class LiteratureEntryCreate(StrictLiteratureModel):
    entry_kind: LiteratureEntryKind
    statement: str | None = Field(default=None, max_length=8000)
    value_text: str | None = Field(default=None, max_length=4000)
    value_number: float | None = Field(default=None, allow_inf_nan=False)
    unit: str | None = Field(default=None, max_length=128)
    status: LiteratureState = "raw"
    locator_kind: LiteratureLocatorKind | None = None
    locator_start: int | None = Field(default=None, ge=1)
    locator_end: int | None = Field(default=None, ge=1)
    context_text: str | None = Field(default=None, max_length=12000)
    request_key: str | None = Field(default=None, min_length=1, max_length=128)
    quantity: str | None = None

    @model_validator(mode="after")
    def validate_semantics(self) -> LiteratureEntryCreate:
        if self.entry_kind == "claim" and not (self.statement or "").strip():
            raise ValueError("claim entries require a non-empty statement")
        if self.entry_kind == "datum" and self.value_number is None and not (self.value_text or "").strip():
            raise ValueError("datum entries require value_number or value_text")
        if self.quantity is not None:
            stripped = self.quantity.strip()
            if not (1 <= len(stripped) <= 128):
                raise ValueError("quantity must be 1 to 128 characters")
            self.quantity = stripped
            if self.entry_kind != "datum":
                raise ValueError("quantity is only allowed for datum entries")
        if self.locator_kind is None and (self.locator_start is not None or self.locator_end is not None):
            raise ValueError("locator offsets require locator_kind")
        if self.locator_kind is not None and self.locator_start is None:
            raise ValueError("locator_kind requires locator_start")
        if self.locator_end is not None and self.locator_start is not None and self.locator_end < self.locator_start:
            raise ValueError("locator_end must be greater than or equal to locator_start")
        return self


class LiteratureEntryAccept(StrictLiteratureModel):
    quantity: str | None = None

    @model_validator(mode="after")
    def validate_semantics(self) -> LiteratureEntryAccept:
        if self.quantity is not None:
            stripped = self.quantity.strip()
            if not (1 <= len(stripped) <= 128):
                raise ValueError("quantity must be 1 to 128 characters")
            self.quantity = stripped
        return self


class LiteratureBackingRead(StrictLiteratureModel):
    artifact_id: str
    filename: str | None
    mime_type: str | None
    sha256: str | None
    availability: str = Field(pattern="^(available|missing|ineligible|unsupported|unsafe)$")
    content_available: bool
    content_url: str | None
    page_text: Literal["available", "no_text", "unreadable"] | None = None


class LiteratureUsedByRead(StrictLiteratureModel):
    kind: LiteratureUsedByKind
    record_id: str
    title: str
    ref: str


class LiteratureIngestionCheckRead(StrictLiteratureModel):
    check: str
    passed: bool | None
    reason: str | None


class LiteratureEntryRead(StrictLiteratureModel):
    id: str
    workspace_id: str
    source_id: str
    entry_kind: LiteratureEntryKind
    statement: str | None
    value_text: str | None
    value_number: float | None
    unit: str | None
    status: LiteratureState
    locator_kind: LiteratureLocatorKind | None
    locator_start: int | None
    locator_end: int | None
    context_text: str | None
    provenance_ref: str
    used_by: list[LiteratureUsedByRead]
    checks: list[ResultCheck] = Field(default_factory=list)
    verdict: Verdict | None = None
    quantity: str | None = None
    conditions: list[dict[str, object]] = Field(default_factory=list)
    ingestion_status: Literal["source_checked", "flagged"] | None = None
    ingestion_checks: list[LiteratureIngestionCheckRead] = Field(default_factory=list)
    batch_id: str | None = None
    stale_source: bool = False
    eligible_for_later_admission: bool = False
    eligibility_reasons: list[str] = Field(default_factory=list)
    created_at: str
    updated_at: str


class LiteratureIngestionBatchRead(StrictLiteratureModel):
    id: str
    state: Literal["running", "done", "failed", "model_unavailable", "unavailable"]
    reason: str | None
    proposals_count: int
    source_checked_count: int
    flagged_count: int
    duplicate_entry_ids: list[str]
    truncated: bool
    notification_state: Literal["none", "pending", "read"]
    ai_job_ids: list[str]
    current: bool
    created_at: str
    updated_at: str


class LiteratureSourceRead(StrictLiteratureModel):
    id: str
    workspace_id: str
    title: str
    source_kind: LiteratureSourceKind
    state: LiteratureState
    citation: str | None
    publisher: str | None
    published_year: int | None
    source_ref: str
    backing: LiteratureBackingRead | None
    entries: list[LiteratureEntryRead]
    ingestion_batch: LiteratureIngestionBatchRead | None = None
    created_at: str
    updated_at: str


class LiteratureSourcePage(StrictLiteratureModel):
    items: list[LiteratureSourceRead]
    offset: int
    limit: int
    total: int
    next_offset: int | None
