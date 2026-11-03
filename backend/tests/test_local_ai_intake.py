import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.modules.local_ai.classification.adapter import ClassificationAdapterConfig, ClassificationAdapterResult
from app.modules.local_ai.classification.contracts import ClassificationAttemptDiagnostics, ClassificationFailureCode
from app.modules.local_ai_eval.deterministic_signals import (
    FIELD_OWNERSHIP,
    FieldOwnership,
    deterministic_fast_intake_baseline,
    field_ownership_for,
)
from app.modules.local_ai_eval.fast_intake_probe import (
    FAST_INTAKE_DETERMINISTIC_MODE,
    FAST_INTAKE_FLAT_MODE,
    FAST_INTAKE_FLAT_SCHEMA_VERSION,
    FAST_INTAKE_SCHEMA_VERSION,
    REPORT_SCHEMA_VERSION,
    BroadStorageBuckets,
    DomainBucket,
    ExplicitMentions,
    FastIntakeFlatSignalV0,
    FastIntakeSignalForm,
    FastIntakeSuitability,
    LikelyFailureCategory,
    ObservableFlags,
    OutputRootType,
    ProjectBucket,
    RecordBucket,
    SensitivityBucket,
    ShortDescription,
    StatusBucket,
    StorageRelevance,
    UncertainField,
    Uncertainty,
    UncertaintyReason,
    build_fast_intake_deterministic_report,
    build_fast_intake_flat_prompt,
    build_fast_intake_prompt,
    build_fast_intake_smoke_report,
    fast_intake_probe_cases,
    normalize_flat_to_fast_intake_form,
    parse_fast_intake_flat_output,
    parse_fast_intake_output,
    summary_lines,
    write_probe_report,
)


def _diagnostics(
    config: ClassificationAdapterConfig,
    *,
    prompt: str = "",
    raw_content_empty: bool = False,
    thinking_present: bool | None = False,
    done_reason: str | None = "stop",
    fallback_used: bool = False,
    fallback_reason: ClassificationFailureCode | None = None,
) -> ClassificationAttemptDiagnostics:
    return ClassificationAttemptDiagnostics(
        model_name=config.model_name,
        endpoint=config.endpoint_url,
        prompt_chars=len(prompt),
        input_chars=0,
        max_output_tokens=config.max_output_tokens,
        temperature=config.temperature,
        timeout_seconds=config.timeout_seconds,
        latency_ms=10,
        raw_content_empty=raw_content_empty,
        thinking_present=thinking_present,
        done_reason=done_reason,
        schema_valid=False,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
    )


def _valid_fast_intake_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": FAST_INTAKE_SCHEMA_VERSION,
        "source": {
            "input_id": "synthetic-1",
            "conversation_id": "synthetic",
            "timestamp": "2026-06-21T00:00:00Z",
            "raw_text_preserved": True,
        },
        "observable_flags": {
            "contains_user_preference": False,
            "contains_user_decision": True,
            "contains_assumption": False,
            "contains_design_constraint": True,
            "contains_open_question": False,
            "contains_action_request": False,
            "contains_test_result": False,
            "contains_numbers_or_metrics": False,
            "mentions_previous_context": False,
            "mentions_project_or_artifact": True,
            "mentions_code_or_command": False,
            "mentions_source_or_literature": False,
        },
        "broad_storage_buckets": {
            "storage_relevance": "high",
            "record_bucket": "decision",
            "project_bucket": "jarvisos",
            "domain_bucket": "memory",
            "sensitivity_bucket": "internal",
            "status_bucket": "proposed",
        },
        "explicit_mentions": {
            "entities": [],
            "projects": ["JarvisOS"],
            "artifacts": [],
            "commits_or_versions": [],
            "numbers_or_metrics": [],
        },
        "short_description": {
            "surface_summary": "Staged memory intake decision.",
            "preserved_user_phrasing": [],
        },
        "uncertainty": {
            "needs_enrichment": True,
            "needs_user_confirmation": False,
            "reason": "important_decision",
        },
        "confidence": {
            "observable": 0.9,
            "bucket_assignment": 0.82,
        },
    }
    payload.update(overrides)
    return payload


