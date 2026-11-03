import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from app.modules.local_ai.classification.adapter import (
    DEFAULT_CLASSIFICATION_MODEL,
    ClassificationAdapterConfig,
    ClassificationAdapterConfigurationError,
    ClassificationAdapterResult,
    LocalGemmaClassificationAdapter,
)
from app.modules.local_ai.classification.contracts import (
    CLASSIFICATION_ADVISORY_HINT_FIELDS,
    CLASSIFICATION_DIAGNOSTIC_NUM_PREDICT_CANDIDATES,
    CLASSIFICATION_INPUT_SCHEMA_VERSION,
    CLASSIFICATION_OUTPUT_SCHEMA_VERSION,
    LOW_CONFIDENCE_THRESHOLD,
    MODEL_NON_AUTHORITY_BOUNDARIES,
    AllowedNextStep,
    ClassificationAttemptDiagnostics,
    ClassificationBudgetPolicy,
    ClassificationFailureCode,
    ClassificationInput,
    ClassificationOutput,
    ClassificationResultSource,
    ClassificationSource,
    ProjectArea,
    SensitivityHint,
    TaskType,
)
from app.modules.local_ai.classification.parser import ClassificationParseError, parse_classification_output
from app.modules.local_ai.classification.prompts import MAX_CLASSIFICATION_PROMPT_CHARS, build_classification_prompt
from app.modules.local_ai.classification.service import classify_text, deterministic_classify
from app.modules.local_ai_eval.classification_budget_probe import (
    CONFIDENCE_CALIBRATION_NUM_PREDICT_CANDIDATES,
    CONFIDENCE_CALIBRATION_REPEAT_COUNT,
    LABEL_AGREEMENT_NUM_PREDICT_CANDIDATES,
    LABEL_AGREEMENT_REPEAT_COUNT,
    MINIMAL_DIAGNOSTIC_NUM_PREDICT_CANDIDATES,
    MINIMAL_REPEAT_NUM_PREDICT_CANDIDATES,
    MODEL_BAKEOFF_NUM_PREDICT_CANDIDATES,
    MODEL_BAKEOFF_REPEAT_COUNT,
    NON_CRITICAL_HINT_NUM_PREDICT_CANDIDATES,
    NON_CRITICAL_HINT_REPEAT_COUNT,
    REPORT_SCHEMA_VERSION,
    CalibrationAcceptancePolicy,
    LabelAgreementProtocolVariant,
    MinimalPromptVariant,
    ModelBakeoffSuitability,
    NonCriticalHintOutput,
    NonCriticalHintProfile,
    NonCriticalHintProtocolVariant,
    NonCriticalHintSuitability,
    OutputControlVariant,
    ProfileFormatMode,
    ProfilePromptStyle,
    build_budget_probe_report,
    build_label_agreement_prompt,
    build_minimal_classification_prompt,
    build_model_bakeoff_probe_report,
    build_non_critical_hint_prompt,
    build_non_critical_hint_repair_report,
    build_profile_bakeoff_prompt,
    build_profile_bakeoff_report,
    confidence_calibration_probe_cases,
    default_probe_cases,
    label_agreement_probe_cases,
    minimal_probe_cases,
    non_critical_hint_probe_cases,
    parse_label_agreement_output,
    parse_minimal_classification_output,
    parse_non_critical_hint_output,
    summary_lines,
    write_probe_report,
)


def _output_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": CLASSIFICATION_OUTPUT_SCHEMA_VERSION,
        "task_type": "code_change",
        "project_area": "jarvisos",
        "complexity_hint": "medium",
        "needs_context": True,
        "sensitivity_hint": "internal",
        "allowed_next_step": "request_bounded_context",
        "confidence": 0.82,
        "refusal_or_uncertainty_reason": None,
    }
    payload.update(overrides)
    return payload


def _diagnostics(
    *,
    prompt: str = "",
    input_chars: int = 0,
    raw_content_empty: bool = False,
    thinking_present: bool | None = None,
    done_reason: str | None = None,
    schema_valid: bool = False,
    fallback_used: bool = False,
    fallback_reason: ClassificationFailureCode | None = None,
) -> ClassificationAttemptDiagnostics:
    return ClassificationAttemptDiagnostics(
        model_name=DEFAULT_CLASSIFICATION_MODEL,
        endpoint="http://localhost:11434/api/chat",
        prompt_chars=len(prompt),
        input_chars=input_chars,
        max_output_tokens=256,
        temperature=0,
        timeout_seconds=15,
        latency_ms=1,
        raw_content_empty=raw_content_empty,
        thinking_present=thinking_present,
        done_reason=done_reason,
        schema_valid=schema_valid,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
    )


class FakeClassificationAdapter:
    def __init__(
        self,
        *,
        content: str | None = None,
        failure_code: ClassificationFailureCode | None = None,
    ) -> None:
        self.content = content
        self.failure_code = failure_code
        self.called = False
        self.prompt = ""
        self.config = ClassificationAdapterConfig()

    def complete(self, prompt: str, *, input_chars: int = 0) -> ClassificationAdapterResult:
        self.called = True
        self.prompt = prompt
        content = json.dumps(_output_payload()) if self.content is None else self.content
        if self.failure_code:
            return ClassificationAdapterResult(
                success=False,
                model_name=DEFAULT_CLASSIFICATION_MODEL,
                runtime_endpoint="http://localhost:11434/api/chat",
                diagnostics=_diagnostics(
                    prompt=prompt,
                    input_chars=input_chars,
                    raw_content_empty=True,
                    fallback_used=True,
                    fallback_reason=self.failure_code,
                ),
                failure_code=self.failure_code,
                failure_message=self.failure_code.value,
            )
        return ClassificationAdapterResult(
            success=True,
            model_name=DEFAULT_CLASSIFICATION_MODEL,
            runtime_endpoint="http://localhost:11434/api/chat",
            diagnostics=_diagnostics(prompt=prompt, input_chars=input_chars, raw_content_empty=content.strip() == ""),
            response_text=content,
        )


class FakeProbeAdapter:
    def __init__(self, config: ClassificationAdapterConfig) -> None:
        self.config = config

    def complete(self, prompt: str, *, input_chars: int = 0) -> ClassificationAdapterResult:
        content = json.dumps(
            _output_payload(
                task_type="documentation",
                project_area="documentation",
                complexity_hint="low",
                sensitivity_hint="internal",
                allowed_next_step="request_bounded_context",
                confidence=0.9,
            )
        )
        return ClassificationAdapterResult(
            success=True,
            model_name=self.config.model_name,
            runtime_endpoint=self.config.endpoint_url,
            diagnostics=ClassificationAttemptDiagnostics(
                model_name=self.config.model_name,
                endpoint=self.config.endpoint_url,
                prompt_chars=len(prompt),
                input_chars=input_chars,
                max_output_tokens=self.config.max_output_tokens,
                temperature=self.config.temperature,
                timeout_seconds=self.config.timeout_seconds,
                latency_ms=12,
                raw_content_empty=False,
                thinking_present=False,
                done_reason="stop",
                schema_valid=False,
                fallback_used=False,
                fallback_reason=None,
            ),
            response_text=content,
        )


class FakeMinimalProbeAdapter:
    def __init__(self, config: ClassificationAdapterConfig) -> None:
        self.config = config

    def complete(self, prompt: str, *, input_chars: int = 0) -> ClassificationAdapterResult:
        content = json.dumps(
            {
                "task_type": "code",
                "project": "jarvisos",
                "sensitivity": "internal",
                "next": "answer",
                "confidence": 0.86,
            }
        )
        return ClassificationAdapterResult(
            success=True,
            model_name=self.config.model_name,
            runtime_endpoint=self.config.endpoint_url,
            diagnostics=ClassificationAttemptDiagnostics(
                model_name=self.config.model_name,
                endpoint=self.config.endpoint_url,
                prompt_chars=len(prompt),
                input_chars=input_chars,
                max_output_tokens=self.config.max_output_tokens,
                temperature=self.config.temperature,
                timeout_seconds=self.config.timeout_seconds,
                latency_ms=7,
                raw_content_empty=False,
                thinking_present=False,
                done_reason="stop",
                schema_valid=False,
                fallback_used=False,
                fallback_reason=None,
            ),
            response_text=content,
        )


class FakeConfidenceCalibrationAdapter:
    def __init__(self, config: ClassificationAdapterConfig) -> None:
        self.config = config

    def complete(self, prompt: str, *, input_chars: int = 0) -> ClassificationAdapterResult:
        content = json.dumps(_minimal_payload_for_prompt(prompt))
        return ClassificationAdapterResult(
            success=True,
            model_name=self.config.model_name,
            runtime_endpoint=self.config.endpoint_url,
            diagnostics=ClassificationAttemptDiagnostics(
                model_name=self.config.model_name,
                endpoint=self.config.endpoint_url,
                prompt_chars=len(prompt),
                input_chars=input_chars,
                max_output_tokens=self.config.max_output_tokens,
                temperature=self.config.temperature,
                timeout_seconds=self.config.timeout_seconds,
                latency_ms=9,
                raw_content_empty=False,
                thinking_present=False,
                done_reason="stop",
                schema_valid=False,
                fallback_used=False,
                fallback_reason=None,
            ),
            response_text=content,
        )


