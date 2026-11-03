"""Deterministic staged-memory intake signals.

These rules are intentionally conservative. They provide a local baseline for
observable fields and ownership checks; they do not authorize memory writes,
retrieval, tools, providers, or canonical promotion.
"""

from __future__ import annotations

import re
from enum import StrEnum

from app.core.packs import mentions_pack_term
from app.modules.local_ai_eval.fast_intake_probe import (
    DomainBucket,
    FastIntakeFlatSignalV0,
    ProjectBucket,
    RecordBucket,
    SensitivityBucket,
    StatusBucket,
    StorageRelevance,
    UncertainField,
    UncertaintyReason,
)


class FieldOwnership(StrEnum):
    DETERMINISTIC_OWNED = "deterministic_owned"
    DETERMINISTIC_FIRST = "deterministic_first"
    HYBRID = "hybrid"
    AI_ADVISORY = "ai_advisory"
    DIAGNOSTIC_ONLY = "diagnostic_only"
    UNTRUSTED_FOR_RUNTIME = "untrusted_for_runtime"


FIELD_OWNERSHIP: dict[str, FieldOwnership] = {
    "schema_version": FieldOwnership.DETERMINISTIC_OWNED,
    "contains_numbers_or_metrics": FieldOwnership.DETERMINISTIC_FIRST,
    "mentions_code_or_command": FieldOwnership.DETERMINISTIC_FIRST,
    "mentions_project_or_artifact": FieldOwnership.DETERMINISTIC_FIRST,
    "mentions_source_or_literature": FieldOwnership.DETERMINISTIC_FIRST,
    "contains_user_preference": FieldOwnership.AI_ADVISORY,
    "contains_user_decision": FieldOwnership.AI_ADVISORY,
    "contains_assumption": FieldOwnership.AI_ADVISORY,
    "contains_design_constraint": FieldOwnership.AI_ADVISORY,
    "contains_open_question": FieldOwnership.AI_ADVISORY,
    "contains_action_request": FieldOwnership.AI_ADVISORY,
    "contains_test_result": FieldOwnership.AI_ADVISORY,
    "mentions_previous_context": FieldOwnership.AI_ADVISORY,
    "storage_relevance": FieldOwnership.HYBRID,
    "record_bucket": FieldOwnership.HYBRID,
    "project_bucket": FieldOwnership.DETERMINISTIC_FIRST,
    "domain_bucket": FieldOwnership.HYBRID,
    "sensitivity_bucket": FieldOwnership.DETERMINISTIC_FIRST,
    "status_bucket": FieldOwnership.HYBRID,
    "needs_enrichment": FieldOwnership.HYBRID,
    "needs_user_confirmation": FieldOwnership.HYBRID,
    "uncertainty_reason": FieldOwnership.HYBRID,
    "confidence_observable": FieldOwnership.AI_ADVISORY,
    "confidence_bucket_assignment": FieldOwnership.AI_ADVISORY,
    "uncertain_fields": FieldOwnership.DIAGNOSTIC_ONLY,
    "advisory_note": FieldOwnership.DIAGNOSTIC_ONLY,
    "advisory_note_present": FieldOwnership.DIAGNOSTIC_ONLY,
    "advisory_note_chars": FieldOwnership.DIAGNOSTIC_ONLY,
    "raw_advisory_note_content": FieldOwnership.DIAGNOSTIC_ONLY,
    "source.input_id": FieldOwnership.DETERMINISTIC_OWNED,
    "source.conversation_id": FieldOwnership.DETERMINISTIC_OWNED,
    "source.timestamp": FieldOwnership.DETERMINISTIC_OWNED,
    "source.raw_text_preserved": FieldOwnership.DETERMINISTIC_OWNED,
    "runtime_approved": FieldOwnership.DETERMINISTIC_OWNED,
    "canonical_promotion": FieldOwnership.DETERMINISTIC_OWNED,
    "memory_write_authorization": FieldOwnership.DETERMINISTIC_OWNED,
    "retrieval_authorization": FieldOwnership.DETERMINISTIC_OWNED,
    "tool_authorization": FieldOwnership.DETERMINISTIC_OWNED,
    "provider_authorization": FieldOwnership.DETERMINISTIC_OWNED,
    "route_selection": FieldOwnership.DETERMINISTIC_OWNED,
    "final_sensitivity_decision": FieldOwnership.DETERMINISTIC_OWNED,
    "sensitivity_downgrade": FieldOwnership.UNTRUSTED_FOR_RUNTIME,
    "secret_public_downgrade": FieldOwnership.UNTRUSTED_FOR_RUNTIME,
    "accepted_canonical_promotion": FieldOwnership.UNTRUSTED_FOR_RUNTIME,
    "workspace_assumption_acceptance": FieldOwnership.UNTRUSTED_FOR_RUNTIME,
    "provider_execution": FieldOwnership.UNTRUSTED_FOR_RUNTIME,
    "tool_execution": FieldOwnership.UNTRUSTED_FOR_RUNTIME,
    "retrieval_execution": FieldOwnership.UNTRUSTED_FOR_RUNTIME,
    "action_execution": FieldOwnership.UNTRUSTED_FOR_RUNTIME,
}


