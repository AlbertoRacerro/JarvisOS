"""Manual CLI-only FastIntakeSignalForm smoke probe.

This module is intentionally not imported by routes or startup code. It calls
only a configured localhost Ollama endpoint when run manually.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from enum import StrEnum
from math import ceil
from pathlib import Path
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.modules.local_ai.classification.adapter import ClassificationAdapterConfig, LocalGemmaClassificationAdapter
from app.modules.local_ai.classification.contracts import (
    DEFAULT_CLASSIFICATION_ENDPOINT_URL,
    DEFAULT_CLASSIFICATION_TEMPERATURE,
    ClassificationAttemptDiagnostics,
    ClassificationFailureCode,
)

REPORT_SCHEMA_VERSION = "fast_intake_probe_report_v1"
REPORT_FILENAME_PREFIX = "fast_intake_probe"
FAST_INTAKE_SCHEMA_VERSION = "fast_intake_v0"
FAST_INTAKE_FLAT_SCHEMA_VERSION = "fast_intake_flat_v0"
FAST_INTAKE_MODE = "smoke"
FAST_INTAKE_FLAT_MODE = "smoke-flat"
FAST_INTAKE_DETERMINISTIC_MODE = "deterministic-baseline"
FAST_INTAKE_NUM_PREDICT = 512
FAST_INTAKE_TIMEOUT_SECONDS = 15.0
FAST_INTAKE_PROMPT_MAX_CHARS = 5000
FAST_INTAKE_REPEAT_COUNT = 1
FAST_INTAKE_CANDIDATE_MODELS = ("qwen3:8b", "gemma4:12b-it-qat")
HIGH_CONFIDENCE_THRESHOLD = 0.8
FAST_INTAKE_FLAT_TOP_LEVEL_FIELDS = (
    "schema_version",
    "contains_user_preference",
    "contains_user_decision",
    "contains_assumption",
    "contains_design_constraint",
    "contains_open_question",
    "contains_action_request",
    "contains_test_result",
    "contains_numbers_or_metrics",
    "mentions_previous_context",
    "mentions_project_or_artifact",
    "mentions_code_or_command",
    "mentions_source_or_literature",
    "storage_relevance",
    "record_bucket",
    "project_bucket",
    "domain_bucket",
    "sensitivity_bucket",
    "status_bucket",
    "needs_enrichment",
    "needs_user_confirmation",
    "uncertainty_reason",
    "confidence_observable",
    "confidence_bucket_assignment",
    "uncertain_fields",
    "advisory_note",
)


class OutputControlVariant(StrEnum):
    think_false = "think_false"
    deterministic = "deterministic"


class OutputRootType(StrEnum):
    object = "object"
    list = "list"
    string = "string"
    empty = "empty"
    invalid_json = "invalid_json"


class LikelyFailureCategory(StrEnum):
    extra_fields = "extra_fields"
    missing_fields = "missing_fields"
    enum_mismatch = "enum_mismatch"
    type_mismatch = "type_mismatch"
    non_object_json = "non_object_json"
    invalid_json = "invalid_json"
    timeout = "timeout"
    empty_output = "empty_output"
    unknown = "unknown"


class StorageRelevance(StrEnum):
    none = "none"
    low = "low"
    medium = "medium"
    high = "high"


class RecordBucket(StrEnum):
    request = "request"
    note = "note"
    decision = "decision"
    assumption = "assumption"
    evidence = "evidence"
    result = "result"
    preference = "preference"
    issue = "issue"
    parameter = "parameter"
    source = "source"
    unknown = "unknown"


class ProjectBucket(StrEnum):
    jarvisos = "jarvisos"
    workspace = "workspace"
    coursework = "coursework"
    personal = "personal"
    general = "general"
    unknown = "unknown"


class DomainBucket(StrEnum):
    local_ai = "local_ai"
    memory = "memory"
    retrieval = "retrieval"
    modeling = "modeling"
    software = "software"
    bioprocess = "bioprocess"
    reactor_design = "reactor_design"
    coursework = "coursework"
    personal = "personal"
    general = "general"
    unknown = "unknown"


class SensitivityBucket(StrEnum):
    public = "public"
    internal = "internal"
    sensitive = "sensitive"
    secret = "secret"
    unknown = "unknown"


class StatusBucket(StrEnum):
    raw = "raw"
    proposed = "proposed"
    accepted = "accepted"
    not_decided = "not_decided"
    unknown = "unknown"


class UncertaintyReason(StrEnum):
    none = "none"
    ambiguous = "ambiguous"
    missing_context = "missing_context"
    sensitive = "sensitive"
    important_decision = "important_decision"
    weak_tags = "weak_tags"
    unknown = "unknown"


class UncertainField(StrEnum):
    contains_user_preference = "contains_user_preference"
    contains_user_decision = "contains_user_decision"
    contains_assumption = "contains_assumption"
    contains_design_constraint = "contains_design_constraint"
    contains_open_question = "contains_open_question"
    contains_action_request = "contains_action_request"
    contains_test_result = "contains_test_result"
    contains_numbers_or_metrics = "contains_numbers_or_metrics"
    mentions_previous_context = "mentions_previous_context"
    mentions_project_or_artifact = "mentions_project_or_artifact"
    mentions_code_or_command = "mentions_code_or_command"
    mentions_source_or_literature = "mentions_source_or_literature"
    storage_relevance = "storage_relevance"
    record_bucket = "record_bucket"
    project_bucket = "project_bucket"
    domain_bucket = "domain_bucket"
    sensitivity_bucket = "sensitivity_bucket"
    status_bucket = "status_bucket"
    needs_enrichment = "needs_enrichment"
    needs_user_confirmation = "needs_user_confirmation"
    uncertainty_reason = "uncertainty_reason"
    confidence_observable = "confidence_observable"
    confidence_bucket_assignment = "confidence_bucket_assignment"


class FastIntakeSuitability(StrEnum):
    rejected = "rejected"
    fast_intake_candidate = "fast_intake_candidate"
    needs_prompt_repair = "needs_prompt_repair"
    needs_more_testing = "needs_more_testing"


class FastIntakeSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_id: str = Field(min_length=1, max_length=80)
    conversation_id: str | None = Field(default=None, max_length=80)
    timestamp: str | None = Field(default=None, max_length=80)
    raw_text_preserved: bool

    @field_validator("raw_text_preserved")
    @classmethod
    def raw_text_must_be_preserved(cls, value: bool) -> bool:
        if value is not True:
            raise ValueError("raw_text_preserved must be true")
        return value


class ObservableFlags(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contains_user_preference: bool
    contains_user_decision: bool
    contains_assumption: bool
    contains_design_constraint: bool
    contains_open_question: bool
    contains_action_request: bool
    contains_test_result: bool
    contains_numbers_or_metrics: bool
    mentions_previous_context: bool
    mentions_project_or_artifact: bool
    mentions_code_or_command: bool
    mentions_source_or_literature: bool


class BroadStorageBuckets(BaseModel):
    model_config = ConfigDict(extra="forbid")

    storage_relevance: StorageRelevance
    record_bucket: RecordBucket
    project_bucket: ProjectBucket
    domain_bucket: DomainBucket
    sensitivity_bucket: SensitivityBucket
    status_bucket: StatusBucket


class ExplicitMentions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entities: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    projects: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    artifacts: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    commits_or_versions: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    numbers_or_metrics: tuple[str, ...] = Field(default_factory=tuple, max_length=8)

    @field_validator("entities", "projects", "artifacts", "commits_or_versions", "numbers_or_metrics")
    @classmethod
    def mentions_must_be_short(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if len(item) > 80:
                raise ValueError("explicit mentions must be at most 80 characters")
        return value


class ShortDescription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    surface_summary: str = Field(max_length=180)
    preserved_user_phrasing: tuple[str, ...] = Field(default_factory=tuple, max_length=3)

    @field_validator("preserved_user_phrasing")
    @classmethod
    def preserved_phrasing_must_be_short(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        for item in value:
            if len(item) > 80:
                raise ValueError("preserved user phrasing must be at most 80 characters")
        return value


class Uncertainty(BaseModel):
    model_config = ConfigDict(extra="forbid")

    needs_enrichment: bool
    needs_user_confirmation: bool
    reason: UncertaintyReason


class IntakeConfidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observable: float = Field(ge=0, le=1)
    bucket_assignment: float = Field(ge=0, le=1)


class FastIntakeSignalForm(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    source: FastIntakeSource
    observable_flags: ObservableFlags
    broad_storage_buckets: BroadStorageBuckets
    explicit_mentions: ExplicitMentions
    short_description: ShortDescription
    uncertainty: Uncertainty
    confidence: IntakeConfidence

    @field_validator("schema_version")
    @classmethod
    def schema_version_must_match(cls, value: str) -> str:
        if value != FAST_INTAKE_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {FAST_INTAKE_SCHEMA_VERSION}")
        return value


class FastIntakeFlatSignalV0(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    contains_user_preference: bool
    contains_user_decision: bool
    contains_assumption: bool
    contains_design_constraint: bool
    contains_open_question: bool
    contains_action_request: bool
    contains_test_result: bool
    contains_numbers_or_metrics: bool
    mentions_previous_context: bool
    mentions_project_or_artifact: bool
    mentions_code_or_command: bool
    mentions_source_or_literature: bool
    storage_relevance: StorageRelevance
    record_bucket: RecordBucket
    project_bucket: ProjectBucket
    domain_bucket: DomainBucket
    sensitivity_bucket: SensitivityBucket
    status_bucket: StatusBucket
    needs_enrichment: bool
    needs_user_confirmation: bool
    uncertainty_reason: UncertaintyReason
    confidence_observable: float = Field(ge=0, le=1)
    confidence_bucket_assignment: float = Field(ge=0, le=1)
    uncertain_fields: tuple[UncertainField, ...] = Field(default_factory=tuple, max_length=5)
    advisory_note: str = Field(default="", max_length=160)

    @field_validator("schema_version")
    @classmethod
    def schema_version_must_match(cls, value: str) -> str:
        if value != FAST_INTAKE_FLAT_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {FAST_INTAKE_FLAT_SCHEMA_VERSION}")
        return value


class FastIntakeValidationDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    output_root_type: OutputRootType
    json_parse_ok: bool
    top_level_keys: tuple[str, ...] = ()
    missing_top_level_fields: tuple[str, ...] = ()
    extra_top_level_fields: tuple[str, ...] = ()
    pydantic_error_paths: tuple[str, ...] = ()
    pydantic_error_types: tuple[str, ...] = ()
    enum_error_paths: tuple[str, ...] = ()
    type_error_paths: tuple[str, ...] = ()
    missing_field_paths: tuple[str, ...] = ()
    extra_field_paths: tuple[str, ...] = ()
    likely_failure_category: LikelyFailureCategory


class ExpectedFastIntake(BaseModel):
    model_config = ConfigDict(extra="forbid")

    observable_flags: ObservableFlags
    broad_storage_buckets: BroadStorageBuckets
    explicit_mentions: ExplicitMentions = Field(default_factory=ExplicitMentions)
    uncertainty: Uncertainty


class FastIntakeProbeCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    text: str = Field(min_length=1, max_length=800)
    input_id: str
    conversation_id: str | None = None
    timestamp: str | None = None
    expected: ExpectedFastIntake


class FastIntakeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str
    model_name: str
    output_control: OutputControlVariant = OutputControlVariant.think_false
    num_predict: int = Field(default=FAST_INTAKE_NUM_PREDICT, ge=1, le=512)
    timeout_seconds: float = Field(default=FAST_INTAKE_TIMEOUT_SECONDS, ge=0.1, le=60)


class FastIntakeCaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    profile_id: str
    model_name: str
    output_control: OutputControlVariant
    num_predict: int = Field(ge=1, le=512)
    latency_ms: int | None = Field(default=None, ge=0)
    done_reason: str | None = None
    raw_content_empty: bool
    thinking_present: bool | None = None
    schema_valid: bool
    accepted: bool
    fallback_used: bool
    fallback_reason: ClassificationFailureCode | None = None
    confidence_observable: float | None = Field(default=None, ge=0, le=1)
    confidence_bucket_assignment: float | None = Field(default=None, ge=0, le=1)
    observable_flag_agreement_rate: float | None = Field(default=None, ge=0, le=1)
    bucket_agreement_rate: float | None = Field(default=None, ge=0, le=1)
    explicit_mentions_partial_match_rate: float | None = Field(default=None, ge=0, le=1)
    storage_relevance_match: bool | None = None
    record_bucket_match: bool | None = None
    project_bucket_match: bool | None = None
    domain_bucket_match: bool | None = None
    sensitivity_bucket_match: bool | None = None
    status_bucket_match: bool | None = None
    returned_observable_flags: dict[str, bool] = Field(default_factory=dict)
    returned_broad_storage_buckets: dict[str, str] = Field(default_factory=dict)
    returned_explicit_mentions: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    returned_uncertainty: dict[str, str | bool] = Field(default_factory=dict)
    short_description_present: bool = False
    phrasing_count: int = Field(default=0, ge=0)
    uncertain_fields: tuple[str, ...] = ()
    advisory_note_present: bool = False
    advisory_note_chars: int = Field(default=0, ge=0, le=160)
    validation_diagnostics: FastIntakeValidationDiagnostics | None = None
    overconfident_wrong: bool = False
    runtime_approved: bool = False


class FastIntakeProfileSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile_id: str
    model_name: str
    attempts: int = Field(ge=0)
    schema_valid_rate: float | None = Field(default=None, ge=0, le=1)
    accepted_rate: float | None = Field(default=None, ge=0, le=1)
    fallback_rate: float | None = Field(default=None, ge=0, le=1)
    empty_content_count: int = Field(ge=0)
    thinking_present_count: int = Field(ge=0)
    done_reason_length_count: int = Field(ge=0)
    timeout_count: int = Field(ge=0)
    mean_latency_ms: float | None = Field(default=None, ge=0)
    p95_latency_ms: int | None = Field(default=None, ge=0)
    observable_flag_agreement_rate: float | None = Field(default=None, ge=0, le=1)
    bucket_agreement_rate: float | None = Field(default=None, ge=0, le=1)
    explicit_mentions_partial_match_rate: float | None = Field(default=None, ge=0, le=1)
    storage_relevance_accuracy: float | None = Field(default=None, ge=0, le=1)
    record_bucket_accuracy: float | None = Field(default=None, ge=0, le=1)
    project_bucket_accuracy: float | None = Field(default=None, ge=0, le=1)
    domain_bucket_accuracy: float | None = Field(default=None, ge=0, le=1)
    sensitivity_bucket_accuracy: float | None = Field(default=None, ge=0, le=1)
    status_bucket_accuracy: float | None = Field(default=None, ge=0, le=1)
    overconfident_wrong_count: int = Field(ge=0)
    uncertain_fields_frequency: dict[str, int] = Field(default_factory=dict)
    advisory_note_present_count: int = Field(ge=0)
    dominant_fallback_reason: ClassificationFailureCode | None = None
    fallback_reason_counts: dict[str, int] = Field(default_factory=dict)
    suitability_label: FastIntakeSuitability
    runtime_approved: bool = False


class FastIntakeComparisonSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_profile_id: str
    compared_profile_id: str
    compared_model_name: str
    observable_flag_agreement_delta: float | None = None
    bucket_agreement_delta: float | None = None
    storage_relevance_accuracy_delta: float | None = None
    record_bucket_accuracy_delta: float | None = None
    project_bucket_accuracy_delta: float | None = None
    domain_bucket_accuracy_delta: float | None = None
    sensitivity_bucket_accuracy_delta: float | None = None
    status_bucket_accuracy_delta: float | None = None
    deterministic_better_fields: tuple[str, ...] = ()
    ai_better_fields: tuple[str, ...] = ()
    tied_fields: tuple[str, ...] = ()
    runtime_approved: bool = False


class FastIntakeProbeReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = REPORT_SCHEMA_VERSION
    mode: str
    created_at_utc: str
    endpoint: str
    temperature: float = Field(ge=0, le=0)
    timeout_seconds: float = Field(ge=0.1, le=300)
    num_predict: int = Field(ge=1, le=512)
    repeat_count: int = Field(default=1, ge=1)
    output_control: OutputControlVariant
    installed_model_names: tuple[str, ...] = ()
    candidate_model_names: tuple[str, ...] = ()
    profile_ids: tuple[str, ...] = ()
    case_ids: tuple[str, ...]
    results: list[FastIntakeCaseResult]
    profile_summaries: list[FastIntakeProfileSummary]
    comparison_summaries: list[FastIntakeComparisonSummary] = Field(default_factory=list)


AdapterFactory = Callable[[ClassificationAdapterConfig], LocalGemmaClassificationAdapter]
InstalledModelFetcher = Callable[[str, float], Iterable[str]]


class FastIntakeOutputControlAdapter(LocalGemmaClassificationAdapter):
    def _payload(self, prompt: str) -> dict[str, object]:
        ClassificationAdapterConfig.model_validate(self.config.model_dump())
        if len(prompt) > FAST_INTAKE_PROMPT_MAX_CHARS:
            raise ValueError("fast intake prompt exceeds bounded prompt budget")
        parsed = urlparse(self.config.endpoint_url)
        if parsed.path == "/api/chat":
            return {
                "model": self.config.model_name,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "format": "json",
                "think": False,
                "options": {"temperature": self.config.temperature, "num_predict": self.config.max_output_tokens},
            }
        return {
            "model": self.config.model_name,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_output_tokens,
            "stream": False,
            "response_format": {"type": "json_object"},
        }


def build_fast_intake_prompt(case: FastIntakeProbeCase) -> str:
    source = {
        "input_id": case.input_id,
        "conversation_id": case.conversation_id,
        "timestamp": case.timestamp,
        "raw_text_preserved": True,
    }
    prompt = f"""Fill FastIntakeSignalForm JSON for one memory-intake input.