class FakeLabelAgreementAdapter:
    def __init__(self, config: ClassificationAdapterConfig) -> None:
        self.config = config

    def complete(self, prompt: str, *, input_chars: int = 0) -> ClassificationAdapterResult:
        content = json.dumps(_label_agreement_payload_for_prompt(prompt))
        return ClassificationAdapterResult(
            success=True,
            model_name=self.config.model_name,
            runtime_endpoint=self.config.endpoint_url,
            diagnostics=ClassificationAttemptDiagnostics(
                model_name=self.config.model_name,
                endpoint=self.config.endpoint_url,
                prompt_chars=len(prompt),
                input_chars=input_chars,
                max_output_tokens=self.config.max_output_tokens,
                temperature=self.config.temperature,
                timeout_seconds=self.config.timeout_seconds,
                latency_ms=11,
                raw_content_empty=False,
                thinking_present=False,
                done_reason="stop",
                schema_valid=False,
                fallback_used=False,
                fallback_reason=None,
            ),
            response_text=content,
        )


class FakeBakeoffAdapter:
    def __init__(self, config: ClassificationAdapterConfig) -> None:
        self.config = config

    def complete(self, prompt: str, *, input_chars: int = 0) -> ClassificationAdapterResult:
        if self.config.model_name == "qwen3:8b":
            return ClassificationAdapterResult(
                success=False,
                model_name=self.config.model_name,
                runtime_endpoint=self.config.endpoint_url,
                diagnostics=ClassificationAttemptDiagnostics(
                    model_name=self.config.model_name,
                    endpoint=self.config.endpoint_url,
                    prompt_chars=len(prompt),
                    input_chars=input_chars,
                    max_output_tokens=self.config.max_output_tokens,
                    temperature=self.config.temperature,
                    timeout_seconds=self.config.timeout_seconds,
                    latency_ms=15000,
                    raw_content_empty=True,
                    thinking_present=None,
                    done_reason=None,
                    schema_valid=False,
                    fallback_used=True,
                    fallback_reason=ClassificationFailureCode.timeout,
                ),
                failure_code=ClassificationFailureCode.timeout,
                failure_message="simulated timeout",
            )
        content = json.dumps(_label_agreement_payload_for_prompt(prompt))
        return ClassificationAdapterResult(
            success=True,
            model_name=self.config.model_name,
            runtime_endpoint=self.config.endpoint_url,
            diagnostics=ClassificationAttemptDiagnostics(
                model_name=self.config.model_name,
                endpoint=self.config.endpoint_url,
                prompt_chars=len(prompt),
                input_chars=input_chars,
                max_output_tokens=self.config.max_output_tokens,
                temperature=self.config.temperature,
                timeout_seconds=self.config.timeout_seconds,
                latency_ms=11,
                raw_content_empty=False,
                thinking_present=False,
                done_reason="stop",
                schema_valid=False,
                fallback_used=False,
                fallback_reason=None,
            ),
            response_text=content,
        )


class PerfectBakeoffAdapter:
    def __init__(self, config: ClassificationAdapterConfig) -> None:
        self.config = config

    def complete(self, prompt: str, *, input_chars: int = 0) -> ClassificationAdapterResult:
        content = json.dumps(_perfect_label_agreement_payload_for_prompt(prompt))
        return ClassificationAdapterResult(
            success=True,
            model_name=self.config.model_name,
            runtime_endpoint=self.config.endpoint_url,
            diagnostics=ClassificationAttemptDiagnostics(
                model_name=self.config.model_name,
                endpoint=self.config.endpoint_url,
                prompt_chars=len(prompt),
                input_chars=input_chars,
                max_output_tokens=self.config.max_output_tokens,
                temperature=self.config.temperature,
                timeout_seconds=self.config.timeout_seconds,
                latency_ms=9,
                raw_content_empty=False,
                thinking_present=False,
                done_reason="stop",
                schema_valid=False,
                fallback_used=False,
                fallback_reason=None,
            ),
            response_text=content,
        )


class FakeNonCriticalHintAdapter:
    def __init__(self, config: ClassificationAdapterConfig) -> None:
        self.config = config

    def complete(self, prompt: str, *, input_chars: int = 0) -> ClassificationAdapterResult:
        content = json.dumps(_non_critical_hint_payload_for_prompt(prompt))
        return ClassificationAdapterResult(
            success=True,
            model_name=self.config.model_name,
            runtime_endpoint=self.config.endpoint_url,
            diagnostics=ClassificationAttemptDiagnostics(
                model_name=self.config.model_name,
                endpoint=self.config.endpoint_url,
                prompt_chars=len(prompt),
                input_chars=input_chars,
                max_output_tokens=self.config.max_output_tokens,
                temperature=self.config.temperature,
                timeout_seconds=self.config.timeout_seconds,
                latency_ms=8,
                raw_content_empty=False,
                thinking_present=False,
                done_reason="stop",
                schema_valid=False,
                fallback_used=False,
                fallback_reason=None,
            ),
            response_text=content,
        )


class FakeProfileFailureAdapter(FakeNonCriticalHintAdapter):
    def complete(self, prompt: str, *, input_chars: int = 0) -> ClassificationAdapterResult:
        if "Classify into the canonical metadata JSON form" in prompt:
            return ClassificationAdapterResult(
                success=False,
                model_name=self.config.model_name,
                runtime_endpoint=self.config.endpoint_url,
                diagnostics=ClassificationAttemptDiagnostics(
                    model_name=self.config.model_name,
                    endpoint=self.config.endpoint_url,
                    prompt_chars=len(prompt),
                    input_chars=input_chars,
                    max_output_tokens=self.config.max_output_tokens,
                    temperature=self.config.temperature,
                    timeout_seconds=self.config.timeout_seconds,
                    latency_ms=15000,
                    raw_content_empty=True,
                    thinking_present=False,
                    done_reason=None,
                    schema_valid=False,
                    fallback_used=True,
                    fallback_reason=ClassificationFailureCode.timeout,
                ),
                failure_code=ClassificationFailureCode.timeout,
                failure_message="simulated profile timeout",
            )
        return super().complete(prompt, input_chars=input_chars)


def _minimal_payload_for_prompt(prompt: str) -> dict[str, object]:
    if "text=help" in prompt:
        return {
            "task_type": "ambiguous",
            "project": "unknown",
            "sensitivity": "unknown",
            "next": "clarify",
            "confidence": 0.44,
        }
    if "delete all logs" in prompt:
        return {
            "task_type": "unsafe",
            "project": "unknown",
            "sensitivity": "unknown",
            "next": "review",
            "confidence": 0.58,
        }
    if "proprietary impeller geometry" in prompt:
        return {
            "task_type": "engineering",
            "project": "workspace",
            "sensitivity": "sensitive",
            "next": "review",
            "confidence": 0.62,
        }
    if "documentation" in prompt:
        return {
            "task_type": "docs",
            "project": "jarvisos",
            "sensitivity": "internal",
            "next": "answer",
            "confidence": 0.72,
        }
    return {
        "task_type": "code",
        "project": "jarvisos",
        "sensitivity": "internal",
        "next": "answer",
        "confidence": 0.9,
    }


def _label_agreement_payload_for_prompt(prompt: str) -> dict[str, object]:
    case_text = prompt.rsplit("text=", 1)[-1]
    if case_text == "help":
        return {
            "task": "unknown",
            "project": "unknown",
            "sensitivity": "unknown",
            "risk": "unknown",
            "next": "clarify",
            "confidence": 0.5,
        }
    if "external API" in case_text:
        return {
            "task": "action",
            "project": "unknown",
            "sensitivity": "internal",
            "risk": "safe",
            "next": "answer",
            "confidence": 0.9,
        }
    if "delete all local logs" in case_text:
        return {
            "task": "action",
            "project": "unknown",
            "sensitivity": "internal",
            "risk": "safe",
            "next": "answer",
            "confidence": 0.9,
        }
    if "workspace project prototype" in case_text:
        return {
            "task": "question",
            "project": "workspace",
            "sensitivity": "public",
            "risk": "safe",
            "next": "answer",
            "confidence": 0.9,
        }
    if "local indexing command" in case_text:
        return {
            "task": "action",
            "project": "unknown",
            "sensitivity": "internal",
            "risk": "needs_review",
            "next": "review",
            "confidence": 0.9,
        }
    if "Euler integration" in case_text:
        return {
            "task": "question",
            "project": "general",
            "sensitivity": "public",
            "risk": "safe",
            "next": "answer",
            "confidence": 0.9,
        }
    if "documentation" in case_text:
        return {
            "task": "docs",
            "project": "jarvisos",
            "sensitivity": "internal",
            "risk": "needs_review",
            "next": "review",
            "confidence": 0.9,
        }
    return {
        "task": "code",
        "project": "jarvisos",
        "sensitivity": "internal",
        "risk": "needs_review",
        "next": "review",
        "confidence": 0.9,
    }


