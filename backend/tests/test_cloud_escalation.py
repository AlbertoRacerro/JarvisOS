from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi import HTTPException

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai import cloud_escalation
from app.modules.ai.cloud_catalog import load_catalog, select_candidate
from app.modules.ai.cloud_escalation import CloudEscalationError, CloudEscalationRequest, create_cloud_escalation
from app.modules.ai.contracts import RoutingDecision
from app.modules.ai.execution import AiTaskOutcome
from app.modules.ai.models import EscalationConfirmRequest
from app.modules.ai.routes import confirm_ai_task_escalation
from app.modules.ai.sensitivity import approve_sanitized_derivative, create_sanitized_derivative
from app.modules.ai.sensitivity_models import SanitizedDerivativeCreate
from app.modules.events.service import utc_now


@pytest.fixture(autouse=True)
def fresh_test_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    config = load_catalog()
    today = datetime.now(UTC).date()
    fresh = replace(config, candidates=tuple((route, tier, quality, "test:qualification", today, source)
                                             for route, tier, quality, _, _, source in config.candidates))
    monkeypatch.setattr(cloud_escalation, "load_catalog", lambda: fresh)


def _source() -> tuple[str, str, str, str]:
    initialize_database()
    workspace_id, thread_id, interaction_id, record_id = "cloud-test", str(uuid4()), str(uuid4()), str(uuid4())
    flow_id, job_id, now = str(uuid4()), str(uuid4()), utc_now()
    with open_sqlite_connection() as connection:
        connection.execute("INSERT INTO workspaces (id, name, slug, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                           (workspace_id, workspace_id, workspace_id, now, now))
        connection.execute("INSERT INTO decisions (id, workspace_id, title, decision_text, status, created_at, updated_at) VALUES (?, ?, 'Local source', 'Proprietary design context', 'accepted', ?, ?)",
                           (record_id, workspace_id, now, now))
        connection.execute("INSERT INTO ai_threads (id, workspace_id, created_at, last_activity_at) VALUES (?, ?, ?, ?)",
                           (thread_id, workspace_id, now, now))
        connection.execute("INSERT INTO ai_flows (id, workspace_id, task_kind, state, created_at, updated_at) VALUES (?, ?, 'general', 'complete', ?, ?)",
                           (flow_id, workspace_id, now, now))
        connection.execute("INSERT INTO ai_jobs (id, created_at, status, task_kind, selected_route_class, provider_id, model_id, route_reason_json, flow_id, execution_class) VALUES (?, ?, 'success', 'general', 'local:llamacpp', 'local_llamacpp', 'gemma', '{}', ?, 'local_compute')",
                           (job_id, now, flow_id))
        connection.execute("INSERT INTO ai_thread_interactions (id, thread_id, request_id, request_digest, interaction_index, user_text, flow_id, persistence_state, created_at, updated_at) VALUES (?, ?, 'local-1', 'digest', 0, 'local question', ?, 'captured', ?, ?)",
                           (interaction_id, thread_id, flow_id, now, now))
        connection.commit()
    return workspace_id, thread_id, interaction_id, record_id


def _derivative(workspace_id: str, record_id: str) -> str:
    drafted = create_sanitized_derivative(SanitizedDerivativeCreate(
        workspace_id=workspace_id, source_refs=[f"decision:{record_id}"],
        content="Explain a generic heat balance with symbolic values only.",
        effective_level="S1", transformations=["removed project identity and numeric values"],
    ))
    return approve_sanitized_derivative(workspace_id, drafted.id).id


def test_catalog_cheapest_adequate_and_unknown_family() -> None:
    catalog = cloud_escalation.load_catalog()
    general = select_candidate(catalog, task_family="general", derivative_content="generic")
    engineering = select_candidate(catalog, task_family="engineering", derivative_content="generic")
    assert general.binding.model_id == "deepseek-flash"
    assert engineering.binding.model_id == "deepseek-v4-flash-0731"
    with pytest.raises(ValueError, match="unqualified task family"):
        select_candidate(catalog, task_family="secret_design", derivative_content="generic")
    stale = replace(catalog, candidates=tuple((route, tier, quality, evidence, date(2026, 1, 1), source)
                                              for route, tier, quality, evidence, _, source in catalog.candidates))
    with pytest.raises(ValueError, match="no eligible model"):
        select_candidate(stale, task_family="general", derivative_content="generic")
    unqualified = replace(catalog, candidates=tuple((route, tier, quality, None, reviewed, source)
                                                    for route, tier, quality, _, reviewed, source in catalog.candidates))
    with pytest.raises(ValueError, match="no eligible model"):
        select_candidate(unqualified, task_family="general", derivative_content="generic")


def test_interaction_escalation_draft_is_read_only_and_infers_family() -> None:
    workspace_id, thread_id, interaction_id, _ = _source()
    draft = cloud_escalation.draft_interaction_escalation(
        workspace_id=workspace_id, thread_id=thread_id, interaction_id=interaction_id,
    )
    assert draft.status == "ready"
    assert draft.text == "local question"
    assert draft.text_digest == cloud_escalation.text_digest(draft.text)
    assert draft.task_family == "general"
    assert draft.task_family_inferred is True
    assert draft.candidate is not None
    override = cloud_escalation.draft_interaction_escalation(
        workspace_id=workspace_id, thread_id=thread_id, interaction_id=interaction_id,
        task_family="engineering",
    )
    assert override.task_family == "engineering"
    assert override.task_family_inferred is False
    edited = cloud_escalation.draft_interaction_escalation(
        workspace_id=workspace_id, thread_id=thread_id, interaction_id=interaction_id,
        text="Explain a generic heat balance.",
    )
    assert edited.status == "ready"
    assert edited.text == "Explain a generic heat balance."
    assert edited.task_family == "engineering"
    with open_sqlite_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM sanitized_derivatives").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM cloud_escalations").fetchone()[0] == 0


