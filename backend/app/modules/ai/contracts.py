from enum import StrEnum
from typing import Any, Protocol
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

AIProviderId = str
AIModelId = str


class AIProviderKind(StrEnum):
    fake = "fake"
    scaleway = "scaleway"
    openai_compatible = "openai_compatible"
    anthropic = "anthropic"
    local = "local"
    unknown = "unknown"


class AIProviderStatus(StrEnum):
    enabled = "enabled"
    disabled = "disabled"
    missing_credentials = "missing_credentials"
    unavailable = "unavailable"
    unknown = "unknown"


class AIProviderHealth(StrEnum):
    healthy = "healthy"
    degraded = "degraded"
    unavailable = "unavailable"
    unknown = "unknown"


class AIModelCapability(StrEnum):
    chat_text = "chat_text"
    structured_json = "structured_json"
    tool_calling = "tool_calling"
    vision_input = "vision_input"
    long_context = "long_context"
    code_reasoning = "code_reasoning"
    source_grounded_summary = "source_grounded_summary"
    low_latency = "low_latency"
    low_cost = "low_cost"
    high_reasoning = "high_reasoning"


class AITaskType(StrEnum):
    smoke_console_test = "smoke_console_test"
    smoke_test = "smoke_test"
    assumption_review = "assumption_review"
    equation_review = "equation_review"
    literature_query_planning = "literature_query_planning"
    source_extraction = "source_extraction"
    model_spec_draft = "model_spec_draft"
    simulation_result_interpretation = "simulation_result_interpretation"
    code_review = "code_review"
    runner_error_explanation = "runner_error_explanation"
    artifact_summary = "artifact_summary"
    decision_support = "decision_support"
    critic_review = "critic_review"
    synthesis = "synthesis"


class AIPrivacyClass(StrEnum):
    public = "public"
    internal = "internal"
    confidential = "confidential"
    sensitive_ip = "sensitive_ip"
    secret = "secret"
    unknown = "unknown"


class AIUsageSource(StrEnum):
    estimated = "estimated"
    actual = "actual"
    mixed = "mixed"


class AIExternalDispatchState(StrEnum):
    not_started = "not_started"
    started = "started"
    unknown = "unknown"


class AIPolicyMode(StrEnum):
    FAST_DEV = "FAST_DEV"
    STRICT_IP = "STRICT_IP"
    DISABLED = "DISABLED"


class AIProviderErrorCode(StrEnum):
    provider_unavailable = "provider_unavailable"
    provider_auth_missing = "provider_auth_missing"
    provider_auth_failed = "provider_auth_failed"
    provider_rate_limited = "provider_rate_limited"
    provider_timeout = "provider_timeout"
    provider_bad_request = "provider_bad_request"
    provider_response_invalid = "provider_response_invalid"
    provider_unknown_error = "provider_unknown_error"


class AIMessage(BaseModel):
    role: str
    content: str


class AIUsage(BaseModel):
    provider_id: AIProviderId
    model_id: AIModelId
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int | None = Field(default=None, ge=0)
    usage_source: AIUsageSource = AIUsageSource.estimated
    provider_cost_estimate: float | None = Field(default=None, ge=0)
    currency: str | None = None

    @model_validator(mode="after")
    def compute_or_validate_total_tokens(self) -> "AIUsage":
        computed_total = self.input_tokens + self.output_tokens
        if self.total_tokens is None:
            self.total_tokens = computed_total
        elif self.total_tokens != computed_total:
            raise ValueError("total_tokens must equal input_tokens + output_tokens.")
        return self


class AIProviderError(BaseModel):
    code: AIProviderErrorCode
    message: str
    retryable: bool = False
    safe_metadata: dict[str, Any] = Field(default_factory=dict)


class AIRequest(BaseModel):
    task_type: AITaskType
    privacy_class: AIPrivacyClass = AIPrivacyClass.unknown
    prompt: str | None = None
    messages: list[AIMessage] = Field(default_factory=list)
    workspace_id: str | None = None
    model_preference: AIModelId | None = None
    max_input_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=0)
    structured_output_schema: dict[str, Any] | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # PNG/JPEG data URLs for adapters whose model can see images; other adapters ignore them.
    images: list[str] = Field(default_factory=list)
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    correlation_id: str | None = None


class AIResponse(BaseModel):
    provider_id: AIProviderId
    model_id: AIModelId
    usage: AIUsage
    request_id: str
    correlation_id: str | None = None
    text: str | None = None
    content: str | None = None
    structured_output: dict[str, Any] | None = None
    finish_reason: str | None = None
    safety_status: str = "not_evaluated"
    blocked_reason: str | None = None
    external_dispatch_state: AIExternalDispatchState | None = None
    raw_provider_metadata: dict[str, Any] = Field(default_factory=dict)
    error: AIProviderError | None = None


