from __future__ import annotations

from app.modules.ai import egress_confirmation_core as _core
from app.modules.ai.egress_rejected_continuation import (
    consume_persisted_rejected_continuation,
)

ConfirmedTicketExecution = _core.ConfirmedTicketExecution
evaluate_provider_budget_gate = _core.evaluate_provider_budget_gate
_create_proposed_records_from_response = _core._create_proposed_records_from_response


def _sync_patchable_bindings() -> None:
    _core.evaluate_provider_budget_gate = evaluate_provider_budget_gate
    _core._create_proposed_records_from_response = (
        _create_proposed_records_from_response
    )


def run_confirmation_ticket(
    ticket_id: str,
    *,
    adapters=None,
    registry=None,
    policy=None,
) -> ConfirmedTicketExecution:
    """Run one 059b confirmation, including rejected 061b cleanup on access."""

    _sync_patchable_bindings()
    metadata = _core._load_ticket_metadata(ticket_id)
    if (
        metadata.ticket_state in {"expired", "revoked"}
        and metadata.continuation_authority_json is not None
    ):
        rejected = consume_persisted_rejected_continuation(ticket_id)
        outcome = _core._outcome(
            flow_id=rejected.flow_id,
            consumed=rejected.consumption,
            metadata=metadata,
            ledger_id=rejected.pause_attempt_id,
            status="config_error",
            response=None,
            error_type=rejected.consumption.reason_code,
            reason_code=rejected.consumption.reason_code,
            context_digest=None,
            blocked=True,
        )
        return ConfirmedTicketExecution(ticket_id, metadata.workspace_id, outcome)

    return _core.run_confirmation_ticket(
        ticket_id,
        adapters=adapters,
        registry=registry,
        policy=policy,
    )
