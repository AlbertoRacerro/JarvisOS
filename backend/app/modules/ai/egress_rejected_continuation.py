from __future__ import annotations

from dataclasses import dataclass

from app.modules.ai import egress_lifecycle, egress_persistence
from app.modules.ai.token_flow_confirmation_resume import (
    parse_continuation_authority,
    terminalize_rejected_continuation_confirmation_in_transaction,
)


@dataclass(frozen=True, slots=True)
class RejectedContinuationAccess:
    flow_id: str
    pause_attempt_id: str
    consumption: egress_lifecycle.EgressTicketConsumption


def consume_persisted_rejected_continuation(
    ticket_id: str,
) -> RejectedContinuationAccess:
    """Terminalize one already-rejected continuation ticket without dispatch."""

    with egress_persistence._immediate_transaction() as connection:
        row = egress_lifecycle._ticket_row(connection, ticket_id)
        if row is None:
            raise egress_persistence.EgressStateError(
                "confirmation ticket was not found"
            )
        state = str(row["ticket_state"])
        if state not in {"expired", "revoked"}:
            raise egress_persistence.EgressStateError(
                f"confirmation ticket is not rejected: {state}"
            )
        authority = parse_continuation_authority(
            row["continuation_authority_json"]
        )
        if authority is None:
            raise egress_persistence.EgressStateError(
                f"confirmation ticket is not pending: {state}"
            )
        flow = connection.execute(
            "SELECT state FROM ai_flows WHERE id = ?",
            (authority.flow_id,),
        ).fetchone()
        if flow is None or flow["state"] != "confirmation_required":
            raise egress_persistence.EgressStateError(
                f"confirmation ticket is not pending: {state}"
            )
        if state == "expired":
            reason_code = "ticket_expired"
        else:
            revocation = connection.execute(
                "SELECT revocation_reason FROM egress_confirmation_tickets WHERE id = ?",
                (ticket_id,),
            ).fetchone()
            reason_code = str(
                revocation["revocation_reason"] or "ticket_revoked"
            )
        pause_attempt_id = (
            terminalize_rejected_continuation_confirmation_in_transaction(
                connection,
                flow_id=authority.flow_id,
                ticket_id=ticket_id,
                terminal_reason=reason_code,
            )
        )
        consumption = egress_lifecycle._ticket_consumption(
            row,
            authorized=False,
            reason_code=reason_code,
            continuation_pause_attempt_id=pause_attempt_id,
        )
        return RejectedContinuationAccess(
            flow_id=authority.flow_id,
            pause_attempt_id=pause_attempt_id,
            consumption=consumption,
        )
