from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.modules.ai.egress_policy import (
    EXTERNAL_PROVIDER_OPERATION,
    EgressPolicyConfig,
)
from app.modules.ai.egress_service import (
    EgressPacketMaterial,
    EgressPacketProjection,
    build_packet_projection,
)
from app.modules.ai.execution import resolve_binding
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.provider_registry import ProviderRegistry
from app.modules.ai.token_flow_continuation import ContinuationDecision
from app.modules.ai.token_flow_segments import (
    list_protected_segment_metadata,
    read_protected_segment,
)
from app.modules.ai.token_flow_service import TokenFlowConflictError

_SAFE_EXTERNAL_LEVELS = frozenset({"S0", "S1"})


@dataclass(frozen=True, slots=True)
class ExternalContinuationPacket:
    decision: ContinuationDecision
    binding: ProviderBinding
    material: EgressPacketMaterial
    projection: EgressPacketProjection


def build_external_continuation_packet(
    *,
    decision: ContinuationDecision,
    route_class: str,
    task_kind: str,
    original_prompt: str,
    workspace_id: str | None,
    prompt_level: str,
    expected_sensitivity_level: str,
    requested_output_tokens: int,
    fallback_index: int = 0,
    prompt_derivative_id: str | None = None,
    context_blocks: tuple[dict[str, Any], ...] = (),
    context_level: str = "S0",
    included_manifest: tuple[dict[str, Any], ...] = (),
    withheld_manifest: tuple[dict[str, Any], ...] = (),
    budget_dropped_manifest: tuple[dict[str, Any], ...] = (),
    source_digests: tuple[tuple[str, str], ...] = (),
    bindings: dict[str, ProviderBinding] | None = None,
    registry: ProviderRegistry | None = None,
    policy: EgressPolicyConfig | None = None,
) -> ExternalContinuationPacket:
    """Build a fresh canonical 059b packet projection without persistence or network."""

    _require_eligible(decision)
    if not isinstance(task_kind, str) or not task_kind.strip():
        raise ValueError("task_kind must be non-empty text")
    if not isinstance(original_prompt, str) or not original_prompt.strip():
        raise ValueError("original_prompt must be non-empty text")
    if isinstance(requested_output_tokens, bool) or not isinstance(
        requested_output_tokens, int
    ) or requested_output_tokens <= 0:
        raise ValueError("requested_output_tokens must be a positive integer")
    if (
        isinstance(fallback_index, bool)
        or not isinstance(fallback_index, int)
        or fallback_index < 0
    ):
        raise ValueError("fallback_index must be a nonnegative integer")
    if prompt_level not in _SAFE_EXTERNAL_LEVELS:
        raise TokenFlowConflictError(
            "external continuation prompt must already be S0 or S1"
        )
    if expected_sensitivity_level not in _SAFE_EXTERNAL_LEVELS:
        raise TokenFlowConflictError(
            "external continuation segments must already be S0 or S1"
        )
    if context_level not in _SAFE_EXTERNAL_LEVELS:
        raise TokenFlowConflictError(
            "external continuation context must already be S0 or S1"
        )

    binding, _ = resolve_binding(route_class, bindings)
    if binding is None:
        raise TokenFlowConflictError(
            "external continuation route is unavailable"
        )
    if (
        not binding.requires_network
        or binding.execution_class != "external_provider"
        or binding.context_window_tokens is None
        or binding.context_window_tokens <= 0
        or binding.max_output_tokens <= 0
    ):
        raise TokenFlowConflictError(
            "external continuation requires complete external-provider metadata"
        )

    metadata = list_protected_segment_metadata(
        flow_id=decision.flow_id,
        workspace_id=workspace_id,
    )
    if not metadata:
        raise TokenFlowConflictError(
            "eligible external continuation has no protected segments"
        )
    if metadata[-1].segment_index != decision.protected_segment_index:
        raise TokenFlowConflictError(
            "external continuation decision no longer references the latest segment"
        )
    if metadata[-1].originating_attempt_id != decision.parent_attempt_id:
        raise TokenFlowConflictError(
            "external continuation decision no longer references the latest parent"
        )

    bodies = [
        read_protected_segment(
            flow_id=decision.flow_id,
            segment_index=item.segment_index,
            workspace_id=workspace_id,
            expected_sensitivity_level=expected_sensitivity_level,
        ).body_text
        for item in metadata
    ]
    effective_output = min(requested_output_tokens, binding.max_output_tokens)
    continuation_prompt = _continuation_prompt(
        original_prompt=original_prompt.strip(),
        accumulated_output="".join(bodies),
    )
    segment_digests = tuple(
        (f"segment:{item.segment_index}", item.body_digest) for item in metadata
    )
    material = EgressPacketMaterial(
        operation=EXTERNAL_PROVIDER_OPERATION,
        task_kind=task_kind.strip(),
        route_class=binding.route_class,
        provider_id=binding.provider_id,
        model_id=binding.model_id,
        fallback_index=fallback_index,
        prompt=continuation_prompt,
        context_blocks=context_blocks,
        prompt_level=prompt_level,
        context_level=context_level,
        final_level=_maximum_level(prompt_level, context_level),
        max_output_tokens=effective_output,
        workspace_id=workspace_id,
        prompt_derivative_id=prompt_derivative_id,
        included_manifest=included_manifest,
        withheld_manifest=withheld_manifest,
        budget_dropped_manifest=budget_dropped_manifest,
        source_digests=tuple(source_digests) + segment_digests,
    )
    projection = build_packet_projection(
        material,
        registry=registry,
        policy=policy,
    )
    return ExternalContinuationPacket(
        decision=decision,
        binding=binding,
        material=material,
        projection=projection,
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


def _maximum_level(first: str, second: str) -> str:
    return "S1" if "S1" in {first, second} else "S0"


def _require_eligible(decision: ContinuationDecision) -> None:
    if not isinstance(decision, ContinuationDecision):
        raise TypeError("decision must be ContinuationDecision")
    if not decision.eligible or decision.reason != "eligible":
        raise TokenFlowConflictError(
            "external continuation packet requires an eligible decision"
        )
