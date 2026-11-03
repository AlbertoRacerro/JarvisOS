from __future__ import annotations

import test_token_flow_external_runtime_integration as integration

from app.core.database import open_sqlite_connection
from app.modules.ai.token_flow_service import get_flow

initialized_database = integration.initialized_database


def test_already_expired_continuation_terminalizes_on_access(
    initialized_database,
) -> None:
    from app.modules.ai.egress_confirmation import run_confirmation_ticket

    paused = integration._pause_external_continuation()
    ticket_id = str(paused.egress_ticket_id)
    flow_id = str(paused.flow_id)
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE egress_confirmation_tickets SET state = 'expired' WHERE id = ?",
            (ticket_id,),
        )
        connection.commit()
    adapter = integration.ConfirmedExternalAdapter()

    outcome = run_confirmation_ticket(
        ticket_id,
        adapters={integration.BINDING.provider_id: adapter},
    ).outcome

    assert outcome.status == "config_error"
    assert outcome.egress_reason_code == "ticket_expired"
    assert outcome.ledger_id == paused.ledger_id
    assert adapter.requests == []
    flow = get_flow(flow_id)
    assert flow["state"] == "failed_terminal"
    assert flow["terminal_reason"] == "ticket_expired"
    assert flow["terminal_attempt_id"] == paused.ledger_id


def test_already_revoked_continuation_terminalizes_on_access(
    initialized_database,
) -> None:
    from app.modules.ai.egress_confirmation import run_confirmation_ticket

    paused = integration._pause_external_continuation()
    ticket_id = str(paused.egress_ticket_id)
    flow_id = str(paused.flow_id)
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            UPDATE egress_confirmation_tickets
            SET state = 'revoked', revocation_reason = 'manual_revoke'
            WHERE id = ?
            """,
            (ticket_id,),
        )
        connection.commit()
    adapter = integration.ConfirmedExternalAdapter()

    outcome = run_confirmation_ticket(
        ticket_id,
        adapters={integration.BINDING.provider_id: adapter},
    ).outcome

    assert outcome.status == "config_error"
    assert outcome.egress_reason_code == "manual_revoke"
    assert outcome.ledger_id == paused.ledger_id
    assert adapter.requests == []
    flow = get_flow(flow_id)
    assert flow["state"] == "failed_terminal"
    assert flow["terminal_reason"] == "manual_revoke"
    assert flow["terminal_attempt_id"] == paused.ledger_id
