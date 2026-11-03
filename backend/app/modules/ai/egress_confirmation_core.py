from __future__ import annotations

import json
import time
from dataclasses import dataclass

from app.core.database import open_sqlite_connection
from app.modules.ai.budget import evaluate_provider_budget_gate
from app.modules.ai.context_builder import canonical_digest, context_sources_manifest
from app.modules.ai.contracts import (
    AIExternalDispatchState,
    AIProviderAdapter,
    AIRequest,
    AIResponse,
    RoutingDecision,
)
from app.modules.ai.egress_lifecycle import (
    EgressTicketConsumption,
    consume_confirmation_ticket,
    start_reserved_attempt,
)
from app.modules.ai.egress_persistence import EgressStateError
from app.modules.ai.egress_policy import EgressPolicyConfig, load_default_egress_policy
from app.modules.ai.egress_runtime import (
    _load_packet,
    _reconcilable_start_failure_reservation,
    _response_status,
)
from app.modules.ai.egress_spine import EgressSpineStateError, create_queued_ai_job
from app.modules.ai.execution import (
    AiTaskOutcome,
    _ai_task_type_for,
    _create_proposed_records_from_response,
    _default_adapters,
    _prompt_for_task,
)
from app.modules.ai.execution_types import ProviderBinding
from app.modules.ai.provider_registry import ProviderRegistry, load_default_provider_registry
from app.modules.ai.settings import get_ai_settings
from app.modules.ai.token_flow_confirmation_resume import (
    ContinuationConfirmationAuthority,
    parse_continuation_authority,
)
from app.modules.ai.token_flow_confirmed_length import continue_after_confirmed_length
from app.modules.ai.token_flow_continuation import ContinuationDecision
from app.modules.ai.token_flow_external_transaction import finalize_external_attempt
from app.modules.ai.token_flow_runtime import normalize_finish_reason, normalize_outcome_reason
from app.modules.ai.token_flow_segments import store_protected_segment
from app.modules.ai.token_flow_service import (
    activate_confirmation_flow,
    get_confirmation_flow_for_ticket,
    transition_flow_state,
)
from app.modules.ai.token_flow_terminalization import terminalize_assembled_output


@dataclass(frozen=True)
class ConfirmedTicketExecution:
    ticket_id: str
    workspace_id: str | None
    outcome: AiTaskOutcome


@dataclass(frozen=True)
class _TicketMetadata:
    ticket_state: str
    task_kind: str
    workspace_id: str | None
    source_count: int
    trigger_ids: tuple[str, ...]
    packet_digest: str
    provider_id: str
    model_id: str
    route_class: str
    fallback_index: int
    max_output_tokens: int
    continuation_authority_json: str | None