def test_interaction_escalation_approval_binds_derivative_to_source_and_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace_id, thread_id, interaction_id, _ = _source()
    text = "Explain a generic heat balance with symbolic values only."
    captured: list[CloudEscalationRequest] = []

    def fake_create(*, workspace_id: str, thread_id: str, payload: CloudEscalationRequest):
        captured.append(payload)
        return payload

    monkeypatch.setattr(cloud_escalation, "create_cloud_escalation", fake_create)
    result = cloud_escalation.escalate_interaction(
        workspace_id=workspace_id, thread_id=thread_id, interaction_id=interaction_id,
        payload=cloud_escalation.EscalationApproval(
            text=text, text_digest=cloud_escalation.text_digest(text),
        ),
    )
    assert result == captured[0]
    derivative = cloud_escalation.revalidate_sanitized_derivative(workspace_id, result.derivative_id)
    assert derivative.status == "approved"
    assert derivative.content == text
    assert derivative.source_refs == [f"interaction:{interaction_id}"]
    with pytest.raises(CloudEscalationError, match="changed after it was shown"):
        cloud_escalation.escalate_interaction(
            workspace_id=workspace_id, thread_id=thread_id, interaction_id=interaction_id,
            payload=cloud_escalation.EscalationApproval(text=text + " changed", text_digest=cloud_escalation.text_digest(text)),
        )
    assert len(captured) == 1


def test_interaction_escalation_refuses_secrets_protected_text_and_foreign_sources() -> None:
    workspace_id, thread_id, interaction_id, _ = _source()
    for text, code in (("api_key=synthetic-test-value", "secret_detected"),
                       ("Explain my proprietary unpublished design", "protected_ip")):
        with pytest.raises(CloudEscalationError) as refused:
            cloud_escalation.escalate_interaction(
                workspace_id=workspace_id, thread_id=thread_id, interaction_id=interaction_id,
                payload=cloud_escalation.EscalationApproval(text=text, text_digest=cloud_escalation.text_digest(text)),
            )
        assert refused.value.code == code
    with pytest.raises(CloudEscalationError, match="completed local source"):
        cloud_escalation.draft_interaction_escalation(
            workspace_id=workspace_id, thread_id=str(uuid4()), interaction_id=interaction_id,
        )
    with open_sqlite_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM sanitized_derivatives").fetchone()[0] == 0


def test_interaction_read_exposes_recorded_usage_and_tool_activity() -> None:
    from app.modules.ai.thread_service import get_thread

    workspace_id, thread_id, interaction_id, _ = _source()
    with open_sqlite_connection() as connection:
        flow_id = connection.execute(
            "SELECT flow_id FROM ai_thread_interactions WHERE id = ?", (interaction_id,)
        ).fetchone()[0]
        job_id = connection.execute("SELECT id FROM ai_jobs WHERE flow_id = ?", (flow_id,)).fetchone()[0]
        connection.execute("UPDATE ai_flows SET terminal_attempt_id = ? WHERE id = ?", (job_id, flow_id))
        connection.execute(
            "UPDATE ai_jobs SET input_tokens = 31, output_tokens = 9, cost_estimate = 0.002, "
            "usage_source = 'actual', latency_ms = 850 WHERE id = ?", (job_id,)
        )
        connection.commit()
    completed = get_thread(workspace_id=workspace_id, thread_id=thread_id).interactions[0]
    assert completed.provider_id == "local_llamacpp"
    assert (completed.input_tokens, completed.output_tokens, completed.latency_ms) == (31, 9, 850)
    assert completed.cost_estimate_usd == pytest.approx(0.002)
    assert completed.activity is None

    with open_sqlite_connection() as connection:
        connection.execute("UPDATE ai_flows SET state = 'running' WHERE id = ?", (flow_id,))
        connection.execute(
            "INSERT INTO events (id, workspace_id, event_type, actor, target_type, payload, created_at) "
            "VALUES (?, ?, 'hermes.tool_result', 'jarvis', 'agent_tool_call', ?, ?)",
            (str(uuid4()), workspace_id,
             '{"interaction_id":"' + interaction_id + '","tool_name":"jarvis_process_read"}', utc_now()),
        )
        connection.commit()
    running = get_thread(workspace_id=workspace_id, thread_id=thread_id).interactions[0]
    assert running.activity == "Reading flowsheet…"
    assert running.elapsed_ms is None