def _perfect_label_agreement_payload_for_prompt(prompt: str) -> dict[str, object]:
    case_text = prompt.rsplit("text=", 1)[-1]
    if case_text == "help":
        return {
            "task": "unknown",
            "project": "unknown",
            "sensitivity": "unknown",
            "risk": "unknown",
            "next": "clarify",
            "confidence": 0.9,
        }
    if "external API" in case_text:
        return {
            "task": "action",
            "project": "unknown",
            "sensitivity": "internal",
            "risk": "needs_review",
            "next": "review",
            "confidence": 0.9,
        }
    if "delete all local logs" in case_text:
        return {
            "task": "action",
            "project": "unknown",
            "sensitivity": "internal",
            "risk": "unsafe",
            "next": "block",
            "confidence": 0.9,
        }
    if "workspace project prototype" in case_text:
        return {
            "task": "question",
            "project": "workspace",
            "sensitivity": "sensitive",
            "risk": "needs_review",
            "next": "review",
            "confidence": 0.9,
        }
    if "local indexing command" in case_text:
        return {
            "task": "action",
            "project": "unknown",
            "sensitivity": "internal",
            "risk": "needs_review",
            "next": "review",
            "confidence": 0.9,
        }
    if "Euler integration" in case_text:
        return {
            "task": "question",
            "project": "general",
            "sensitivity": "public",
            "risk": "safe",
            "next": "answer",
            "confidence": 0.9,
        }
    if "documentation" in case_text:
        return {
            "task": "docs",
            "project": "jarvisos",
            "sensitivity": "internal",
            "risk": "needs_review",
            "next": "review",
            "confidence": 0.9,
        }
    return {
        "task": "code",
        "project": "jarvisos",
        "sensitivity": "internal",
        "risk": "needs_review",
        "next": "review",
        "confidence": 0.9,
    }


def _non_critical_hint_payload_for_prompt(prompt: str) -> dict[str, object]:
    case_text = prompt.rsplit("text=", 1)[-1]
    if case_text == "help":
        return {
            "task_hint": "unknown",
            "project_hint": "unknown",
            "topic_hints": ["general"],
            "context_need_hint": "clarify",
            "confidence": 0.9,
        }
    if "failing JarvisOS backend classification pytest" in case_text:
        return {
            "task_hint": "debug",
            "project_hint": "jarvisos",
            "topic_hints": ["jarvisos", "local_ai", "classification"],
            "context_need_hint": "small",
            "confidence": 0.9,
        }
    if "design documentation" in case_text:
        return {
            "task_hint": "docs",
            "project_hint": "jarvisos",
            "topic_hints": ["jarvisos", "docs", "local_ai"],
            "context_need_hint": "small",
            "confidence": 0.9,
        }
    if "workspace project impeller modeling concept" in case_text:
        return {
            "task_hint": "question",
            "project_hint": "workspace",
            "topic_hints": ["workspace"],
            "context_need_hint": "small",
            "confidence": 0.9,
        }
    if "residence time" in case_text:
        return {
            "task_hint": "question",
            "project_hint": "coursework",
            "topic_hints": ["coursework"],
            "context_need_hint": "none",
            "confidence": 0.9,
        }
    if "Euler integration" in case_text:
        return {
            "task_hint": "question",
            "project_hint": "general",
            "topic_hints": ["general"],
            "context_need_hint": "none",
            "confidence": 0.9,
        }
    if "weekly admin" in case_text:
        return {
            "task_hint": "planning",
            "project_hint": "personal",
            "topic_hints": ["planning", "general"],
            "context_need_hint": "small",
            "confidence": 0.9,
        }
    if "Gemma and Qwen models" in case_text:
        return {
            "task_hint": "planning",
            "project_hint": "jarvisos",
            "topic_hints": ["jarvisos", "local_ai", "models"],
            "context_need_hint": "medium",
            "confidence": 0.9,
        }
    return {
        "task_hint": "general",
        "project_hint": "unknown",
        "topic_hints": ["general"],
        "context_need_hint": "small",
        "confidence": 0.6,
    }


def test_classification_input_contract_is_strict_and_bounded() -> None:
    payload = ClassificationInput(
        schema_version=CLASSIFICATION_INPUT_SCHEMA_VERSION,
        text="Implement a small JarvisOS backend patch.",
        source=ClassificationSource.codex_task,
        metadata={"scope": "test"},
    )

    assert payload.text.startswith("Implement")

    with pytest.raises(ValidationError):
        ClassificationInput(text="", source=ClassificationSource.user_prompt)
    with pytest.raises(ValidationError):
        ClassificationInput(text="x" * 1201, source=ClassificationSource.user_prompt)
    with pytest.raises(ValidationError):
        ClassificationInput(text="ok", source=ClassificationSource.user_prompt, metadata={str(i): "x" for i in range(11)})
    with pytest.raises(ValidationError):
        ClassificationInput(text="ok", source=ClassificationSource.user_prompt, unexpected="nope")


def test_classification_output_contract_forbids_extra_fields_and_wrong_schema() -> None:
    output = ClassificationOutput.model_validate(_output_payload())

    assert output.schema_version == CLASSIFICATION_OUTPUT_SCHEMA_VERSION

    with pytest.raises(ValidationError):
        ClassificationOutput.model_validate(_output_payload(extra_field="not allowed"))
    with pytest.raises(ValidationError):
        ClassificationOutput.model_validate(_output_payload(schema_version="wrong"))
    with pytest.raises(ValidationError):
        ClassificationOutput.model_validate(_output_payload(confidence=1.1))


def test_classification_contract_exposes_only_non_critical_advisory_hint_fields() -> None:
    assert CLASSIFICATION_ADVISORY_HINT_FIELDS == (
        "task_hint",
        "project_hint",
        "topic_hint",
        "context_need_hint",
        "confidence",
    )
    assert "risk" not in ClassificationOutput.model_fields
    assert "next_action" not in ClassificationOutput.model_fields
    assert "provider_selection" not in ClassificationOutput.model_fields
    assert "tool_execution" not in ClassificationOutput.model_fields
    assert "memory_write" not in ClassificationOutput.model_fields
    assert "retrieval" not in ClassificationOutput.model_fields
    assert "final_sensitivity" not in ClassificationOutput.model_fields
    assert {
        "risk",
        "next_action",
        "permission",
        "provider_selection",
        "tool_execution",
        "memory_write",
        "retrieval",
        "route_selection",
        "external_calls",
        "final_sensitivity",
        "safety_decisions",
    }.issubset(set(MODEL_NON_AUTHORITY_BOUNDARIES))


def test_classification_budget_policy_is_explicit_and_bounded() -> None:
    policy = ClassificationBudgetPolicy()

    assert policy.model_name == DEFAULT_CLASSIFICATION_MODEL
    assert policy.max_input_chars == 1200
    assert policy.max_prompt_chars == MAX_CLASSIFICATION_PROMPT_CHARS
    assert policy.max_output_tokens == 256
    assert policy.diagnostic_num_predict_candidates == CLASSIFICATION_DIAGNOSTIC_NUM_PREDICT_CANDIDATES
    assert policy.diagnostic_num_predict_candidates == (128, 256, 384, 512)
    assert policy.temperature == 0
    assert policy.timeout_seconds == 15


def test_prompt_is_bounded_json_only_instruction() -> None:
    request = ClassificationInput(text="x" * 1200, source=ClassificationSource.manual_test)
    prompt = build_classification_prompt(request)

    assert len(prompt) <= MAX_CLASSIFICATION_PROMPT_CHARS
    assert "JSON only" in prompt
    assert "no tools/retrieval/memory/routing/external/state" in prompt
    assert "text=" in prompt


def test_parser_accepts_valid_output_and_rejects_invalid_shapes() -> None:
    output = parse_classification_output(json.dumps(_output_payload()))

    assert output.task_type == TaskType.code_change

    with pytest.raises(ClassificationParseError) as invalid_json:
        parse_classification_output("{not json")
    assert invalid_json.value.code == ClassificationFailureCode.invalid_json

    with pytest.raises(ClassificationParseError) as non_object:
        parse_classification_output('["not", "object"]')
    assert non_object.value.code == ClassificationFailureCode.non_object_json

    with pytest.raises(ClassificationParseError) as extra_field:
        parse_classification_output(json.dumps(_output_payload(extra="nope")))
    assert extra_field.value.code == ClassificationFailureCode.extra_fields


def test_parser_rejects_authority_claims_and_impossible_combinations() -> None:
    with pytest.raises(ClassificationParseError) as authority:
        parse_classification_output(
            json.dumps(_output_payload(refusal_or_uncertainty_reason="I will execute the requested tool."))
        )
    assert authority.value.code == ClassificationFailureCode.model_claimed_authority

    with pytest.raises(ClassificationParseError) as impossible:
        parse_classification_output(
            json.dumps(
                _output_payload(
                    sensitivity_hint="secret",
                    allowed_next_step="answer_locally",
                )
            )
        )
    assert impossible.value.code == ClassificationFailureCode.impossible_combination


@pytest.mark.parametrize(
    "authority_text",
    [
        "I will execute the requested tool.",
        "I will call the external provider.",
        "I will retrieve files for this request.",
        "I will write memory for this user.",
        "Provider routing is authorized.",
    ],
)
def test_parser_rejects_model_claims_to_provider_tool_memory_or_retrieval_authority(authority_text: str) -> None:
    with pytest.raises(ClassificationParseError) as authority:
        parse_classification_output(json.dumps(_output_payload(refusal_or_uncertainty_reason=authority_text)))

    assert authority.value.code == ClassificationFailureCode.model_claimed_authority


def test_parser_accepts_low_confidence_for_service_policy() -> None:
    output = parse_classification_output(json.dumps(_output_payload(confidence=LOW_CONFIDENCE_THRESHOLD - 0.01)))

    assert output.confidence < LOW_CONFIDENCE_THRESHOLD


@pytest.mark.parametrize(
    "url",
    [
        "https://localhost:11434/api/chat",
        "http://api.example.com/api/chat",
        "http://192.168.1.10:11434/api/chat",
        "http://user:pass@localhost:11434/api/chat",
    ],
)
def test_adapter_rejects_non_local_or_unsafe_endpoints(url: str) -> None:
    with pytest.raises((ClassificationAdapterConfigurationError, ValidationError)):
        ClassificationAdapterConfig(endpoint_url=url)