class ProviderRegistryEntry(BaseModel):
    provider_id: AIProviderId
    kind: AIProviderKind
    display_name: str
    status: AIProviderStatus = AIProviderStatus.disabled
    health: AIProviderHealth = AIProviderHealth.unknown
    enabled: bool = False
    credential_required: bool = False
    supports_streaming: bool = False
    supports_structured_output: bool = False
    supports_vision: bool = False
    locality: str = "unknown"
    notes: str | None = None


class ModelRegistryEntry(BaseModel):
    model_id: AIModelId
    provider_id: AIProviderId
    provider_model_name: str
    display_name: str
    enabled: bool = False
    capabilities: set[AIModelCapability] = Field(default_factory=set)
    default_task_types: set[AITaskType] = Field(default_factory=set)
    context_window_tokens: int | None = Field(default=None, ge=0)
    max_output_tokens: int | None = Field(default=None, ge=0)
    latency_class: str = "unknown"
    reasoning_class: str = "unknown"
    allowed_privacy_classes: set[AIPrivacyClass] = Field(
        default_factory=lambda: {AIPrivacyClass.public, AIPrivacyClass.internal}
    )
    notes: str | None = None


class RoutingDecision(BaseModel):
    provider_id: AIProviderId | None = None
    model_id: AIModelId | None = None
    blocked: bool = False
    blocked_reason: str | None = None
    considered_models: list[AIModelId] = Field(default_factory=list)
    decision_reason: str = ""


class GateDecision(BaseModel):
    allowed: bool
    gate_name: str
    blocked_reason: str | None = None
    privacy_class: AIPrivacyClass | None = None
    estimated_usage: AIUsage | None = None
    safe_metadata: dict[str, Any] = Field(default_factory=dict)


class AuthorityDecision(BaseModel):
    allowed: bool
    requires_confirmation: bool = False
    blocked_reason: str | None = None
    risk_level: str = "low"
    policy_notes: list[str] = Field(default_factory=list)


class AIProviderAdapter(Protocol):
    provider_id: AIProviderId

    def health(self) -> AIProviderHealth:
        """Return provider health without making an expensive generation call."""

    def list_models(self) -> list[ModelRegistryEntry]:
        """Return models exposed by this adapter."""

    def complete(self, request: AIRequest) -> AIResponse:
        """Execute a provider-neutral completion request."""

    def stream(self, request: AIRequest) -> Any:
        """Future streaming hook. Implementations may raise NotImplementedError."""


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[AIProviderId, AIProviderAdapter] = {}
        self._entries: dict[AIProviderId, ProviderRegistryEntry] = {}

    def register_provider(self, adapter: AIProviderAdapter, entry: ProviderRegistryEntry) -> None:
        if adapter.provider_id != entry.provider_id:
            raise ValueError("Provider adapter id must match registry entry provider_id.")
        self._providers[entry.provider_id] = adapter
        self._entries[entry.provider_id] = entry

    def get_provider(self, provider_id: AIProviderId) -> AIProviderAdapter | None:
        return self._providers.get(provider_id)

    def get_entry(self, provider_id: AIProviderId) -> ProviderRegistryEntry | None:
        return self._entries.get(provider_id)

    def list_providers(self, *, enabled_only: bool = False) -> list[ProviderRegistryEntry]:
        entries = list(self._entries.values())
        if enabled_only:
            return [entry for entry in entries if entry.enabled]
        return entries


class ModelRegistry:
    def __init__(self) -> None:
        self._models: dict[AIModelId, ModelRegistryEntry] = {}

    def register_model(self, entry: ModelRegistryEntry) -> None:
        self._models[entry.model_id] = entry

    def get_model(self, model_id: AIModelId) -> ModelRegistryEntry | None:
        return self._models.get(model_id)

    def list_models(self, *, enabled_only: bool = False) -> list[ModelRegistryEntry]:
        models = list(self._models.values())
        if enabled_only:
            return [model for model in models if model.enabled]
        return models

    def list_enabled_models(self) -> list[ModelRegistryEntry]:
        return self.list_models(enabled_only=True)

    def find_models(
        self,
        *,
        capability: AIModelCapability | None = None,
        task_type: AITaskType | None = None,
        privacy_class: AIPrivacyClass | None = None,
        provider_id: AIProviderId | None = None,
        enabled_only: bool = True,
    ) -> list[ModelRegistryEntry]:
        models = self.list_models(enabled_only=enabled_only)
        if capability is not None:
            models = [model for model in models if capability in model.capabilities]
        if task_type is not None:
            models = [model for model in models if task_type in model.default_task_types]
        if privacy_class is not None:
            models = [model for model in models if privacy_class in model.allowed_privacy_classes]
        if provider_id is not None:
            models = [model for model in models if model.provider_id == provider_id]
        return models
