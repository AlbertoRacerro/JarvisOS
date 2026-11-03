from __future__ import annotations

from dataclasses import dataclass, replace

import pytest
from pydantic import ValidationError

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.contracts import (
    AIExternalDispatchState,
    AIProviderError,
    AIProviderErrorCode,
    AIRequest,
    AIResponse,
    AITaskType,
    AIUsage,
    AIUsageSource,
)
from app.modules.ai.egress_confirmation import run_confirmation_ticket
from app.modules.ai.egress_persistence import EgressStateError
from app.modules.ai.egress_policy import load_default_egress_policy
from app.modules.ai.egress_runtime import run_external_task
from app.modules.ai.models import AISettingsUpdate, EscalationConfirmRequest
from app.modules.ai.provider_registry import load_default_provider_registry, resolve_model_pricing
from app.modules.ai.settings import ensure_ai_settings, update_ai_settings


@dataclass
class _Adapter:
    provider_id: str
    response: AIResponse | None = None
    raises: Exception | None = None

    def __post_init__(self) -> None:
        self.requests: list[AIRequest] = []

    def complete(self, request: AIRequest) -> AIResponse:
        self.requests.append(request)
        if self.raises is not None:
            raise self.raises
        assert self.response is not None
        return self.response

    def health(self):  # pragma: no cover - protocol method unused
        raise NotImplementedError

    def list_models(self):  # pragma: no cover - protocol method unused
        raise NotImplementedError

    def stream(self, request: AIRequest):  # pragma: no cover - protocol method unused
        raise NotImplementedError


