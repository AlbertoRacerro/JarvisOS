import json
from typing import Any

from pydantic import ValidationError

from app.modules.local_ai.classification.contracts import (
    AllowedNextStep,
    ClassificationFailureCode,
    ClassificationOutput,
    SensitivityHint,
    TaskType,
)

MAX_CLASSIFICATION_RESPONSE_CHARS = 2000
AUTHORITY_CLAIM_MARKERS = (
    "i will execute",
    "i will call",
    "i will route",
    "i will retrieve",
    "i will write memory",
    "execute tool",
    "call external",
    "provider routing",
    "authorized",
)


class ClassificationParseError(ValueError):
    def __init__(self, code: ClassificationFailureCode, message: str) -> None:
        super().__init__(message)
        self.code = code


def parse_classification_output(response_text: str) -> ClassificationOutput:
    text = response_text.strip()
    if not text:
        raise ClassificationParseError(ClassificationFailureCode.empty_content, "model output was empty")
    if len(text) > MAX_CLASSIFICATION_RESPONSE_CHARS:
        raise ClassificationParseError(ClassificationFailureCode.over_budget_prompt, "model output exceeded response budget")
    lowered = text.lower()
    if any(marker in lowered for marker in AUTHORITY_CLAIM_MARKERS):
        raise ClassificationParseError(ClassificationFailureCode.model_claimed_authority, "model output claimed authority")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClassificationParseError(ClassificationFailureCode.invalid_json, exc.msg) from exc
    if not isinstance(parsed, dict):
        raise ClassificationParseError(ClassificationFailureCode.non_object_json, "model output must be a JSON object")
    try:
        output = ClassificationOutput.model_validate(parsed)
    except ValidationError as exc:
        code = ClassificationFailureCode.extra_fields if _has_extra_field_error(exc) else ClassificationFailureCode.schema_invalid
        raise ClassificationParseError(code, exc.errors()[0]["msg"]) from exc
    validate_classification_combination(output)
    return output


def validate_classification_combination(output: ClassificationOutput) -> None:
    if output.task_type == TaskType.unsafe_tool_request and output.allowed_next_step not in {
        AllowedNextStep.human_review,
        AllowedNextStep.deterministic_review,
        AllowedNextStep.no_action,
    }:
        raise ClassificationParseError(
            ClassificationFailureCode.impossible_combination,
            "unsafe tool requests cannot be marked directly actionable",
        )
    if output.task_type == TaskType.ambiguous and output.allowed_next_step == AllowedNextStep.answer_locally:
        raise ClassificationParseError(
            ClassificationFailureCode.impossible_combination,
            "ambiguous input cannot be marked answer_locally",
        )
    if output.sensitivity_hint in {SensitivityHint.secret, SensitivityHint.sensitive_ip} and output.allowed_next_step in {
        AllowedNextStep.answer_locally,
        AllowedNextStep.request_bounded_context,
    }:
        raise ClassificationParseError(
            ClassificationFailureCode.impossible_combination,
            "secret or sensitive_ip output requires review",
        )


def extract_response_text(response_json: dict[str, Any]) -> str:
    message = response_json.get("message")
    if isinstance(message, dict) and isinstance(message.get("content"), str):
        return message["content"]
    choices = response_json.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            choice_message = first.get("message")
            if isinstance(choice_message, dict) and isinstance(choice_message.get("content"), str):
                return choice_message["content"]
    raise ValueError("Missing model response content")


def response_indicates_thinking_budget_exhausted(response_json: dict[str, Any]) -> bool:
    message = response_json.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    thinking = message.get("thinking")
    return content == "" and bool(thinking) and response_json.get("done_reason") == "length"


def _has_extra_field_error(exc: ValidationError) -> bool:
    return any(error.get("type") == "extra_forbidden" for error in exc.errors())