def _valid_flat_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": FAST_INTAKE_FLAT_SCHEMA_VERSION,
        "contains_user_preference": False,
        "contains_user_decision": True,
        "contains_assumption": False,
        "contains_design_constraint": True,
        "contains_open_question": False,
        "contains_action_request": False,
        "contains_test_result": False,
        "contains_numbers_or_metrics": False,
        "mentions_previous_context": False,
        "mentions_project_or_artifact": True,
        "mentions_code_or_command": False,
        "mentions_source_or_literature": False,
        "storage_relevance": "high",
        "record_bucket": "decision",
        "project_bucket": "jarvisos",
        "domain_bucket": "memory",
        "sensitivity_bucket": "internal",
        "status_bucket": "proposed",
        "needs_enrichment": True,
        "needs_user_confirmation": False,
        "uncertainty_reason": "important_decision",
        "confidence_observable": 0.9,
        "confidence_bucket_assignment": 0.82,
        "uncertain_fields": [],
        "advisory_note": "",
    }
    payload.update(overrides)
    return payload


class FakeFastIntakeAdapter:
    def __init__(self, config: ClassificationAdapterConfig) -> None:
        self.config = config

    def complete(self, prompt: str, *, input_chars: int = 0) -> ClassificationAdapterResult:
        content = json.dumps(_payload_for_prompt(prompt))
        return ClassificationAdapterResult(
            success=True,
            model_name=self.config.model_name,
            runtime_endpoint=self.config.endpoint_url,
            diagnostics=_diagnostics(self.config, prompt=prompt),
            response_text=content,
        )


class FailingFastIntakeAdapter(FakeFastIntakeAdapter):
    def complete(self, prompt: str, *, input_chars: int = 0) -> ClassificationAdapterResult:
        if self.config.model_name == "qwen3:8b":
            return ClassificationAdapterResult(
                success=False,
                model_name=self.config.model_name,
                runtime_endpoint=self.config.endpoint_url,
                diagnostics=_diagnostics(
                    self.config,
                    prompt=prompt,
                    raw_content_empty=True,
                    done_reason=None,
                    fallback_used=True,
                    fallback_reason=ClassificationFailureCode.timeout,
                ),
                failure_code=ClassificationFailureCode.timeout,
                failure_message="simulated timeout",
            )
        return super().complete(prompt, input_chars=input_chars)


class FakeFlatFastIntakeAdapter:
    def __init__(self, config: ClassificationAdapterConfig) -> None:
        self.config = config

    def complete(self, prompt: str, *, input_chars: int = 0) -> ClassificationAdapterResult:
        content = json.dumps(_flat_payload_for_prompt(prompt))
        return ClassificationAdapterResult(
            success=True,
            model_name=self.config.model_name,
            runtime_endpoint=self.config.endpoint_url,
            diagnostics=_diagnostics(self.config, prompt=prompt),
            response_text=content,
        )


class FailingFlatFastIntakeAdapter(FakeFlatFastIntakeAdapter):
    def complete(self, prompt: str, *, input_chars: int = 0) -> ClassificationAdapterResult:
        if self.config.model_name == "qwen3:8b":
            return ClassificationAdapterResult(
                success=False,
                model_name=self.config.model_name,
                runtime_endpoint=self.config.endpoint_url,
                diagnostics=_diagnostics(
                    self.config,
                    prompt=prompt,
                    raw_content_empty=True,
                    done_reason=None,
                    fallback_used=True,
                    fallback_reason=ClassificationFailureCode.timeout,
                ),
                failure_code=ClassificationFailureCode.timeout,
                failure_message="simulated timeout",
            )
        return super().complete(prompt, input_chars=input_chars)