SECRET_PATTERN = re.compile(
    r"(?i)\b(api[_-]?key|secret|token|password|private[_-]?key|credential|"
    r"key_placeholder|\.env)\b"
)
METRIC_PATTERN = re.compile(
    r"(?i)(\d+(?:\.\d+)?|%|\b\d+(?:\.\d+)?\s*/\s*h\b|"
    r"\b[a-f0-9]{7,40}\b|schema-valid|accepted_rate|fallback_rate|"
    r"passed\s+\d+|failed\s+\d+)"
)
CODE_PATTERN = re.compile(
    r"(?i)(\bgit\b|\bpytest\b|\bpython\s+-m\b|\bpowershell\b|\bcommit\b|"
    r"\bbackend\b|\btests?\b|api[_-]?key|key_placeholder|\.py\b|\.md\b|"
    r"\.json\b|[A-Za-z]:\\|/)"
)
SOURCE_PATTERN = re.compile(
    r"(?i)(\bsource\b|\bliterature\b|\bpaper\b|\bdoi\b|\bjournal\b|"
    r"\bauthors?\b|\b20\d{2}\b|\b19\d{2}\b)"
)
PROJECT_PATTERN = re.compile(
    r"(?i)(jarvisos|codex|fast\s*intake|local_ai|local ai|gemma|qwen|"
    r"\bprovider\b|\bretrieval\b|memory intake|memory ingestion|staged memory|"
    r"\b\d+[a-z]-[a-z](?:-[a-z]+)?\b|photobioreactor|microalgae|"
    r"\betfe\b|coursework)"
)
PREVIOUS_CONTEXT_PATTERN = re.compile(
    r"(?i)(previous|earlier|as above|that one|same prompt|riprendi|mentioned|"
    r"current repository state|for now)"
)
REQUEST_PATTERN = re.compile(
    r"(?i)^\s*(please|run|add|update|create|commit|verify|check|review|"
    r"continue|riprendi|procedi|fammi|dimmi|can you|could you)\b"
)
PREFERENCE_PATTERN = re.compile(
    r"(?i)(\bi prefer\b|\bpreference\b|\bdefault\b|\bpreferisco\b|\bvoglio\b|"
    r"modalit[aà] standard|d'ora in poi|from now on)"
)
DECISION_PATTERN = re.compile(r"(?i)(\bdecision\b|\bdecided\b|\bconfirmed\b|\bapproved\b|\baccepted\b)")
ASSUMPTION_PATTERN = re.compile(r"(?i)(\bassume\b|\bassumption\b|\bcandidate\b|\btentative\b|\bnot decided\b)")
CONSTRAINT_PATTERN = re.compile(r"(?i)(\bmust\b|\bshould\b|\bdo not\b|\bnever\b|\brequirement\b|\bconstraint\b)")
TEST_RESULT_PATTERN = re.compile(
    r"(?i)(\btests?\s+passed\b|\bpassed\s+\d+\b|\bfailed\s+\d+\b|"
    r"\bschema-valid\b|\baccepted_rate\b|\bfallback_rate\b|\bgit diff --check\b)"
)
OPEN_QUESTION_PATTERN = re.compile(r"(?i)(\?|^\s*(why|what|when|where|how|is|are|can|could|should)\b)")
WORKSPACE_PATTERN = re.compile(r"(?i)(photobioreactor|microalgae|\betfe\b|reactor)")