def test_unapproved_derivative_does_not_enter_egress(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id, thread_id, interaction_id, record_id = _source()
    drafted = create_sanitized_derivative(SanitizedDerivativeCreate(
        workspace_id=workspace_id, source_refs=[f"decision:{record_id}"], content="generic",
        effective_level="S1", transformations=["removed project details"],
    ))
    monkeypatch.setattr(cloud_escalation, "run_ai_task", lambda **_: pytest.fail("provider path reached"))
    with pytest.raises(CloudEscalationError, match="approved"):
        create_cloud_escalation(workspace_id=workspace_id, thread_id=thread_id,
                                payload=CloudEscalationRequest(request_id="cloud-1", source_interaction_id=interaction_id,
                                                               derivative_id=drafted.id, task_family="engineering"))
    with open_sqlite_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM cloud_escalations").fetchone()[0] == 0


def test_protected_unclassified_prompt_stops_before_network(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.modules.ai.contracts import AIPolicyMode
    from app.modules.ai.execution import run_ai_task
    from app.modules.ai.models import AISettingsUpdate
    from app.modules.ai.settings import update_ai_settings

    workspace_id, _, _, _ = _source()
    monkeypatch.setenv("SCALEWAY_API_KEY", "synthetic-test-only")
    update_ai_settings(AISettingsUpdate(
        policy_mode=AIPolicyMode.STRICT_IP, provider_mode="scaleway",
        paid_ai_enabled=True, monthly_api_budget_usd=1, scaleway_enabled=True,
        scaleway_monthly_token_cap=5000, scaleway_hard_stop_token_cap=5000,
    ))
    result = run_ai_task(
        user_prompt="Analyze this proprietary unpublished design without classification.",
        task_kind="engineering", route_class="external:scaleway",
        max_output_tokens=64, workspace_id=workspace_id,
    )
    assert result.status == "validation_error"
    assert result.decision.blocked_reason == "prompt_sanitization_required"
    with open_sqlite_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM egress_attempts WHERE network_attempt = 1").fetchone()[0] == 0


def test_approved_derivative_zero_budget_fails_without_network_and_is_idempotent() -> None:
    workspace_id, thread_id, interaction_id, record_id = _source()
    derivative_id = _derivative(workspace_id, record_id)
    payload = CloudEscalationRequest(request_id="cloud-1", source_interaction_id=interaction_id,
                                     derivative_id=derivative_id, task_family="engineering")
    first = create_cloud_escalation(workspace_id=workspace_id, thread_id=thread_id, payload=payload)
    again = create_cloud_escalation(workspace_id=workspace_id, thread_id=thread_id, payload=payload)
    assert first.id == again.id
    assert first.state == "failed"
    assert first.accounted_cost_usd == "0"
    assert first.response_text is None
    assert first.created_at
    with open_sqlite_connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM egress_attempts WHERE network_attempt = 1").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM cloud_escalations").fetchone()[0] == 1


def test_thread_cap_counts_concurrent_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    workspace_id, thread_id, interaction_id, record_id = _source()
    derivative_id = _derivative(workspace_id, record_id)
    catalog = cloud_escalation.load_catalog()
    candidate = select_candidate(catalog, task_family="engineering", derivative_content="generic")
    captured: list[dict] = []

    def fake_run(**kwargs):
        captured.append(kwargs)
        return AiTaskOutcome(
            status="validation_error", ledger_id=str(uuid4()), selected_route_class=candidate.binding.route_class,
            decision=RoutingDecision(provider_id=candidate.binding.provider_id, model_id=candidate.binding.model_id),
            egress_ticket_id=str(uuid4()), flow_id=str(uuid4()),
        )

    monkeypatch.setattr(cloud_escalation, "run_ai_task", fake_run)
    monkeypatch.setattr(cloud_escalation, "load_catalog", lambda: replace(catalog, max_thread_usd=Decimal("0.001")))
    first = create_cloud_escalation(workspace_id=workspace_id, thread_id=thread_id,
                                    payload=CloudEscalationRequest(request_id="cloud-1", source_interaction_id=interaction_id,
                                                                   derivative_id=derivative_id, task_family="engineering"))
    assert first.state == "confirmation_required"
    assert captured[0]["user_prompt"] == cloud_escalation._RAW_PROMPT
    assert captured[0]["context_blocks"] == [{
        "source": f"derivative:{derivative_id}", "id": derivative_id,
        "content": "Explain a generic heat balance with symbolic values only.",
    }]
    with pytest.raises(CloudEscalationError, match="thread budget exceeded"):
        create_cloud_escalation(workspace_id=workspace_id, thread_id=thread_id,
                                payload=CloudEscalationRequest(request_id="cloud-2", source_interaction_id=interaction_id,
                                                               derivative_id=derivative_id, task_family="engineering"))
    assert len(captured) == 1
    assert first.ticket_id is not None
    with pytest.raises(HTTPException) as blocked:
        confirm_ai_task_escalation(EscalationConfirmRequest(ticket_id=first.ticket_id))
    assert blocked.value.status_code == 409