def _payload_for_prompt(prompt: str) -> dict[str, object]:
    source_text = prompt.rsplit("Input:", 1)[-1]
    payload = _valid_fast_intake_payload()
    if "ok grazie" in source_text:
        payload["observable_flags"] = {
            key: False for key in payload["observable_flags"]  # type: ignore[index]
        }
        payload["broad_storage_buckets"] = {
            "storage_relevance": "low",
            "record_bucket": "unknown",
            "project_bucket": "unknown",
            "domain_bucket": "unknown",
            "sensitivity_bucket": "public",
            "status_bucket": "raw",
        }
        payload["explicit_mentions"] = {
            "entities": [],
            "projects": [],
            "artifacts": [],
            "commits_or_versions": [],
            "numbers_or_metrics": [],
        }
        payload["uncertainty"] = {
            "needs_enrichment": False,
            "needs_user_confirmation": False,
            "reason": "none",
        }
    if "API_KEY_PLACEHOLDER_12345" in source_text:
        payload["observable_flags"] = {
            "contains_user_preference": False,
            "contains_user_decision": False,
            "contains_assumption": False,
            "contains_design_constraint": False,
            "contains_open_question": False,
            "contains_action_request": False,
            "contains_test_result": False,
            "contains_numbers_or_metrics": True,
            "mentions_previous_context": False,
            "mentions_project_or_artifact": False,
            "mentions_code_or_command": True,
            "mentions_source_or_literature": False,
        }
        payload["broad_storage_buckets"] = {
            "storage_relevance": "high",
            "record_bucket": "note",
            "project_bucket": "unknown",
            "domain_bucket": "software",
            "sensitivity_bucket": "secret",
            "status_bucket": "raw",
        }
        payload["explicit_mentions"] = {
            "entities": [],
            "projects": [],
            "artifacts": ["API_KEY_PLACEHOLDER_12345"],
            "commits_or_versions": [],
            "numbers_or_metrics": ["12345"],
        }
        payload["uncertainty"] = {
            "needs_enrichment": True,
            "needs_user_confirmation": True,
            "reason": "sensitive",
        }
    return payload


def _flat_payload_for_prompt(prompt: str) -> dict[str, object]:
    source_text = prompt.rsplit("Input:", 1)[-1]
    payload = _valid_flat_payload()
    if "ok grazie" in source_text:
        payload.update(
            {
                "contains_user_decision": False,
                "contains_design_constraint": False,
                "mentions_project_or_artifact": False,
                "storage_relevance": "low",
                "record_bucket": "unknown",
                "project_bucket": "unknown",
                "domain_bucket": "unknown",
                "sensitivity_bucket": "public",
                "status_bucket": "raw",
                "needs_enrichment": False,
                "uncertainty_reason": "none",
                "confidence_observable": 0.88,
                "confidence_bucket_assignment": 0.8,
            }
        )
    if "API_KEY_PLACEHOLDER_12345" in source_text:
        payload.update(
            {
                "contains_user_decision": False,
                "contains_design_constraint": False,
                "contains_numbers_or_metrics": True,
                "mentions_project_or_artifact": False,
                "mentions_code_or_command": True,
                "storage_relevance": "high",
                "record_bucket": "note",
                "project_bucket": "unknown",
                "domain_bucket": "software",
                "sensitivity_bucket": "secret",
                "status_bucket": "raw",
                "needs_enrichment": True,
                "needs_user_confirmation": True,
                "uncertainty_reason": "sensitive",
                "uncertain_fields": ["project_bucket"],
                "advisory_note": "Secret-like placeholder detected.",
            }
        )
    return payload


def test_fast_intake_schema_accepts_broad_buckets_and_booleans() -> None:
    form = FastIntakeSignalForm.model_validate(_valid_fast_intake_payload())

    assert form.schema_version == FAST_INTAKE_SCHEMA_VERSION
    assert form.source.raw_text_preserved is True
    assert form.observable_flags.contains_user_decision is True
    assert form.broad_storage_buckets.storage_relevance == StorageRelevance.high
    assert form.broad_storage_buckets.record_bucket == RecordBucket.decision
    assert form.broad_storage_buckets.project_bucket == ProjectBucket.jarvisos
    assert form.broad_storage_buckets.domain_bucket == DomainBucket.memory
    assert form.broad_storage_buckets.sensitivity_bucket == SensitivityBucket.internal
    assert form.broad_storage_buckets.status_bucket == StatusBucket.proposed
    assert form.uncertainty.reason == UncertaintyReason.important_decision


