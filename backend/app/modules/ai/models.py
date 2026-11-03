import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.packs import default_workspace_slug
from app.modules.ai.context_builder import DEFAULT_CONTEXT_BUDGET_CHARS, MAX_CONTEXT_BLOCKS
from app.modules.ai.contracts import AIPolicyMode, AITaskType, AIUsage


class AISettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy_mode: AIPolicyMode | None = None
    monthly_api_budget_usd: float | None = Field(default=None, ge=0)
    paid_ai_enabled: bool | None = None
    default_ai_provider: str | None = None
    default_ai_model: str | None = None
    provider_mode: str | None = None
    use_fake_provider_when_budget_zero: bool | None = None
    scaleway_enabled: bool | None = None
    scaleway_smoke_test_enabled: bool | None = None
    scaleway_live_smoke_test_enabled: bool | None = None
    # Recognized legacy write-only aliases. The settings service intentionally ignores them.
    scaleway_token_cap: int | None = Field(default=None, ge=0)
    scaleway_tokens_month_to_date: int | None = Field(default=None, ge=0)
    scaleway_monthly_token_cap: int | None = Field(default=None, ge=0)
    scaleway_hard_stop_token_cap: int | None = Field(default=None, ge=0)
    scaleway_input_tokens_month_to_date: int | None = Field(default=None, ge=0)
    scaleway_output_tokens_month_to_date: int | None = Field(default=None, ge=0)
    smoke_test_mode_enabled: bool | None = None
    max_direct_continuations: int | None = Field(default=None, ge=0, le=16, strict=True)


class AISettingsRead(BaseModel):
    policy_mode: AIPolicyMode
    monthly_api_budget_usd: float
    api_spend_month_to_date_usd: float
    paid_ai_enabled: bool
    default_ai_provider: str
    default_ai_model: str
    provider_mode: str
    use_fake_provider_when_budget_zero: bool
    scaleway_enabled: bool
    scaleway_smoke_test_enabled: bool
    scaleway_live_smoke_test_enabled: bool
    scaleway_monthly_token_cap: int
    scaleway_hard_stop_token_cap: int
    scaleway_free_tier_reference_tokens: int
    scaleway_input_tokens_month_to_date: int
    scaleway_output_tokens_month_to_date: int
    usage_total_tokens: int
    smoke_test_mode_enabled: bool
    max_direct_continuations: int
    max_direct_continuations_min: int = 0
    max_direct_continuations_max: int = 16
    direct_continuation_policy_version: str
    updated_at: str


class AIStatusRead(BaseModel):
    policy_mode: AIPolicyMode
    ai_enabled: bool
    active_provider_mode: str
    provider_mode: str
    provider_id: str
    adapter_enabled: bool
    fake_provider_enabled: bool
    scaleway_enabled: bool
    scaleway_api_key_configured: bool
    scaleway_provider_implementation: str
    scaleway_smoke_test_enabled: bool
    scaleway_live_smoke_test_enabled: bool
    paid_ai_enabled: bool
    monthly_api_budget_usd: float
    spend_month_to_date_usd: float
    scaleway_monthly_token_cap: int
    scaleway_hard_stop_token_cap: int
    scaleway_free_tier_reference_tokens: int
    scaleway_input_tokens_month_to_date: int
    scaleway_output_tokens_month_to_date: int
    usage_total_tokens: int
    budget_status: str
    credential_status: str
    external_calls_allowed: bool
    blocking_reason: str | None
    default_ai_provider: str
    default_ai_model: str


class ProviderCredentialStatus(BaseModel):
    key_present: bool
    effective_source: Literal[
        "environment",
        "secure_persisted",
        "absent",
        "invalid",
        "unknown",
        "not_required",
    ]
    persisted_state: Literal[
        "absent", "usable", "corrupted", "unavailable", "not_supported"
    ]
    reason_code: str | None = None


class ProviderCredentialCapabilities(BaseModel):
    replace_persisted: bool = False
    delete_persisted: bool = False


