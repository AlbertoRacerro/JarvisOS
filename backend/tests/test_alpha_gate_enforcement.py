"""Spec 021 — server-owned external-provider execution gate tests."""

from __future__ import annotations

import json
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.modules.ai.contracts import (
    AIExternalDispatchState,
    AIPolicyMode,
    AIProviderError,
    AIProviderErrorCode,
    AIRequest,
    AIResponse,
    AIUsage,
)
from app.modules.ai.execution_types import ProviderBinding


def _isolate_and_init(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "jarvisos-alpha-gate"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.database import initialize_database

    initialize_database()


def _configure_external_allowed(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "deepseek-test-key")
    monkeypatch.setenv("GLM_API_KEY", "glm-test-key")
    from app.modules.ai.models import AISettingsUpdate
    from app.modules.ai.settings import update_ai_settings

    # External-dispatch gating is exercised on the explicit FAST_DEV opt-in (STRICT_IP is the default).
    update_ai_settings(AISettingsUpdate(paid_ai_enabled=True, monthly_api_budget_usd=100,
                                        policy_mode=AIPolicyMode.FAST_DEV))


def _all_ai_jobs() -> list[dict]:
    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        rows = connection.execute("SELECT * FROM ai_jobs ORDER BY created_at ASC").fetchall()
    return [dict(row) for row in rows]


def _seed_prior_network_attempt(
    *,
    route_class: str,
    provider_id: str,
    model_id: str,
    fallback_index: int,
) -> None:
    """Consume t1 through the real 059b lifecycle for one concrete binding."""

    from app.modules.ai.context_builder import canonical_digest
    from app.modules.ai.egress_lifecycle import (
        consume_confirmation_ticket,
        reconcile_reserved_attempt,
        start_reserved_attempt,
    )
    from app.modules.ai.egress_persistence import prepare_egress_attempt
    from app.modules.ai.egress_policy import EXTERNAL_PROVIDER_OPERATION
    from app.modules.ai.egress_service import EgressPacketMaterial
    from app.modules.ai.egress_spine import create_queued_ai_job, finalize_queued_ai_job

    material = EgressPacketMaterial(
        operation=EXTERNAL_PROVIDER_OPERATION,
        task_kind="test",
        route_class=route_class,
        provider_id=provider_id,
        model_id=model_id,
        fallback_index=fallback_index,
        prompt=f"Seed prior network attempt for {provider_id}/{model_id}.",
        context_blocks=(),
        prompt_level="S1",
        context_level="S0",
        final_level="S1",
        max_output_tokens=16,
    )
    preparation = prepare_egress_attempt(material)
    assert preparation.ticket_id is not None
    consumed = consume_confirmation_ticket(preparation.ticket_id)
    assert consumed.authorized is True
    queued = create_queued_ai_job(
        task_kind="test",
        requested_route_class=route_class,
        selected_route_class=route_class,
        provider_id=provider_id,
        model_id=model_id,
        decision_reason="seed prior 059b attempt",
        prompt_digest=canonical_digest({"prompt": material.prompt}),
    )
    start_reserved_attempt(consumed.reservation_id or "", ai_job_id=queued.ai_job_id)
    response = AIResponse(
        provider_id=provider_id,
        model_id=model_id,
        request_id=str(uuid4()),
        text="seed",
        content="seed",
        usage=AIUsage(
            provider_id=provider_id,
            model_id=model_id,
            input_tokens=1,
            output_tokens=1,
            provider_cost_estimate=0.0,
        ),
        safety_status="allowed",
    )
    finalize_queued_ai_job(
        queued.ai_job_id,
        status="success",
        response=response,
        latency_ms=1,
    )
    reconcile_reserved_attempt(
        consumed.reservation_id or "",
        ai_job_id=queued.ai_job_id,
        network_attempt=True,
        actual_input_tokens=1,
        actual_output_tokens=1,
        usage_source="actual",
    )


class _SuccessAdapter:
    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id
        self.requests: list[AIRequest] = []

    def health(self):  # pragma: no cover - not used
        ...

    def list_models(self):  # pragma: no cover - not used
        return []

    def complete(self, request: AIRequest) -> AIResponse:
        self.requests.append(request)
        return AIResponse(
            provider_id=self.provider_id,
            model_id=request.model_preference or "test-model",
            request_id=request.request_id,
            text="ok",
            content="ok",
            usage=AIUsage(
                provider_id=self.provider_id,
                model_id=request.model_preference or "test-model",
                input_tokens=1,
                output_tokens=1,
            ),
            safety_status="allowed",
            external_dispatch_state=AIExternalDispatchState.started,
        )

    def stream(self, request: AIRequest):  # pragma: no cover - not used
        raise NotImplementedError


class _RetryableAdapter(_SuccessAdapter):
    def complete(self, request: AIRequest) -> AIResponse:
        self.requests.append(request)
        return AIResponse(
            provider_id=self.provider_id,
            model_id=request.model_preference or "test-model",
            request_id=request.request_id,
            usage=AIUsage(
                provider_id=self.provider_id,
                model_id=request.model_preference or "test-model",
            ),
            safety_status="blocked",
            blocked_reason="provider_failed",
            error=AIProviderError(
                code=AIProviderErrorCode.provider_timeout,
                message="retryable timeout",
                retryable=True,
            ),
            external_dispatch_state=AIExternalDispatchState.started,
        )


def test_alpha_gate_fails_closed_on_missing_server_context() -> None:
    from app.modules.ai.budget import (
        ALPHA_EXTERNAL_PROVIDER_CALL,
        evaluate_alpha_execution_gate,
    )

    decision = evaluate_alpha_execution_gate(
        settings=None,
        provider_id="deepseek",
        operation=ALPHA_EXTERNAL_PROVIDER_CALL,
    )

    assert decision.allowed is False
    assert decision.reason == "alpha_gate_missing_context"


def test_alpha_gate_rejects_unknown_operation_and_missing_provider(monkeypatch, tmp_path) -> None:
    _isolate_and_init(monkeypatch, tmp_path)
    from app.modules.ai.budget import (
        ALPHA_EXTERNAL_PROVIDER_CALL,
        evaluate_alpha_execution_gate,
    )
    from app.modules.ai.settings import get_ai_settings

    unsupported = evaluate_alpha_execution_gate(
        settings=get_ai_settings(),
        provider_id="deepseek",
        operation="filesystem_write",
    )
    missing_provider = evaluate_alpha_execution_gate(
        settings=get_ai_settings(),
        provider_id=None,
        operation=ALPHA_EXTERNAL_PROVIDER_CALL,
    )

    assert unsupported.allowed is False
    assert unsupported.reason == "alpha_gate_unsupported_operation:filesystem_write"
    assert missing_provider.allowed is False
    assert missing_provider.reason == "alpha_gate_missing_provider"


def test_network_binding_gate_denial_prevents_adapter_call(monkeypatch, tmp_path) -> None:
    _isolate_and_init(monkeypatch, tmp_path)
    _configure_external_allowed(monkeypatch)
    from app.modules.ai import budget
    from app.modules.ai.budget import AlphaGateDecision
    from app.modules.ai.execution import run_ai_task

    calls: list[tuple[str | None, str]] = []

    def _deny(*, settings, provider_id, operation):
        assert settings is not None
        calls.append((provider_id, operation))
        return AlphaGateDecision(False, "alpha_test_denied", operation, provider_id)

    monkeypatch.setattr(budget, "evaluate_alpha_execution_gate", _deny)
    adapter = _SuccessAdapter("deepseek")
    binding = ProviderBinding(
        "external:test",
        "deepseek",
        "deepseek-v4-pro",
        True,
        128,
        execution_class="external_provider",
        context_window_tokens=8192,
    )

    outcome = run_ai_task(
        user_prompt="must not execute",
        route_class="external:test",
        max_output_tokens=64,
        adapters={"deepseek": adapter},
        bindings={"external:test": binding},
    )

    assert outcome.status == "config_error"
    assert adapter.requests == []
    assert calls == [("deepseek", budget.ALPHA_EXTERNAL_PROVIDER_CALL)]
    row = _all_ai_jobs()[-1]
    reason = json.loads(row["route_reason_json"])["decision_reason"]
    assert "alpha_test_denied" in reason


def test_offline_fixture_binding_bypasses_only_external_gate(monkeypatch, tmp_path) -> None:
    _isolate_and_init(monkeypatch, tmp_path)
    from app.modules.ai import budget
    from app.modules.ai.execution import run_ai_task

    def _unexpected_gate_call(**kwargs):
        raise AssertionError(f"offline binding reached external alpha gate: {kwargs}")

    monkeypatch.setattr(budget, "evaluate_alpha_execution_gate", _unexpected_gate_call)
    adapter = _SuccessAdapter("fixture")
    binding = ProviderBinding(
        "external:fixture",
        "fixture",
        "scripted",
        False,
        128,
        execution_class="synthetic",
        context_window_tokens=4096,
    )

    outcome = run_ai_task(
        user_prompt="offline fixture",
        route_class="external:fixture",
        adapters={"fixture": adapter},
        bindings={"external:fixture": binding},
    )

    assert outcome.status == "success"
    assert len(adapter.requests) == 1


def test_each_fallback_binding_is_gated_before_its_adapter(monkeypatch, tmp_path) -> None:
    _isolate_and_init(monkeypatch, tmp_path)
    _configure_external_allowed(monkeypatch)
    _seed_prior_network_attempt(
        route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        fallback_index=0,
    )
    _seed_prior_network_attempt(
        route_class="external:cheap",
        provider_id="glm",
        model_id="glm-5.2",
        fallback_index=1,
    )
    from app.modules.ai import budget
    from app.modules.ai.budget import AlphaGateDecision
    from app.modules.ai.execution import run_ai_task

    calls: list[str | None] = []

    def _gate(*, settings, provider_id, operation):
        assert settings is not None
        calls.append(provider_id)
        if provider_id == "glm":
            return AlphaGateDecision(False, "alpha_glm_denied", operation, provider_id)
        return AlphaGateDecision(True, "alpha_gate_open", operation, provider_id)

    monkeypatch.setattr(budget, "evaluate_alpha_execution_gate", _gate)
    first = _RetryableAdapter("deepseek")
    second = _SuccessAdapter("glm")
    baseline = len(_all_ai_jobs())

    outcome = run_ai_task(
        user_prompt="retry then deny fallback",
        route_class="external:cheap",
        max_output_tokens=64,
        adapters={"deepseek": first, "glm": second},
    )

    assert outcome.status == "config_error"
    assert len(first.requests) == 1
    assert second.requests == []
    assert calls == ["deepseek", "glm"]
    rows = _all_ai_jobs()[baseline:]
    assert [row["provider_id"] for row in rows] == ["deepseek", "glm"]
    second_reason = json.loads(rows[-1]["route_reason_json"])["decision_reason"]
    assert "alpha_glm_denied" in second_reason


@pytest.mark.parametrize(
    "field",
    ["alpha", "confirmed", "force_external_allowed", "side_effectful"],
)
def test_task_payload_fields_cannot_self_authorize(field: str) -> None:
    from app.modules.ai.models import AITaskRunRequest

    with pytest.raises(ValidationError):
        AITaskRunRequest.model_validate({"prompt": "execute", field: True})