Rules:
- Return one JSON object only.
- Preserve source exactly: {json.dumps(source, separators=(',', ':'))}
- Use only broad buckets and observable flags.
- This is not canonical memory and authorizes nothing.
- Do not add tool, provider, route, retrieval, execution, memory_write, promotion, or final_sensitivity fields.
- Keep short_description brief. Empty arrays are OK.

Allowed enum values:
storage_relevance: none, low, medium, high
record_bucket: request, note, decision, assumption, evidence, result, preference, issue, parameter, source, unknown
project_bucket: jarvisos, workspace, coursework, personal, general, unknown
domain_bucket: local_ai, memory, retrieval, modeling, software, bioprocess, reactor_design, coursework, personal, general, unknown
sensitivity_bucket: public, internal, sensitive, secret, unknown
status_bucket: raw, proposed, accepted, not_decided, unknown
uncertainty.reason: none, ambiguous, missing_context, sensitive, important_decision, weak_tags, unknown

Required top-level keys:
schema_version, source, observable_flags, broad_storage_buckets, explicit_mentions, short_description, uncertainty, confidence

Input:
{case.text}
"""
    if len(prompt) > FAST_INTAKE_PROMPT_MAX_CHARS:
        raise ValueError("fast intake prompt exceeds bounded prompt budget")
    return prompt


def build_fast_intake_flat_prompt(case: FastIntakeProbeCase) -> str:
    prompt = f"""You are a local JSON extraction model inside JarvisOS.

