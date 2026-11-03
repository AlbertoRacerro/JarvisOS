from __future__ import annotations

from app.modules.ai.egress_confirmation import run_confirmation_ticket
from app.modules.ai.models import AITaskRunResponse, EscalationConfirmRequest, EscalationConfirmResponse


def confirm_escalation(request: EscalationConfirmRequest) -> EscalationConfirmResponse:
    confirmed = run_confirmation_ticket(request.ticket_id)
    outcome = confirmed.outcome
    response = outcome.response
    task_response = AITaskRunResponse(
        status=outcome.status,
        ledger_id=outcome.ledger_id,
        selected_route_class=outcome.selected_route_class,
        decision_reason=outcome.decision.decision_reason,
        blocked_reason=outcome.decision.blocked_reason,
        response_text=response.text if response is not None else None,
        provider_id=response.provider_id if response is not None else outcome.decision.provider_id,
        model_id=response.model_id if response is not None else outcome.decision.model_id,
        usage=response.usage if response is not None else None,
        error_type=outcome.error_type,
        include_project_context=confirmed.workspace_id is not None,
        workspace_id=confirmed.workspace_id,
        context_digest=outcome.context_digest,
        context_sources_count=outcome.context_sources_count,
        records_parse_error=outcome.records_parse_error,
        proposed_record_ids=outcome.proposed_record_ids or [],
        confirmation_payload={"ticket_id": confirmed.ticket_id},
        egress_decision_id=outcome.egress_decision_id,
        egress_packet_digest=outcome.egress_packet_digest,
        egress_ticket_id=outcome.egress_ticket_id,
        egress_reservation_id=outcome.egress_reservation_id,
        egress_reason_code=outcome.egress_reason_code,
        egress_trigger_ids=list(outcome.egress_trigger_ids),
    )
    return EscalationConfirmResponse(
        status=outcome.status,
        ticket_id=confirmed.ticket_id,
        execution_ledger_id=outcome.ledger_id,
        reason_code=outcome.egress_reason_code,
        task_response=task_response,
    )