def test_fast_intake_schema_forbids_authorization_fields() -> None:
    payload = _valid_fast_intake_payload(
        tool_authorization="run_tests",
        provider_authorization="deepseek",
        retrieval_authorization=True,
        memory_write_authorization=True,
        route_selection="memory_runtime",
        final_sensitivity_decision="public",
        canonical_memory_promotion=True,
        knowledge_card={"title": "Do not accept"},
    )

    with pytest.raises(ValidationError) as exc:
        FastIntakeSignalForm.model_validate(payload)

    forbidden_locations = {tuple(error["loc"]) for error in exc.value.errors()}
    assert ("tool_authorization",) in forbidden_locations
    assert ("provider_authorization",) in forbidden_locations
    assert ("retrieval_authorization",) in forbidden_locations
    assert ("memory_write_authorization",) in forbidden_locations
    assert ("route_selection",) in forbidden_locations
    assert ("final_sensitivity_decision",) in forbidden_locations
    assert ("canonical_memory_promotion",) in forbidden_locations
    assert ("knowledge_card",) in forbidden_locations


def test_fast_intake_invalid_enum_fails() -> None:
    payload = _valid_fast_intake_payload(
        broad_storage_buckets={
            "storage_relevance": "urgent",
            "record_bucket": "decision",
            "project_bucket": "jarvisos",
            "domain_bucket": "memory",
            "sensitivity_bucket": "internal",
            "status_bucket": "proposed",
        }
    )

    with pytest.raises(ValueError):
        parse_fast_intake_output(json.dumps(payload))


def test_fast_intake_flat_schema_accepts_valid_output() -> None:
    flat = FastIntakeFlatSignalV0.model_validate(
        _valid_flat_payload(
            uncertain_fields=["project_bucket", "confidence_bucket_assignment"],
            advisory_note="Project inferred from explicit mention.",
        )
    )

    assert flat.schema_version == FAST_INTAKE_FLAT_SCHEMA_VERSION
    assert flat.contains_user_decision is True
    assert flat.record_bucket == RecordBucket.decision
    assert flat.uncertain_fields == (
        UncertainField.project_bucket,
        UncertainField.confidence_bucket_assignment,
    )
    assert flat.advisory_note == "Project inferred from explicit mention."


def test_fast_intake_flat_schema_forbids_unknown_extra_keys() -> None:
    payload = _valid_flat_payload(tool_authorization="run_tests", source={"input_id": "model-generated"})

    with pytest.raises(ValidationError) as exc:
        FastIntakeFlatSignalV0.model_validate(payload)

    assert ("tool_authorization",) in {tuple(error["loc"]) for error in exc.value.errors()}
    assert ("source",) in {tuple(error["loc"]) for error in exc.value.errors()}


def test_fast_intake_flat_schema_allows_only_bounded_advisory_channels() -> None:
    assert "uncertain_fields" in FastIntakeFlatSignalV0.model_fields
    assert "advisory_note" in FastIntakeFlatSignalV0.model_fields
    assert "tool_authorization" not in FastIntakeFlatSignalV0.model_fields
    assert "provider_authorization" not in FastIntakeFlatSignalV0.model_fields
    assert "retrieval_authorization" not in FastIntakeFlatSignalV0.model_fields
    assert "memory_write_authorization" not in FastIntakeFlatSignalV0.model_fields
    assert "route_selection" not in FastIntakeFlatSignalV0.model_fields
    assert "canonical_memory_promotion" not in FastIntakeFlatSignalV0.model_fields


def test_fast_intake_flat_uncertain_fields_are_limited() -> None:
    with pytest.raises(ValidationError):
        FastIntakeFlatSignalV0.model_validate(_valid_flat_payload(uncertain_fields=["not_a_real_field"]))

    with pytest.raises(ValidationError):
        FastIntakeFlatSignalV0.model_validate(
            _valid_flat_payload(
                uncertain_fields=[
                    "contains_user_preference",
                    "contains_user_decision",
                    "contains_assumption",
                    "contains_design_constraint",
                    "contains_open_question",
                    "contains_action_request",
                ]
            )
        )


def test_fast_intake_flat_advisory_note_is_bounded() -> None:
    with pytest.raises(ValidationError):
        FastIntakeFlatSignalV0.model_validate(_valid_flat_payload(advisory_note="x" * 161))


def test_fast_intake_flat_invalid_enum_fails() -> None:
    with pytest.raises(ValueError):
        parse_fast_intake_flat_output(json.dumps(_valid_flat_payload(storage_relevance="urgent")))