Return exactly one JSON object.
Do not use markdown.
Do not explain outside JSON.
Do not add extra keys.
Do not nest objects.
Use exactly the keys shown in the template.
Use only the enum values shown.

Your output is advisory only.
It cannot authorize tools, providers, retrieval, memory writes, actions, routes, final sensitivity, or canonical promotion.

If you are unsure about specific fields, list those field names in uncertain_fields.
If you need to add one short non-authoritative observation, use advisory_note.
Do not create any other note/reasoning/analysis fields.

Fill this flat JSON template:

{{
  "schema_version": "fast_intake_flat_v0",

  "contains_user_preference": false,
  "contains_user_decision": false,
  "contains_assumption": false,
  "contains_design_constraint": false,
  "contains_open_question": false,
  "contains_action_request": false,
  "contains_test_result": false,
  "contains_numbers_or_metrics": false,
  "mentions_previous_context": false,
  "mentions_project_or_artifact": false,
  "mentions_code_or_command": false,
  "mentions_source_or_literature": false,

  "storage_relevance": "none",
  "record_bucket": "unknown",
  "project_bucket": "unknown",
  "domain_bucket": "unknown",
  "sensitivity_bucket": "unknown",
  "status_bucket": "raw",

  "needs_enrichment": false,
  "needs_user_confirmation": false,
  "uncertainty_reason": "none",

  "confidence_observable": 0.0,
  "confidence_bucket_assignment": 0.0,

  "uncertain_fields": [],
  "advisory_note": ""
}}

Allowed enum values:
storage_relevance = none | low | medium | high
record_bucket = request | note | decision | assumption | evidence | result | preference | issue | parameter | source | unknown
project_bucket = jarvisos | workspace | coursework | personal | general | unknown
domain_bucket = local_ai | memory | retrieval | software | modeling | bioprocess | reactor_design | coursework | personal | general | unknown
sensitivity_bucket = public | internal | sensitive | secret | unknown
status_bucket = raw | proposed | accepted | not_decided | unknown
uncertainty_reason = none | ambiguous | missing_context | sensitive | important_decision | weak_tags | unknown

Allowed uncertain_fields values:
contains_user_preference, contains_user_decision, contains_assumption, contains_design_constraint, contains_open_question, contains_action_request, contains_test_result, contains_numbers_or_metrics, mentions_previous_context, mentions_project_or_artifact, mentions_code_or_command, mentions_source_or_literature, storage_relevance, record_bucket, project_bucket, domain_bucket, sensitivity_bucket, status_bucket, needs_enrichment, needs_user_confirmation, uncertainty_reason, confidence_observable, confidence_bucket_assignment

Guidance:
- If the input is casual and not worth memory, use storage_relevance = none or low.
- If the input contains a durable user preference, use record_bucket = preference.
- If the input says something is decided, use record_bucket = decision.
- If the input says something is possible, candidate, tentative, or not final, use record_bucket = assumption and status_bucket = not_decided or proposed.
- If the input reports test results, commits, metrics, or run outcomes, use record_bucket = result.
- If the input asks to do something, use record_bucket = request and contains_action_request = true.
- If the input asks a question, use contains_open_question = true.
- If unsure, use unknown and lower confidence.
- Never invent project names or source references.
- Keep advisory_note under 160 characters. Empty string is allowed.