def run_confirmation_ticket(
    ticket_id: str,
    *,
    adapters: dict[str, AIProviderAdapter] | None = None,
    registry: ProviderRegistry | None = None,
    policy: EgressPolicyConfig | None = None,
) -> ConfirmedTicketExecution:
    """Consume and execute the exact external attempt bound to one 059b ticket."""

    started_at = time.perf_counter()
    registry = registry or load_default_provider_registry()
    policy = policy or load_default_egress_policy()
    adapter_table = adapters if adapters is not None else _default_adapters()

    metadata = _load_ticket_metadata(ticket_id)
    if metadata.ticket_state != "pending":
        raise EgressStateError(f"confirmation ticket is not pending: {metadata.ticket_state}")
    flow = get_confirmation_flow_for_ticket(ticket_id)
    flow_id = str(flow["id"])

    is_continuation = metadata.continuation_authority_json is not None
    consumed = consume_confirmation_ticket(
        ticket_id,
        registry=registry,
        policy=policy,
        continuation_flow_id=flow_id if is_continuation else None,
    )
    binding = (
        _binding_from_ticket(metadata, registry)
        if consumed.authorized
        else _persisted_external_binding(metadata)
    )
    continuation_authority = (
        parse_continuation_authority(consumed.continuation_authority_json)
        if consumed.authorized
        else None
    )
    continuation_decision = (
        continuation_authority.decision()
        if continuation_authority is not None
        else None
    )
    if not is_continuation:
        activate_confirmation_flow(flow_id=flow_id, ticket_id=ticket_id)
    base_route_metadata = _route_metadata(consumed, metadata)

    if not consumed.authorized and is_continuation:
        if consumed.continuation_pause_attempt_id is None:
            raise EgressSpineStateError(
                "rejected continuation confirmation omitted pause attempt"
            )
        outcome = _outcome(
            flow_id=flow_id,
            consumed=consumed,
            metadata=metadata,
            ledger_id=consumed.continuation_pause_attempt_id,
            status="config_error",
            response=None,
            error_type=consumed.reason_code,
            reason_code=consumed.reason_code,
            context_digest=None,
            blocked=True,
        )
        return ConfirmedTicketExecution(ticket_id, metadata.workspace_id, outcome)

    if not _ticket_metadata_matches_consumption(metadata, consumed):
        packet = _safe_load_consumed_packet(consumed)
        outcome = _terminal_outcome(
            flow_id=flow_id,
            binding=binding,
            registry=registry,
            consumed=consumed,
            metadata=metadata,
            route_metadata={**base_route_metadata, "ticket_metadata_drift": True},
            status="config_error",
            reason_code="ticket_metadata_drift",
            error_type="config_error",
            started_at=started_at,
            reservation_id=consumed.reservation_id,
            packet=packet,
            continuation_decision=continuation_decision,
        )
        return ConfirmedTicketExecution(ticket_id, metadata.workspace_id, outcome)

    if not consumed.authorized:
        outcome = _terminal_outcome(
            flow_id=flow_id,
            binding=binding,
            registry=registry,
            consumed=consumed,
            metadata=metadata,
            route_metadata=base_route_metadata,
            status="config_error",
            reason_code=consumed.reason_code,
            error_type=consumed.reason_code,
            started_at=started_at,
            reservation_id=None,
            packet=None,
            continuation_decision=continuation_decision,
        )
        return ConfirmedTicketExecution(ticket_id, metadata.workspace_id, outcome)

    reservation_id = consumed.reservation_id
    if reservation_id is None or consumed.packet_json is None:
        raise EgressSpineStateError("authorized confirmation ticket omitted reservation or packet")

    try:
        packet = _load_packet(consumed.packet_json)
    except Exception as exc:
        outcome = _terminal_outcome(
            flow_id=flow_id,
            binding=binding,
            registry=registry,
            consumed=consumed,
            metadata=metadata,
            route_metadata=base_route_metadata,
            status="config_error",
            reason_code="persisted_packet_invalid",
            error_type=type(exc).__name__,
            started_at=started_at,
            reservation_id=reservation_id,
            packet=None,
            continuation_decision=continuation_decision,
        )
        return ConfirmedTicketExecution(ticket_id, metadata.workspace_id, outcome)

    gate = evaluate_provider_budget_gate(get_ai_settings(), consumed.provider_id)
    if not gate.allowed:
        outcome = _terminal_outcome(
            flow_id=flow_id,
            binding=binding,
            registry=registry,
            consumed=consumed,
            metadata=metadata,
            route_metadata={
                **base_route_metadata,
                "provider_gate_reason": gate.blocking_reason,
            },
            status="config_error",
            reason_code=gate.blocking_reason or "provider_gate_blocked",
            error_type="config_error",
            started_at=started_at,
            reservation_id=reservation_id,
            packet=packet,
            continuation_decision=continuation_decision,
        )
        return ConfirmedTicketExecution(ticket_id, metadata.workspace_id, outcome)

    adapter = adapter_table.get(consumed.provider_id)
    if adapter is None:
        outcome = _terminal_outcome(
            flow_id=flow_id,
            binding=binding,
            registry=registry,
            consumed=consumed,
            metadata=metadata,
            route_metadata={**base_route_metadata, "adapter_unavailable": True},
            status="config_error",
            reason_code="adapter_unavailable",
            error_type="config_error",
            started_at=started_at,
            reservation_id=reservation_id,
            packet=packet,
            continuation_decision=continuation_decision,
        )
        return ConfirmedTicketExecution(ticket_id, metadata.workspace_id, outcome)

    queued = _create_confirmation_job(
        consumed=consumed,
        metadata=metadata,
        route_metadata=base_route_metadata,
        packet=packet,
    )
    context_digest = _context_digest(packet)

    try:
        reserved = start_reserved_attempt(
            reservation_id,
            ai_job_id=queued.ai_job_id,
        )
        if reserved.packet_json != consumed.packet_json:
            raise EgressSpineStateError("ticket packet changed between consumption and attempt start")
        if (
            reserved.provider_id,
            reserved.model_id,
            reserved.route_class,
            reserved.fallback_index,
            reserved.max_output_tokens,
        ) != (
            consumed.provider_id,
            consumed.model_id,
            consumed.route_class,
            consumed.fallback_index,
            consumed.max_output_tokens,
        ):
            raise EgressSpineStateError("ticket reservation binding changed before execution")
        reserved_packet = _load_packet(reserved.packet_json)
        request = AIRequest(
            task_type=_ai_task_type_for(metadata.task_kind),
            prompt=_prompt_for_task(
                metadata.task_kind,
                reserved_packet["context_blocks"],
                reserved_packet["prompt"],
            ),
            model_preference=reserved.model_id,
            max_output_tokens=reserved.max_output_tokens,
            metadata={
                "egress_confirmation_ticket_id": consumed.ticket_id,
                "egress_decision_id": consumed.decision_id,
                "egress_packet_digest": consumed.packet_digest,
                "selected_route_class": consumed.route_class,
            },
        )
    except Exception as exc:
        finalize_external_attempt(
            flow_id=flow_id,
            ai_job_id=queued.ai_job_id,
            binding=binding,
            fallback_index=consumed.fallback_index,
            status="config_error",
            response=None,
            latency_ms=_elapsed_ms(started_at),
            error_type=type(exc).__name__,
            adapter_invoked=False,
            dispatch_state=AIExternalDispatchState.not_started,
            requested_output_ceiling=metadata.max_output_tokens,
            effective_output_ceiling=metadata.max_output_tokens,
            outcome_reason="egress_start_failed",
            reservation_id=_reconcilable_start_failure_reservation(reservation_id),
            registry=registry,
            continuation_decision=continuation_decision,
        )
        outcome = _outcome(
            flow_id=flow_id,
            consumed=consumed,
            metadata=metadata,
            ledger_id=queued.ai_job_id,
            status="config_error",
            response=None,
            error_type=type(exc).__name__,
            reason_code="egress_start_failed",
            context_digest=context_digest,
            blocked=True,
        )
        _finish_confirmation_flow(
            flow_id,
            outcome,
            continuation_authority=continuation_authority,
        )
        return ConfirmedTicketExecution(ticket_id, metadata.workspace_id, outcome)

    try:
        response = adapter.complete(request)
    except Exception as exc:
        finalize_external_attempt(
            flow_id=flow_id,
            ai_job_id=queued.ai_job_id,
            binding=binding,
            fallback_index=consumed.fallback_index,
            status="provider_error",
            response=None,
            latency_ms=_elapsed_ms(started_at),
            error_type=type(exc).__name__,
            adapter_invoked=True,
            dispatch_state=AIExternalDispatchState.unknown,
            requested_output_ceiling=metadata.max_output_tokens,
            effective_output_ceiling=reserved.max_output_tokens,
            outcome_reason=type(exc).__name__,
            reservation_id=reservation_id,
            registry=registry,
            continuation_decision=continuation_decision,
        )
        outcome = _outcome(
            flow_id=flow_id,
            consumed=consumed,
            metadata=metadata,
            ledger_id=queued.ai_job_id,
            status="provider_error",
            response=None,
            error_type=type(exc).__name__,
            reason_code="ticket_consumed",
            context_digest=context_digest,
            blocked=False,
        )
        _finish_confirmation_flow(
            flow_id,
            outcome,
            continuation_authority=continuation_authority,
        )
        return ConfirmedTicketExecution(ticket_id, metadata.workspace_id, outcome)

    status, error_type = _response_status(response)
    binding_mismatch = (response.provider_id, response.model_id) != (binding.provider_id, binding.model_id) or (
        response.usage.provider_id,
        response.usage.model_id,
    ) != (binding.provider_id, binding.model_id)
    dispatch_invalid = response.external_dispatch_state not in {
        AIExternalDispatchState.not_started,
        AIExternalDispatchState.started,
        AIExternalDispatchState.unknown,
    } or (status == "success" and response.external_dispatch_state is AIExternalDispatchState.not_started)
    if binding_mismatch or dispatch_invalid:
        reason_code = "response_binding_mismatch" if binding_mismatch else "response_dispatch_invalid"
        error_name = "EgressSpineStateError" if binding_mismatch else "EgressContractError"
        finalize_external_attempt(
            flow_id=flow_id,
            ai_job_id=queued.ai_job_id,
            binding=binding,
            fallback_index=consumed.fallback_index,
            status="provider_error",
            response=None,
            latency_ms=_elapsed_ms(started_at),
            error_type=error_name,
            adapter_invoked=True,
            dispatch_state=AIExternalDispatchState.unknown,
            requested_output_ceiling=metadata.max_output_tokens,
            effective_output_ceiling=reserved.max_output_tokens,
            outcome_reason=reason_code,
            reservation_id=reservation_id,
            registry=registry,
            continuation_decision=continuation_decision,
        )
        outcome = _outcome(
            flow_id=flow_id,
            consumed=consumed,
            metadata=metadata,
            ledger_id=queued.ai_job_id,
            status="provider_error",
            response=None,
            error_type=error_name,
            reason_code=reason_code,
            context_digest=context_digest,
            blocked=False,
        )
        _finish_confirmation_flow(
            flow_id,
            outcome,
            continuation_authority=continuation_authority,
        )
        return ConfirmedTicketExecution(ticket_id, metadata.workspace_id, outcome)

    assert response.external_dispatch_state is not None
    finalize_external_attempt(
        flow_id=flow_id,
        ai_job_id=queued.ai_job_id,
        binding=binding,
        fallback_index=consumed.fallback_index,
        status=status,
        response=response,
        latency_ms=_elapsed_ms(started_at),
        error_type=error_type,
        adapter_invoked=True,
        dispatch_state=response.external_dispatch_state,
        requested_output_ceiling=metadata.max_output_tokens,
        effective_output_ceiling=reserved.max_output_tokens,
        outcome_reason=error_type or status,
        reservation_id=reservation_id,
        registry=registry,
        continuation_decision=continuation_decision,
    )
    outcome = _outcome(
        flow_id=flow_id,
        consumed=consumed,
        metadata=metadata,
        ledger_id=queued.ai_job_id,
        status=status,
        response=response,
        error_type=error_type,
        reason_code="ticket_consumed",
        context_digest=context_digest,
        blocked=False,
    )
    finish_reason = normalize_finish_reason(
        response.finish_reason, failed=response.error is not None
    )
    if (
        continuation_authority is not None
        and status == "success"
        and finish_reason == "length"
        and not metadata.task_kind.startswith("cloud_escalation_")
    ):
        outcome = continue_after_confirmed_length(
            initial_outcome=outcome,
            current_packet=packet,
            task_kind=metadata.task_kind,
            binding=binding,
            fallback_index=consumed.fallback_index,
            max_output_tokens=metadata.max_output_tokens,
            adapters=adapter_table,
            registry=registry,
            policy=policy,
            expected_sensitivity_level=(
                continuation_authority.expected_sensitivity_level
            ),
        )
        if (
            outcome.status == "success"
            and outcome.response is not None
            and normalize_finish_reason(
                outcome.response.finish_reason,
                failed=outcome.response.error is not None,
            )
            == "stop"
        ):
            proposed_ids, parse_error = _create_proposed_records_from_response(
                task_kind=metadata.task_kind,
                response=outcome.response,
                ledger_id=outcome.ledger_id,
                workspace_id=metadata.workspace_id,
            )
            outcome.proposed_record_ids = proposed_ids
            outcome.records_parse_error = parse_error
        return ConfirmedTicketExecution(
            ticket_id, metadata.workspace_id, outcome
        )
    _finish_confirmation_flow(
        flow_id,
        outcome,
        continuation_authority=continuation_authority,
    )
    if (
        outcome.status == "success"
        and outcome.response is not None
        and normalize_finish_reason(
            outcome.response.finish_reason,
            failed=outcome.response.error is not None,
        )
        == "stop"
    ):
        proposed_ids, parse_error = _create_proposed_records_from_response(
            task_kind=metadata.task_kind,
            response=outcome.response,
            ledger_id=outcome.ledger_id,
            workspace_id=metadata.workspace_id,
        )
        outcome.proposed_record_ids = proposed_ids
        outcome.records_parse_error = parse_error
    return ConfirmedTicketExecution(ticket_id, metadata.workspace_id, outcome)