def _mentions_workspace_project(normalized: str) -> bool:
    return bool(WORKSPACE_PATTERN.search(normalized)) or mentions_pack_term(normalized)
COURSEWORK_PATTERN = re.compile(r"(?i)(coursework|assignment|\bcstr\b|\bexam\b)")
BIOPROCESS_PATTERN = re.compile(r"(?i)(bioprocess|bioreactor|\bkla\b|aerobic|fermentation)")
MEMORY_PATTERN = re.compile(r"(?i)(memory|intake|retrieval|storage|canonical|staged)")
LOCAL_AI_PATTERN = re.compile(r"(?i)(gemma|qwen|local_ai|local ai|model|schema|prompt|classification|ollama)")
SOFTWARE_PATTERN = re.compile(r"(?i)(code|git|pytest|python|backend|frontend|commit|route|api|test)")
PERSONAL_PATTERN = re.compile(r"(?i)(personal|doctor|family|address|phone)")


def deterministic_fast_intake_baseline(text: str) -> FastIntakeFlatSignalV0:
    """Return deterministic advisory intake fields for a single user message."""

    normalized = " ".join(text.split())
    lower = normalized.lower()

    contains_secret = bool(SECRET_PATTERN.search(normalized))
    contains_numbers_or_metrics = bool(METRIC_PATTERN.search(normalized))
    mentions_code_or_command = bool(CODE_PATTERN.search(normalized))
    mentions_source_or_literature = bool(SOURCE_PATTERN.search(normalized))
    mentions_project_or_artifact = bool(PROJECT_PATTERN.search(normalized)) or mentions_code_or_command
    contains_user_preference = bool(PREFERENCE_PATTERN.search(normalized))
    contains_user_decision = bool(DECISION_PATTERN.search(normalized))
    contains_assumption = bool(ASSUMPTION_PATTERN.search(normalized))
    if re.search(r"(?i)(not decided|not final|undecided|non definitivo)", normalized):
        contains_user_decision = False
    contains_design_constraint = bool(CONSTRAINT_PATTERN.search(normalized))
    contains_open_question = bool(OPEN_QUESTION_PATTERN.search(normalized))
    contains_action_request = bool(REQUEST_PATTERN.search(normalized))
    contains_test_result = bool(TEST_RESULT_PATTERN.search(normalized))
    mentions_previous_context = bool(PREVIOUS_CONTEXT_PATTERN.search(normalized))

    if "ok" == lower or lower in {"ok thanks", "ok grazie", "thanks", "grazie"}:
        storage_relevance = StorageRelevance.low
    elif contains_secret:
        storage_relevance = StorageRelevance.high
    elif contains_user_decision or contains_user_preference or contains_assumption:
        storage_relevance = StorageRelevance.high
    elif contains_test_result or mentions_source_or_literature or contains_design_constraint:
        storage_relevance = StorageRelevance.high
    elif contains_action_request or contains_open_question:
        storage_relevance = StorageRelevance.medium
    elif mentions_project_or_artifact:
        storage_relevance = StorageRelevance.medium
    else:
        storage_relevance = StorageRelevance.low

    record_bucket = _record_bucket(
        normalized=normalized,
        contains_secret=contains_secret,
        contains_action_request=contains_action_request,
        contains_open_question=contains_open_question,
        contains_test_result=contains_test_result,
        contains_user_preference=contains_user_preference,
        contains_user_decision=contains_user_decision,
        contains_assumption=contains_assumption,
        mentions_source_or_literature=mentions_source_or_literature,
    )
    project_bucket = _project_bucket(normalized)
    domain_bucket = _domain_bucket(normalized)
    sensitivity_bucket = SensitivityBucket.secret if contains_secret else _sensitivity_bucket(normalized)
    status_bucket = _status_bucket(
        normalized=normalized,
        contains_test_result=contains_test_result,
        contains_user_decision=contains_user_decision,
        contains_assumption=contains_assumption,
    )

    needs_user_confirmation = bool(
        contains_secret
        or contains_user_decision
        or contains_assumption
        or contains_user_preference
        or status_bucket == StatusBucket.not_decided
    )
    needs_enrichment = bool(
        contains_secret
        or contains_design_constraint
        or mentions_source_or_literature
        or contains_assumption
        or project_bucket in {ProjectBucket.workspace, ProjectBucket.jarvisos}
        or storage_relevance == StorageRelevance.high
    )
    uncertainty_reason = _uncertainty_reason(
        contains_secret=contains_secret,
        contains_assumption=contains_assumption,
        contains_user_decision=contains_user_decision,
        contains_user_preference=contains_user_preference,
        contains_open_question=contains_open_question,
        status_bucket=status_bucket,
    )

    uncertain_fields = _uncertain_fields(
        storage_relevance=storage_relevance,
        project_bucket=project_bucket,
        domain_bucket=domain_bucket,
        contains_open_question=contains_open_question,
    )

    return FastIntakeFlatSignalV0(
        schema_version="fast_intake_flat_v0",
        contains_numbers_or_metrics=contains_numbers_or_metrics,
        mentions_code_or_command=mentions_code_or_command,
        mentions_project_or_artifact=mentions_project_or_artifact,
        mentions_source_or_literature=mentions_source_or_literature,
        contains_user_preference=contains_user_preference,
        contains_user_decision=contains_user_decision,
        contains_assumption=contains_assumption,
        contains_design_constraint=contains_design_constraint,
        contains_open_question=contains_open_question,
        contains_action_request=contains_action_request,
        contains_test_result=contains_test_result,
        mentions_previous_context=mentions_previous_context,
        storage_relevance=storage_relevance,
        record_bucket=record_bucket,
        project_bucket=project_bucket,
        domain_bucket=domain_bucket,
        sensitivity_bucket=sensitivity_bucket,
        status_bucket=status_bucket,
        needs_enrichment=needs_enrichment,
        needs_user_confirmation=needs_user_confirmation,
        uncertainty_reason=uncertainty_reason,
        confidence_observable=_confidence_observable(
            storage_relevance=storage_relevance,
            contains_secret=contains_secret,
            contains_numbers_or_metrics=contains_numbers_or_metrics,
            mentions_code_or_command=mentions_code_or_command,
            mentions_source_or_literature=mentions_source_or_literature,
        ),
        confidence_bucket_assignment=_confidence_bucket_assignment(
            project_bucket=project_bucket,
            domain_bucket=domain_bucket,
            record_bucket=record_bucket,
        ),
        uncertain_fields=uncertain_fields,
        advisory_note="",
    )