Input:
{case.text}
"""
    if len(prompt) > FAST_INTAKE_PROMPT_MAX_CHARS:
        raise ValueError("fast intake flat prompt exceeds bounded prompt budget")
    return prompt


def parse_fast_intake_output(response_text: str) -> FastIntakeSignalForm:
    try:
        raw = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ValueError(ClassificationFailureCode.invalid_json.value) from exc
    if not isinstance(raw, dict):
        raise ValueError(ClassificationFailureCode.non_object_json.value)
    try:
        return FastIntakeSignalForm.model_validate(raw)
    except ValidationError as exc:
        errors = exc.errors()
        if any(error.get("type") == "extra_forbidden" for error in errors):
            raise ValueError(ClassificationFailureCode.extra_fields.value) from exc
        raise ValueError(ClassificationFailureCode.schema_invalid.value) from exc


def parse_fast_intake_flat_output(response_text: str) -> FastIntakeFlatSignalV0:
    try:
        raw = json.loads(response_text)
    except json.JSONDecodeError as exc:
        raise ValueError(ClassificationFailureCode.invalid_json.value) from exc
    if not isinstance(raw, dict):
        raise ValueError(ClassificationFailureCode.non_object_json.value)
    try:
        return FastIntakeFlatSignalV0.model_validate(raw)
    except ValidationError as exc:
        errors = exc.errors()
        if any(error.get("type") == "extra_forbidden" for error in errors):
            raise ValueError(ClassificationFailureCode.extra_fields.value) from exc
        raise ValueError(ClassificationFailureCode.schema_invalid.value) from exc


def normalize_flat_to_fast_intake_form(
    flat_output: FastIntakeFlatSignalV0,
    case: FastIntakeProbeCase,
) -> FastIntakeSignalForm:
    return FastIntakeSignalForm(
        schema_version=FAST_INTAKE_SCHEMA_VERSION,
        source=FastIntakeSource(
            input_id=case.input_id,
            conversation_id=case.conversation_id,
            timestamp=case.timestamp,
            raw_text_preserved=True,
        ),
        observable_flags=ObservableFlags(
            contains_user_preference=flat_output.contains_user_preference,
            contains_user_decision=flat_output.contains_user_decision,
            contains_assumption=flat_output.contains_assumption,
            contains_design_constraint=flat_output.contains_design_constraint,
            contains_open_question=flat_output.contains_open_question,
            contains_action_request=flat_output.contains_action_request,
            contains_test_result=flat_output.contains_test_result,
            contains_numbers_or_metrics=flat_output.contains_numbers_or_metrics,
            mentions_previous_context=flat_output.mentions_previous_context,
            mentions_project_or_artifact=flat_output.mentions_project_or_artifact,
            mentions_code_or_command=flat_output.mentions_code_or_command,
            mentions_source_or_literature=flat_output.mentions_source_or_literature,
        ),
        broad_storage_buckets=BroadStorageBuckets(
            storage_relevance=flat_output.storage_relevance,
            record_bucket=flat_output.record_bucket,
            project_bucket=flat_output.project_bucket,
            domain_bucket=flat_output.domain_bucket,
            sensitivity_bucket=flat_output.sensitivity_bucket,
            status_bucket=flat_output.status_bucket,
        ),
        explicit_mentions=ExplicitMentions(),
        short_description=ShortDescription(surface_summary="", preserved_user_phrasing=()),
        uncertainty=Uncertainty(
            needs_enrichment=flat_output.needs_enrichment,
            needs_user_confirmation=flat_output.needs_user_confirmation,
            reason=flat_output.uncertainty_reason,
        ),
        confidence=IntakeConfidence(
            observable=flat_output.confidence_observable,
            bucket_assignment=flat_output.confidence_bucket_assignment,
        ),
    )


def build_fast_intake_smoke_report(
    *,
    mode: str = FAST_INTAKE_MODE,
    endpoint_url: str = DEFAULT_CLASSIFICATION_ENDPOINT_URL,
    timeout_seconds: float = FAST_INTAKE_TIMEOUT_SECONDS,
    installed_model_names: Iterable[str] | None = None,
    installed_model_fetcher: InstalledModelFetcher | None = None,
    profiles: Iterable[FastIntakeProfile] | None = None,
    cases: Iterable[FastIntakeProbeCase] | None = None,
    adapter_factory: AdapterFactory = FastIntakeOutputControlAdapter,
    created_at_utc: datetime | None = None,
) -> FastIntakeProbeReport:
    if mode not in {FAST_INTAKE_MODE, FAST_INTAKE_FLAT_MODE}:
        raise ValueError("fast intake mode must be smoke or smoke-flat")
    installed = tuple(installed_model_names) if installed_model_names is not None else tuple(
        (installed_model_fetcher or _ollama_installed_model_names)(endpoint_url, timeout_seconds)
    )
    profile_candidates = tuple(profiles or _default_profiles())
    runnable_profiles = tuple(profile for profile in profile_candidates if profile.model_name in installed)
    case_list = tuple(cases or fast_intake_probe_cases())
    results: list[FastIntakeCaseResult] = []
    for profile in runnable_profiles:
        for case in case_list:
            adapter = adapter_factory(
                ClassificationAdapterConfig(
                    endpoint_url=endpoint_url,
                    model_name=profile.model_name,
                    timeout_seconds=profile.timeout_seconds,
                    max_output_tokens=profile.num_predict,
                    temperature=DEFAULT_CLASSIFICATION_TEMPERATURE,
                )
            )
            results.append(_case_result(case, profile, adapter, mode=mode))
    summaries = [_profile_summary(profile, results) for profile in runnable_profiles]
    created = created_at_utc or datetime.now(UTC)
    return FastIntakeProbeReport(
        mode=mode,
        created_at_utc=created.isoformat(),
        endpoint=ClassificationAdapterConfig(endpoint_url=endpoint_url).endpoint_url,
        temperature=DEFAULT_CLASSIFICATION_TEMPERATURE,
        timeout_seconds=timeout_seconds,
        num_predict=FAST_INTAKE_NUM_PREDICT,
        repeat_count=FAST_INTAKE_REPEAT_COUNT,
        output_control=OutputControlVariant.think_false,
        installed_model_names=installed,
        candidate_model_names=tuple(profile.model_name for profile in runnable_profiles),
        profile_ids=tuple(profile.profile_id for profile in runnable_profiles),
        case_ids=tuple(case.case_id for case in case_list),
        results=results,
        profile_summaries=summaries,
    )


def build_fast_intake_deterministic_report(
    *,
    cases: Iterable[FastIntakeProbeCase] | None = None,
    previous_report_path: Path | None = None,
    created_at_utc: datetime | None = None,
) -> FastIntakeProbeReport:
    case_list = tuple(cases or fast_intake_probe_cases())
    profile = FastIntakeProfile(
        profile_id="deterministic_fast_intake_baseline",
        model_name="deterministic_rules",
        output_control=OutputControlVariant.deterministic,
        num_predict=1,
        timeout_seconds=0.1,
    )
    results = [_deterministic_case_result(case, profile) for case in case_list]
    summary = _profile_summary(profile, results)
    comparison_path = previous_report_path if previous_report_path is not None else _latest_report_path(FAST_INTAKE_FLAT_MODE)
    previous_report = _load_report(comparison_path)
    created = created_at_utc or datetime.now(UTC)
    return FastIntakeProbeReport(
        mode=FAST_INTAKE_DETERMINISTIC_MODE,
        created_at_utc=created.isoformat(),
        endpoint="local-deterministic",
        temperature=0,
        timeout_seconds=0.1,
        num_predict=1,
        repeat_count=1,
        output_control=OutputControlVariant.deterministic,
        installed_model_names=(),
        candidate_model_names=(profile.model_name,),
        profile_ids=(profile.profile_id,),
        case_ids=tuple(case.case_id for case in case_list),
        results=results,
        profile_summaries=[summary],
        comparison_summaries=_comparison_summaries(summary, previous_report),
    )


def fast_intake_probe_cases() -> tuple[FastIntakeProbeCase, ...]:
    return (
        _case(
            "jarvisos_memory_decision",
            "Memory ingestion should be cheap at write time; full contextual interpretation should happen later only when the memory is retrieved or promoted.",
            flags=dict(contains_user_decision=True, contains_design_constraint=True, mentions_project_or_artifact=True),
            buckets=dict(
                storage_relevance=StorageRelevance.high,
                record_bucket=RecordBucket.decision,
                project_bucket=ProjectBucket.jarvisos,
                domain_bucket=DomainBucket.memory,
                sensitivity_bucket=SensitivityBucket.internal,
                status_bucket=StatusBucket.proposed,
            ),
            mentions=dict(projects=("JarvisOS",)),
            uncertainty=dict(needs_enrichment=True, reason=UncertaintyReason.important_decision),
        ),
        _case(
            "jarvisos_casual_low_value",
            "ok grazie",
            buckets=dict(
                storage_relevance=StorageRelevance.low,
                record_bucket=RecordBucket.unknown,
                project_bucket=ProjectBucket.unknown,
                domain_bucket=DomainBucket.unknown,
                sensitivity_bucket=SensitivityBucket.public,
                status_bucket=StatusBucket.raw,
            ),
        ),
        _case(
            "workspace_tentative_assumption",
            "For the workspace project, ETFE is a candidate material for the tubes, but it is not decided yet.",
            flags=dict(contains_assumption=True, contains_design_constraint=True, mentions_project_or_artifact=True),
            buckets=dict(
                storage_relevance=StorageRelevance.high,
                record_bucket=RecordBucket.assumption,
                project_bucket=ProjectBucket.workspace,
                domain_bucket=DomainBucket.reactor_design,
                sensitivity_bucket=SensitivityBucket.sensitive,
                status_bucket=StatusBucket.not_decided,
            ),
            mentions=dict(entities=("ETFE",), projects=("workspace",)),
            uncertainty=dict(needs_enrichment=True, reason=UncertaintyReason.important_decision),
        ),
        _case(
            "workspace_confirmed_design_decision",
            "Decision: the workspace project will treat ETFE tube material as accepted for the current reviewed concept note.",
            flags=dict(contains_user_decision=True, contains_design_constraint=True, mentions_project_or_artifact=True),
            buckets=dict(
                storage_relevance=StorageRelevance.high,
                record_bucket=RecordBucket.decision,
                project_bucket=ProjectBucket.workspace,
                domain_bucket=DomainBucket.reactor_design,
                sensitivity_bucket=SensitivityBucket.sensitive,
                status_bucket=StatusBucket.accepted,
            ),
            mentions=dict(entities=("ETFE",), projects=("workspace",), artifacts=("concept note",)),
            uncertainty=dict(needs_enrichment=True, reason=UncertaintyReason.important_decision),
        ),
        _case(
            "codex_report_commit_metrics",
            "Codex report: commit c137038 passed 299 backend tests and git diff --check for the staged memory intake docs.",
            flags=dict(
                contains_test_result=True,
                contains_numbers_or_metrics=True,
                mentions_project_or_artifact=True,
                mentions_code_or_command=True,
            ),
            buckets=dict(
                storage_relevance=StorageRelevance.high,
                record_bucket=RecordBucket.result,
                project_bucket=ProjectBucket.jarvisos,
                domain_bucket=DomainBucket.software,
                sensitivity_bucket=SensitivityBucket.internal,
                status_bucket=StatusBucket.accepted,
            ),
            mentions=dict(commits_or_versions=("c137038",), numbers_or_metrics=("299",)),
            uncertainty=dict(needs_enrichment=False, reason=UncertaintyReason.none),
        ),
        _case(
            "coursework_question",
            "Can you explain how residence time affects conversion in a CSTR for my chemical engineering coursework?",
            flags=dict(contains_open_question=True),
            buckets=dict(
                storage_relevance=StorageRelevance.medium,
                record_bucket=RecordBucket.request,
                project_bucket=ProjectBucket.coursework,
                domain_bucket=DomainBucket.coursework,
                sensitivity_bucket=SensitivityBucket.public,
                status_bucket=StatusBucket.raw,
            ),
            uncertainty=dict(needs_enrichment=False, reason=UncertaintyReason.none),
        ),
        _case(
            "literature_source_reference",
            "Source note: Smith 2021 reports kLa values near 180 1/h for a stirred bioreactor under aerobic conditions.",
            flags=dict(
                contains_numbers_or_metrics=True,
                mentions_source_or_literature=True,
                mentions_project_or_artifact=True,
            ),
            buckets=dict(
                storage_relevance=StorageRelevance.high,
                record_bucket=RecordBucket.source,
                project_bucket=ProjectBucket.general,
                domain_bucket=DomainBucket.bioprocess,
                sensitivity_bucket=SensitivityBucket.public,
                status_bucket=StatusBucket.proposed,
            ),
            mentions=dict(entities=("Smith 2021",), numbers_or_metrics=("180 1/h",)),
            uncertainty=dict(needs_enrichment=True, reason=UncertaintyReason.missing_context),
        ),
        _case(
            "open_question_memory_design",
            "Open question: should fast intake store weak project buckets or leave them unknown until enrichment?",
            flags=dict(contains_open_question=True, mentions_previous_context=True),
            buckets=dict(
                storage_relevance=StorageRelevance.high,
                record_bucket=RecordBucket.issue,
                project_bucket=ProjectBucket.jarvisos,
                domain_bucket=DomainBucket.memory,
                sensitivity_bucket=SensitivityBucket.internal,
                status_bucket=StatusBucket.not_decided,
            ),
            uncertainty=dict(needs_enrichment=True, reason=UncertaintyReason.missing_context),
        ),
        _case(
            "user_preference",
            "I prefer concise engineering summaries with clear assumptions and no marketing language.",
            flags=dict(contains_user_preference=True),
            buckets=dict(
                storage_relevance=StorageRelevance.high,
                record_bucket=RecordBucket.preference,
                project_bucket=ProjectBucket.personal,
                domain_bucket=DomainBucket.general,
                sensitivity_bucket=SensitivityBucket.internal,
                status_bucket=StatusBucket.accepted,
            ),
            uncertainty=dict(needs_enrichment=False, reason=UncertaintyReason.none),
        ),
        _case(
            "jarvisos_action_request",
            "Please run the backend tests and commit the JarvisOS documentation patch if everything passes.",
            flags=dict(
                contains_action_request=True,
                mentions_project_or_artifact=True,
                mentions_code_or_command=True,
            ),
            buckets=dict(
                storage_relevance=StorageRelevance.medium,
                record_bucket=RecordBucket.request,
                project_bucket=ProjectBucket.jarvisos,
                domain_bucket=DomainBucket.software,
                sensitivity_bucket=SensitivityBucket.internal,
                status_bucket=StatusBucket.raw,
            ),
            uncertainty=dict(needs_enrichment=False, reason=UncertaintyReason.none),
        ),
        _case(
            "secret_placeholder",
            "Do not store this placeholder secret: API_KEY_PLACEHOLDER_12345.",
            flags=dict(contains_numbers_or_metrics=True, mentions_code_or_command=True),
            buckets=dict(
                storage_relevance=StorageRelevance.high,
                record_bucket=RecordBucket.note,
                project_bucket=ProjectBucket.unknown,
                domain_bucket=DomainBucket.software,
                sensitivity_bucket=SensitivityBucket.secret,
                status_bucket=StatusBucket.raw,
            ),
            mentions=dict(artifacts=("API_KEY_PLACEHOLDER_12345",), numbers_or_metrics=("12345",)),
            uncertainty=dict(
                needs_enrichment=True,
                needs_user_confirmation=True,
                reason=UncertaintyReason.sensitive,
            ),
        ),
        _case(
            "ambiguous_followup",
            "That one should stay as proposed for now.",
            flags=dict(mentions_previous_context=True),
            buckets=dict(
                storage_relevance=StorageRelevance.medium,
                record_bucket=RecordBucket.note,
                project_bucket=ProjectBucket.unknown,
                domain_bucket=DomainBucket.unknown,
                sensitivity_bucket=SensitivityBucket.unknown,
                status_bucket=StatusBucket.proposed,
            ),
            uncertainty=dict(needs_enrichment=True, reason=UncertaintyReason.missing_context),
        ),
    )


def write_probe_report(report: FastIntakeProbeReport, report_dir: Path | None = None) -> Path:
    target_dir = report_dir or default_report_dir()
    target_dir.mkdir(parents=True, exist_ok=True)
    created = datetime.fromisoformat(report.created_at_utc)
    timestamp = created.strftime("%Y%m%dT%H%M%S")
    path = target_dir / f"{REPORT_FILENAME_PREFIX}_{report.mode}_{timestamp}.json"
    path.write_text(report.model_dump_json(indent=2), encoding="utf-8")
    return path


def summary_lines(report: FastIntakeProbeReport, report_path: Path) -> list[str]:
    lines = [
        f"report={report_path}",
        (
            f"mode={report.mode} cases={len(report.case_ids)} profiles={len(report.profile_ids)} "
            f"results={len(report.results)}"
        ),
    ]
    for summary in report.profile_summaries:
        lines.append(
            
                f"profile={summary.profile_id} model={summary.model_name} attempts={summary.attempts} "
                f"schema_valid_rate={summary.schema_valid_rate} accepted_rate={summary.accepted_rate} "
                f"fallback_rate={summary.fallback_rate} observable_flags={summary.observable_flag_agreement_rate} "
                f"buckets={summary.bucket_agreement_rate} mentions={summary.explicit_mentions_partial_match_rate} "
                f"mean_latency_ms={summary.mean_latency_ms} p95_latency_ms={summary.p95_latency_ms} "
                f"overconfident_wrong={summary.overconfident_wrong_count} "
                f"advisory_note_present={summary.advisory_note_present_count} "
                f"dominant_fallback={summary.dominant_fallback_reason} suitability={summary.suitability_label}"
            
        )
        lines.append(
            f"profile={summary.profile_id} uncertain_fields={summary.uncertain_fields_frequency}"
        )
        lines.append(
            
                f"profile={summary.profile_id} bucket_accuracy "
                f"storage={summary.storage_relevance_accuracy},record={summary.record_bucket_accuracy},"
                f"project={summary.project_bucket_accuracy},domain={summary.domain_bucket_accuracy},"
                f"sensitivity={summary.sensitivity_bucket_accuracy},status={summary.status_bucket_accuracy}"
            
        )
    for comparison in report.comparison_summaries:
        lines.append(
            
                f"comparison baseline={comparison.baseline_profile_id} compared={comparison.compared_profile_id} "
                f"model={comparison.compared_model_name} observable_delta="
                f"{comparison.observable_flag_agreement_delta} bucket_delta={comparison.bucket_agreement_delta} "
                f"deterministic_better={comparison.deterministic_better_fields} "
                f"ai_better={comparison.ai_better_fields} tied={comparison.tied_fields}"
            
        )
    return lines


def default_report_dir() -> Path:
    return Path(__file__).resolve().parents[4] / "local_eval_reports"


def _latest_report_path(mode: str) -> Path | None:
    matches = sorted(default_report_dir().glob(f"{REPORT_FILENAME_PREFIX}_{mode}_*.json"))
    if not matches:
        return None
    return matches[-1]


def _load_report(path: Path | None) -> FastIntakeProbeReport | None:
    if path is None or not path.exists():
        return None
    return FastIntakeProbeReport.model_validate_json(path.read_text(encoding="utf-8"))


def _comparison_summaries(
    baseline: FastIntakeProfileSummary,
    previous_report: FastIntakeProbeReport | None,
) -> list[FastIntakeComparisonSummary]:
    if previous_report is None:
        return []
    return [_comparison_summary(baseline, compared) for compared in previous_report.profile_summaries]


def _comparison_summary(
    baseline: FastIntakeProfileSummary,
    compared: FastIntakeProfileSummary,
) -> FastIntakeComparisonSummary:
    metric_pairs = {
        "storage_relevance": (baseline.storage_relevance_accuracy, compared.storage_relevance_accuracy),
        "record_bucket": (baseline.record_bucket_accuracy, compared.record_bucket_accuracy),
        "project_bucket": (baseline.project_bucket_accuracy, compared.project_bucket_accuracy),
        "domain_bucket": (baseline.domain_bucket_accuracy, compared.domain_bucket_accuracy),
        "sensitivity_bucket": (baseline.sensitivity_bucket_accuracy, compared.sensitivity_bucket_accuracy),
        "status_bucket": (baseline.status_bucket_accuracy, compared.status_bucket_accuracy),
    }
    deterministic_better = tuple(
        field
        for field, (baseline_value, compared_value) in metric_pairs.items()
        if baseline_value is not None and compared_value is not None and baseline_value > compared_value
    )
    ai_better = tuple(
        field
        for field, (baseline_value, compared_value) in metric_pairs.items()
        if baseline_value is not None and compared_value is not None and baseline_value < compared_value
    )
    tied = tuple(
        field
        for field, (baseline_value, compared_value) in metric_pairs.items()
        if baseline_value is not None and compared_value is not None and baseline_value == compared_value
    )
    return FastIntakeComparisonSummary(
        baseline_profile_id=baseline.profile_id,
        compared_profile_id=compared.profile_id,
        compared_model_name=compared.model_name,
        observable_flag_agreement_delta=_delta(
            baseline.observable_flag_agreement_rate,
            compared.observable_flag_agreement_rate,
        ),
        bucket_agreement_delta=_delta(baseline.bucket_agreement_rate, compared.bucket_agreement_rate),
        storage_relevance_accuracy_delta=_delta(
            baseline.storage_relevance_accuracy,
            compared.storage_relevance_accuracy,
        ),
        record_bucket_accuracy_delta=_delta(baseline.record_bucket_accuracy, compared.record_bucket_accuracy),
        project_bucket_accuracy_delta=_delta(baseline.project_bucket_accuracy, compared.project_bucket_accuracy),
        domain_bucket_accuracy_delta=_delta(baseline.domain_bucket_accuracy, compared.domain_bucket_accuracy),
        sensitivity_bucket_accuracy_delta=_delta(
            baseline.sensitivity_bucket_accuracy,
            compared.sensitivity_bucket_accuracy,
        ),
        status_bucket_accuracy_delta=_delta(baseline.status_bucket_accuracy, compared.status_bucket_accuracy),
        deterministic_better_fields=deterministic_better,
        ai_better_fields=ai_better,
        tied_fields=tied,
        runtime_approved=False,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manual local FastIntakeSignalForm smoke probe.")
    parser.add_argument(
        "--mode",
        choices=(FAST_INTAKE_MODE, FAST_INTAKE_FLAT_MODE, FAST_INTAKE_DETERMINISTIC_MODE),
        default=FAST_INTAKE_MODE,
    )
    args = parser.parse_args(argv)
    if args.mode == FAST_INTAKE_DETERMINISTIC_MODE:
        report = build_fast_intake_deterministic_report()
    else:
        report = build_fast_intake_smoke_report(mode=args.mode)
    path = write_probe_report(report)
    for line in summary_lines(report, path):
        print(line)
    return 0


def _deterministic_case_result(case: FastIntakeProbeCase, profile: FastIntakeProfile) -> FastIntakeCaseResult:
    from app.modules.local_ai_eval.deterministic_signals import deterministic_fast_intake_baseline

    flat_output = deterministic_fast_intake_baseline(case.text)
    output = normalize_flat_to_fast_intake_form(flat_output, case)
    flag_rate = _observable_flag_agreement(output, case.expected)
    bucket_matches = _bucket_matches(output, case.expected)
    bucket_rate = _ratio(sum(1 for item in bucket_matches.values() if item), len(bucket_matches))
    mention_rate = _mentions_partial_match(output, case.expected)
    overconfident_wrong = _overconfident_wrong(output, flag_rate, bucket_matches)
    return FastIntakeCaseResult(
        case_id=case.case_id,
        profile_id=profile.profile_id,
        model_name=profile.model_name,
        output_control=profile.output_control,
        num_predict=profile.num_predict,
        latency_ms=0,
        done_reason="deterministic",
        raw_content_empty=False,
        thinking_present=False,
        schema_valid=True,
        accepted=True,
        fallback_used=False,
        fallback_reason=None,
        confidence_observable=output.confidence.observable,
        confidence_bucket_assignment=output.confidence.bucket_assignment,
        observable_flag_agreement_rate=flag_rate,
        bucket_agreement_rate=bucket_rate,
        explicit_mentions_partial_match_rate=mention_rate,
        storage_relevance_match=bucket_matches["storage_relevance"],
        record_bucket_match=bucket_matches["record_bucket"],
        project_bucket_match=bucket_matches["project_bucket"],
        domain_bucket_match=bucket_matches["domain_bucket"],
        sensitivity_bucket_match=bucket_matches["sensitivity_bucket"],
        status_bucket_match=bucket_matches["status_bucket"],
        returned_observable_flags=output.observable_flags.model_dump(),
        returned_broad_storage_buckets={
            key: str(value) for key, value in output.broad_storage_buckets.model_dump().items()
        },
        returned_explicit_mentions=_redacted_mentions(output.explicit_mentions),
        returned_uncertainty=output.uncertainty.model_dump(),
        short_description_present=False,
        phrasing_count=0,
        uncertain_fields=tuple(item.value for item in flat_output.uncertain_fields),
        advisory_note_present=False,
        advisory_note_chars=0,
        validation_diagnostics=None,
        overconfident_wrong=overconfident_wrong,
        runtime_approved=False,
    )


def _case_result(
    case: FastIntakeProbeCase,
    profile: FastIntakeProfile,
    adapter: LocalGemmaClassificationAdapter,
    *,
    mode: str,
) -> FastIntakeCaseResult:
    prompt = build_fast_intake_flat_prompt(case) if mode == FAST_INTAKE_FLAT_MODE else build_fast_intake_prompt(case)
    result = adapter.complete(prompt, input_chars=len(case.text))
    diagnostics = result.diagnostics
    if not result.success or not result.response_text:
        return _failure_case_result(
            case,
            profile,
            diagnostics,
            result.failure_code or ClassificationFailureCode.unknown,
            validation_diagnostics=_diagnostics_for_adapter_failure(result.failure_code, diagnostics),
        )
    uncertain_fields: tuple[str, ...] = ()
    advisory_note_present = False
    advisory_note_chars = 0
    try:
        if mode == FAST_INTAKE_FLAT_MODE:
            flat_output = parse_fast_intake_flat_output(result.response_text)
            output = normalize_flat_to_fast_intake_form(flat_output, case)
            uncertain_fields = tuple(item.value for item in flat_output.uncertain_fields)
            advisory_note_present = bool(flat_output.advisory_note.strip())
            advisory_note_chars = len(flat_output.advisory_note)
        else:
            output = parse_fast_intake_output(result.response_text)
    except ValueError as exc:
        reason = _failure_code_from_value_error(exc)
        validation_diagnostics = _sanitized_validation_diagnostics(result.response_text)
        return _failure_case_result(case, profile, diagnostics, reason, validation_diagnostics=validation_diagnostics)
    flag_rate = _observable_flag_agreement(output, case.expected)
    bucket_matches = _bucket_matches(output, case.expected)
    bucket_rate = _ratio(sum(1 for item in bucket_matches.values() if item), len(bucket_matches))
    mention_rate = _mentions_partial_match(output, case.expected)
    overconfident_wrong = _overconfident_wrong(output, flag_rate, bucket_matches)
    return FastIntakeCaseResult(
        case_id=case.case_id,
        profile_id=profile.profile_id,
        model_name=profile.model_name,
        output_control=profile.output_control,
        num_predict=profile.num_predict,
        latency_ms=diagnostics.latency_ms,
        done_reason=diagnostics.done_reason,
        raw_content_empty=diagnostics.raw_content_empty,
        thinking_present=diagnostics.thinking_present,
        schema_valid=True,
        accepted=True,
        fallback_used=False,
        fallback_reason=None,
        confidence_observable=output.confidence.observable,
        confidence_bucket_assignment=output.confidence.bucket_assignment,
        observable_flag_agreement_rate=flag_rate,
        bucket_agreement_rate=bucket_rate,
        explicit_mentions_partial_match_rate=mention_rate,
        storage_relevance_match=bucket_matches["storage_relevance"],
        record_bucket_match=bucket_matches["record_bucket"],
        project_bucket_match=bucket_matches["project_bucket"],
        domain_bucket_match=bucket_matches["domain_bucket"],
        sensitivity_bucket_match=bucket_matches["sensitivity_bucket"],
        status_bucket_match=bucket_matches["status_bucket"],
        returned_observable_flags=output.observable_flags.model_dump(),
        returned_broad_storage_buckets={
            key: str(value) for key, value in output.broad_storage_buckets.model_dump().items()
        },
        returned_explicit_mentions=_redacted_mentions(output.explicit_mentions),
        returned_uncertainty=output.uncertainty.model_dump(),
        short_description_present=bool(output.short_description.surface_summary.strip()),
        phrasing_count=len(output.short_description.preserved_user_phrasing),
        uncertain_fields=uncertain_fields,
        advisory_note_present=advisory_note_present,
        advisory_note_chars=advisory_note_chars,
        overconfident_wrong=overconfident_wrong,
        runtime_approved=False,
    )


def _failure_case_result(
    case: FastIntakeProbeCase,
    profile: FastIntakeProfile,
    diagnostics: ClassificationAttemptDiagnostics,
    fallback_reason: ClassificationFailureCode,
    *,
    validation_diagnostics: FastIntakeValidationDiagnostics | None = None,
) -> FastIntakeCaseResult:
    return FastIntakeCaseResult(
        case_id=case.case_id,
        profile_id=profile.profile_id,
        model_name=profile.model_name,
        output_control=profile.output_control,
        num_predict=profile.num_predict,
        latency_ms=diagnostics.latency_ms,
        done_reason=diagnostics.done_reason,
        raw_content_empty=diagnostics.raw_content_empty,
        thinking_present=diagnostics.thinking_present,
        schema_valid=False,
        accepted=False,
        fallback_used=True,
        fallback_reason=fallback_reason,
        validation_diagnostics=validation_diagnostics,
        runtime_approved=False,
    )


def _profile_summary(profile: FastIntakeProfile, results: list[FastIntakeCaseResult]) -> FastIntakeProfileSummary:
    group = [result for result in results if result.profile_id == profile.profile_id]
    fallback_reasons = Counter(result.fallback_reason for result in group if result.fallback_reason is not None)
    dominant_reason = fallback_reasons.most_common(1)[0][0] if fallback_reasons else None
    latencies = [result.latency_ms for result in group if result.latency_ms is not None]
    summary = FastIntakeProfileSummary(
        profile_id=profile.profile_id,
        model_name=profile.model_name,
        attempts=len(group),
        schema_valid_rate=_ratio(sum(1 for result in group if result.schema_valid), len(group)),
        accepted_rate=_ratio(sum(1 for result in group if result.accepted), len(group)),
        fallback_rate=_ratio(sum(1 for result in group if result.fallback_used), len(group)),
        empty_content_count=sum(1 for result in group if result.raw_content_empty),
        thinking_present_count=sum(1 for result in group if result.thinking_present is True),
        done_reason_length_count=sum(
            1
            for result in group
            if result.done_reason == "length" or result.fallback_reason == ClassificationFailureCode.done_reason_length
        ),
        timeout_count=sum(1 for result in group if result.fallback_reason == ClassificationFailureCode.timeout),
        mean_latency_ms=_mean([float(item) for item in latencies]),
        p95_latency_ms=_p95(latencies),
        observable_flag_agreement_rate=_mean_present(
            [result.observable_flag_agreement_rate for result in group if result.observable_flag_agreement_rate is not None]
        ),
        bucket_agreement_rate=_mean_present(
            [result.bucket_agreement_rate for result in group if result.bucket_agreement_rate is not None]
        ),
        explicit_mentions_partial_match_rate=_mean_present(
            [
                result.explicit_mentions_partial_match_rate
                for result in group
                if result.explicit_mentions_partial_match_rate is not None
            ]
        ),
        storage_relevance_accuracy=_bool_accuracy(group, "storage_relevance_match"),
        record_bucket_accuracy=_bool_accuracy(group, "record_bucket_match"),
        project_bucket_accuracy=_bool_accuracy(group, "project_bucket_match"),
        domain_bucket_accuracy=_bool_accuracy(group, "domain_bucket_match"),
        sensitivity_bucket_accuracy=_bool_accuracy(group, "sensitivity_bucket_match"),
        status_bucket_accuracy=_bool_accuracy(group, "status_bucket_match"),
        overconfident_wrong_count=sum(1 for result in group if result.overconfident_wrong),
        uncertain_fields_frequency=_uncertain_fields_frequency(group),
        advisory_note_present_count=sum(1 for result in group if result.advisory_note_present),
        dominant_fallback_reason=dominant_reason,
        fallback_reason_counts=dict(sorted((reason.value, count) for reason, count in fallback_reasons.items())),
        suitability_label=FastIntakeSuitability.needs_more_testing,
        runtime_approved=False,
    )
    return summary.model_copy(update={"suitability_label": _suitability(summary)})


def _suitability(summary: FastIntakeProfileSummary) -> FastIntakeSuitability:
    if summary.attempts == 0:
        return FastIntakeSuitability.rejected
    if summary.schema_valid_rate is None or summary.schema_valid_rate < 0.5:
        return FastIntakeSuitability.rejected
    if (
        summary.timeout_count
        or summary.empty_content_count
        or summary.thinking_present_count
        or summary.done_reason_length_count
        or (summary.fallback_rate or 0) > 0.2
    ):
        return FastIntakeSuitability.needs_prompt_repair
    if (
        (summary.schema_valid_rate or 0) >= 0.95
        and (summary.accepted_rate or 0) >= 0.95
        and (summary.observable_flag_agreement_rate or 0) >= 0.8
        and (summary.bucket_agreement_rate or 0) >= 0.7
        and summary.overconfident_wrong_count <= 1
    ):
        return FastIntakeSuitability.fast_intake_candidate
    if (summary.observable_flag_agreement_rate or 0) >= 0.7 and (summary.bucket_agreement_rate or 0) >= 0.5:
        return FastIntakeSuitability.needs_more_testing
    return FastIntakeSuitability.needs_prompt_repair


def _sanitized_validation_diagnostics(response_text: str | None) -> FastIntakeValidationDiagnostics:
    if response_text is None or response_text.strip() == "":
        return FastIntakeValidationDiagnostics(
            output_root_type=OutputRootType.empty,
            json_parse_ok=False,
            missing_top_level_fields=FAST_INTAKE_FLAT_TOP_LEVEL_FIELDS,
            likely_failure_category=LikelyFailureCategory.empty_output,
        )
    try:
        raw = json.loads(response_text)
    except json.JSONDecodeError:
        return FastIntakeValidationDiagnostics(
            output_root_type=OutputRootType.invalid_json,
            json_parse_ok=False,
            likely_failure_category=LikelyFailureCategory.invalid_json,
        )
    if not isinstance(raw, dict):
        root_type = OutputRootType.list if isinstance(raw, list) else OutputRootType.string
        return FastIntakeValidationDiagnostics(
            output_root_type=root_type,
            json_parse_ok=True,
            likely_failure_category=LikelyFailureCategory.non_object_json,
        )
    top_level_keys = tuple(sorted(_sanitize_path_part(key) for key in raw))
    missing = tuple(field for field in FAST_INTAKE_FLAT_TOP_LEVEL_FIELDS if field not in raw)
    extra = tuple(sorted(_sanitize_path_part(key) for key in raw if key not in FAST_INTAKE_FLAT_TOP_LEVEL_FIELDS))
    try:
        FastIntakeFlatSignalV0.model_validate(raw)
    except ValidationError as exc:
        errors = exc.errors()
        paths = tuple(_error_path(error) for error in errors)
        types = tuple(str(error.get("type", "unknown")) for error in errors)
        enum_paths = tuple(path for path, error_type in zip(paths, types, strict=True) if error_type == "enum")
        type_paths = tuple(
            path
            for path, error_type in zip(paths, types, strict=True)
            if error_type.endswith("_type") or error_type.endswith("_parsing")
        )
        missing_paths = tuple(path for path, error_type in zip(paths, types, strict=True) if error_type == "missing")
        extra_paths = tuple(path for path, error_type in zip(paths, types, strict=True) if error_type == "extra_forbidden")
        return FastIntakeValidationDiagnostics(
            output_root_type=OutputRootType.object,
            json_parse_ok=True,
            top_level_keys=top_level_keys,
            missing_top_level_fields=missing,
            extra_top_level_fields=extra,
            pydantic_error_paths=paths,
            pydantic_error_types=types,
            enum_error_paths=enum_paths,
            type_error_paths=type_paths,
            missing_field_paths=missing_paths,
            extra_field_paths=extra_paths,
            likely_failure_category=_likely_validation_failure(missing_paths, extra_paths, enum_paths, type_paths),
        )
    return FastIntakeValidationDiagnostics(
        output_root_type=OutputRootType.object,
        json_parse_ok=True,
        top_level_keys=top_level_keys,
        missing_top_level_fields=missing,
        extra_top_level_fields=extra,
        likely_failure_category=LikelyFailureCategory.unknown,
    )


def _diagnostics_for_adapter_failure(
    failure_code: ClassificationFailureCode | None,
    diagnostics: ClassificationAttemptDiagnostics,
) -> FastIntakeValidationDiagnostics:
    if failure_code == ClassificationFailureCode.timeout:
        category = LikelyFailureCategory.timeout
    elif diagnostics.raw_content_empty:
        category = LikelyFailureCategory.empty_output
    else:
        category = LikelyFailureCategory.unknown
    return FastIntakeValidationDiagnostics(
        output_root_type=OutputRootType.empty if diagnostics.raw_content_empty else OutputRootType.invalid_json,
        json_parse_ok=False,
        missing_top_level_fields=FAST_INTAKE_FLAT_TOP_LEVEL_FIELDS,
        likely_failure_category=category,
    )


def _likely_validation_failure(
    missing_paths: tuple[str, ...],
    extra_paths: tuple[str, ...],
    enum_paths: tuple[str, ...],
    type_paths: tuple[str, ...],
) -> LikelyFailureCategory:
    if extra_paths:
        return LikelyFailureCategory.extra_fields
    if missing_paths:
        return LikelyFailureCategory.missing_fields
    if enum_paths:
        return LikelyFailureCategory.enum_mismatch
    if type_paths:
        return LikelyFailureCategory.type_mismatch
    return LikelyFailureCategory.unknown


def _error_path(error: dict[str, object]) -> str:
    loc = error.get("loc")
    if not isinstance(loc, tuple):
        return "unknown"
    return ".".join(_sanitize_path_part(str(part)) for part in loc)


def _sanitize_path_part(value: str) -> str:
    return _redact_mention(value)


def _uncertain_fields_frequency(results: list[FastIntakeCaseResult]) -> dict[str, int]:
    counter = Counter(field for result in results for field in result.uncertain_fields)
    return dict(sorted(counter.items()))


def _observable_flag_agreement(output: FastIntakeSignalForm, expected: ExpectedFastIntake) -> float:
    returned = output.observable_flags.model_dump()
    target = expected.observable_flags.model_dump()
    matches = sum(1 for key, value in target.items() if returned.get(key) == value)
    return _ratio(matches, len(target)) or 0


def _bucket_matches(output: FastIntakeSignalForm, expected: ExpectedFastIntake) -> dict[str, bool]:
    returned = output.broad_storage_buckets
    target = expected.broad_storage_buckets
    return {
        "storage_relevance": returned.storage_relevance == target.storage_relevance,
        "record_bucket": returned.record_bucket == target.record_bucket,
        "project_bucket": returned.project_bucket == target.project_bucket,
        "domain_bucket": returned.domain_bucket == target.domain_bucket,
        "sensitivity_bucket": returned.sensitivity_bucket == target.sensitivity_bucket,
        "status_bucket": returned.status_bucket == target.status_bucket,
    }


def _mentions_partial_match(output: FastIntakeSignalForm, expected: ExpectedFastIntake) -> float | None:
    expected_mentions = expected.explicit_mentions.model_dump()
    returned_mentions = output.explicit_mentions.model_dump()
    rates: list[float] = []
    for field, expected_values in expected_mentions.items():
        expected_set = {_normalize_mention(item) for item in expected_values}
        if not expected_set:
            continue
        returned_set = {_normalize_mention(item) for item in returned_mentions.get(field, ())}
        rates.append(_ratio(len(expected_set & returned_set), len(expected_set)) or 0)
    return _mean(rates)


def _overconfident_wrong(
    output: FastIntakeSignalForm,
    observable_rate: float,
    bucket_matches: dict[str, bool],
) -> bool:
    high_confidence = (
        output.confidence.observable >= HIGH_CONFIDENCE_THRESHOLD
        or output.confidence.bucket_assignment >= HIGH_CONFIDENCE_THRESHOLD
    )
    if not high_confidence:
        return False
    important_bucket_wrong = not all(
        bucket_matches[field]
        for field in ("storage_relevance", "record_bucket", "project_bucket", "domain_bucket", "sensitivity_bucket")
    )
    return observable_rate < 0.75 or important_bucket_wrong


def _case(
    case_id: str,
    text: str,
    *,
    flags: dict[str, bool] | None = None,
    buckets: dict[str, StorageRelevance | RecordBucket | ProjectBucket | DomainBucket | SensitivityBucket | StatusBucket],
    mentions: dict[str, tuple[str, ...]] | None = None,
    uncertainty: dict[str, bool | UncertaintyReason] | None = None,
) -> FastIntakeProbeCase:
    flag_values = _default_flags()
    flag_values.update(flags or {})
    uncertainty_values: dict[str, bool | UncertaintyReason] = {
        "needs_enrichment": False,
        "needs_user_confirmation": False,
        "reason": UncertaintyReason.none,
    }
    uncertainty_values.update(uncertainty or {})
    return FastIntakeProbeCase(
        case_id=case_id,
        text=text,
        input_id=f"synthetic-{case_id}",
        conversation_id="synthetic-fast-intake",
        timestamp="2026-06-21T00:00:00Z",
        expected=ExpectedFastIntake(
            observable_flags=ObservableFlags(**flag_values),
            broad_storage_buckets=BroadStorageBuckets(**buckets),
            explicit_mentions=ExplicitMentions(**(mentions or {})),
            uncertainty=Uncertainty(**uncertainty_values),
        ),
    )


def _default_flags() -> dict[str, bool]:
    return {
        "contains_user_preference": False,
        "contains_user_decision": False,
        "contains_assumption": False,
        "contains_design_constraint": False,
        "contains_open_question": False,
        "contains_action_request": False,
        "contains_test_result": False,
        "contains_numbers_or_metrics": False,
        "mentions_previous_context": False,
        "mentions_project_or_artifact": False,
        "mentions_code_or_command": False,
        "mentions_source_or_literature": False,
    }


def _default_profiles() -> tuple[FastIntakeProfile, ...]:
    return (
        FastIntakeProfile(profile_id="qwen8_fast_intake_think_false", model_name="qwen3:8b"),
        FastIntakeProfile(profile_id="gemma12_fast_intake_think_false", model_name="gemma4:12b-it-qat"),
    )


def _ollama_installed_model_names(endpoint_url: str, timeout_seconds: float) -> tuple[str, ...]:
    config = ClassificationAdapterConfig(endpoint_url=endpoint_url, timeout_seconds=timeout_seconds)
    client = httpx.Client(timeout=config.timeout_seconds)
    try:
        response = client.get("http://localhost:11434/api/tags", timeout=config.timeout_seconds)
        response.raise_for_status()
        payload = response.json()
    finally:
        client.close()
    models = payload.get("models")
    if not isinstance(models, list):
        return ()
    names = []
    for item in models:
        if isinstance(item, dict) and isinstance(item.get("name"), str):
            names.append(item["name"])
    return tuple(names)


def _failure_code_from_value_error(exc: ValueError) -> ClassificationFailureCode:
    message = str(exc)
    for code in ClassificationFailureCode:
        if code.value in message:
            return code
    return ClassificationFailureCode.schema_invalid


def _normalize_mention(value: str) -> str:
    return value.strip().casefold()


def _redacted_mentions(mentions: ExplicitMentions) -> dict[str, tuple[str, ...]]:
    return {
        key: tuple(_redact_mention(item) for item in value)
        for key, value in mentions.model_dump().items()
    }


def _redact_mention(value: str) -> str:
    lowered = value.casefold()
    if any(marker in lowered for marker in ("api_key", "secret", "token", "password", "key_placeholder")):
        return "[redacted]"
    return value


def _bool_accuracy(results: list[FastIntakeCaseResult], attr: str) -> float | None:
    values = [getattr(result, attr) for result in results if getattr(result, attr) is not None]
    return _ratio(sum(1 for item in values if item), len(values))


def _p95(values: list[int]) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    return round(sum(values) / len(values), 3)


def _mean_present(values: list[float]) -> float | None:
    return _mean(values)


def _delta(left: float | None, right: float | None) -> float | None:
    if left is None or right is None:
        return None
    return round(left - right, 3)


def _ratio(numerator: int, denominator: int) -> float | None:
    if denominator == 0:
        return None
    return round(numerator / denominator, 3)


if __name__ == "__main__":
    raise SystemExit(main())
