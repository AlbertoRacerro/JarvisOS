from __future__ import annotations

import json

from app.core.database import open_sqlite_connection
from app.modules.ai.contracts import AIProviderAdapter
from app.modules.ai.egress_policy import EgressPolicyConfig
from app.modules.ai.egress_runtime import ExternalTaskOutcome, _load_packet
from app.modules.ai.execution import AiTaskOutcome, _ai_task_type_for, _prompt_for_task
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.provider_registry import ProviderRegistry
from app.modules.ai.token_flow_external_runtime import (
    run_silent_allow_external_continuations,
)
from app.modules.ai.token_flow_segments import store_protected_segment
from app.modules.ai.token_flow_terminalization import terminalize_assembled_output


class ConfirmedLengthContinuationError(RuntimeError):
    """A confirmed length response could not re-enter the governed loop."""


def continue_after_confirmed_length(
    *,
    initial_outcome: AiTaskOutcome,
    current_packet: dict[str, object],
    task_kind: str,
    binding: ProviderBinding,
    fallback_index: int,
    max_output_tokens: int,
    adapters: dict[str, AIProviderAdapter],
    registry: ProviderRegistry,
    policy: EgressPolicyConfig,
    expected_sensitivity_level: str,
) -> AiTaskOutcome:
    """Continue a confirmed length child through the existing external loop."""

    flow_id = initial_outcome.flow_id
    if not isinstance(flow_id, str) or not flow_id:
        raise ConfirmedLengthContinuationError(
            "confirmed length outcome omitted flow identity"
        )
    try:
        origin = _load_origin(flow_id)
        context_blocks = current_packet.get("context_blocks")
        if not isinstance(context_blocks, list):
            raise ConfirmedLengthContinuationError(
                "confirmed continuation packet context is malformed"
            )
        result = run_silent_allow_external_continuations(
            initial_outcome=initial_outcome,
            original_prompt=origin.original_prompt,
            task_kind=task_kind,
            selected_route_class=binding.route_class,
            requested_route_class=origin.requested_route_class,
            context_blocks=context_blocks,
            max_output_tokens=max_output_tokens,
            adapters=adapters,
            bindings={binding.route_class: binding},
            workspace_id=origin.workspace_id,
            task_type_for=_ai_task_type_for,
            task_prompt_for=_prompt_for_task,
            binding=binding,
            fallback_index=fallback_index,
            registry=registry,
            policy=policy,
        )
    except Exception as exc:
        return _terminalize_origin_failure(
            outcome=initial_outcome,
            expected_sensitivity_level=expected_sensitivity_level,
            error=exc,
        )
    return _as_ai_outcome(result)


class _Origin:
    def __init__(
        self,
        *,
        original_prompt: str,
        requested_route_class: str | None,
        workspace_id: str | None,
    ) -> None:
        self.original_prompt = original_prompt
        self.requested_route_class = requested_route_class
        self.workspace_id = workspace_id


def _load_origin(flow_id: str) -> _Origin:
    with open_sqlite_connection() as connection:
        flow = connection.execute(
            "SELECT requested_route_class, workspace_id FROM ai_flows WHERE id = ?",
            (flow_id,),
        ).fetchone()
        first = connection.execute(
            """
            SELECT route_reason_json
            FROM ai_jobs
            WHERE flow_id = ? AND flow_attempt_index = 0
            """,
            (flow_id,),
        ).fetchone()
        if flow is None or first is None:
            raise ConfirmedLengthContinuationError(
                "confirmed continuation origin attempt is unavailable"
            )
        try:
            route_metadata = json.loads(first["route_reason_json"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise ConfirmedLengthContinuationError(
                "confirmed continuation origin metadata is malformed"
            ) from exc
        packet_digest = (
            route_metadata.get("egress_packet_digest")
            if isinstance(route_metadata, dict)
            else None
        )
        if not isinstance(packet_digest, str) or not packet_digest:
            raise ConfirmedLengthContinuationError(
                "confirmed continuation origin packet is unavailable"
            )
        packet_row = connection.execute(
            "SELECT packet_json FROM egress_packets WHERE packet_digest = ?",
            (packet_digest,),
        ).fetchone()
    if packet_row is None:
        raise ConfirmedLengthContinuationError(
            "confirmed continuation origin packet is unavailable"
        )
    packet = _load_packet(str(packet_row["packet_json"]))
    prompt = packet.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        raise ConfirmedLengthContinuationError(
            "confirmed continuation origin prompt is malformed"
        )
    return _Origin(
        original_prompt=prompt,
        requested_route_class=flow["requested_route_class"],
        workspace_id=flow["workspace_id"],
    )


def _terminalize_origin_failure(
    *,
    outcome: AiTaskOutcome,
    expected_sensitivity_level: str,
    error: Exception,
) -> AiTaskOutcome:
    flow_id = str(outcome.flow_id)
    response = outcome.response
    if response is None or not response.text:
        raise ConfirmedLengthContinuationError(
            "confirmed length response is unavailable"
        ) from error
    with open_sqlite_connection() as connection:
        flow = connection.execute(
            "SELECT workspace_id FROM ai_flows WHERE id = ?",
            (flow_id,),
        ).fetchone()
    if flow is None:
        raise ConfirmedLengthContinuationError(
            "confirmed continuation flow disappeared"
        ) from error
    store_protected_segment(
        flow_id=flow_id,
        originating_attempt_id=outcome.ledger_id,
        body_text=response.text,
        effective_sensitivity_level=expected_sensitivity_level,
        workspace_id=flow["workspace_id"],
    )
    _, assembled = terminalize_assembled_output(
        flow_id=flow_id,
        terminal_attempt_id=outcome.ledger_id,
        new_state="partial_terminal",
        terminal_reason="continuation_origin_packet_unavailable",
        workspace_id=flow["workspace_id"],
        expected_sensitivity_level=expected_sensitivity_level,
    )
    outcome.response = response.model_copy(
        update={"text": assembled.body_text, "content": assembled.body_text}
    )
    outcome.error_type = type(error).__name__
    outcome.egress_reason_code = "continuation_origin_packet_unavailable"
    return outcome


def _as_ai_outcome(result: object) -> AiTaskOutcome:
    if isinstance(result, AiTaskOutcome):
        return result
    if not isinstance(result, ExternalTaskOutcome):
        raise ConfirmedLengthContinuationError(
            "external continuation returned an unsupported outcome"
        )
    return AiTaskOutcome(
        status=result.status,
        ledger_id=result.ledger_id,
        selected_route_class=result.selected_route_class,
        decision=result.decision,
        response=result.response,
        error_type=result.error_type,
        context_digest=result.context_digest,
        context_sources_count=result.context_sources_count,
        egress_decision_id=result.egress_decision_id,
        egress_packet_digest=result.egress_packet_digest,
        egress_ticket_id=result.egress_ticket_id,
        egress_reservation_id=result.egress_reservation_id,
        egress_reason_code=result.egress_reason_code,
        egress_trigger_ids=result.egress_trigger_ids,
        flow_id=result.flow_id,
    )