class ProviderSettingsProvider(BaseModel):
    provider_id: str
    kind: str
    enabled: bool
    requires_network: bool
    execution_class: str
    monthly_token_cap: int
    monthly_cost_cap_usd: float
    external_calls_allowed: bool
    blocking_reason: str | None = None
    credential: ProviderCredentialStatus
    credential_capabilities: ProviderCredentialCapabilities


class ProviderSettingsRead(BaseModel):
    providers: list[ProviderSettingsProvider]
    default_provider_id: str
    policy_mode: AIPolicyMode
    external_calls_allowed: bool
    blocking_reason: str | None = None
    monthly_api_budget_usd: float
    spend_month_to_date_usd: float


class ModelingDraftRequest(BaseModel):
    workspace_id: str
    informal_model_idea: str = Field(min_length=1)
    model_context: str | None = None
    quality_level: str = "draft"
    provider_mode: str | None = None


class ModelingDraft(BaseModel):
    engineering_question: str
    model_title_suggestion: str
    model_scope: str
    proposed_assumptions: list[str]
    proposed_parameters: list[str]
    expected_inputs: list[str]
    expected_outputs: list[str]
    missing_information: list[str]
    model_weaknesses: list[str]
    suggested_next_step: str


class AIMetadata(BaseModel):
    provider: str
    model: str
    provider_mode: str
    task_type: str
    quality_level: str
    paid_api_call_attempted: bool
    blocked_by_budget: bool
    blocked_reason: str | None = None
    estimated_cost_usd: float | None = None
    monthly_budget_usd: float
    spend_month_to_date_usd: float
    success: bool


class ModelingDraftResponse(BaseModel):
    draft: ModelingDraft | None
    ai_metadata: AIMetadata


class AITaskRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(min_length=1)
    route_class: str | None = None
    task_kind: str = Field(
        default="general", pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$"
    )
    max_tokens: int | None = Field(default=None, ge=1)
    context_blocks: list[dict[str, Any]] | None = None
    include_project_context: bool = False
    workspace_id: str | None = None

    @model_validator(mode="after")
    def validate_context_blocks_bounds(self) -> "AITaskRunRequest":
        if self.context_blocks is None:
            return self
        if len(self.context_blocks) > MAX_CONTEXT_BLOCKS:
            raise ValueError(f"context_blocks must contain at most {MAX_CONTEXT_BLOCKS} items.")
        serialized = json.dumps(self.context_blocks, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        if len(serialized) > DEFAULT_CONTEXT_BUDGET_CHARS:
            raise ValueError(
                f"context_blocks serialized size must be at most {DEFAULT_CONTEXT_BUDGET_CHARS} characters."
            )
        return self


class ContextPackSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kinds: list[str] = Field(default_factory=list)
    statuses: dict[str, list[str]] | list[str] | None = None
    ids: list[str] | None = None
    query: str | None = None
    max_items_per_kind: int = Field(default=10, ge=1, le=100)


class ContextPackPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str = Field(default_factory=default_workspace_slug)
    budget_chars: int = Field(default=DEFAULT_CONTEXT_BUDGET_CHARS, ge=1)
    selection: ContextPackSelectionRequest = Field(default_factory=ContextPackSelectionRequest)


class ContextPackPreviewResponse(BaseModel):
    blocks: list[dict[str, Any]]
    context_digest: str | None
    context_sources_manifest: list[dict[str, Any]]
    char_count: int
    estimated_token_count: int
    included_count: int
    dropped_count: int
    budget_chars: int


class EscalationConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ticket_id: str = Field(min_length=1)


class AITaskRunResponse(BaseModel):
    status: str
    ledger_id: str
    selected_route_class: str | None
    decision_reason: str
    blocked_reason: str | None = None
    response_text: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    usage: AIUsage | None = None
    error_type: str | None = None
    include_project_context: bool = False
    workspace_id: str | None = None
    context_digest: str | None = None
    context_sources_count: int = 0
    records_parse_error: str | None = None
    proposed_record_ids: list[str] = Field(default_factory=list)
    auto_metadata: dict[str, Any] | None = None
    confirmation_payload: dict[str, Any] | None = None
    escalation_proposal: dict[str, Any] | None = None
    egress_decision_id: str | None = None
    egress_packet_digest: str | None = None
    egress_ticket_id: str | None = None
    egress_reservation_id: str | None = None
    egress_reason_code: str | None = None
    egress_trigger_ids: list[str] = Field(default_factory=list)


class EscalationConfirmResponse(BaseModel):
    status: str
    ticket_id: str
    execution_ledger_id: str
    reason_code: str | None = None
    task_response: AITaskRunResponse


class SmokeTestRequest(BaseModel):
    provider_mode: str | None = None
    smoke_mode: str = "synthetic"


class SmokeTestTokenMetadata(BaseModel):
    blocked_by_token_cap: bool
    estimated_input_tokens: int
    estimated_output_tokens: int
    reported_input_tokens: int | None = None
    reported_output_tokens: int | None = None
    monthly_token_cap: int
    hard_stop_token_cap: int
    token_usage_month_to_date: int
    usage_source: str = "estimated"


class SmokeTestResult(BaseModel):
    case_id: str
    input_excerpt: str
    expected_class: str
    local_privacy_class: str
    provider_reported_class: str | None = None
    fake_classification: str | None = None
    passed: bool
    provider_mode: str
    provider: str
    smoke_mode: str = "synthetic"
    external_call_attempted: bool = False
    external_call_succeeded: bool = False
    blocking_reason: str | None = None
    response_text: str | None = None
    usage_source: str = "estimated"
    provider_metadata: dict[str, object] | None = None
    token_metadata: SmokeTestTokenMetadata


class SmokeTestResponse(BaseModel):
    provider_mode: str
    smoke_mode: str
    external_call_attempted: bool
    external_call_succeeded: bool
    results: list[SmokeTestResult]


class SmokeConsoleRequest(BaseModel):
    workspace_id: str | None = None
    prompt: str = ""
    max_output_tokens: int | None = Field(default=None, ge=1)


class SmokeConsoleResponse(BaseModel):
    response_text: str | None = None
    provider: str
    model: str
    mode: str = "live_smoke_console"
    privacy_class: str
    blocked_reason: str | None = None
    external_call_attempted: bool
    external_call_succeeded: bool
    estimated_input_tokens: int
    estimated_output_tokens: int
    actual_input_tokens: int | None = None
    actual_output_tokens: int | None = None
    usage_source: str
    current_month_input_tokens: int
    current_month_output_tokens: int
    current_month_total_tokens: int
    configured_monthly_token_cap: int
    token_threshold: int = 500000
    token_threshold_percent: float
    remaining_tokens_to_threshold: int


class ProviderSmokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: str | None = None
    prompt: str = ""
    max_output_tokens: int | None = Field(default=None, ge=1)


class ProviderSmokeResponse(BaseModel):
    response_text: str | None = None
    provider: str
    model: str
    mode: str = "strong_provider_smoke"
    privacy_class: str
    blocked_reason: str | None = None
    external_call_attempted: bool
    external_call_succeeded: bool
    estimated_input_tokens: int
    estimated_output_tokens: int
    actual_input_tokens: int | None = None
    actual_output_tokens: int | None = None
    usage_source: str
    provider_metadata: dict[str, object] | None = None


class SupervisorPublicTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = ""
    task_type: AITaskType | None = None
    workspace_id: str | None = None
    max_output_tokens: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SupervisorPublicTestResponse(BaseModel):
    answer: str | None = None
    task_type: AITaskType
    policy_mode: AIPolicyMode
    provider_id: str | None
    model_id: str | None
    usage: AIUsage | None = None
    safety_status: str
    blocked_reason: str | None = None
    event_id: str | None = None
    request_id: str
    correlation_id: str | None = None
    external_call_attempted: bool
    external_call_succeeded: bool
    limitations: list[str] = Field(default_factory=list)
