"""145 frozen Hermes <-> Jarvis session/control/event and governed inference/tool contracts.

These are wire shapes only. Jarvis stays authoritative: every product or
auxiliary inference maps onto ``run_ai_task`` (see ``run_ai_task_kwargs``),
capability grants are issued by Jarvis policy or the operator and never inferred
from model output, and events are DTOs that the Hermes adapter projects through
existing owners (no event bus, no second transcript store).
"""

from __future__ import annotations

import json
import math
from datetime import datetime
from typing import Annotated, Any, Final, Literal

from pydantic import Field, JsonValue, StringConstraints, field_validator, model_validator

from app.modules.ai.jarvis_context_models import (
    ContentDigest,
    ContractId,
    FrozenContract,
    SourceRef,
    UtcDatetime,
)

AGENT_CONTRACTS_VERSION: Final = "agent_contracts.v1"
AgentContractsVersion = Literal["agent_contracts.v1"]

MAX_REFS = 32
MAX_JSON_PAYLOAD_CHARS = 65_536
MAX_PROMPT_CHARS = 128_000

TaskKind = Annotated[str, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")]
RouteClass = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*:[a-z][a-z0-9_]*$")]
ReasonCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]
EventKind = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){0,3}$", max_length=64)]
ScalarValue = str | int | float | bool

AgentControlKind = Literal["start", "interrupt", "resume", "close"]
ToolCallStatus = Literal["succeeded", "failed", "refused", "cancelled", "deadline_exceeded"]


class AgentControlTargetError(ValueError):
    """The command does not address the current session mapping."""


class StaleAgentGenerationError(ValueError):
    """expected_generation no longer matches the Jarvis-owned session mapping."""


def _bounded_json(value: dict[str, JsonValue]) -> dict[str, JsonValue]:
    try:
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except ValueError as exc:
        raise ValueError("payload must be finite JSON") from exc
    if len(encoded) > MAX_JSON_PAYLOAD_CHARS:
        raise ValueError(f"payload must serialize to at most {MAX_JSON_PAYLOAD_CHARS} characters")
    return value


def _bounded_scalars(value: dict[str, ScalarValue]) -> dict[str, ScalarValue]:
    if len(value) > MAX_REFS:
        raise ValueError(f"at most {MAX_REFS} constraint entries are allowed")
    for key, item in value.items():
        if not key or len(key) > 64:
            raise ValueError("constraint keys must be 1..64 characters")
        if isinstance(item, str) and len(item) > 256:
            raise ValueError("constraint string values must be at most 256 characters")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError("constraint numbers must be finite")
    return value


class _Deadline(FrozenContract):
    deadline_at: UtcDatetime
    cancellation_id: ContractId | None = None

    def is_expired(self, now: datetime) -> bool:
        return now >= self.deadline_at


# --- A. Hermes <-> Jarvis -------------------------------------------------


class AgentSessionRef(FrozenContract):
    """Jarvis-owned mapping of one AI thread to one Hermes session generation.

    ``jarvis_thread_id`` is ``ai_threads.id``. ``generation`` increases every
    time Jarvis (re)binds a Hermes session to the thread (start or recovery).
    ``upstream_revision`` is the pinned Hermes release/commit serving it.
    """

    jarvis_thread_id: ContractId
    hermes_session_id: ContractId
    profile_id: ContractId
    workspace_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=1)
    upstream_revision: str = Field(min_length=1, max_length=128)


class AgentControlCommand(_Deadline):
    """Idempotent (by ``command_id``) CAS command against the thread's session mapping.

    ``start`` addresses the thread (no Hermes session exists yet or it is being
    replaced); interrupt/resume/close address the exact current Hermes session.
    ``expected_generation`` is the mapping generation the sender observed; 0
    means "no session was ever bound".
    """

    schema_version: AgentContractsVersion = AGENT_CONTRACTS_VERSION
    command_id: ContractId
    kind: AgentControlKind
    correlation_id: ContractId
    jarvis_thread_id: ContractId
    workspace_id: str = Field(min_length=1, max_length=128)
    profile_id: ContractId
    hermes_session_id: ContractId | None = None
    expected_generation: int = Field(ge=0)
    requested_at: UtcDatetime

    @model_validator(mode="after")
    def require_target(self) -> AgentControlCommand:
        if self.kind == "start":
            if self.hermes_session_id is not None:
                raise ValueError("start addresses the thread and must not name a hermes_session_id")
        else:
            if self.hermes_session_id is None:
                raise ValueError(f"{self.kind} requires the target hermes_session_id")
            if self.expected_generation < 1:
                raise ValueError(f"{self.kind} requires expected_generation >= 1")
        if self.deadline_at <= self.requested_at:
            raise ValueError("deadline_at must be after requested_at")
        return self