def test_fast_intake_flat_output_normalizes_to_canonical_nested_form() -> None:
    case = fast_intake_probe_cases()[0]
    flat = FastIntakeFlatSignalV0.model_validate(_valid_flat_payload())
    canonical = normalize_flat_to_fast_intake_form(flat, case)

    assert canonical.schema_version == FAST_INTAKE_SCHEMA_VERSION
    assert canonical.source.input_id == case.input_id
    assert canonical.source.conversation_id == case.conversation_id
    assert canonical.source.timestamp == case.timestamp
    assert canonical.source.raw_text_preserved is True
    assert canonical.observable_flags.contains_user_decision is True
    assert canonical.broad_storage_buckets.project_bucket == ProjectBucket.jarvisos
    assert canonical.explicit_mentions == ExplicitMentions()
    assert canonical.short_description == ShortDescription(surface_summary="", preserved_user_phrasing=())
    assert canonical.uncertainty.reason == UncertaintyReason.important_decision
    assert canonical.confidence.observable == 0.9


def test_fast_intake_flat_prompt_is_bounded_and_excludes_nested_model_generated_fields() -> None:
    prompt = build_fast_intake_flat_prompt(fast_intake_probe_cases()[0])

    assert len(prompt) <= 5000
    assert "Do not nest objects." in prompt
    assert "uncertain_fields" in prompt
    assert "advisory_note" in prompt
    assert '"source"' not in prompt
    assert '"explicit_mentions"' not in prompt
    assert '"short_description"' not in prompt


