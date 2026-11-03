from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.modules.ai.contracts import AIRequest, AITaskType
from app.modules.ai.execution import resolve_binding
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.token_flow_continuation import ContinuationDecision
from app.modules.ai.token_flow_segments import (
    list_protected_segment_metadata,
    read_protected_segment,
)
from app.modules.ai.token_flow_service import TokenFlowConflictError
from app.modules.ai.token_guard import estimate_tokens

LocalPlanReason = Literal[
    "ready",
    "route_unavailable",
    "external_route_requires_059b",
    "binding_metadata_incomplete",
    "context_capacity_exceeded",
]


@dataclass(frozen=True, slots=True)
class LocalContinuationPlan:
    ready: bool
    reason: LocalPlanReason
    decision: ContinuationDecision
    binding: ProviderBinding | None
    request: AIRequest | None
    estimated_input_tokens: int | None
    effective_output_tokens: int | None
    context_window_tokens: int | None


def plan_local_continuation(
    *,
    decision: ContinuationDecision,
    route_class: str,
    task_type: AITaskType,
    original_prompt: str,
    workspace_id: str | None,
    expected_sensitivity_level: str,
    requested_output_tokens: int,
    bindings: dict[str, ProviderBinding] | None = None,
) -> LocalContinuationPlan:
    """Build but never execute one freshly validated local continuation request."""

    _require_eligible(decision)
    if not isinstance(task_type, AITaskType):
        raise TypeError("task_type must be AITaskType")
    if not isinstance(original_prompt, str) or not original_prompt.strip():
        raise ValueError("original_prompt must be non-empty text")
    if isinstance(requested_output_tokens, bool) or not isinstance(
        requested_output_tokens, int
    ) or requested_output_tokens <= 0:
        raise ValueError("requested_output_tokens must be a positive integer")

    binding, _ = resolve_binding(route_class, bindings)
    if binding is None:
        return _blocked(decision, "route_unavailable")
    if binding.requires_network or binding.execution_class == "external_provider":
        return _blocked(
            decision,
            "external_route_requires_059b",
            binding=binding,
        )
    if (
        binding.execution_class not in {"synthetic", "local_compute"}
        or binding.context_window_tokens is None
        or binding.context_window_tokens <= 0
        or binding.max_output_tokens <= 0
    ):
        return _blocked(
            decision,
            "binding_metadata_incomplete",
            binding=binding,
        )

    metadata = list_protected_segment_metadata(
        flow_id=decision.flow_id,
        workspace_id=workspace_id,
    )
    if not metadata:
        raise TokenFlowConflictError(
            "eligible continuation flow has no protected segments"
        )
    if metadata[-1].segment_index != decision.protected_segment_index:
        raise TokenFlowConflictError(
            "eligible continuation decision no longer references the latest segment"
        )
    if metadata[-1].originating_attempt_id != decision.parent_attempt_id:
        raise TokenFlowConflictError(
            "eligible continuation decision no longer references the latest parent"
        )

    validated_bodies = [
        read_protected_segment(
            flow_id=decision.flow_id,
            segment_index=item.segment_index,
            workspace_id=workspace_id,
            expected_sensitivity_level=expected_sensitivity_level,
        ).body_text
        for item in metadata
    ]
    accumulated_output = "".join(validated_bodies)
    prompt = _continuation_prompt(
        original_prompt=original_prompt.strip(),
        accumulated_output=accumulated_output,
    )
    effective_output = min(requested_output_tokens, binding.max_output_tokens)
    estimated_input = estimate_tokens(prompt)
    if estimated_input + effective_output > binding.context_window_tokens:
        return _blocked(
            decision,
            "context_capacity_exceeded",
            binding=binding,
            estimated_input=estimated_input,
            effective_output=effective_output,
        )

    request = AIRequest(
        task_type=task_type,
        prompt=prompt,
        workspace_id=workspace_id,
        model_preference=binding.model_id,
        max_input_tokens=estimated_input,
        max_output_tokens=effective_output,
        correlation_id=decision.flow_id,
        metadata={
            "continuation_flow_id": decision.flow_id,
            "continuation_parent_attempt_id": decision.parent_attempt_id,
            "continuation_index": decision.next_continuation_index,
            "continuation_segment_count": len(metadata),
            "selected_route_class": binding.route_class,
        },
    )
    return LocalContinuationPlan(
        ready=True,
        reason="ready",
        decision=decision,
        binding=binding,
        request=request,
        estimated_input_tokens=estimated_input,
        effective_output_tokens=effective_output,
        context_window_tokens=binding.context_window_tokens,
    )


def _continuation_prompt(*, original_prompt: str, accumulated_output: str) -> str:
    return (
        "ORIGINAL_REQUEST:\n"
        f"{original_prompt}\n\n"
        "VALIDATED_PARTIAL_OUTPUT:\n"
        f"{accumulated_output}\n\n"
        "CONTINUATION_INSTRUCTION:\n"
        "Continue exactly where the validated partial output stopped. "
        "Do not restart, summarize, or repeat completed text."
    )


def _require_eligible(decision: ContinuationDecision) -> None:
    if not isinstance(decision, ContinuationDecision):
        raise TypeError("decision must be ContinuationDecision")
    if not decision.eligible or decision.reason != "eligible":
        raise TokenFlowConflictError(
            "local continuation planning requires an eligible decision"
        )


def _blocked(
    decision: ContinuationDecision,
    reason: LocalPlanReason,
    *,
    binding: ProviderBinding | None = None,
    estimated_input: int | None = None,
    effective_output: int | None = None,
) -> LocalContinuationPlan:
    return LocalContinuationPlan(
        ready=False,
        reason=reason,
        decision=decision,
        binding=binding,
        request=None,
        estimated_input_tokens=estimated_input,
        effective_output_tokens=effective_output,
        context_window_tokens=(
            binding.context_window_tokens if binding is not None else None
        ),
    )