def check_control_target(command: AgentControlCommand, current: AgentSessionRef | None, *, now: datetime) -> None:
    """Deterministic refusal of stale, expired, or mis-addressed control commands."""
    if command.is_expired(now):
        raise AgentControlTargetError("control command deadline has passed")
    current_generation = current.generation if current is not None else 0
    if current is not None and (
        current.jarvis_thread_id != command.jarvis_thread_id or current.workspace_id != command.workspace_id
    ):
        raise AgentControlTargetError("control command targets a different thread mapping")
    if command.kind != "start":
        if current is None:
            raise AgentControlTargetError(f"{command.kind} requires a bound session")
        if current.hermes_session_id != command.hermes_session_id:
            raise AgentControlTargetError(f"{command.kind} targets a superseded hermes session")
        if current.profile_id != command.profile_id:
            raise AgentControlTargetError(f"{command.kind} names a different profile than the bound session")
    if command.expected_generation != current_generation:
        raise StaleAgentGenerationError(
            f"expected generation {command.expected_generation}, current is {current_generation}"
        )


class AgentEvent(FrozenContract):
    """Bounded session event; content lives behind ``payload_ref`` in a Jarvis owner.

    ``sequence`` is strictly increasing per (hermes_session_id, generation) so
    replay after worker loss is ordered and ``event_id`` deduplicates.
    """

    schema_version: AgentContractsVersion = AGENT_CONTRACTS_VERSION
    event_id: ContractId
    session_ref: AgentSessionRef
    sequence: int = Field(ge=0)
    kind: EventKind
    occurred_at: UtcDatetime
    correlation_id: ContractId | None = None
    payload_ref: SourceRef | None = None
    payload_digest: ContentDigest | None = None
    evidence_refs: tuple[SourceRef, ...] = Field(default=(), max_length=MAX_REFS)

    @model_validator(mode="after")
    def payload_ref_requires_digest(self) -> AgentEvent:
        if (self.payload_ref is None) != (self.payload_digest is None):
            raise ValueError("payload_ref and payload_digest must be provided together")
        return self


class CapabilityScope(FrozenContract):
    workspace_id: str = Field(min_length=1, max_length=128)
    jarvis_thread_id: ContractId | None = None
    object_refs: tuple[SourceRef, ...] = Field(default=(), max_length=MAX_REFS)


class CapabilityGrantRef(FrozenContract):
    """Reference to a server-issued grant for one registered capability.

    Issued only by Jarvis policy or the operator; a model/agent cannot mint or
    widen it. The broker re-checks the live grant, so this ref is never
    sufficient authority by itself.
    """

    grant_id: ContractId
    capability_id: ContractId
    issuer: Literal["jarvis_policy", "operator"]
    scope: CapabilityScope
    constraints: dict[str, ScalarValue] = Field(default_factory=dict)
    issued_at: UtcDatetime
    expires_at: UtcDatetime
    revoked_at: UtcDatetime | None = None
    revocation_reason: ReasonCode | None = None

    @field_validator("constraints")
    @classmethod
    def check_constraints(cls, value: dict[str, ScalarValue]) -> dict[str, ScalarValue]:
        return _bounded_scalars(value)

    @model_validator(mode="after")
    def validate_lifetime(self) -> CapabilityGrantRef:
        if self.expires_at <= self.issued_at:
            raise ValueError("grant expires_at must be after issued_at")
        if (self.revoked_at is None) != (self.revocation_reason is None):
            raise ValueError("revoked_at and revocation_reason must be provided together")
        return self

    def is_active(self, now: datetime) -> bool:
        return self.revoked_at is None and self.issued_at <= now < self.expires_at


# --- B. Governed inference / tool exchange --------------------------------