def field_ownership_for(field_name: str) -> FieldOwnership:
    return FIELD_OWNERSHIP[field_name]


def _record_bucket(
    *,
    normalized: str,
    contains_secret: bool,
    contains_action_request: bool,
    contains_open_question: bool,
    contains_test_result: bool,
    contains_user_preference: bool,
    contains_user_decision: bool,
    contains_assumption: bool,
    mentions_source_or_literature: bool,
) -> RecordBucket:
    if contains_user_preference:
        return RecordBucket.preference
    if contains_user_decision:
        return RecordBucket.decision
    if contains_action_request and not normalized.lower().startswith(("report", "codex report")):
        return RecordBucket.request
    if contains_test_result:
        return RecordBucket.result
    if contains_assumption:
        return RecordBucket.assumption
    if mentions_source_or_literature:
        return RecordBucket.source
    if contains_open_question:
        return RecordBucket.issue
    if contains_secret:
        return RecordBucket.note
    return RecordBucket.note


def _project_bucket(normalized: str) -> ProjectBucket:
    if _mentions_workspace_project(normalized):
        return ProjectBucket.workspace
    if re.search(r"(?i)(jarvisos|codex|fast\s*intake|local_ai|local ai|memory ingestion|memory intake|staged memory|\b\d+[a-z]-[a-z](?:-[a-z]+)?\b)", normalized):
        return ProjectBucket.jarvisos
    if COURSEWORK_PATTERN.search(normalized):
        return ProjectBucket.coursework
    return ProjectBucket.general


