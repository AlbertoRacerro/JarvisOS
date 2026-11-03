from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

CLASSIFICATION_INPUT_SCHEMA_VERSION = "local_ai_classification_input_v1"
CLASSIFICATION_OUTPUT_SCHEMA_VERSION = "local_ai_classification_output_v1"
DEFAULT_CLASSIFICATION_MODEL_NAME = "gemma4:12b-it-qat"
DEFAULT_CLASSIFICATION_ENDPOINT_URL = "http://localhost:11434/api/chat"
DEFAULT_CLASSIFICATION_MAX_OUTPUT_TOKENS = 256
DEFAULT_CLASSIFICATION_TIMEOUT_SECONDS = 15.0
DEFAULT_CLASSIFICATION_TEMPERATURE = 0.0
CLASSIFICATION_DIAGNOSTIC_NUM_PREDICT_CANDIDATES = (128, 256, 384, 512)
LOW_CONFIDENCE_THRESHOLD = 0.65
CLASSIFICATION_ADVISORY_HINT_FIELDS = (
    "task_hint",
    "project_hint",
    "topic_hint",
    "context_need_hint",
    "confidence",
)
MODEL_NON_AUTHORITY_BOUNDARIES = (
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
)


class ClassificationSource(StrEnum):
    user_prompt = "user_prompt"
    codex_task = "codex_task"
    system_note = "system_note"
    manual_test = "manual_test"
    unknown = "unknown"


class TaskType(StrEnum):
    code_change = "code_change"
    documentation = "documentation"
    bug_report = "bug_report"
    project_planning = "project_planning"
    engineering_question = "engineering_question"
    personal_question = "personal_question"
    external_api_request = "external_api_request"
    local_note = "local_note"
    ambiguous = "ambiguous"
    overbroad_orchestration_request = "overbroad_orchestration_request"
    unsafe_tool_request = "unsafe_tool_request"
    unknown = "unknown"


class ProjectArea(StrEnum):
    jarvisos = "jarvisos"
    project = "project"
    local_ai = "local_ai"
    python_runner = "python_runner"
    documentation = "documentation"
    general_engineering = "general_engineering"
    personal = "personal"
    unknown = "unknown"


class ComplexityHint(StrEnum):
    trivial = "trivial"
    low = "low"
    medium = "medium"
    high = "high"
    unknown = "unknown"


class SensitivityHint(StrEnum):
    public = "public"
    internal = "internal"
    confidential = "confidential"
    sensitive_ip = "sensitive_ip"
    secret = "secret"
    unknown = "unknown"


class AllowedNextStep(StrEnum):
    answer_locally = "answer_locally"
    ask_clarification = "ask_clarification"
    request_bounded_context = "request_bounded_context"
    deterministic_review = "deterministic_review"
    human_review = "human_review"
    no_action = "no_action"


class ClassificationFailureCode(StrEnum):
    invalid_json = "invalid_json"
    non_object_json = "non_object_json"
    empty_content = "empty_content"
    extra_fields = "extra_fields"
    schema_invalid = "schema_invalid"
    model_claimed_authority = "model_claimed_authority"
    impossible_combination = "impossible_combination"
    low_confidence = "low_confidence"
    invalid_endpoint = "invalid_endpoint"
    over_budget_prompt = "over_budget_prompt"
    timeout = "timeout"
    http_error = "http_error"
    thinking_budget_exhausted = "thinking_budget_exhausted"
    done_reason_length = "done_reason_length"
    deterministic_override = "deterministic_override"
    unknown = "unknown"


class ClassificationResultSource(StrEnum):
    deterministic = "deterministic"
    model = "model"
    fallback = "fallback"
    model_with_deterministic_override = "model_with_deterministic_override"


class ClassificationBudgetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_name: str = DEFAULT_CLASSIFICATION_MODEL_NAME
    endpoint_url: str = DEFAULT_CLASSIFICATION_ENDPOINT_URL
    max_input_chars: int = Field(default=1200, ge=1, le=1200)
    max_prompt_chars: int = Field(default=2000, ge=1, le=2000)
    max_output_tokens: int = Field(default=DEFAULT_CLASSIFICATION_MAX_OUTPUT_TOKENS, ge=1, le=512)
    diagnostic_num_predict_candidates: tuple[int, ...] = CLASSIFICATION_DIAGNOSTIC_NUM_PREDICT_CANDIDATES
    temperature: float = Field(default=DEFAULT_CLASSIFICATION_TEMPERATURE, ge=0, le=0)
    timeout_seconds: float = Field(default=DEFAULT_CLASSIFICATION_TIMEOUT_SECONDS, ge=0.1, le=60)


class ClassificationAttemptDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_name: str
    endpoint: str
    prompt_chars: int = Field(ge=0)
    input_chars: int = Field(ge=0)
    max_output_tokens: int = Field(ge=1, le=512)
    temperature: float = Field(ge=0, le=0)
    timeout_seconds: float = Field(ge=0.1, le=300)
    latency_ms: int | None = Field(default=None, ge=0)
    raw_content_empty: bool
    thinking_present: bool | None = None
    done_reason: str | None = None
    schema_valid: bool
    fallback_used: bool
    fallback_reason: ClassificationFailureCode | None = None


class ClassificationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = CLASSIFICATION_INPUT_SCHEMA_VERSION
    text: str = Field(min_length=1, max_length=1200)
    source: ClassificationSource = ClassificationSource.unknown
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def schema_version_must_match(cls, value: str) -> str:
        if value != CLASSIFICATION_INPUT_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {CLASSIFICATION_INPUT_SCHEMA_VERSION}")
        return value

    @field_validator("metadata")
    @classmethod
    def metadata_must_be_small_string_map(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 10:
            raise ValueError("metadata must contain at most 10 keys")
        for key, item in value.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("metadata keys must be non-empty strings")
            if len(key) > 80:
                raise ValueError("metadata keys must be at most 80 characters")
            if not isinstance(item, str):
                raise ValueError("metadata values must be strings")
            if len(item) > 200:
                raise ValueError("metadata values must be at most 200 characters")
        return value


class ClassificationOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str = CLASSIFICATION_OUTPUT_SCHEMA_VERSION
    task_type: TaskType = Field(description="Advisory semantic task hint only; not permission or routing authority.")
    project_area: ProjectArea = Field(description="Advisory project hint only; not route, retrieval, or provider authority.")
    complexity_hint: ComplexityHint = Field(description="Advisory topic/complexity hint only.")
    needs_context: bool = Field(description="Advisory context-need hint only; does not authorize retrieval.")
    sensitivity_hint: SensitivityHint = Field(
        description="Diagnostic/model hint only; final sensitivity is owned by JarvisOS policy and hard overrides."
    )
    allowed_next_step: AllowedNextStep = Field(
        description="Diagnostic/model hint only; never permission to act, route, retrieve, write memory, call providers, or run tools."
    )
    confidence: float = Field(ge=0, le=1)
    refusal_or_uncertainty_reason: str | None = Field(default=None, max_length=240)

    @field_validator("schema_version")
    @classmethod
    def schema_version_must_match(cls, value: str) -> str:
        if value != CLASSIFICATION_OUTPUT_SCHEMA_VERSION:
            raise ValueError(f"schema_version must be {CLASSIFICATION_OUTPUT_SCHEMA_VERSION}")
        return value


class ClassificationAdvisoryHints(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_hint: TaskType
    project_hint: ProjectArea
    topic_hint: ComplexityHint
    context_need_hint: bool
    confidence: float = Field(ge=0, le=1)


class ClassificationServiceResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: ClassificationOutput
    advisory_hints: ClassificationAdvisoryHints | None = None
    source: ClassificationResultSource
    model_output_accepted: bool = False
    fallback_reasons: list[ClassificationFailureCode] = Field(default_factory=list)
    deterministic_reasons: list[str] = Field(default_factory=list)
    diagnostics: ClassificationAttemptDiagnostics | None = None


def make_advisory_hints(output: ClassificationOutput) -> ClassificationAdvisoryHints:
    return ClassificationAdvisoryHints(
        task_hint=output.task_type,
        project_hint=output.project_area,
        topic_hint=output.complexity_hint,
        context_need_hint=output.needs_context,
        confidence=output.confidence,
    )


def make_output(
    *,
    task_type: TaskType,
    project_area: ProjectArea,
    complexity_hint: ComplexityHint,
    needs_context: bool,
    sensitivity_hint: SensitivityHint,
    allowed_next_step: AllowedNextStep,
    confidence: float,
    refusal_or_uncertainty_reason: str | None = None,
) -> ClassificationOutput:
    return ClassificationOutput(
        schema_version=CLASSIFICATION_OUTPUT_SCHEMA_VERSION,
        task_type=task_type,
        project_area=project_area,
        complexity_hint=complexity_hint,
        needs_context=needs_context,
        sensitivity_hint=sensitivity_hint,
        allowed_next_step=allowed_next_step,
        confidence=confidence,
        refusal_or_uncertainty_reason=refusal_or_uncertainty_reason,
    )
