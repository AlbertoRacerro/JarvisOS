from __future__ import annotations

import test_token_flow_external_runtime_integration as integration
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.modules.ai import escalations
from app.modules.ai.egress_confirmation import run_confirmation_ticket
from app.modules.ai.routes import router
from app.modules.ai.token_flow_service import get_flow

initialized_database = integration.initialized_database


def test_confirmation_endpoint_resumes_paused_continuation_after_restart(
    initialized_database,
    monkeypatch,
) -> None:
    paused = integration._pause_external_continuation()
    ticket_id = str(paused.egress_ticket_id)
    flow_id = str(paused.flow_id)
    adapter = integration.ConfirmedExternalAdapter()

    def execute_ticket(requested_ticket_id: str):
        return run_confirmation_ticket(
            requested_ticket_id,
            adapters={integration.BINDING.provider_id: adapter},
        )

    monkeypatch.setattr(escalations, "run_confirmation_ticket", execute_ticket)

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post(
            "/ai/tasks/escalations/confirm",
            json={"ticket_id": ticket_id},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "success"
    assert payload["ticket_id"] == ticket_id
    assert payload["execution_ledger_id"] == payload["task_response"]["ledger_id"]
    assert payload["task_response"]["response_text"] == (
        "external alpha external omega"
    )
    assert len(adapter.requests) == 1

    flow = get_flow(flow_id)
    assert flow["state"] == "complete"
    assert flow["terminal_attempt_id"] == payload["execution_ledger_id"]
