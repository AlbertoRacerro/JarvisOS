from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.budget import ProviderBudgetGate
from app.modules.ai.contracts import (
    AIExternalDispatchState,
    AIRequest,
    AIResponse,
    AITaskType,
    AIUsage,
    AIUsageSource,
)
from app.modules.ai.egress_confirmation import run_confirmation_ticket
from app.modules.ai.egress_runtime import run_external_task
from app.modules.ai.models import AISettingsUpdate
from app.modules.ai.provider_registry import parse_provider_registry
from app.modules.ai.settings import ensure_ai_settings, update_ai_settings

MODEL_ID = "gemma-4-26b-a4b-it"


@dataclass
class _ScalewayAdapter:
    provider_id: str = "scaleway"

    def __post_init__(self) -> None:
        self.requests: list[AIRequest] = []

    def complete(self, request: AIRequest) -> AIResponse:
        self.requests.append(request)
        return AIResponse(
            provider_id="scaleway",
            model_id=MODEL_ID,
            request_id=request.request_id,
            correlation_id=request.correlation_id,
            text="registered scaleway response",
            content="registered scaleway response",
            usage=AIUsage(
                provider_id="scaleway",
                model_id=MODEL_ID,
                input_tokens=13,
                output_tokens=3,
                usage_source=AIUsageSource.actual,
                provider_cost_estimate=(13 * 1.25 + 3 * 2.5) / 1_000_000,
                currency="USD",
            ),
            finish_reason="stop",
            safety_status="allowed",
            external_dispatch_state=AIExternalDispatchState.started,
        )

    def health(self):  # pragma: no cover - protocol method unused
        raise NotImplementedError

    def list_models(self):  # pragma: no cover - protocol method unused
        raise NotImplementedError

    def stream(self, request: AIRequest):  # pragma: no cover - protocol method unused
        raise NotImplementedError


def _registry():
    return parse_provider_registry(
        {
            "version": 1,
            "providers": {
                "scaleway": {
                    "kind": "scaleway",
                    "execution_class": "external_provider",
                    "enabled": True,
                    "requires_network": True,
                    "base_url": "https://api.scaleway.test/v1",
                    "api_key_ref": "env:SCALEWAY_API_KEY",
                    "models": {
                        MODEL_ID: {
                            "provider_model_name": MODEL_ID,
                            "route_classes": ["external:scaleway"],
                            "context_window_tokens": 8192,
                            "max_output_tokens": 256,
                            "pricing": {
                                "currency": "USD",
                                "input_usd_per_1m_tokens": 1.25,
                                "output_usd_per_1m_tokens": 2.5,
                                "pricing_version": "scaleway-test-v1",
                                "pricing_effective_at": "2026-07-18T00:00:00Z",
                            },
                        }
                    },
                }
            },
            "fallback_chains": {},
        }
    )


def test_registered_scaleway_attempt_uses_external_flow_contract(monkeypatch) -> None:
    initialize_database()
    ensure_ai_settings()
    update_ai_settings(
        AISettingsUpdate(
            policy_mode="FAST_DEV",
            monthly_api_budget_usd=100,
            paid_ai_enabled=True,
            provider_mode="scaleway",
            scaleway_enabled=True,
            scaleway_smoke_test_enabled=True,
            scaleway_live_smoke_test_enabled=True,
        )
    )
    monkeypatch.setenv("SCALEWAY_API_KEY", "test-only-secret")
    allowed = lambda settings, provider_id: ProviderBudgetGate(  # noqa: E731
        True, None, provider_id
    )
    import app.modules.ai.egress_confirmation as confirmation
    import app.modules.ai.egress_runtime as runtime

    monkeypatch.setattr(runtime, "evaluate_provider_budget_gate", allowed)
    monkeypatch.setattr(confirmation, "evaluate_provider_budget_gate", allowed)

    registry = _registry()
    binding = registry.bindings["external:scaleway"]
    adapter = _ScalewayAdapter()
    paused = run_external_task(
        user_prompt="Explain a public engineering calculation.",
        task_kind="general",
        selected_route_class="external:scaleway",
        requested_route_class="external:scaleway",
        context_blocks=None,
        max_output_tokens=64,
        adapters={"scaleway": adapter},
        bindings={"external:scaleway": binding},
        workspace_id=None,
        context_build_error=None,
        external_blocked_reason=None,
        task_type_for=lambda _task_kind: AITaskType.synthesis,
        registry=registry,
    )
    assert paused.status == "validation_error"
    assert paused.egress_reason_code == "confirmation_required"
    assert paused.egress_ticket_id is not None
    assert paused.flow_id is not None
    assert adapter.requests == []

    confirmed = run_confirmation_ticket(
        paused.egress_ticket_id,
        adapters={"scaleway": adapter},
        registry=registry,
    ).outcome

    assert confirmed.status == "success"
    assert confirmed.flow_id == paused.flow_id
    assert len(adapter.requests) == 1
    assert adapter.requests[0].model_preference == MODEL_ID
    with open_sqlite_connection() as connection:
        job = connection.execute(
            "SELECT execution_class, adapter_invoked, external_dispatch_state, "
            "normalized_usage_source, accounting_basis, "
            "accounted_provider_spend_usd_decimal FROM ai_jobs WHERE id = ?",
            (confirmed.ledger_id,),
        ).fetchone()
        flow = connection.execute(
            "SELECT state, terminal_attempt_id, external_provider_spend_usd_decimal FROM ai_flows WHERE id = ?",
            (confirmed.flow_id,),
        ).fetchone()
    assert job["execution_class"] == "external_provider"
    assert job["adapter_invoked"] == 1
    assert job["external_dispatch_state"] == "started"
    assert job["normalized_usage_source"] == "actual"
    assert job["accounting_basis"] == "provider_exact"
    expected = Decimal("0.00002375")
    assert Decimal(job["accounted_provider_spend_usd_decimal"]) == expected
    assert flow["state"] == "complete"
    assert flow["terminal_attempt_id"] == confirmed.ledger_id
    assert Decimal(flow["external_provider_spend_usd_decimal"]) == expected
