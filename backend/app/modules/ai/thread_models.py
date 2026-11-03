from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from app.modules.ai.jarvis_context_models import JarvisContextRequest
from app.modules.ai.models import ContextPackSelectionRequest
from app.modules.ai.thread_ui_context import MAX_IMAGES, UiContext, validate_images
from app.modules.workspace_actions.models import ActionOutcome, SurfaceRef

PersistenceState = Literal["reserved", "dispatching", "captured", "capture_failed"]


class AIThreadCreate(BaseModel):
    workspace_id: str = Field(min_length=1, max_length=128)
    title: str | None = Field(default=None, max_length=120)


class AIConversationRouteAvailability(BaseModel):
    configured: bool
    runtime_reachable: bool | None
    model_installed: bool | None
    model_loaded: bool | None
    qualified: Literal[True, False, "unknown"]
    reason_code: str | None = None
    message: str


class AIConversationRoute(BaseModel):
    route_class: str
    label: str
    model_id: str
    execution_class: Literal["local_compute", "synthetic", "agent"]
    availability: AIConversationRouteAvailability


class AIConversationOptions(BaseModel):
    routes: list[AIConversationRoute]
    availability: Literal["configured", "unavailable"]


class AIThreadSubmit(BaseModel):
    request_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
    prompt: str = Field(min_length=1, max_length=12000)
    task_kind: str = Field(default="general", pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    route_class: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$")
    max_tokens: int | None = Field(default=None, gt=0)
    context_selection: ContextPackSelectionRequest | None = None
    expected_context_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    jarvis_context: JarvisContextRequest | None = None
    expected_jarvis_context_digest: str | None = Field(
        default=None, pattern=r"^sha256:[0-9a-f]{64}$"
    )
    surface_context: SurfaceRef | None = None
    # Bubu: DOM-first page description and optional screenshots (see thread_ui_context).
    ui_context: UiContext | None = None
    images: list[str] | None = Field(default=None, max_length=MAX_IMAGES)

    @field_validator("images")
    @classmethod
    def validate_image_data_urls(cls, value: list[str] | None) -> list[str] | None:
        return validate_images(value)

    @model_validator(mode="after")
    def validate_context_binding(self) -> "AIThreadSubmit":
        if (self.context_selection is None) != (self.expected_context_digest is None):
            raise ValueError("context_selection and expected_context_digest must be supplied together")
        if (self.jarvis_context is None) != (self.expected_jarvis_context_digest is None):
            raise ValueError(
                "jarvis_context and expected_jarvis_context_digest must be supplied together"
            )
        return self


class AIThreadSummary(BaseModel):
    id: str
    workspace_id: str
    title: str | None = None
    created_at: str
    last_activity_at: str


class AIThreadInteractionRead(BaseModel):
    id: str
    request_id: str
    interaction_index: int
    user_text: str
    assistant_text: str | None = None
    assistant_text_truncated: bool
    flow_id: str
    persistence_state: PersistenceState
    persistence_error: str | None = None
    flow_state: str
    terminal_reason: str | None = None
    attempt_count: int
    terminal_attempt_id: str | None = None
    route_class: str | None = None
    execution_class: str | None = None
    model_id: str | None = None
    proposal_ids: list[str] = Field(default_factory=list)
    proposal_count: int = 0
    proposals_truncated: bool = False
    # Spec 159: per-turn usage from the existing ai_jobs rows of this flow.
    provider_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_estimate_usd: float | None = None
    usage_source: str | None = None
    latency_ms: int | None = None
    completed_at: str | None = None
    elapsed_ms: int | None = None
    # Concise status of a running turn, derived from recorded tool events.
    activity: str | None = None
    surface_summary: str | None = None
    surface_digest: str | None = None
    # Bubu: screenshots attached to this turn; unsupported = the selected model never saw them.
    image_count: int = 0
    images_unsupported: bool = False
    actions: list[ActionOutcome] = Field(default_factory=list)
    technical_details: str | None = None
    created_at: str
    updated_at: str


class AIThreadDetail(AIThreadSummary):
    interactions: list[AIThreadInteractionRead] = Field(default_factory=list)
    has_older: bool = False


class AIThreadList(BaseModel):
    threads: list[AIThreadSummary] = Field(default_factory=list)


class AIThreadSubmitRead(BaseModel):
    interaction: AIThreadInteractionRead