def test_fast_intake_report_omits_raw_case_prompt_messages_and_output(tmp_path: Path) -> None:
    cases = fast_intake_probe_cases()
    report = build_fast_intake_smoke_report(
        installed_model_names=("qwen3:8b", "gemma4:12b-it-qat"),
        adapter_factory=FakeFastIntakeAdapter,
        created_at_utc=datetime(2026, 6, 21, 13, 0, tzinfo=UTC),
    )
    path = write_probe_report(report, tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    lines = summary_lines(report, path)
    serialized = report.model_dump_json()

    assert path.name == "fast_intake_probe_smoke_20260621T130000.json"
    assert payload["schema_version"] == REPORT_SCHEMA_VERSION
    assert report.mode == "smoke"
    assert set(report.candidate_model_names) == {"qwen3:8b", "gemma4:12b-it-qat"}
    assert all(summary.runtime_approved is False for summary in report.profile_summaries)
    assert any("model=qwen3:8b" in line for line in lines)

    for case in cases:
        assert case.case_id in serialized
        assert case.text not in serialized
        assert build_fast_intake_prompt(case) not in serialized
    assert "API_KEY_PLACEHOLDER_12345" not in serialized
    assert "[redacted]" in serialized
    assert "messages" not in serialized
    assert "response_text" not in serialized
    assert "surface_summary" not in serialized
    assert "preserved_user_phrasing" not in serialized


def test_fast_intake_flat_report_stores_advisory_metadata_not_raw_note(tmp_path: Path) -> None:
    cases = fast_intake_probe_cases()
    report = build_fast_intake_smoke_report(
        mode=FAST_INTAKE_FLAT_MODE,
        installed_model_names=("qwen3:8b", "gemma4:12b-it-qat"),
        adapter_factory=FakeFlatFastIntakeAdapter,
        created_at_utc=datetime(2026, 6, 21, 14, 0, tzinfo=UTC),
    )
    path = write_probe_report(report, tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    lines = summary_lines(report, path)
    serialized = report.model_dump_json()

    assert path.name == "fast_intake_probe_smoke-flat_20260621T140000.json"
    assert payload["mode"] == FAST_INTAKE_FLAT_MODE
    assert all(summary.runtime_approved is False for summary in report.profile_summaries)
    assert any("advisory_note_present=" in line for line in lines)
    assert any("uncertain_fields=" in line for line in lines)
    assert any(summary.advisory_note_present_count >= 1 for summary in report.profile_summaries)
    assert any("project_bucket" in summary.uncertain_fields_frequency for summary in report.profile_summaries)

    for case in cases:
        assert case.case_id in serialized
        assert case.text not in serialized
        assert build_fast_intake_flat_prompt(case) not in serialized
    assert "Secret-like placeholder detected." not in serialized
    assert "API_KEY_PLACEHOLDER_12345" not in serialized
    assert "messages" not in serialized
    assert "response_text" not in serialized
    assert "advisory_note_chars" in serialized
    assert "advisory_note_present" in serialized


def test_fast_intake_flat_sanitized_validation_diagnostics_exclude_raw_values() -> None:
    class InvalidFlatAdapter:
        def __init__(self, config: ClassificationAdapterConfig) -> None:
            self.config = config

        def complete(self, prompt: str, *, input_chars: int = 0) -> ClassificationAdapterResult:
            content = json.dumps(
                {
                    "schema_version": FAST_INTAKE_FLAT_SCHEMA_VERSION,
                    "storage_relevance": "urgent",
                    "extra_reasoning": "raw value should not be copied",
                }
            )
            return ClassificationAdapterResult(
                success=True,
                model_name=self.config.model_name,
                runtime_endpoint=self.config.endpoint_url,
                diagnostics=_diagnostics(self.config, prompt=prompt),
                response_text=content,
            )

    report = build_fast_intake_smoke_report(
        mode=FAST_INTAKE_FLAT_MODE,
        installed_model_names=("gemma4:12b-it-qat",),
        cases=(fast_intake_probe_cases()[0],),
        adapter_factory=InvalidFlatAdapter,
        created_at_utc=datetime(2026, 6, 21, 14, 10, tzinfo=UTC),
    )
    result = report.results[0]
    diag = result.validation_diagnostics
    serialized = report.model_dump_json()

    assert result.schema_valid is False
    assert result.fallback_reason == ClassificationFailureCode.extra_fields
    assert diag is not None
    assert diag.output_root_type == OutputRootType.object
    assert diag.json_parse_ok is True
    assert "extra_reasoning" in diag.extra_top_level_fields
    assert "storage_relevance" in diag.enum_error_paths
    assert diag.likely_failure_category == LikelyFailureCategory.extra_fields
    assert "raw value should not be copied" not in serialized


def test_fast_intake_model_failure_does_not_abort_smoke_test() -> None:
    report = build_fast_intake_smoke_report(
        installed_model_names=("qwen3:8b", "gemma4:12b-it-qat"),
        adapter_factory=FailingFastIntakeAdapter,
        created_at_utc=datetime(2026, 6, 21, 13, 15, tzinfo=UTC),
    )
    by_model = {summary.model_name: summary for summary in report.profile_summaries}

    assert by_model["qwen3:8b"].timeout_count == by_model["qwen3:8b"].attempts
    assert by_model["qwen3:8b"].suitability_label == FastIntakeSuitability.rejected
    assert by_model["gemma4:12b-it-qat"].schema_valid_rate == 1
    assert any(result.model_name == "gemma4:12b-it-qat" and result.schema_valid for result in report.results)
    assert any(result.model_name == "qwen3:8b" and result.fallback_used for result in report.results)


def test_fast_intake_flat_model_failure_does_not_abort_smoke_test() -> None:
    report = build_fast_intake_smoke_report(
        mode=FAST_INTAKE_FLAT_MODE,
        installed_model_names=("qwen3:8b", "gemma4:12b-it-qat"),
        adapter_factory=FailingFlatFastIntakeAdapter,
        created_at_utc=datetime(2026, 6, 21, 14, 15, tzinfo=UTC),
    )
    by_model = {summary.model_name: summary for summary in report.profile_summaries}

    assert by_model["qwen3:8b"].timeout_count == by_model["qwen3:8b"].attempts
    assert by_model["qwen3:8b"].suitability_label == FastIntakeSuitability.rejected
    assert by_model["gemma4:12b-it-qat"].schema_valid_rate == 1
    assert any(result.model_name == "gemma4:12b-it-qat" and result.schema_valid for result in report.results)
    assert any(
        result.model_name == "qwen3:8b"
        and result.validation_diagnostics is not None
        and result.validation_diagnostics.likely_failure_category == LikelyFailureCategory.timeout
        for result in report.results
    )


def test_fast_intake_profiles_cannot_be_runtime_approved_even_when_candidate() -> None:
    report = build_fast_intake_smoke_report(
        installed_model_names=("gemma4:12b-it-qat",),
        adapter_factory=FakeFastIntakeAdapter,
        created_at_utc=datetime(2026, 6, 21, 13, 30, tzinfo=UTC),
    )
    flat_report = build_fast_intake_smoke_report(
        mode=FAST_INTAKE_FLAT_MODE,
        installed_model_names=("gemma4:12b-it-qat",),
        adapter_factory=FakeFlatFastIntakeAdapter,
        created_at_utc=datetime(2026, 6, 21, 13, 35, tzinfo=UTC),
    )

    assert report.profile_summaries
    assert all(summary.runtime_approved is False for summary in report.profile_summaries)
    assert all(result.runtime_approved is False for result in report.results)
    assert flat_report.profile_summaries
    assert all(summary.runtime_approved is False for summary in flat_report.profile_summaries)
    assert all(result.runtime_approved is False for result in flat_report.results)
    assert not hasattr(report.profile_summaries[0], "memory_write_authorization")
    assert not hasattr(report.profile_summaries[0], "retrieval_authorization")


def test_fast_intake_schema_components_are_broad_not_final_cards() -> None:
    assert set(FastIntakeSignalForm.model_fields) == {
        "schema_version",
        "source",
        "observable_flags",
        "broad_storage_buckets",
        "explicit_mentions",
        "short_description",
        "uncertainty",
        "confidence",
    }
    assert "knowledge_card" not in FastIntakeSignalForm.model_fields
    assert "memory_card" not in FastIntakeSignalForm.model_fields
    assert "decision_card" not in FastIntakeSignalForm.model_fields
    assert BroadStorageBuckets(
        storage_relevance=StorageRelevance.medium,
        record_bucket=RecordBucket.note,
        project_bucket=ProjectBucket.general,
        domain_bucket=DomainBucket.general,
        sensitivity_bucket=SensitivityBucket.internal,
        status_bucket=StatusBucket.raw,
    )
    assert ObservableFlags(**{key: False for key in ObservableFlags.model_fields})
    assert ExplicitMentions()
    assert ShortDescription(surface_summary="", preserved_user_phrasing=())
    assert Uncertainty(needs_enrichment=False, needs_user_confirmation=False, reason=UncertaintyReason.none)


def test_deterministic_fast_intake_detects_secret_as_hard_override() -> None:
    flat = deterministic_fast_intake_baseline("Do not store this placeholder secret: API_KEY_PLACEHOLDER_12345.")

    assert isinstance(flat, FastIntakeFlatSignalV0)
    assert flat.contains_numbers_or_metrics is True
    assert flat.mentions_code_or_command is True
    assert flat.storage_relevance == StorageRelevance.high
    assert flat.sensitivity_bucket == SensitivityBucket.secret
    assert flat.needs_user_confirmation is True
    assert flat.uncertainty_reason == UncertaintyReason.sensitive
    assert flat.advisory_note == ""


def test_deterministic_fast_intake_maps_project_and_casual_cases() -> None:
    jarvisos = deterministic_fast_intake_baseline("JarvisOS FastIntake should keep source IDs deterministic.")
    zephyr = deterministic_fast_intake_baseline(
        "For Zephyr, ETFE is a candidate tube material, but it is not decided yet."
    )
    casual = deterministic_fast_intake_baseline("ok grazie")

    assert jarvisos.project_bucket == ProjectBucket.jarvisos
    assert jarvisos.mentions_project_or_artifact is True
    assert zephyr.project_bucket == ProjectBucket.workspace
    assert zephyr.domain_bucket == DomainBucket.reactor_design
    assert zephyr.contains_assumption is True
    assert zephyr.status_bucket == StatusBucket.not_decided
    assert casual.storage_relevance == StorageRelevance.low
    assert casual.needs_enrichment is False
    assert casual.mentions_project_or_artifact is False


def test_deterministic_fast_intake_scores_obvious_project_and_result_fields() -> None:
    flat = deterministic_fast_intake_baseline(
        "Codex report: commit c137038 passed 299 backend tests and git diff --check."
    )

    assert flat.contains_test_result is True
    assert flat.contains_numbers_or_metrics is True
    assert flat.mentions_code_or_command is True
    assert flat.mentions_project_or_artifact is True
    assert flat.record_bucket == RecordBucket.result
    assert flat.project_bucket == ProjectBucket.jarvisos
    assert flat.domain_bucket == DomainBucket.software
    assert flat.status_bucket == StatusBucket.accepted


def test_deterministic_fast_intake_maps_not_decided_candidate_as_assumption() -> None:
    flat = deterministic_fast_intake_baseline("This is a candidate approach, not decided yet.")

    assert flat.contains_assumption is True
    assert flat.record_bucket == RecordBucket.assumption
    assert flat.status_bucket == StatusBucket.not_decided
    assert flat.needs_user_confirmation is True


def test_deterministic_field_ownership_covers_flat_schema_and_authority_boundaries() -> None:
    missing = set(FastIntakeFlatSignalV0.model_fields) - set(FIELD_OWNERSHIP)

    assert missing == set()
    assert field_ownership_for("source.input_id") == FieldOwnership.DETERMINISTIC_OWNED
    assert field_ownership_for("runtime_approved") == FieldOwnership.DETERMINISTIC_OWNED
    assert field_ownership_for("memory_write_authorization") == FieldOwnership.DETERMINISTIC_OWNED
    assert field_ownership_for("retrieval_authorization") == FieldOwnership.DETERMINISTIC_OWNED
    assert field_ownership_for("provider_authorization") == FieldOwnership.DETERMINISTIC_OWNED
    assert field_ownership_for("tool_authorization") == FieldOwnership.DETERMINISTIC_OWNED
    assert field_ownership_for("final_sensitivity_decision") == FieldOwnership.DETERMINISTIC_OWNED
    assert field_ownership_for("sensitivity_downgrade") == FieldOwnership.UNTRUSTED_FOR_RUNTIME
    assert field_ownership_for("tool_execution") == FieldOwnership.UNTRUSTED_FOR_RUNTIME
    assert field_ownership_for("provider_execution") == FieldOwnership.UNTRUSTED_FOR_RUNTIME
    assert field_ownership_for("advisory_note") == FieldOwnership.DIAGNOSTIC_ONLY
    assert field_ownership_for("advisory_note_present") == FieldOwnership.DIAGNOSTIC_ONLY
    assert field_ownership_for("advisory_note_chars") == FieldOwnership.DIAGNOSTIC_ONLY


def test_deterministic_baseline_report_omits_raw_text_and_compares_previous_flat_report(tmp_path: Path) -> None:
    previous = build_fast_intake_smoke_report(
        mode=FAST_INTAKE_FLAT_MODE,
        installed_model_names=("qwen3:8b", "gemma4:12b-it-qat"),
        adapter_factory=FakeFlatFastIntakeAdapter,
        created_at_utc=datetime(2026, 6, 21, 14, 0, tzinfo=UTC),
    )
    previous_path = write_probe_report(previous, tmp_path)
    report = build_fast_intake_deterministic_report(
        previous_report_path=previous_path,
        created_at_utc=datetime(2026, 6, 21, 15, 0, tzinfo=UTC),
    )
    path = write_probe_report(report, tmp_path)
    serialized = report.model_dump_json()
    lines = summary_lines(report, path)

    assert path.name == "fast_intake_probe_deterministic-baseline_20260621T150000.json"
    assert report.mode == FAST_INTAKE_DETERMINISTIC_MODE
    assert report.endpoint == "local-deterministic"
    assert report.profile_ids == ("deterministic_fast_intake_baseline",)
    assert report.candidate_model_names == ("deterministic_rules",)
    assert report.output_control.value == "deterministic"
    assert len(report.results) == len(fast_intake_probe_cases())
    assert len(report.comparison_summaries) == len(previous.profile_summaries)
    assert all(result.schema_valid for result in report.results)
    assert all(result.runtime_approved is False for result in report.results)
    assert all(summary.runtime_approved is False for summary in report.profile_summaries)
    assert any("comparison baseline=deterministic_fast_intake_baseline" in line for line in lines)

    for case in fast_intake_probe_cases():
        assert case.case_id in serialized
        assert case.text not in serialized
        assert build_fast_intake_flat_prompt(case) not in serialized
    assert "API_KEY_PLACEHOLDER_12345" not in serialized
    assert "messages" not in serialized
    assert "response_text" not in serialized


def test_deterministic_baseline_report_does_not_require_previous_report() -> None:
    report = build_fast_intake_deterministic_report(
        previous_report_path=Path("does-not-exist.json"),
        created_at_utc=datetime(2026, 6, 21, 15, 30, tzinfo=UTC),
    )

    assert report.comparison_summaries == []
    assert report.installed_model_names == ()
    assert report.profile_summaries[0].attempts == len(fast_intake_probe_cases())
    assert report.profile_summaries[0].runtime_approved is False