def test_adapter_uses_local_mocked_http_client_without_live_model_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["model"] == DEFAULT_CLASSIFICATION_MODEL
        assert payload["stream"] is False
        assert payload["format"] == "json"
        assert payload["options"]["temperature"] == 0
        assert payload["options"]["num_predict"] == 256
        return httpx.Response(200, json={"message": {"content": json.dumps(_output_payload())}}, request=request)

    adapter = LocalGemmaClassificationAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    prompt = build_classification_prompt(ClassificationInput(text="Implement JarvisOS tests."))
    result = adapter.complete(prompt, input_chars=len("Implement JarvisOS tests."))

    assert result.success is True
    assert result.response_text is not None
    assert result.diagnostics.prompt_chars == len(prompt)
    assert result.diagnostics.input_chars == len("Implement JarvisOS tests.")
    assert result.diagnostics.max_output_tokens == 256
    assert result.diagnostics.timeout_seconds == 15
    assert result.diagnostics.raw_content_empty is False
    assert result.diagnostics.schema_valid is False
    assert result.diagnostics.fallback_used is False


def test_adapter_reports_thinking_budget_exhaustion() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"message": {"content": "", "thinking": "still thinking"}, "done_reason": "length"},
            request=request,
        )

    adapter = LocalGemmaClassificationAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = adapter.complete(build_classification_prompt(ClassificationInput(text="Classify this.")))

    assert result.success is False
    assert result.failure_code == ClassificationFailureCode.thinking_budget_exhausted
    assert result.diagnostics.raw_content_empty is True
    assert result.diagnostics.thinking_present is True
    assert result.diagnostics.done_reason == "length"
    assert result.diagnostics.fallback_reason == ClassificationFailureCode.thinking_budget_exhausted