class InferenceEnvelope(_Deadline):
    """A request for Jarvis to run inference; never a provider call by itself.

    ``route_class`` and ``model_candidate`` are requests/recommendations: the
    binding, sensitivity, egress and budget authority inside ``run_ai_task``
    decide. ``agent_session`` marks Hermes-originated (including auxiliary)
    inference, which is governed identically.
    """

    schema_version: AgentContractsVersion = AGENT_CONTRACTS_VERSION
    envelope_id: ContractId
    correlation_id: ContractId
    task_kind: TaskKind = "general"
    workspace_id: str | None = Field(default=None, min_length=1, max_length=128)
    prompt: str = Field(min_length=1, max_length=MAX_PROMPT_CHARS)
    route_class: RouteClass | None = None
    model_candidate: str | None = Field(default=None, min_length=1, max_length=256)
    max_output_tokens: int | None = Field(default=None, ge=1)
    flow_id: ContractId | None = None
    context_bundle_id: ContractId | None = None
    context_bundle_digest: ContentDigest | None = None
    sensitivity_refs: tuple[SourceRef, ...] = Field(default=(), max_length=MAX_REFS)
    policy_refs: tuple[SourceRef, ...] = Field(default=(), max_length=MAX_REFS)
    agent_session: AgentSessionRef | None = None
    provenance_refs: tuple[SourceRef, ...] = Field(default=(), max_length=MAX_REFS)
    requested_at: UtcDatetime

    @model_validator(mode="after")
    def validate_envelope(self) -> InferenceEnvelope:
        if (self.context_bundle_id is None) != (self.context_bundle_digest is None):
            raise ValueError("context_bundle_id and context_bundle_digest must be provided together")
        if self.deadline_at <= self.requested_at:
            raise ValueError("deadline_at must be after requested_at")
        return self


def run_ai_task_kwargs(
    envelope: InferenceEnvelope,
    *,
    context_blocks: list[dict[str, object]] | None = None,
    structured_output_schema: dict[str, Any] | None = None,
) -> dict[str, object]:
    """The only mapping of an envelope into Jarvis execution: ``run_ai_task(**kwargs)``.

    ``context_blocks`` are the blocks the caller materialized from the envelope's
    context bundle after authoritative reread; run_ai_task re-digests them.
    run_ai_task has no deadline, cancellation or model-override parameter today.
    The H/L execution adapters own enforcing ``deadline_at``/``cancellation_id``
    during execution and binding a revalidated ``model_candidate``; a local
    candidate is admitted only under a ``ResourceLease`` whose request has
    ``owner_kind="inference_envelope"`` and ``owner_id=envelope_id``.
    """
    return {
        "user_prompt": envelope.prompt,
        "task_kind": envelope.task_kind,
        "route_class": envelope.route_class,
        "context_blocks": context_blocks,
        "max_output_tokens": envelope.max_output_tokens,
        "workspace_id": envelope.workspace_id,
        "existing_flow_id": envelope.flow_id,
        "structured_output_schema": structured_output_schema,
    }


class StructuredToolCall(_Deadline):
    schema_version: AgentContractsVersion = AGENT_CONTRACTS_VERSION
    call_id: ContractId
    capability_id: ContractId
    grant_id: ContractId
    correlation_id: ContractId
    session_ref: AgentSessionRef | None = None
    arguments: dict[str, JsonValue] = Field(default_factory=dict)
    argument_artifact: SourceRef | None = None
    requested_at: UtcDatetime
    provenance_refs: tuple[SourceRef, ...] = Field(default=(), max_length=MAX_REFS)

    @field_validator("arguments")
    @classmethod
    def check_arguments(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        return _bounded_json(value)

    @model_validator(mode="after")
    def validate_call(self) -> StructuredToolCall:
        if self.arguments and self.argument_artifact is not None:
            raise ValueError("provide inline arguments or argument_artifact, not both")
        if self.deadline_at <= self.requested_at:
            raise ValueError("deadline_at must be after requested_at")
        return self


class StructuredToolResult(FrozenContract):
    schema_version: AgentContractsVersion = AGENT_CONTRACTS_VERSION
    call_id: ContractId
    capability_id: ContractId
    status: ToolCallStatus
    result: dict[str, JsonValue] | None = None
    result_artifact: SourceRef | None = None
    error_code: ReasonCode | None = None
    completed_at: UtcDatetime
    evidence_refs: tuple[SourceRef, ...] = Field(default=(), max_length=MAX_REFS)

    @field_validator("result")
    @classmethod
    def check_result(cls, value: dict[str, JsonValue] | None) -> dict[str, JsonValue] | None:
        return None if value is None else _bounded_json(value)

    @model_validator(mode="after")
    def validate_status(self) -> StructuredToolResult:
        if self.status == "succeeded":
            if self.error_code is not None:
                raise ValueError("succeeded results must not carry error_code")
        else:
            if self.error_code is None:
                raise ValueError(f"{self.status} results require error_code")
            if self.result is not None or self.result_artifact is not None:
                raise ValueError(f"{self.status} results must not carry result content")
        return self