def _load_ticket_metadata(ticket_id: str) -> _TicketMetadata:
    with open_sqlite_connection() as connection:
        row = connection.execute(
            """
            SELECT ticket.state AS ticket_state, packet.task_kind, packet.workspace_id,
                   decision.source_count,
                   ticket.trigger_ids_json, ticket.packet_digest,
                   ticket.continuation_authority_json, packet.provider_id, packet.model_id, packet.route_class,
                   packet.fallback_index, packet.max_output_tokens
            FROM egress_confirmation_tickets AS ticket
            JOIN egress_decisions AS decision ON decision.id = ticket.decision_id
            JOIN egress_packets AS packet ON packet.id = ticket.packet_id
            WHERE ticket.id = ?
            """,
            (ticket_id,),
        ).fetchone()
    if row is None:
        raise EgressStateError("confirmation ticket metadata was not found")
    try:
        trigger_ids = json.loads(row["trigger_ids_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise EgressStateError("confirmation ticket trigger metadata is malformed") from exc
    if not isinstance(trigger_ids, list) or not all(isinstance(value, str) for value in trigger_ids):
        raise EgressStateError("confirmation ticket trigger metadata is malformed")
    return _TicketMetadata(
        ticket_state=str(row["ticket_state"]),
        task_kind=str(row["task_kind"]),
        workspace_id=row["workspace_id"],
        source_count=int(row["source_count"]),
        trigger_ids=tuple(trigger_ids),
        packet_digest=str(row["packet_digest"]),
        provider_id=str(row["provider_id"]),
        model_id=str(row["model_id"]),
        route_class=str(row["route_class"]),
        fallback_index=int(row["fallback_index"]),
        max_output_tokens=int(row["max_output_tokens"]),
        continuation_authority_json=row["continuation_authority_json"],
    )


def _ticket_metadata_matches_consumption(
    metadata: _TicketMetadata,
    consumed: EgressTicketConsumption,
) -> bool:
    return (
        metadata.packet_digest,
        metadata.provider_id,
        metadata.model_id,
        metadata.route_class,
        metadata.fallback_index,
        metadata.max_output_tokens,
    ) == (
        consumed.packet_digest,
        consumed.provider_id,
        consumed.model_id,
        consumed.route_class,
        consumed.fallback_index,
        consumed.max_output_tokens,
    )


def _safe_load_consumed_packet(
    consumed: EgressTicketConsumption,
) -> dict[str, object] | None:
    if consumed.packet_json is None:
        return None
    try:
        return _load_packet(consumed.packet_json)
    except Exception:
        return None


def _create_confirmation_job(
    *,
    consumed: EgressTicketConsumption,
    metadata: _TicketMetadata,
    route_metadata: dict[str, object],
    packet: dict[str, object] | None,
):
    blocks = packet["context_blocks"] if packet is not None else []
    prompt_digest = canonical_digest({"effective_packet_prompt": packet["prompt"]}) if packet is not None else None
    context_digest = canonical_digest(blocks) if blocks else None
    context_sources = context_sources_manifest(blocks) if blocks else None
    return create_queued_ai_job(
        task_kind=metadata.task_kind,
        requested_route_class=consumed.route_class,
        selected_route_class=consumed.route_class,
        provider_id=consumed.provider_id,
        model_id=consumed.model_id,
        decision_reason=f"confirmed_ticket:{consumed.ticket_id}",
        prompt_digest=prompt_digest,
        context_digest=context_digest,
        context_sources=context_sources,
        route_metadata=route_metadata,
    )


def _terminal_outcome(
    *,
    flow_id: str,
    binding: ProviderBinding,
    registry: ProviderRegistry,
    consumed: EgressTicketConsumption,
    metadata: _TicketMetadata,
    route_metadata: dict[str, object],
    status: str,
    reason_code: str,
    error_type: str,
    started_at: float,
    reservation_id: str | None,
    packet: dict[str, object] | None,
    continuation_decision: ContinuationDecision | None,
) -> AiTaskOutcome:
    queued = _create_confirmation_job(
        consumed=consumed,
        metadata=metadata,
        route_metadata=route_metadata,
        packet=packet,
    )
    finalize_external_attempt(
        flow_id=flow_id,
        ai_job_id=queued.ai_job_id,
        binding=binding,
        fallback_index=consumed.fallback_index,
        status=status,
        response=None,
        latency_ms=_elapsed_ms(started_at),
        error_type=error_type,
        adapter_invoked=False,
        dispatch_state=AIExternalDispatchState.not_started,
        requested_output_ceiling=metadata.max_output_tokens,
        effective_output_ceiling=metadata.max_output_tokens,
        outcome_reason=reason_code,
        reservation_id=reservation_id,
        registry=registry,
        use_confirmation_pricing_snapshot=not consumed.authorized,
        continuation_decision=continuation_decision,
    )
    outcome = _outcome(
        flow_id=flow_id,
        consumed=consumed,
        metadata=metadata,
        ledger_id=queued.ai_job_id,
        status=status,
        response=None,
        error_type=error_type,
        reason_code=reason_code,
        context_digest=_context_digest(packet),
        blocked=True,
    )
    _finish_confirmation_flow(
        flow_id,
        outcome,
        continuation_authority=(
            parse_continuation_authority(consumed.continuation_authority_json)
        ),
    )
    return outcome


def _outcome(
    *,
    flow_id: str,
    consumed: EgressTicketConsumption,
    metadata: _TicketMetadata,
    ledger_id: str,
    status: str,
    response: AIResponse | None,
    error_type: str | None,
    reason_code: str,
    context_digest: str | None,
    blocked: bool,
) -> AiTaskOutcome:
    decision = RoutingDecision(
        provider_id=consumed.provider_id,
        model_id=consumed.model_id,
        blocked=blocked,
        blocked_reason=reason_code if blocked else None,
        decision_reason=(f"egress_ticket:{reason_code}" if blocked else f"confirmed_ticket:{consumed.ticket_id}"),
    )
    return AiTaskOutcome(
        status=status,
        ledger_id=ledger_id,
        selected_route_class=consumed.route_class,
        decision=decision,
        response=response,
        error_type=error_type,
        context_digest=context_digest,
        context_sources_count=metadata.source_count,
        egress_decision_id=consumed.decision_id,
        egress_packet_digest=consumed.packet_digest,
        egress_ticket_id=consumed.ticket_id,
        egress_reservation_id=consumed.reservation_id,
        egress_reason_code=reason_code,
        egress_trigger_ids=metadata.trigger_ids,
        flow_id=flow_id,
    )


def _finish_confirmation_flow(
    flow_id: str,
    outcome: AiTaskOutcome,
    *,
    continuation_authority: ContinuationConfirmationAuthority | None,
) -> None:
    finish_reason = (
        normalize_finish_reason(
            outcome.response.finish_reason,
            failed=outcome.response.error is not None,
        )
        if outcome.status == "success" and outcome.response is not None
        else None
    )
    if continuation_authority is not None:
        if (
            outcome.status == "success"
            and outcome.response is not None
            and outcome.response.text
        ):
            store_protected_segment(
                flow_id=flow_id,
                originating_attempt_id=outcome.ledger_id,
                body_text=outcome.response.text,
                effective_sensitivity_level=(
                    continuation_authority.expected_sensitivity_level
                ),
                workspace_id=_flow_workspace_id(flow_id),
            )
        complete = outcome.status == "success" and finish_reason == "stop"
        if complete:
            reason = "completed"
        elif outcome.status == "success":
            reason = (
                "output_length_limit"
                if finish_reason == "length"
                else f"continuation_finish_{finish_reason}"
            )
        else:
            failure_reason = outcome.egress_reason_code
            if failure_reason == "ticket_consumed":
                failure_reason = None
            reason = f"continuation_{normalize_outcome_reason(failure_reason or outcome.error_type or outcome.status)}"
        _, assembled = terminalize_assembled_output(
            flow_id=flow_id,
            terminal_attempt_id=outcome.ledger_id,
            new_state="complete" if complete else "partial_terminal",
            terminal_reason=reason,
            workspace_id=_flow_workspace_id(flow_id),
            expected_sensitivity_level=(
                continuation_authority.expected_sensitivity_level
            ),
        )
        if outcome.response is not None:
            outcome.response = outcome.response.model_copy(
                update={"text": assembled.body_text, "content": assembled.body_text}
            )
        return

    if outcome.status == "success" and finish_reason == "stop":
        state = "complete"
        reason = "completed"
    elif outcome.status == "success":
        state = "partial_terminal"
        reason = (
            "output_length_limit"
            if finish_reason == "length"
            else f"finish_{finish_reason}"
        )
    else:
        failure_reason = outcome.egress_reason_code
        if failure_reason == "ticket_consumed":
            failure_reason = None
        state = "failed_terminal"
        reason = normalize_outcome_reason(
            failure_reason or outcome.error_type or outcome.status
        )
    transition_flow_state(
        flow_id=flow_id,
        new_state=state,
        terminal_reason=reason,
        terminal_attempt_id=outcome.ledger_id,
        terminal_response_text=(
            outcome.response.text
            if state == "complete" and outcome.response is not None
            else None
        ),
    )


def _flow_workspace_id(flow_id: str) -> str | None:
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT workspace_id FROM ai_flows WHERE id = ?",
            (flow_id,),
        ).fetchone()
    if row is None:
        raise EgressSpineStateError("confirmation flow disappeared")
    return row["workspace_id"]


def _binding_from_ticket(metadata: _TicketMetadata, registry: ProviderRegistry) -> ProviderBinding:
    provider = registry.providers.get(metadata.provider_id)
    model = registry.models.get((metadata.provider_id, metadata.model_id))
    if (
        provider is None
        or model is None
        or provider.execution_class != "external_provider"
        or not provider.enabled
        or not provider.requires_network
        or metadata.route_class not in model.route_classes
        or model.pricing is None
    ):
        raise EgressSpineStateError("confirmation ticket binding is not a registered external provider")
    return ProviderBinding(
        route_class=metadata.route_class,
        provider_id=metadata.provider_id,
        model_id=metadata.model_id,
        requires_network=True,
        max_output_tokens=model.max_output_tokens,
        execution_class=provider.execution_class,
        context_window_tokens=model.context_window_tokens,
    )


def _persisted_external_binding(metadata: _TicketMetadata) -> ProviderBinding:
    """Rebuild non-dispatched identity from the server-owned ticket snapshot."""

    return ProviderBinding(
        route_class=metadata.route_class,
        provider_id=metadata.provider_id,
        model_id=metadata.model_id,
        requires_network=True,
        max_output_tokens=metadata.max_output_tokens,
        execution_class="external_provider",
        context_window_tokens=None,
    )


def _route_metadata(
    consumed: EgressTicketConsumption,
    metadata: _TicketMetadata,
) -> dict[str, object]:
    return {
        "confirmation_mode": "ticket_id_only",
        "egress_confirmation_ticket_id": consumed.ticket_id,
        "egress_decision_id": consumed.decision_id,
        "egress_packet_digest": consumed.packet_digest,
        "egress_reason_code": consumed.reason_code,
        "egress_reservation_id": consumed.reservation_id,
        "egress_trigger_ids": list(metadata.trigger_ids),
        "fallback_attempt_index": consumed.fallback_index,
        "fallback_chain_route": consumed.route_class,
        "fallback_model_id": consumed.model_id,
        "fallback_provider_id": consumed.provider_id,
    }


def _context_digest(packet: dict[str, object] | None) -> str | None:
    if packet is None:
        return None
    blocks = packet["context_blocks"]
    return canonical_digest(blocks) if blocks else None


def _elapsed_ms(started_at: float) -> int:
    return int((time.perf_counter() - started_at) * 1000)