def _bootstrap(monkeypatch) -> None:
    initialize_database()
    ensure_ai_settings()
    update_ai_settings(
        AISettingsUpdate(
            policy_mode="FAST_DEV",
            monthly_api_budget_usd=100,
            paid_ai_enabled=True,
            provider_mode="deepseek",
        )
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-secret")
    monkeypatch.setenv("GLM_API_KEY", "test-only-secret")


def _pending_ticket(
    monkeypatch,
    *,
    route_class: str = "external:cheap",
    prompt: str = "Explain a generic pump sizing method.",
) -> str:
    _bootstrap(monkeypatch)
    registry = load_default_provider_registry()
    binding = registry.bindings[route_class]
    adapter = _Adapter(binding.provider_id)
    outcome = run_external_task(
        user_prompt=prompt,
        task_kind="general",
        selected_route_class=route_class,
        requested_route_class=route_class,
        context_blocks=None,
        max_output_tokens=64,
        adapters={binding.provider_id: adapter},
        bindings={route_class: binding},
        workspace_id=None,
        context_build_error=None,
        external_blocked_reason=None,
        task_type_for=lambda _task_kind: AITaskType.synthesis,
        registry=registry,
    )
    assert outcome.status == "validation_error"
    assert outcome.egress_reason_code == "confirmation_required"
    assert outcome.egress_ticket_id is not None
    assert outcome.flow_id is not None
    assert adapter.requests == []
    return outcome.egress_ticket_id


def _success_response(provider_id: str = "deepseek", model_id: str = "deepseek-v4-pro") -> AIResponse:
    pricing = resolve_model_pricing(load_default_provider_registry(), provider_id, model_id)
    return AIResponse(
        provider_id=provider_id,
        model_id=model_id,
        request_id="test-request",
        text="confirmed answer",
        content="confirmed answer",
        usage=AIUsage(
            provider_id=provider_id,
            model_id=model_id,
            input_tokens=11,
            output_tokens=7,
            usage_source=AIUsageSource.actual,
            provider_cost_estimate=(11 * pricing.input_usd_per_1m_tokens + 7 * pricing.output_usd_per_1m_tokens) / 1_000_000,
            currency="USD",
        ),
        finish_reason="stop",
        safety_status="allowed",
        external_dispatch_state=AIExternalDispatchState.started,
    )


def test_confirmation_request_accepts_only_ticket_id() -> None:
    request = EscalationConfirmRequest.model_validate({"ticket_id": "ticket-1"})
    assert request.ticket_id == "ticket-1"
    for payload in (
        {"proposal": {"outbound_text": "client-owned"}},
        {"ticket_id": "ticket-1", "task_kind": "general"},
        {"ticket_id": "ticket-1", "outbound_text": "replacement"},
    ):
        with pytest.raises(ValidationError):
            EscalationConfirmRequest.model_validate(payload)


def test_unrelated_malformed_paused_flow_does_not_block_exact_ticket(monkeypatch) -> None:
    unrelated_ticket = _pending_ticket(
        monkeypatch,
        prompt="Explain a generic valve sizing method.",
    )
    exact_ticket = _pending_ticket(
        monkeypatch,
        prompt="Explain a generic compressor sizing method.",
    )
    with open_sqlite_connection() as connection:
        unrelated_attempt = connection.execute(
            """
            SELECT attempt.id
            FROM ai_jobs AS attempt
            WHERE json_valid(attempt.route_reason_json)
              AND json_extract(attempt.route_reason_json, '$.egress_ticket_id') = ?
            """,
            (unrelated_ticket,),
        ).fetchone()
        assert unrelated_attempt is not None
        connection.execute(
            "UPDATE ai_jobs SET route_reason_json = ? WHERE id = ?",
            ("{malformed", unrelated_attempt["id"]),
        )
        connection.commit()
    adapter = _Adapter("deepseek", response=_success_response())

    outcome = run_confirmation_ticket(
        exact_ticket,
        adapters={"deepseek": adapter},
    ).outcome

    assert outcome.status == "success"
    assert outcome.egress_ticket_id == exact_ticket
    assert len(adapter.requests) == 1


def test_ticket_confirmation_executes_exact_binding_once(monkeypatch) -> None:
    ticket_id = _pending_ticket(monkeypatch)
    adapter = _Adapter("deepseek", response=_success_response())

    confirmed = run_confirmation_ticket(
        ticket_id,
        adapters={"deepseek": adapter},
    )

    outcome = confirmed.outcome
    assert outcome.status == "success"
    assert outcome.response is not None
    assert outcome.response.text == "confirmed answer"
    assert outcome.egress_ticket_id == ticket_id
    assert outcome.egress_reason_code == "ticket_consumed"
    assert outcome.egress_trigger_ids == ("t1",)
    assert outcome.flow_id is not None
    assert len(adapter.requests) == 1
    request = adapter.requests[0]
    assert request.prompt == "Explain a generic pump sizing method."
    assert request.model_preference == "deepseek-v4-pro"
    assert request.max_output_tokens == 64
    with open_sqlite_connection() as connection:
        job = connection.execute(
            "SELECT status, provider_id, model_id, flow_id, execution_class, "
            "adapter_invoked, external_dispatch_state, accounting_basis, "
            "accounted_provider_spend_usd_decimal FROM ai_jobs WHERE id = ?",
            (outcome.ledger_id,),
        ).fetchone()
        attempt = connection.execute(
            "SELECT network_attempt, provider_id, model_id, fallback_index FROM egress_attempts"
        ).fetchone()
        reservation = connection.execute(
            "SELECT state FROM egress_budget_reservations WHERE id = ?",
            (outcome.egress_reservation_id,),
        ).fetchone()
        flow = connection.execute(
            "SELECT state, terminal_attempt_id, external_provider_spend_usd_decimal, "
            "ordered_attempt_ids_json FROM ai_flows WHERE id = ?",
            (outcome.flow_id,),
        ).fetchone()
    assert job["status"] == "success"
    assert job["provider_id"] == "deepseek"
    assert job["model_id"] == "deepseek-v4-pro"
    assert job["flow_id"] == outcome.flow_id
    assert job["execution_class"] == "external_provider"
    assert job["adapter_invoked"] == 1
    assert job["external_dispatch_state"] == "started"
    assert job["accounting_basis"] == "provider_exact"
    assert float(job["accounted_provider_spend_usd_decimal"]) > 0
    assert tuple(attempt) == (1, "deepseek", "deepseek-v4-pro", 0)
    assert reservation["state"] == "reconciled"
    assert flow["state"] == "complete"
    assert flow["terminal_attempt_id"] == outcome.ledger_id
    assert float(flow["external_provider_spend_usd_decimal"]) > 0
    assert outcome.ledger_id in flow["ordered_attempt_ids_json"]


def test_ticket_confirmation_is_one_shot_and_second_call_makes_no_provider_call(
    monkeypatch,
) -> None:
    ticket_id = _pending_ticket(monkeypatch)
    first = _Adapter("deepseek", response=_success_response())
    run_confirmation_ticket(ticket_id, adapters={"deepseek": first})
    second = _Adapter("deepseek", response=_success_response())

    with pytest.raises(EgressStateError, match="not pending: consumed"):
        run_confirmation_ticket(ticket_id, adapters={"deepseek": second})

    assert len(first.requests) == 1
    assert second.requests == []
    with open_sqlite_connection() as connection:
        attempts = connection.execute("SELECT COUNT(*) AS count FROM egress_attempts").fetchone()["count"]
    assert attempts == 1


def test_gate_change_after_consumption_releases_without_network(monkeypatch) -> None:
    ticket_id = _pending_ticket(monkeypatch)
    import app.modules.ai.egress_confirmation as confirmation
    from app.modules.ai.budget import ProviderBudgetGate

    monkeypatch.setattr(
        confirmation,
        "evaluate_provider_budget_gate",
        lambda settings, provider_id: ProviderBudgetGate(False, "gate_closed_after_ticket", provider_id),
    )
    adapter = _Adapter("deepseek", response=_success_response())

    outcome = run_confirmation_ticket(ticket_id, adapters={"deepseek": adapter}).outcome

    assert outcome.status == "config_error"
    assert outcome.decision.blocked_reason == "gate_closed_after_ticket"
    assert adapter.requests == []
    with open_sqlite_connection() as connection:
        reservation = connection.execute(
            "SELECT state, reconciliation_status FROM egress_budget_reservations WHERE id = ?",
            (outcome.egress_reservation_id,),
        ).fetchone()
        attempt = connection.execute(
            "SELECT network_attempt, actual_input_tokens, actual_output_tokens, actual_cost_usd FROM egress_attempts"
        ).fetchone()
        job = connection.execute(
            "SELECT flow_id, execution_class, adapter_invoked, "
            "external_dispatch_state, accounting_basis FROM ai_jobs WHERE id = ?",
            (outcome.ledger_id,),
        ).fetchone()
        flow = connection.execute(
            "SELECT state, terminal_attempt_id FROM ai_flows WHERE id = ?",
            (outcome.flow_id,),
        ).fetchone()
    assert tuple(reservation) == ("released", "not_sent")
    assert tuple(attempt) == (0, 0, 0, 0.0)
    assert job["flow_id"] == outcome.flow_id
    assert job["execution_class"] == "external_provider"
    assert job["adapter_invoked"] == 0
    assert job["external_dispatch_state"] == "not_started"
    assert job["accounting_basis"] == "external_not_sent"
    assert flow["state"] == "failed_terminal"
    assert flow["terminal_attempt_id"] == outcome.ledger_id


def test_retryable_confirmed_error_does_not_open_fallback(monkeypatch) -> None:
    ticket_id = _pending_ticket(monkeypatch)
    retryable = AIResponse(
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        request_id="test-request",
        usage=AIUsage(provider_id="deepseek", model_id="deepseek-v4-pro"),
        safety_status="blocked",
        blocked_reason="provider_failed",
        error=AIProviderError(
            code=AIProviderErrorCode.provider_timeout,
            message="timeout",
            retryable=True,
        ),
    )
    deepseek = _Adapter("deepseek", response=retryable)
    glm = _Adapter("glm", response=_success_response("glm", "glm-5.2"))

    outcome = run_confirmation_ticket(
        ticket_id,
        adapters={"deepseek": deepseek, "glm": glm},
    ).outcome

    assert outcome.status == "provider_error"
    assert len(deepseek.requests) == 1
    assert glm.requests == []
    with open_sqlite_connection() as connection:
        providers = [
            row["provider_id"]
            for row in connection.execute("SELECT provider_id FROM egress_attempts ORDER BY created_at")
        ]
    assert providers == ["deepseek"]


def test_response_binding_mismatch_is_reconciled_as_network_attempt(monkeypatch) -> None:
    ticket_id = _pending_ticket(monkeypatch)
    adapter = _Adapter("deepseek", response=_success_response("glm", "glm-5.2"))

    outcome = run_confirmation_ticket(ticket_id, adapters={"deepseek": adapter}).outcome

    assert outcome.status == "provider_error"
    assert outcome.egress_reason_code == "response_binding_mismatch"
    assert len(adapter.requests) == 1
    with open_sqlite_connection() as connection:
        job = connection.execute(
            "SELECT status, error_type FROM ai_jobs WHERE id = ?",
            (outcome.ledger_id,),
        ).fetchone()
        attempt = connection.execute("SELECT network_attempt, reconciliation_status FROM egress_attempts").fetchone()
    assert job["status"] == "provider_error"
    assert job["error_type"] == "EgressSpineStateError"
    assert attempt["network_attempt"] == 1
    assert attempt["reconciliation_status"] == "conservative_missing_usage"


def test_length_stopped_confirmed_ticket_skips_record_capture(monkeypatch) -> None:
    ticket_id = _pending_ticket(monkeypatch)
    response = _success_response().model_copy(update={"finish_reason": "length"})
    adapter = _Adapter("deepseek", response=response)
    import app.modules.ai.egress_confirmation as confirmation

    def fail_capture(**_kwargs):
        pytest.fail("truncated confirmed output must not create proposed records")

    monkeypatch.setattr(confirmation, "_create_proposed_records_from_response", fail_capture)

    outcome = run_confirmation_ticket(ticket_id, adapters={"deepseek": adapter}).outcome

    assert outcome.status == "success"
    assert outcome.response is not None
    assert outcome.response.finish_reason == "length"
    assert outcome.proposed_record_ids is None
    assert outcome.records_parse_error is None
    with open_sqlite_connection() as connection:
        flow = connection.execute(
            "SELECT state, terminal_reason, terminal_attempt_id FROM ai_flows WHERE id = ?",
            (outcome.flow_id,),
        ).fetchone()
    assert tuple(flow) == ("partial_terminal", "output_length_limit", outcome.ledger_id)


def test_expired_ticket_terminalizes_paused_flow_without_adapter_call(monkeypatch) -> None:
    ticket_id = _pending_ticket(monkeypatch)
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE egress_confirmation_tickets SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", ticket_id),
        )
        connection.commit()
    adapter = _Adapter("deepseek", response=_success_response())

    outcome = run_confirmation_ticket(ticket_id, adapters={"deepseek": adapter}).outcome

    assert outcome.status == "config_error"
    assert outcome.egress_reason_code == "ticket_expired"
    assert outcome.flow_id is not None
    assert adapter.requests == []
    with open_sqlite_connection() as connection:
        ticket = connection.execute(
            "SELECT state FROM egress_confirmation_tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()
        job = connection.execute(
            "SELECT flow_id, execution_class, adapter_invoked, "
            "external_dispatch_state, accounting_basis, "
            "accounted_provider_spend_usd_decimal FROM ai_jobs WHERE id = ?",
            (outcome.ledger_id,),
        ).fetchone()
        flow = connection.execute(
            "SELECT state, terminal_reason, terminal_attempt_id FROM ai_flows WHERE id = ?",
            (outcome.flow_id,),
        ).fetchone()
    assert ticket["state"] == "expired"
    assert job["flow_id"] == outcome.flow_id
    assert job["execution_class"] == "external_provider"
    assert job["adapter_invoked"] == 0
    assert job["external_dispatch_state"] == "not_started"
    assert job["accounting_basis"] == "external_not_sent"
    assert job["accounted_provider_spend_usd_decimal"] == "0"
    assert tuple(flow) == ("failed_terminal", "ticket_expired", outcome.ledger_id)


def test_policy_drift_revokes_ticket_and_terminalizes_paused_flow(monkeypatch) -> None:
    ticket_id = _pending_ticket(monkeypatch)
    policy = load_default_egress_policy()
    drifted_policy = replace(
        policy,
        max_prompt_chars=policy.max_prompt_chars + 1,
        config_digest="policy-drift-test",
    )
    adapter = _Adapter("deepseek", response=_success_response())

    outcome = run_confirmation_ticket(
        ticket_id,
        adapters={"deepseek": adapter},
        policy=drifted_policy,
    ).outcome

    assert outcome.status == "config_error"
    assert outcome.egress_reason_code == "ticket_binding_or_policy_drift"
    assert adapter.requests == []
    with open_sqlite_connection() as connection:
        ticket = connection.execute(
            "SELECT state, revocation_reason FROM egress_confirmation_tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()
        job = connection.execute(
            "SELECT execution_class, adapter_invoked, external_dispatch_state, "
            "accounting_basis FROM ai_jobs WHERE id = ?",
            (outcome.ledger_id,),
        ).fetchone()
        flow = connection.execute(
            "SELECT state, terminal_reason, terminal_attempt_id FROM ai_flows WHERE id = ?",
            (outcome.flow_id,),
        ).fetchone()
    assert tuple(ticket) == ("revoked", "ticket_binding_or_policy_drift")
    assert tuple(job) == (
        "external_provider",
        0,
        "not_started",
        "external_not_sent",
    )
    assert tuple(flow) == (
        "failed_terminal",
        "ticket_binding_or_policy_drift",
        outcome.ledger_id,
    )


def _assert_registry_drift_terminalized(
    ticket_id: str, outcome, adapter: _Adapter
) -> None:
    assert outcome.status == "config_error"
    assert outcome.egress_reason_code == "ticket_binding_or_policy_drift"
    assert outcome.flow_id is not None
    assert adapter.requests == []
    with open_sqlite_connection() as connection:
        ticket = connection.execute(
            "SELECT state, revocation_reason "
            "FROM egress_confirmation_tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()
        decision = connection.execute(
            "SELECT decision.pricing_version "
            "FROM egress_confirmation_tickets AS ticket "
            "JOIN egress_decisions AS decision ON decision.id = ticket.decision_id "
            "WHERE ticket.id = ?",
            (ticket_id,),
        ).fetchone()
        job = connection.execute(
            "SELECT flow_id, execution_class, adapter_invoked, "
            "external_dispatch_state, accounting_basis, "
            "accounted_provider_spend_usd_decimal, pricing_version "
            "FROM ai_jobs WHERE id = ?",
            (outcome.ledger_id,),
        ).fetchone()
        flow = connection.execute(
            "SELECT state, terminal_reason, terminal_attempt_id, "
            "external_provider_spend_usd_decimal "
            "FROM ai_flows WHERE id = ?",
            (outcome.flow_id,),
        ).fetchone()
    assert tuple(ticket) == ("revoked", "ticket_binding_or_policy_drift")
    assert job["flow_id"] == outcome.flow_id
    assert job["execution_class"] == "external_provider"
    assert job["adapter_invoked"] == 0
    assert job["external_dispatch_state"] == "not_started"
    assert job["accounting_basis"] == "external_not_sent"
    assert job["accounted_provider_spend_usd_decimal"] == "0"
    assert job["pricing_version"] == decision["pricing_version"]
    assert tuple(flow) == (
        "failed_terminal",
        "ticket_binding_or_policy_drift",
        outcome.ledger_id,
        "0",
    )


def test_disabled_provider_revokes_ticket_and_terminalizes_paused_flow(
    monkeypatch,
) -> None:
    ticket_id = _pending_ticket(monkeypatch)
    registry = load_default_provider_registry()
    providers = dict(registry.providers)
    providers["deepseek"] = replace(providers["deepseek"], enabled=False)
    drifted_registry = replace(
        registry,
        providers=providers,
        bindings={
            route: binding
            for route, binding in registry.bindings.items()
            if binding.provider_id != "deepseek"
        },
    )
    adapter = _Adapter("deepseek", response=_success_response())

    outcome = run_confirmation_ticket(
        ticket_id,
        adapters={"deepseek": adapter},
        registry=drifted_registry,
    ).outcome

    _assert_registry_drift_terminalized(ticket_id, outcome, adapter)


def test_removed_model_revokes_ticket_and_terminalizes_paused_flow(
    monkeypatch,
) -> None:
    ticket_id = _pending_ticket(monkeypatch)
    registry = load_default_provider_registry()
    models = dict(registry.models)
    models.pop(("deepseek", "deepseek-v4-pro"))
    drifted_registry = replace(
        registry,
        models=models,
        bindings={
            route: binding
            for route, binding in registry.bindings.items()
            if (binding.provider_id, binding.model_id)
            != ("deepseek", "deepseek-v4-pro")
        },
    )
    adapter = _Adapter("deepseek", response=_success_response())

    outcome = run_confirmation_ticket(
        ticket_id,
        adapters={"deepseek": adapter},
        registry=drifted_registry,
    ).outcome

    _assert_registry_drift_terminalized(ticket_id, outcome, adapter)