def test_adapter_reports_done_reason_length_without_valid_content() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"message": {"content": ""}, "done_reason": "length"},
            request=request,
        )

    adapter = LocalGemmaClassificationAdapter(client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = adapter.complete(build_classification_prompt(ClassificationInput(text="Classify this.")))

    assert result.success is False
    assert result.failure_code == ClassificationFailureCode.done_reason_length
    assert result.diagnostics.raw_content_empty is True
    assert result.diagnostics.thinking_present is None
    assert result.diagnostics.done_reason == "length"


def test_adapter_reports_timeout_http_error_and_invalid_endpoint() -> None:
    request = ClassificationInput(text="Implement JarvisOS tests.")
    prompt = build_classification_prompt(request)

    def timeout_handler(http_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow local model", request=http_request)

    timeout_adapter = LocalGemmaClassificationAdapter(client=httpx.Client(transport=httpx.MockTransport(timeout_handler)))
    timeout_result = timeout_adapter.complete(prompt, input_chars=len(request.text))

    assert timeout_result.success is False
    assert timeout_result.failure_code == ClassificationFailureCode.timeout
    assert timeout_result.diagnostics.fallback_reason == ClassificationFailureCode.timeout

    def http_error_handler(http_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "local failure"}, request=http_request)

    http_error_adapter = LocalGemmaClassificationAdapter(client=httpx.Client(transport=httpx.MockTransport(http_error_handler)))
    http_error_result = http_error_adapter.complete(prompt, input_chars=len(request.text))

    assert http_error_result.success is False
    assert http_error_result.failure_code == ClassificationFailureCode.http_error
    assert http_error_result.diagnostics.fallback_reason == ClassificationFailureCode.http_error

    invalid_endpoint_adapter = LocalGemmaClassificationAdapter()
    invalid_endpoint_adapter.config.endpoint_url = "https://localhost:11434/api/chat"
    invalid_endpoint_result = invalid_endpoint_adapter.complete(prompt, input_chars=len(request.text))

    assert invalid_endpoint_result.success is False
    assert invalid_endpoint_result.failure_code == ClassificationFailureCode.invalid_endpoint
    assert invalid_endpoint_result.diagnostics.fallback_reason == ClassificationFailureCode.invalid_endpoint


def test_service_accepts_valid_model_output() -> None:
    adapter = FakeClassificationAdapter(
        content=json.dumps(
            _output_payload(
                task_type="documentation",
                project_area="documentation",
                complexity_hint="low",
                needs_context=True,
                sensitivity_hint="internal",
                allowed_next_step="request_bounded_context",
                confidence=0.9,
            )
        )
    )

    result = classify_text(ClassificationInput(text="Update docs for JarvisOS.", source=ClassificationSource.codex_task), adapter=adapter)

    assert result.source == ClassificationResultSource.model
    assert result.model_output_accepted is True
    assert result.classification.task_type == TaskType.documentation
    assert result.advisory_hints is not None
    assert result.advisory_hints.task_hint == TaskType.documentation
    assert result.advisory_hints.project_hint == ProjectArea.documentation
    assert result.advisory_hints.context_need_hint is True
    assert result.advisory_hints.confidence == 0.9
    assert result.diagnostics is not None
    assert result.diagnostics.schema_valid is True
    assert result.diagnostics.fallback_used is False
    assert result.diagnostics.fallback_reason is None
    assert result.diagnostics.raw_content_empty is False
    assert adapter.called is True


def test_service_falls_back_on_invalid_json_and_low_confidence() -> None:
    invalid = classify_text(
        ClassificationInput(text="Implement backend tests for JarvisOS."),
        adapter=FakeClassificationAdapter(content="{bad json"),
    )
    low_confidence = classify_text(
        ClassificationInput(text="help"),
        adapter=FakeClassificationAdapter(content=json.dumps(_output_payload(task_type="unknown", confidence=0.2))),
    )

    assert invalid.source == ClassificationResultSource.fallback
    assert invalid.fallback_reasons == [ClassificationFailureCode.invalid_json]
    assert invalid.diagnostics is not None
    assert invalid.diagnostics.schema_valid is False
    assert invalid.diagnostics.fallback_used is True
    assert invalid.diagnostics.fallback_reason == ClassificationFailureCode.invalid_json
    assert low_confidence.source == ClassificationResultSource.fallback
    assert low_confidence.fallback_reasons == [ClassificationFailureCode.low_confidence]
    assert low_confidence.classification.allowed_next_step == AllowedNextStep.ask_clarification
    assert low_confidence.diagnostics is not None
    assert low_confidence.diagnostics.schema_valid is True
    assert low_confidence.diagnostics.fallback_reason == ClassificationFailureCode.low_confidence


def test_service_falls_back_on_empty_content_timeout_http_error_and_done_reason_length() -> None:
    empty = classify_text(
        ClassificationInput(text="Implement backend tests for JarvisOS."),
        adapter=FakeClassificationAdapter(content=""),
    )
    timeout = classify_text(
        ClassificationInput(text="Implement backend tests for JarvisOS."),
        adapter=FakeClassificationAdapter(failure_code=ClassificationFailureCode.timeout),
    )
    http_error = classify_text(
        ClassificationInput(text="Implement backend tests for JarvisOS."),
        adapter=FakeClassificationAdapter(failure_code=ClassificationFailureCode.http_error),
    )
    done_reason_length = classify_text(
        ClassificationInput(text="Implement backend tests for JarvisOS."),
        adapter=FakeClassificationAdapter(failure_code=ClassificationFailureCode.done_reason_length),
    )

    assert empty.source == ClassificationResultSource.fallback
    assert empty.fallback_reasons == [ClassificationFailureCode.empty_content]
    assert empty.diagnostics is not None
    assert empty.diagnostics.raw_content_empty is True
    assert empty.diagnostics.fallback_reason == ClassificationFailureCode.empty_content
    assert timeout.fallback_reasons == [ClassificationFailureCode.timeout]
    assert timeout.diagnostics is not None
    assert timeout.diagnostics.fallback_reason == ClassificationFailureCode.timeout
    assert http_error.fallback_reasons == [ClassificationFailureCode.http_error]
    assert http_error.diagnostics is not None
    assert http_error.diagnostics.fallback_reason == ClassificationFailureCode.http_error
    assert done_reason_length.fallback_reasons == [ClassificationFailureCode.done_reason_length]
    assert done_reason_length.diagnostics is not None
    assert done_reason_length.diagnostics.fallback_reason == ClassificationFailureCode.done_reason_length


def test_service_omits_metadata_from_prompt_budget_and_calls_adapter() -> None:
    adapter = FakeClassificationAdapter()
    request = ClassificationInput(
        text="x" * 1200,
        source=ClassificationSource.manual_test,
        metadata={f"k{i}": f"METADATA_MARKER_{i}" for i in range(10)},
    )

    result = classify_text(request, adapter=adapter)

    assert result.source in {ClassificationResultSource.model, ClassificationResultSource.model_with_deterministic_override}
    assert result.diagnostics is not None
    assert result.diagnostics.prompt_chars == MAX_CLASSIFICATION_PROMPT_CHARS
    assert result.diagnostics.input_chars == 1200
    assert adapter.called is True
    assert "METADATA_MARKER" not in adapter.prompt


def test_diagnostic_metadata_does_not_include_raw_prompt_text() -> None:
    marker = "SENSITIVE_MARKER_123"
    adapter = FakeClassificationAdapter()

    result = classify_text(
        ClassificationInput(text=f"Implement JarvisOS tests with {marker}.", source=ClassificationSource.codex_task),
        adapter=adapter,
    )

    assert result.diagnostics is not None
    assert marker in adapter.prompt
    diagnostics_json = result.diagnostics.model_dump_json()
    assert marker not in diagnostics_json
    assert "Implement JarvisOS tests" not in diagnostics_json


def test_service_deterministic_hard_checks_prevent_model_downgrade() -> None:
    adapter = FakeClassificationAdapter(
        content=json.dumps(
            _output_payload(
                task_type="engineering_question",
                project_area="general_engineering",
                sensitivity_hint="public",
                allowed_next_step="answer_locally",
                confidence=0.95,
            )
        )
    )

    result = classify_text(
        ClassificationInput(text="Zephyr proprietary impeller geometry for patent design."),
        adapter=adapter,
    )

    assert result.classification.sensitivity_hint == SensitivityHint.sensitive_ip
    assert result.classification.allowed_next_step == AllowedNextStep.human_review
    assert result.model_output_accepted is False
    assert adapter.called is False


def test_service_applies_deterministic_next_step_override_for_external_api_request() -> None:
    adapter = FakeClassificationAdapter(
        content=json.dumps(
            _output_payload(
                task_type="engineering_question",
                project_area="general_engineering",
                sensitivity_hint="public",
                allowed_next_step="answer_locally",
                confidence=0.9,
            )
        )
    )

    result = classify_text(ClassificationInput(text="Call OpenAI to solve this engineering question."), adapter=adapter)

    assert result.source == ClassificationResultSource.model_with_deterministic_override
    assert result.classification.allowed_next_step == AllowedNextStep.deterministic_review
    assert result.classification.sensitivity_hint == SensitivityHint.internal
    assert result.advisory_hints is not None
    assert result.advisory_hints.task_hint == TaskType.engineering_question
    assert result.advisory_hints.project_hint == ProjectArea.general_engineering
    assert result.model_output_accepted is True
    assert "jarvisos_final_sensitivity_policy" in result.deterministic_reasons
    assert "jarvisos_next_step_policy" in result.deterministic_reasons
    assert result.diagnostics is not None
    assert result.diagnostics.schema_valid is True
    assert result.diagnostics.fallback_used is False
    assert result.diagnostics.fallback_reason == ClassificationFailureCode.deterministic_override


def test_model_output_cannot_authorize_provider_tool_memory_or_retrieval_permissions() -> None:
    adapter = FakeClassificationAdapter(
        content=json.dumps(
            _output_payload(
                task_type="external_api_request",
                project_area="general_engineering",
                complexity_hint="low",
                needs_context=False,
                sensitivity_hint="public",
                allowed_next_step="answer_locally",
                confidence=0.92,
            )
        )
    )

    result = classify_text(
        ClassificationInput(text="Call DeepSeek through an external API and retrieve project context."),
        adapter=adapter,
    )

    assert result.model_output_accepted is True
    assert result.advisory_hints is not None
    assert result.advisory_hints.task_hint == TaskType.external_api_request
    assert result.classification.allowed_next_step == AllowedNextStep.deterministic_review
    assert result.classification.sensitivity_hint == SensitivityHint.internal
    assert result.classification.needs_context is False
    assert "jarvisos_next_step_policy" in result.deterministic_reasons


def test_minimal_prompt_and_parser_are_output_only_and_flat() -> None:
    request = ClassificationInput(text="Implement JarvisOS tests.", source=ClassificationSource.manual_test)
    prompt = build_minimal_classification_prompt(request)
    repaired_prompt = build_minimal_classification_prompt(
        request,
        variant=MinimalPromptVariant.minimal_think_false_v2,
    )
    output = parse_minimal_classification_output(
        json.dumps(
            {
                "task_type": "code",
                "project": "jarvisos",
                "sensitivity": "internal",
                "next": "answer",
                "confidence": 0.8,
            }
        )
    )

    assert len(prompt) <= 700
    assert "Return only one JSON object" in prompt
    assert "No reasoning" in prompt
    assert "No markdown" in prompt
    assert len(repaired_prompt) <= 700
    assert "high confidence for obvious" in repaired_prompt
    assert "genuinely ambiguous" in repaired_prompt
    assert output.task_type == "code"
    assert output.project == "jarvisos"


def test_minimal_parser_rejects_invalid_json_extra_fields_and_bad_enum() -> None:
    with pytest.raises(ClassificationParseError) as invalid_json:
        parse_minimal_classification_output("{bad")
    assert invalid_json.value.code == ClassificationFailureCode.invalid_json

    with pytest.raises(ClassificationParseError) as extra_field:
        parse_minimal_classification_output(
            json.dumps(
                {
                    "task_type": "code",
                    "project": "jarvisos",
                    "sensitivity": "internal",
                    "next": "answer",
                    "confidence": 0.8,
                    "extra": "nope",
                }
            )
        )
    assert extra_field.value.code == ClassificationFailureCode.extra_fields

    with pytest.raises(ClassificationParseError) as bad_enum:
        parse_minimal_classification_output(
            json.dumps(
                {
                    "task_type": "invented",
                    "project": "jarvisos",
                    "sensitivity": "internal",
                    "next": "answer",
                    "confidence": 0.8,
                }
            )
        )
    assert bad_enum.value.code == ClassificationFailureCode.schema_invalid


def test_label_agreement_prompt_and_parser_are_split_and_flat() -> None:
    request = ClassificationInput(text="Decide whether local execution needs review.", source=ClassificationSource.manual_test)
    prompt = build_label_agreement_prompt(request, variant=LabelAgreementProtocolVariant.split_fields_v2)
    output = parse_label_agreement_output(
        json.dumps(
            {
                "task": "action",
                "project": "unknown",
                "sensitivity": "internal",
                "risk": "needs_review",
                "next": "review",
                "confidence": 0.82,
            }
        )
    )

    assert len(prompt) <= 1200
    assert "sensitivity=public|internal|sensitive|secret|unknown" in prompt
    assert "risk=safe|needs_review|unsafe|unknown" in prompt
    assert "Classify each field independently" in prompt
    assert output.task == "action"
    assert output.risk == "needs_review"


def test_label_agreement_parser_rejects_invalid_json_extra_fields_and_bad_enum() -> None:
    with pytest.raises(ClassificationParseError) as invalid_json:
        parse_label_agreement_output("{bad")
    assert invalid_json.value.code == ClassificationFailureCode.invalid_json

    with pytest.raises(ClassificationParseError) as extra_field:
        parse_label_agreement_output(
            json.dumps(
                {
                    "task": "action",
                    "project": "unknown",
                    "sensitivity": "internal",
                    "risk": "needs_review",
                    "next": "review",
                    "confidence": 0.82,
                    "extra": "nope",
                }
            )
        )
    assert extra_field.value.code == ClassificationFailureCode.extra_fields

    with pytest.raises(ClassificationParseError) as bad_enum:
        parse_label_agreement_output(
            json.dumps(
                {
                    "task": "action",
                    "project": "unknown",
                    "sensitivity": "internal",
                    "risk": "invented",
                    "next": "review",
                    "confidence": 0.82,
                }
            )
        )
    assert bad_enum.value.code == ClassificationFailureCode.schema_invalid


def test_probe_report_uses_fixed_variants_and_omits_raw_case_text() -> None:
    cases = default_probe_cases()
    report = build_budget_probe_report(
        adapter_factory=FakeProbeAdapter,
        created_at_utc=datetime(2026, 6, 20, 12, 0, tzinfo=UTC),
    )

    assert report.schema_version == REPORT_SCHEMA_VERSION
    assert report.num_predict_variants == CLASSIFICATION_DIAGNOSTIC_NUM_PREDICT_CANDIDATES
    assert report.output_control_variants == (OutputControlVariant.default,)
    assert report.repeat_count == 1
    assert report.case_ids == tuple(case.case_id for case in cases)
    assert len(report.results) == len(cases) * len(CLASSIFICATION_DIAGNOSTIC_NUM_PREDICT_CANDIDATES)
    assert {result.num_predict for result in report.results} == set(CLASSIFICATION_DIAGNOSTIC_NUM_PREDICT_CANDIDATES)
    assert all(result.schema_valid for result in report.results)
    assert all(result.accepted for result in report.results)
    assert all(result.fallback_used is False for result in report.results)
    assert all(result.task_type == TaskType.documentation for result in report.results)

    serialized = report.model_dump_json()
    for case in cases:
        assert case.case_id in serialized
        assert case.request.text not in serialized
    assert "messages" not in serialized
    assert "prompt" not in serialized


def test_minimal_probe_report_uses_small_variants_and_omits_raw_case_text() -> None:
    cases = minimal_probe_cases()
    report = build_budget_probe_report(
        mode="minimal",
        adapter_factory=FakeMinimalProbeAdapter,
        created_at_utc=datetime(2026, 6, 20, 13, 0, tzinfo=UTC),
    )

    assert report.mode == "minimal"
    assert report.num_predict_variants == MINIMAL_DIAGNOSTIC_NUM_PREDICT_CANDIDATES
    assert report.output_control_variants == (OutputControlVariant.default,)
    assert report.repeat_count == 1
    assert report.num_predict_variants == (128, 256, 512)
    assert report.case_ids == tuple(case.case_id for case in cases)
    assert len(cases) == 3
    assert len(report.results) == len(cases) * len(MINIMAL_DIAGNOSTIC_NUM_PREDICT_CANDIDATES)
    assert all(result.schema_valid for result in report.results)
    assert all(result.accepted for result in report.results)
    assert all(result.task_type == TaskType.code_change for result in report.results)
    assert all(result.project_area == ProjectArea.jarvisos for result in report.results)
    assert all(result.allowed_next_step == AllowedNextStep.answer_locally for result in report.results)

    serialized = report.model_dump_json()
    for case in cases:
        assert case.case_id in serialized
        assert case.request.text not in serialized
    assert "messages" not in serialized
    assert "prompt" not in serialized


def test_minimal_repeat_probe_repeats_512_with_output_controls() -> None:
    cases = minimal_probe_cases()
    report = build_budget_probe_report(
        mode="minimal-repeat",
        adapter_factory=FakeMinimalProbeAdapter,
        created_at_utc=datetime(2026, 6, 20, 14, 0, tzinfo=UTC),
    )

    assert report.mode == "minimal-repeat"
    assert report.num_predict_variants == MINIMAL_REPEAT_NUM_PREDICT_CANDIDATES
    assert report.num_predict_variants == (512,)
    assert report.output_control_variants == (OutputControlVariant.default, OutputControlVariant.think_false)
    assert report.repeat_count == 3
    assert len(report.results) == len(cases) * 3 * 2
    assert {result.repeat_index for result in report.results} == {1, 2, 3}
    assert {result.output_control for result in report.results} == {
        OutputControlVariant.default,
        OutputControlVariant.think_false,
    }
    assert all(result.accepted for result in report.results)

    serialized = report.model_dump_json()
    for case in cases:
        assert case.case_id in serialized
        assert case.request.text not in serialized
    assert "messages" not in serialized
    assert "prompt" not in serialized


def test_confidence_calibration_probe_uses_think_false_and_policy_summaries() -> None:
    cases = confidence_calibration_probe_cases()
    report = build_budget_probe_report(
        mode="confidence-calibration",
        adapter_factory=FakeConfidenceCalibrationAdapter,
        created_at_utc=datetime(2026, 6, 20, 15, 0, tzinfo=UTC),
    )

    assert report.mode == "confidence-calibration"
    assert report.num_predict_variants == CONFIDENCE_CALIBRATION_NUM_PREDICT_CANDIDATES
    assert report.num_predict_variants == (512,)
    assert report.output_control_variants == (OutputControlVariant.think_false,)
    assert report.protocol_variants == (
        MinimalPromptVariant.minimal_think_false_v1,
        MinimalPromptVariant.minimal_think_false_v2,
    )
    assert report.repeat_count == CONFIDENCE_CALIBRATION_REPEAT_COUNT
    assert report.repeat_count == 3
    assert len(cases) == 5
    assert len(report.results) == len(cases) * 3 * 2
    assert {result.output_control for result in report.results} == {OutputControlVariant.think_false}
    assert {result.protocol_variant for result in report.results} == set(report.protocol_variants)
    assert all(result.num_predict == 512 for result in report.results)
    assert all(result.schema_valid for result in report.results)
    assert all(result.label_agreement is True for result in report.results)
    assert all(result.risky_acceptance is False for result in report.results)

    policy_by_name = {summary.policy: summary for summary in report.policy_summaries}
    assert set(policy_by_name) == {
        CalibrationAcceptancePolicy.strict_current_threshold,
        CalibrationAcceptancePolicy.moderate_threshold,
        CalibrationAcceptancePolicy.schema_valid_but_low_confidence_as_proposed,
    }
    assert policy_by_name[CalibrationAcceptancePolicy.strict_current_threshold].accepted_count == 12
    assert policy_by_name[CalibrationAcceptancePolicy.strict_current_threshold].fallback_count == 18
    assert policy_by_name[CalibrationAcceptancePolicy.moderate_threshold].accepted_count == 24
    assert policy_by_name[CalibrationAcceptancePolicy.moderate_threshold].fallback_count == 6
    assert policy_by_name[CalibrationAcceptancePolicy.schema_valid_but_low_confidence_as_proposed].accepted_count == 30
    assert policy_by_name[CalibrationAcceptancePolicy.schema_valid_but_low_confidence_as_proposed].fallback_count == 0
    assert all(summary.risky_acceptances == 0 for summary in report.policy_summaries)

    case_by_id = {summary.case_id: summary for summary in report.case_summaries}
    assert case_by_id["ambiguous_task"].confidence_min == 0.44
    assert case_by_id["ambiguous_task"].confidence_mean == 0.44
    assert case_by_id["ambiguous_task"].confidence_max == 0.44
    assert case_by_id["ambiguous_task"].fallback_count == 6
    assert case_by_id["obvious_code_task"].accepted_count == 6
    assert all(summary.label_agreement_rate == 1 for summary in report.case_summaries)

    serialized = report.model_dump_json()
    for case in cases:
        assert case.case_id in serialized
        assert case.request.text not in serialized
    assert "messages" not in serialized
    assert "prompt" not in serialized


def test_label_agreement_probe_splits_fields_and_reports_safety_summaries() -> None:
    cases = label_agreement_probe_cases()
    report = build_budget_probe_report(
        mode="label-agreement",
        adapter_factory=FakeLabelAgreementAdapter,
        created_at_utc=datetime(2026, 6, 21, 9, 0, tzinfo=UTC),
    )

    assert report.mode == "label-agreement"
    assert report.num_predict_variants == LABEL_AGREEMENT_NUM_PREDICT_CANDIDATES
    assert report.num_predict_variants == (512,)
    assert report.output_control_variants == (OutputControlVariant.think_false,)
    assert report.protocol_variants == (
        LabelAgreementProtocolVariant.split_fields_v1,
        LabelAgreementProtocolVariant.split_fields_v2,
    )
    assert report.repeat_count == LABEL_AGREEMENT_REPEAT_COUNT
    assert report.repeat_count == 3
    assert len(cases) == 8
    assert len(report.results) == len(cases) * 3 * 2
    assert {result.output_control for result in report.results} == {OutputControlVariant.think_false}
    assert {result.protocol_variant for result in report.results} == set(report.protocol_variants)
    assert all(result.num_predict == 512 for result in report.results)
    assert all(result.schema_valid for result in report.results)

    field_by_name = {summary.field: summary for summary in report.label_field_summaries}
    assert field_by_name["task"].agreement_rate == 1
    assert field_by_name["project"].agreement_rate == 1
    assert field_by_name["sensitivity"].agreement_rate == 0.875
    assert field_by_name["risk"].agreement_rate == 0.625
    assert field_by_name["next"].agreement_rate == 0.625

    safety = report.label_safety_summary
    assert safety is not None
    assert safety.total_results == 48
    assert safety.risky_mismatch_count == 18
    assert safety.unsafe_sensitive_false_negative_count == 12
    assert safety.accepted_risky_mismatch_count == 18
    assert safety.deterministic_catchable_count == 12
    assert safety.deterministic_uncatchable_count == 6
    assert safety.deterministic_catch_rules == {
        "destructive_command_words": 6,
        "external_provider_name": 6,
    }

    case_by_id = {summary.case_id: summary for summary in report.label_case_summaries}
    assert case_by_id["workspace_sensitive_task"].risky_mismatch_count == 6
    assert case_by_id["workspace_sensitive_task"].deterministic_catchable_count == 0
    assert case_by_id["destructive_command_task"].unsafe_sensitive_false_negative_count == 6
    assert case_by_id["destructive_command_task"].deterministic_catchable_count == 6
    assert case_by_id["ambiguous_task"].fallback_count == 6
    assert case_by_id["ambiguous_task"].all_fields_agreement_rate == 1

    lines = summary_lines(report, Path("report.json"))
    assert any("label_safety" in line for line in lines)

    serialized = report.model_dump_json()
    for case in cases:
        assert case.case_id in serialized
        assert case.request.text not in serialized
    assert "messages" not in serialized
    assert "prompt" not in serialized


def test_model_bakeoff_discovers_only_installed_allowed_candidates() -> None:
    report = build_model_bakeoff_probe_report(
        installed_model_names=("qwen3:8b", "not-a-candidate:1b", "gemma4:12b-it-qat"),
        adapter_factory=FakeBakeoffAdapter,
        created_at_utc=datetime(2026, 6, 21, 10, 0, tzinfo=UTC),
    )

    assert report.mode == "model-bakeoff"
    assert report.installed_model_names == ("qwen3:8b", "not-a-candidate:1b", "gemma4:12b-it-qat")
    assert report.candidate_model_names == ("gemma4:12b-it-qat", "qwen3:8b")
    assert report.num_predict_variants == MODEL_BAKEOFF_NUM_PREDICT_CANDIDATES
    assert report.num_predict_variants == (512,)
    assert report.repeat_count == MODEL_BAKEOFF_REPEAT_COUNT
    assert report.repeat_count == 2
    assert report.protocol_variants == (LabelAgreementProtocolVariant.split_fields_v2,)


def test_model_bakeoff_model_failure_does_not_stop_report() -> None:
    report = build_model_bakeoff_probe_report(
        installed_model_names=("gemma4:12b-it-qat", "qwen3:8b"),
        adapter_factory=FakeBakeoffAdapter,
        created_at_utc=datetime(2026, 6, 21, 10, 15, tzinfo=UTC),
    )

    summary_by_model = {summary.model_name: summary for summary in report.model_summaries}

    assert set(summary_by_model) == {"gemma4:12b-it-qat", "qwen3:8b"}
    assert summary_by_model["gemma4:12b-it-qat"].attempts == 16
    assert summary_by_model["gemma4:12b-it-qat"].schema_valid_rate == 1
    assert summary_by_model["gemma4:12b-it-qat"].think_settings == (OutputControlVariant.think_false,)
    assert summary_by_model["qwen3:8b"].attempts == 16
    assert summary_by_model["qwen3:8b"].timeout_count == 16
    assert summary_by_model["qwen3:8b"].schema_valid_rate == 0
    assert summary_by_model["qwen3:8b"].suitability_label == ModelBakeoffSuitability.rejected


def test_model_bakeoff_report_omits_raw_case_text_and_includes_model_aggregates() -> None:
    cases = label_agreement_probe_cases()
    report = build_model_bakeoff_probe_report(
        installed_model_names=("gemma4:12b-it-qat",),
        adapter_factory=FakeBakeoffAdapter,
        created_at_utc=datetime(2026, 6, 21, 10, 30, tzinfo=UTC),
    )
    lines = summary_lines(report, Path("report.json"))

    assert len(report.results) == len(cases) * MODEL_BAKEOFF_REPEAT_COUNT
    assert len(report.model_summaries) == 1
    assert len(report.model_summaries[0].field_agreement) == 5
    assert report.model_summaries[0].runtime_approved is False
    assert any("model=gemma4:12b-it-qat" in line for line in lines)

    serialized = report.model_dump_json()
    for case in cases:
        assert case.case_id in serialized
        assert case.request.text not in serialized
    assert "messages" not in serialized
    assert "prompt" not in serialized
    assert "response_text" not in serialized


def test_model_bakeoff_safety_fields_are_diagnostic_and_no_model_is_runtime_approved() -> None:
    report = build_model_bakeoff_probe_report(
        installed_model_names=("qwen3:14b",),
        adapter_factory=PerfectBakeoffAdapter,
        created_at_utc=datetime(2026, 6, 21, 10, 45, tzinfo=UTC),
    )

    summary = report.model_summaries[0]

    assert summary.suitability_label == ModelBakeoffSuitability.non_critical_hint_candidate
    assert summary.runtime_approved is False
    assert summary.accepted_risky_mismatch_count == 0
    assert summary.unsafe_sensitive_false_negative_count == 0
    assert {field.field: field.agreement_rate for field in summary.field_agreement} == {
        "task": 1,
        "project": 1,
        "sensitivity": 1,
        "risk": 1,
        "next": 1,
    }
    assert all(result.returned_label_risk is not None for result in report.results)
    assert all(result.returned_label_next is not None for result in report.results)


def test_non_critical_hint_schema_excludes_authority_and_safety_fields() -> None:
    assert set(NonCriticalHintOutput.model_fields) == {
        "task_hint",
        "project_hint",
        "topic_hints",
        "context_need_hint",
        "confidence",
    }
    forbidden_fields = {
        "risk",
        "next",
        "sensitivity",
        "provider",
        "tool",
        "memory",
        "retrieval",
        "route",
        "execution",
        "rationale",
    }
    assert forbidden_fields.isdisjoint(NonCriticalHintOutput.model_fields)

    prompt = build_non_critical_hint_prompt(
        ClassificationInput(text="Debug a JarvisOS local AI test.", source=ClassificationSource.manual_test),
        variant=NonCriticalHintProtocolVariant.explicit_enum_v1,
    )
    parsed = parse_non_critical_hint_output(
        json.dumps(
                {
                    "task_hint": "debug",
                    "project_hint": "jarvisos",
                    "topic_hints": ["jarvisos", "local_ai", "classification"],
                    "context_need_hint": "small",
                    "confidence": 0.9,
                }
        )
    )

    assert len(prompt) <= 900
    assert parsed.task_hint == "debug"
    with pytest.raises(ClassificationParseError) as extra:
        parse_non_critical_hint_output(
            json.dumps(
                {
                    "task_hint": "debug",
                    "project_hint": "jarvisos",
                    "topic_hints": ["local_ai"],
                    "context_need_hint": "small",
                    "confidence": 0.9,
                    "risk": "safe",
                }
            )
        )
    assert extra.value.code == ClassificationFailureCode.extra_fields


def test_non_critical_hint_repair_compares_only_intended_models() -> None:
    report = build_non_critical_hint_repair_report(
        installed_model_names=(
            "gemma4:12b-it-qat",
            "gemma4:31b-it-qat",
            "qwen3:8b",
            "qwen3:14b",
            "mistral-small3.2:24b",
        ),
        adapter_factory=FakeNonCriticalHintAdapter,
        created_at_utc=datetime(2026, 6, 21, 11, 0, tzinfo=UTC),
    )

    assert report.mode == "non-critical-hint-repair"
    assert report.candidate_model_names == ("gemma4:12b-it-qat", "qwen3:8b")
    assert "gemma4:31b-it-qat" not in report.candidate_model_names
    assert "qwen3:14b" not in report.candidate_model_names
    assert "mistral-small3.2:24b" not in report.candidate_model_names
    assert report.num_predict_variants == NON_CRITICAL_HINT_NUM_PREDICT_CANDIDATES
    assert report.repeat_count == NON_CRITICAL_HINT_REPEAT_COUNT
    assert report.protocol_variants == (
        NonCriticalHintProtocolVariant.compact_json_v1,
        NonCriticalHintProtocolVariant.explicit_enum_v1,
    )


def test_non_critical_hint_report_omits_raw_text_and_has_protocol_aggregates() -> None:
    cases = non_critical_hint_probe_cases()
    report = build_non_critical_hint_repair_report(
        installed_model_names=("gemma4:12b-it-qat", "qwen3:8b"),
        adapter_factory=FakeNonCriticalHintAdapter,
        created_at_utc=datetime(2026, 6, 21, 11, 15, tzinfo=UTC),
    )
    lines = summary_lines(report, Path("report.json"))

    assert len(cases) == 8
    assert len(report.results) == len(cases) * 2 * 2 * 2
    assert len(report.hint_summaries) == 4
    assert {summary.model_name for summary in report.hint_summaries} == {"gemma4:12b-it-qat", "qwen3:8b"}
    assert {summary.protocol_variant for summary in report.hint_summaries} == set(report.protocol_variants)
    assert all(len(summary.field_agreement) == 4 for summary in report.hint_summaries)
    assert all(summary.runtime_approved is False for summary in report.hint_summaries)
    assert all(summary.suitability_label == NonCriticalHintSuitability.non_critical_hint_candidate for summary in report.hint_summaries)
    assert any("hint_model=gemma4:12b-it-qat" in line for line in lines)

    serialized = report.model_dump_json()
    for case in cases:
        assert case.case_id in serialized
        assert case.request.text not in serialized
    assert "messages" not in serialized
    assert "prompt" not in serialized
    assert "response_text" not in serialized
    assert '"risk"' not in serialized
    assert '"next"' not in serialized
    assert '"sensitivity"' not in serialized


def test_non_critical_hint_mode_cannot_runtime_approve_even_perfect_output() -> None:
    report = build_non_critical_hint_repair_report(
        installed_model_names=("gemma4:12b-it-qat",),
        adapter_factory=FakeNonCriticalHintAdapter,
        created_at_utc=datetime(2026, 6, 21, 11, 30, tzinfo=UTC),
    )

    assert report.hint_summaries
    assert all(summary.suitability_label == NonCriticalHintSuitability.non_critical_hint_candidate for summary in report.hint_summaries)
    assert all(summary.runtime_approved is False for summary in report.hint_summaries)
    assert all(result.returned_label_risk is None for result in report.results)
    assert all(result.returned_label_next is None for result in report.results)
    assert all(result.returned_label_sensitivity is None for result in report.results)


def test_profile_bakeoff_profiles_share_canonical_form_but_differ_prompt_style() -> None:
    request = ClassificationInput(text="Debug JarvisOS local AI classification.", source=ClassificationSource.manual_test)
    gemma_profile = NonCriticalHintProfile(
        profile_id="gemma_test",
        model_name="gemma4:12b-it-qat",
        think_setting=OutputControlVariant.think_false,
        format_mode=ProfileFormatMode.json,
        prompt_style=ProfilePromptStyle.gemma_compact,
    )
    qwen_profile = NonCriticalHintProfile(
        profile_id="qwen_test",
        model_name="qwen3:8b",
        think_setting=OutputControlVariant.think_false,
        format_mode=ProfileFormatMode.json,
        prompt_style=ProfilePromptStyle.qwen_explicit,
    )

    gemma_prompt = build_profile_bakeoff_prompt(request, profile=gemma_profile)
    qwen_prompt = build_profile_bakeoff_prompt(request, profile=qwen_profile)

    assert gemma_prompt != qwen_prompt
    assert "task_hint" in gemma_prompt
    assert "task_hint" in qwen_prompt
    assert set(NonCriticalHintOutput.model_fields) == {
        "task_hint",
        "project_hint",
        "topic_hints",
        "context_need_hint",
        "confidence",
    }
    assert len(gemma_prompt) <= 900
    assert len(qwen_prompt) <= 900


def test_profile_bakeoff_report_stores_profile_ids_without_raw_prompt_case_or_output() -> None:
    cases = non_critical_hint_probe_cases()
    profiles = (
        NonCriticalHintProfile(
            profile_id="gemma_test_profile",
            model_name="gemma4:12b-it-qat",
            think_setting=OutputControlVariant.think_false,
            format_mode=ProfileFormatMode.json,
            prompt_style=ProfilePromptStyle.gemma_compact,
        ),
        NonCriticalHintProfile(
            profile_id="qwen_test_profile",
            model_name="qwen3:8b",
            think_setting=OutputControlVariant.think_false,
            format_mode=ProfileFormatMode.json,
            prompt_style=ProfilePromptStyle.qwen_explicit,
        ),
    )
    report = build_profile_bakeoff_report(
        installed_model_names=("gemma4:12b-it-qat", "qwen3:8b"),
        profiles=profiles,
        adapter_factory=FakeNonCriticalHintAdapter,
        created_at_utc=datetime(2026, 6, 21, 12, 0, tzinfo=UTC),
    )
    serialized = report.model_dump_json()

    assert report.mode == "profile-bakeoff"
    assert report.profile_ids == ("gemma_test_profile", "qwen_test_profile")
    assert {result.profile_id for result in report.results} == set(report.profile_ids)
    assert len(report.hint_summaries) == 2
    assert all(summary.profile_id in report.profile_ids for summary in report.hint_summaries)
    assert all(summary.style_id is not None for summary in report.hint_summaries)
    assert all(summary.format_mode == ProfileFormatMode.json for summary in report.hint_summaries)
    assert all(summary.runtime_approved is False for summary in report.hint_summaries)
    assert all(summary.suitability_label == NonCriticalHintSuitability.non_critical_hint_candidate for summary in report.hint_summaries)

    for case in cases:
        assert case.case_id in serialized
        assert case.request.text not in serialized
    assert "messages" not in serialized
    assert "prompt" not in serialized
    assert "response_text" not in serialized
    assert '"risk"' not in serialized
    assert '"next"' not in serialized
    assert '"sensitivity"' not in serialized


def test_profile_bakeoff_profile_failure_does_not_abort_diagnostic() -> None:
    profiles = (
        NonCriticalHintProfile(
            profile_id="gemma_ok_profile",
            model_name="gemma4:12b-it-qat",
            think_setting=OutputControlVariant.think_false,
            format_mode=ProfileFormatMode.json,
            prompt_style=ProfilePromptStyle.gemma_compact,
        ),
        NonCriticalHintProfile(
            profile_id="qwen_failing_profile",
            model_name="qwen3:8b",
            think_setting=OutputControlVariant.think_false,
            format_mode=ProfileFormatMode.json,
            prompt_style=ProfilePromptStyle.qwen_explicit,
        ),
    )
    report = build_profile_bakeoff_report(
        installed_model_names=("gemma4:12b-it-qat", "qwen3:8b"),
        profiles=profiles,
        adapter_factory=FakeProfileFailureAdapter,
        created_at_utc=datetime(2026, 6, 21, 12, 15, tzinfo=UTC),
    )

    by_profile = {summary.profile_id: summary for summary in report.hint_summaries}

    assert set(by_profile) == {"gemma_ok_profile", "qwen_failing_profile"}
    assert by_profile["gemma_ok_profile"].schema_valid_rate == 1
    assert by_profile["qwen_failing_profile"].timeout_count == by_profile["qwen_failing_profile"].attempts
    assert by_profile["qwen_failing_profile"].suitability_label == NonCriticalHintSuitability.rejected
    assert any(result.profile_id == "gemma_ok_profile" and result.schema_valid for result in report.results)
    assert any(result.profile_id == "qwen_failing_profile" and result.fallback_used for result in report.results)


def test_probe_rejects_non_local_endpoint_and_noncanonical_variants() -> None:
    with pytest.raises((ClassificationAdapterConfigurationError, ValidationError)):
        build_budget_probe_report(
            endpoint_url="https://localhost:11434/api/chat",
            adapter_factory=FakeProbeAdapter,
        )

    with pytest.raises(ValueError, match="128/256/384/512"):
        build_budget_probe_report(
            num_predict_variants=(256,),
            adapter_factory=FakeProbeAdapter,
        )

    with pytest.raises(ValueError, match="128/256/512"):
        build_budget_probe_report(
            mode="minimal",
            num_predict_variants=(256,),
            adapter_factory=FakeMinimalProbeAdapter,
        )

    with pytest.raises(ValueError, match="512"):
        build_budget_probe_report(
            mode="minimal-repeat",
            num_predict_variants=(256,),
            adapter_factory=FakeMinimalProbeAdapter,
        )

    with pytest.raises(ValueError, match="512"):
        build_budget_probe_report(
            mode="confidence-calibration",
            num_predict_variants=(256,),
            adapter_factory=FakeConfidenceCalibrationAdapter,
        )

    with pytest.raises(ValueError, match="512"):
        build_budget_probe_report(
            mode="label-agreement",
            num_predict_variants=(256,),
            adapter_factory=FakeLabelAgreementAdapter,
        )


def test_probe_writes_timestamped_report_and_summary_without_live_call(tmp_path: Path) -> None:
    report = build_budget_probe_report(
        cases=(default_probe_cases()[0],),
        adapter_factory=FakeProbeAdapter,
        created_at_utc=datetime(2026, 6, 20, 12, 30, tzinfo=UTC),
    )

    path = write_probe_report(report, tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    lines = summary_lines(report, path)

    assert path.parent == tmp_path
    assert path.name == "classification_budget_probe_20260620T123000.json"
    assert payload["schema_version"] == REPORT_SCHEMA_VERSION
    assert payload["case_ids"] == ["jarvisos_code_task"]
    assert "output_controls=1" in lines[1]
    assert "repeats=1" in lines[1]
    assert "results=4" in lines[1]
    assert "schema_valid=4" in lines[2]


def test_minimal_probe_writes_mode_specific_report_name(tmp_path: Path) -> None:
    report = build_budget_probe_report(
        mode="minimal",
        cases=(minimal_probe_cases()[0],),
        adapter_factory=FakeMinimalProbeAdapter,
        created_at_utc=datetime(2026, 6, 20, 13, 30, tzinfo=UTC),
    )

    path = write_probe_report(report, tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    lines = summary_lines(report, path)

    assert path.name == "classification_budget_probe_minimal_20260620T133000.json"
    assert payload["mode"] == "minimal"
    assert payload["case_ids"] == ["jarvisos_code_task"]
    assert "mode=minimal" in lines[1]
    assert "output_controls=1" in lines[1]
    assert "repeats=1" in lines[1]
    assert "results=3" in lines[1]


def test_minimal_repeat_probe_writes_mode_specific_report_name(tmp_path: Path) -> None:
    report = build_budget_probe_report(
        mode="minimal-repeat",
        cases=(minimal_probe_cases()[0],),
        adapter_factory=FakeMinimalProbeAdapter,
        created_at_utc=datetime(2026, 6, 20, 14, 30, tzinfo=UTC),
    )

    path = write_probe_report(report, tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    lines = summary_lines(report, path)

    assert path.name == "classification_budget_probe_minimal-repeat_20260620T143000.json"
    assert payload["mode"] == "minimal-repeat"
    assert payload["output_control_variants"] == ["default", "think_false"]
    assert payload["repeat_count"] == 3
    assert "mode=minimal-repeat" in lines[1]
    assert "output_controls=2" in lines[1]
    assert "repeats=3" in lines[1]
    assert "results=6" in lines[1]


@pytest.mark.parametrize(
    ("text", "task_type", "project_area", "sensitivity", "next_step"),
    [
        ("Implement backend classification tests for JarvisOS.", TaskType.code_change, ProjectArea.jarvisos, SensitivityHint.internal, AllowedNextStep.request_bounded_context),
        ("Update docs for the JarvisOS runbook.", TaskType.documentation, ProjectArea.documentation, SensitivityHint.internal, AllowedNextStep.request_bounded_context),
        ("Plan a future Zephyr modeling workflow.", TaskType.engineering_question, ProjectArea.project, SensitivityHint.internal, AllowedNextStep.request_bounded_context),
        ("Zephyr proprietary impeller geometry for patent design.", TaskType.engineering_question, ProjectArea.project, SensitivityHint.sensitive_ip, AllowedNextStep.human_review),
        ("What is Euler integration in generic engineering?", TaskType.engineering_question, ProjectArea.general_engineering, SensitivityHint.public, AllowedNextStep.answer_locally),
        ("What should I do with my day?", TaskType.personal_question, ProjectArea.personal, SensitivityHint.internal, AllowedNextStep.answer_locally),
        ("Call DeepSeek through an external API.", TaskType.external_api_request, ProjectArea.unknown, SensitivityHint.internal, AllowedNextStep.deterministic_review),
        ("Codex patch request: fix the JarvisOS backend.", TaskType.code_change, ProjectArea.jarvisos, SensitivityHint.internal, AllowedNextStep.request_bounded_context),
        ("Local-only note: remember this is private.", TaskType.local_note, ProjectArea.unknown, SensitivityHint.internal, AllowedNextStep.no_action),
        ("help", TaskType.ambiguous, ProjectArea.unknown, SensitivityHint.internal, AllowedNextStep.ask_clarification),
        ("Build a full local gatekeeper and provider routing orchestrator.", TaskType.overbroad_orchestration_request, ProjectArea.local_ai, SensitivityHint.internal, AllowedNextStep.human_review),
        ("Read all files and run PowerShell to delete all logs.", TaskType.unsafe_tool_request, ProjectArea.unknown, SensitivityHint.unknown, AllowedNextStep.human_review),
    ],
)
def test_golden_deterministic_classification_cases(
    text: str,
    task_type: TaskType,
    project_area: ProjectArea,
    sensitivity: SensitivityHint,
    next_step: AllowedNextStep,
) -> None:
    output, reasons = deterministic_classify(ClassificationInput(text=text, source=ClassificationSource.manual_test))

    assert output.task_type == task_type
    assert output.project_area == project_area
    assert output.sensitivity_hint == sensitivity
    assert output.allowed_next_step == next_step
    assert reasons
