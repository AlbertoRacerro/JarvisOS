from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from app.modules.ai.contracts import AIProviderAdapter, AIResponse, AITaskType
from app.modules.ai.egress_policy import EgressPolicyConfig
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.provider_registry import ProviderRegistry
from app.modules.ai.token_flow_continuation import evaluate_direct_continuation
from app.modules.ai.token_flow_runtime import normalize_finish_reason
from app.modules.ai.token_flow_segments import store_protected_segment
from app.modules.ai.token_flow_service import get_flow, transition_flow_state
from app.modules.ai.token_flow_terminalization import terminalize_assembled_output


def run_silent_allow_external_continuations(
    *,
    initial_outcome: object,
    original_prompt: str,
    task_kind: str,
    selected_route_class: str,
    requested_route_class: str | None,
    context_blocks: list[dict[str, object]] | None,
    max_output_tokens: int,
    adapters: dict[str, AIProviderAdapter],
    bindings: dict[str, ProviderBinding] | None,
    workspace_id: str | None,
    task_type_for: Callable[[str], AITaskType],
    task_prompt_for: Callable[[str, list[dict], str], str],
    binding: ProviderBinding,
    fallback_index: int,
    registry: ProviderRegistry,
    policy: EgressPolicyConfig,
):
    """Continue one silent-allow external flow through fresh complete 059b attempts."""

    from app.modules.ai.egress_runtime import _run_binding

    flow_id = str(initial_outcome.flow_id)
    flow_workspace_id = get_flow(flow_id)["workspace_id"]
    current = initial_outcome

    while True:
        response = current.response
        if current.status != "success" or response is None or not response.text:
            raise RuntimeError(
                "external continuation requires a successful non-empty response"
            )
        sensitivity_level = _packet_final_level(current.egress_packet_digest)
        store_protected_segment(
            flow_id=flow_id,
            originating_attempt_id=current.ledger_id,
            body_text=response.text,
            effective_sensitivity_level=sensitivity_level,
            workspace_id=flow_workspace_id,
        )
        decision = evaluate_direct_continuation(
            flow_id=flow_id,
            workspace_id=flow_workspace_id,
            expected_sensitivity_level=sensitivity_level,
        )
        if not decision.eligible:
            _, assembled = terminalize_assembled_output(
                flow_id=flow_id,
                terminal_attempt_id=current.ledger_id,
                new_state="partial_terminal",
                terminal_reason=f"continuation_{decision.reason}",
                workspace_id=flow_workspace_id,
                expected_sensitivity_level=sensitivity_level,
            )
            return replace(
                current,
                response=_assembled_response(response, assembled.body_text),
            )

        child = _run_binding(
            user_prompt=original_prompt,
            task_kind=task_kind,
            selected_route_class=selected_route_class,
            requested_route_class=requested_route_class,
            context_blocks=context_blocks,
            max_output_tokens=max_output_tokens,
            adapters=adapters,
            workspace_id=workspace_id,
            context_build_error=None,
            external_blocked_reason=None,
            task_type_for=task_type_for,
            task_prompt_for=task_prompt_for,
            binding=binding,
            fallback_index=fallback_index,
            prior_retryable_error_code=None,
            registry=registry,
            policy=policy,
            flow_id=flow_id,
            continuation_decision=decision,
            continuation_expected_sensitivity_level=sensitivity_level,
        )
        if (
            child.status == "validation_error"
            and child.egress_reason_code == "confirmation_required"
            and child.egress_ticket_id is not None
        ):
            transition_flow_state(
                flow_id=flow_id,
                new_state="confirmation_required",
            )
            return child
        if child.status != "success" or child.response is None:
            _, assembled = terminalize_assembled_output(
                flow_id=flow_id,
                terminal_attempt_id=child.ledger_id,
                new_state="partial_terminal",
                terminal_reason=f"continuation_{child.egress_reason_code or child.error_type or child.status}",
                workspace_id=flow_workspace_id,
                expected_sensitivity_level=sensitivity_level,
            )
            fallback_response = _assembled_response(response, assembled.body_text)
            return replace(child, response=fallback_response)

        finish_reason = normalize_finish_reason(
            child.response.finish_reason,
            failed=child.response.error is not None,
        )
        current = child
        if finish_reason == "length":
            continue

        child_sensitivity = _packet_final_level(child.egress_packet_digest)
        store_protected_segment(
            flow_id=flow_id,
            originating_attempt_id=child.ledger_id,
            body_text=child.response.text or "",
            effective_sensitivity_level=child_sensitivity,
            workspace_id=flow_workspace_id,
        )
        complete = finish_reason == "stop"
        _, assembled = terminalize_assembled_output(
            flow_id=flow_id,
            terminal_attempt_id=child.ledger_id,
            new_state="complete" if complete else "partial_terminal",
            terminal_reason=(
                "completed"
                if complete
                else f"continuation_finish_{finish_reason}"
            ),
            workspace_id=flow_workspace_id,
            expected_sensitivity_level=child_sensitivity,
        )
        return replace(
            child,
            response=_assembled_response(child.response, assembled.body_text),
        )


def _packet_final_level(packet_digest: str | None) -> str:
    if not packet_digest:
        raise RuntimeError("external continuation packet digest is missing")
    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT final_level FROM egress_packets WHERE packet_digest = ?",
            (packet_digest,),
        ).fetchone()
    if row is None or row["final_level"] not in {"S0", "S1"}:
        raise RuntimeError(
            "external continuation packet sensitivity is unavailable"
        )
    return str(row["final_level"])


def _assembled_response(response: AIResponse, body_text: str) -> AIResponse:
    return response.model_copy(update={"text": body_text, "content": body_text})