def _domain_bucket(normalized: str) -> DomainBucket:
    if _mentions_workspace_project(normalized):
        return DomainBucket.reactor_design
    if COURSEWORK_PATTERN.search(normalized):
        return DomainBucket.coursework
    if BIOPROCESS_PATTERN.search(normalized):
        return DomainBucket.bioprocess
    if MEMORY_PATTERN.search(normalized):
        return DomainBucket.memory
    if LOCAL_AI_PATTERN.search(normalized):
        return DomainBucket.local_ai
    if SOFTWARE_PATTERN.search(normalized):
        return DomainBucket.software
    if PERSONAL_PATTERN.search(normalized):
        return DomainBucket.personal
    return DomainBucket.general


def _sensitivity_bucket(normalized: str) -> SensitivityBucket:
    if PERSONAL_PATTERN.search(normalized):
        return SensitivityBucket.sensitive_personal
    return SensitivityBucket.public


def _status_bucket(
    *,
    normalized: str,
    contains_test_result: bool,
    contains_user_decision: bool,
    contains_assumption: bool,
) -> StatusBucket:
    if re.search(
        r"(?i)(not decided|not final|undecided|tentative|candidate|maybe|draft|"
        r"needs review|da valutare|non definitivo|proposed for now)",
        normalized,
    ):
        return StatusBucket.not_decided
    if re.search(r"(?i)(commit succeeded|tests passed)", normalized) or contains_test_result or contains_user_decision:
        return StatusBucket.accepted
    if contains_assumption:
        return StatusBucket.not_decided
    return StatusBucket.raw


def _uncertainty_reason(
    *,
    contains_secret: bool,
    contains_assumption: bool,
    contains_user_decision: bool,
    contains_user_preference: bool,
    contains_open_question: bool,
    status_bucket: StatusBucket,
) -> UncertaintyReason:
    if contains_secret:
        return UncertaintyReason.sensitive
    if contains_assumption or status_bucket == StatusBucket.not_decided:
        return UncertaintyReason.important_decision
    if contains_user_decision or contains_user_preference:
        return UncertaintyReason.important_decision
    if contains_open_question:
        return UncertaintyReason.missing_context
    return UncertaintyReason.none


def _uncertain_fields(
    *,
    storage_relevance: StorageRelevance,
    project_bucket: ProjectBucket,
    domain_bucket: DomainBucket,
    contains_open_question: bool,
) -> tuple[UncertainField, ...]:
    fields: list[UncertainField] = []
    if storage_relevance != StorageRelevance.low and project_bucket == ProjectBucket.general:
        fields.append(UncertainField.project_bucket)
    if storage_relevance != StorageRelevance.low and domain_bucket == DomainBucket.general:
        fields.append(UncertainField.domain_bucket)
    if contains_open_question:
        fields.append(UncertainField.status_bucket)
    return tuple(fields[:2])


def _confidence_observable(
    *,
    storage_relevance: StorageRelevance,
    contains_secret: bool,
    contains_numbers_or_metrics: bool,
    mentions_code_or_command: bool,
    mentions_source_or_literature: bool,
) -> float:
    if contains_secret:
        return 0.79
    if contains_numbers_or_metrics or mentions_code_or_command or mentions_source_or_literature:
        return 0.78
    if storage_relevance == StorageRelevance.low:
        return 0.72
    return 0.76


def _confidence_bucket_assignment(
    *,
    project_bucket: ProjectBucket,
    domain_bucket: DomainBucket,
    record_bucket: RecordBucket,
) -> float:
    confidence = 0.62
    if project_bucket != ProjectBucket.general:
        confidence += 0.04
    if domain_bucket != DomainBucket.general:
        confidence += 0.04
    if record_bucket != RecordBucket.note:
        confidence += 0.02
    return min(confidence, 0.74)


__all__ = [
    "FIELD_OWNERSHIP",
    "FieldOwnership",
    "deterministic_fast_intake_baseline",
    "field_ownership_for",
]
