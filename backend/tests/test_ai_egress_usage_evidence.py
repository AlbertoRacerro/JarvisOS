from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.contracts import (
    AIRequest,
    AIResponse,
    AITaskType,
    AIUsage,
    AIUsageSource,
)
from app.modules.ai.egress_lifecycle import (
    consume_confirmation_ticket,
    reconcile_reserved_attempt,
    start_reserved_attempt,
)
from app.modules.ai.egress_persistence import prepare_egress_attempt
from app.modules.ai.egress_policy import EXTERNAL_PROVIDER_OPERATION
from app.modules.ai.egress_service import EgressPacketMaterial
from app.modules.ai.egress_spine import create_queued_ai_job, finalize_queued_ai_job
from app.modules.ai.models import AISettingsUpdate
from app.modules.ai.providers.openai_compat_adapter import OpenAICompatAdapter
from app.modules.ai.settings import ensure_ai_settings, update_ai_settings
from app.modules.ai.usage_cost import actual_registry_cost_usd
from app.modules.events.service import utc_now

WORKSPACE_ID = "main"
NOW = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)


def _bootstrap(monkeypatch) -> None:
    initialize_database()
    ensure_ai_settings()
    update_ai_settings(
        AISettingsUpdate(
            policy_mode="FAST_DEV",
            monthly_api_budget_usd=100.0,
            paid_ai_enabled=True,
            provider_mode="deepseek",
        )
    )
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-secret")
    with open_sqlite_connection() as connection:
        now = utc_now()
        connection.execute(
            """
            INSERT OR IGNORE INTO workspaces (
                id, name, slug, description, status, created_at, updated_at
            ) VALUES (?, 'Zephyr', 'main', NULL, 'active', ?, ?)
            """,
            (WORKSPACE_ID, now, now),
        )
        connection.commit()


def _material() -> EgressPacketMaterial:
    return EgressPacketMaterial(
        operation=EXTERNAL_PROVIDER_OPERATION,
        task_kind="general",
        route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        fallback_index=0,
        prompt="Explain a generic engineering method.",
        context_blocks=(),
        prompt_level="S1",
        context_level="S0",
        final_level="S1",
        max_output_tokens=64,
        workspace_id=WORKSPACE_ID,
    )


def _started_attempt(monkeypatch):
    _bootstrap(monkeypatch)
    preparation = prepare_egress_attempt(_material(), now=NOW)
    assert preparation.ticket_id is not None
    consumed = consume_confirmation_ticket(
        preparation.ticket_id,
        now=NOW + timedelta(seconds=1),
    )
    assert consumed.authorized is True
    queued = create_queued_ai_job(
        task_kind="general",
        requested_route_class="external:cheap",
        selected_route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        decision_reason="usage-evidence-test",
        now=NOW + timedelta(seconds=2),
    )
    start_reserved_attempt(
        consumed.reservation_id,
        ai_job_id=queued.ai_job_id,
        now=NOW + timedelta(seconds=3),
    )
    return preparation, consumed, queued.ai_job_id


def _response(*, usage_source: AIUsageSource, cost: float | None) -> AIResponse:
    return AIResponse(
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        request_id="request-usage-evidence",
        text="Generic answer.",
        content="Generic answer.",
        usage=AIUsage(
            provider_id="deepseek",
            model_id="deepseek-v4-pro",
            input_tokens=10,
            output_tokens=20,
            usage_source=usage_source,
            provider_cost_estimate=cost,
            currency="USD" if cost is not None else None,
        ),
        finish_reason="stop",
        safety_status="allowed",
    )


def test_openai_adapter_prices_only_provider_reported_usage(monkeypatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-secret")

    def actual_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={
                "choices": [{"message": {"content": "Answer"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 20},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(actual_handler)) as client:
        adapter = OpenAICompatAdapter(
            provider_id="deepseek",
            model_id="deepseek-v4-pro",
            base_url="https://example.invalid",
            api_key_ref="env:DEEPSEEK_API_KEY",
            client=client,
        )
        actual = adapter.complete(
            AIRequest(
                task_type=AITaskType.synthesis,
                prompt="Generic prompt.",
                max_output_tokens=64,
            )
        )
    assert actual.usage.usage_source == AIUsageSource.actual
    assert actual.usage.provider_cost_estimate == pytest.approx(
        actual_registry_cost_usd(
            provider_id="deepseek", model_id="deepseek-v4-pro",
            input_tokens=10, output_tokens=20,
        )
    )
    assert actual.usage.currency == "USD"

    def estimated_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            request=request,
            json={"choices": [{"message": {"content": "Answer"}, "finish_reason": "stop"}]},
        )

    with httpx.Client(transport=httpx.MockTransport(estimated_handler)) as client:
        adapter = OpenAICompatAdapter(
            provider_id="deepseek",
            model_id="deepseek-v4-pro",
            base_url="https://example.invalid",
            api_key_ref="env:DEEPSEEK_API_KEY",
            client=client,
        )
        estimated = adapter.complete(
            AIRequest(
                task_type=AITaskType.synthesis,
                prompt="Generic prompt.",
                max_output_tokens=64,
            )
        )
    assert estimated.usage.usage_source == AIUsageSource.estimated
    assert estimated.usage.provider_cost_estimate is None
    assert estimated.usage.currency is None


def test_verified_usage_preserves_actual_reconciliation(monkeypatch) -> None:
    preparation, consumed, ai_job_id = _started_attempt(monkeypatch)
    cost = actual_registry_cost_usd(
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        input_tokens=10,
        output_tokens=20,
    )
    assert cost is not None
    response = _response(usage_source=AIUsageSource.actual, cost=cost)
    finalize_queued_ai_job(
        ai_job_id,
        status="success",
        response=response,
        latency_ms=1,
    )

    result = reconcile_reserved_attempt(
        consumed.reservation_id,
        ai_job_id=ai_job_id,
        network_attempt=True,
        actual_input_tokens=response.usage.input_tokens,
        actual_output_tokens=response.usage.output_tokens,
        usage_source="actual",
        now=NOW + timedelta(seconds=4),
    )

    assert result.reconciliation_status == "actual"
    assert result.actual_input_tokens == 10
    assert result.actual_output_tokens == 20
    assert result.actual_cost_usd == pytest.approx(cost)
    with open_sqlite_connection() as connection:
        attempt = connection.execute(
            "SELECT * FROM egress_attempts WHERE id = ?",
            (result.egress_attempt_id,),
        ).fetchone()
        reservation = connection.execute(
            "SELECT * FROM egress_budget_reservations WHERE id = ?",
            (consumed.reservation_id,),
        ).fetchone()
        job = connection.execute(
            "SELECT input_tokens, output_tokens, cost_estimate, usage_source "
            "FROM ai_jobs WHERE id = ?",
            (ai_job_id,),
        ).fetchone()
    assert attempt["reconciliation_status"] == "actual"
    assert attempt["actual_input_tokens"] == 10
    assert attempt["actual_output_tokens"] == 20
    assert attempt["actual_cost_usd"] == pytest.approx(cost)
    assert reservation["reconciliation_status"] == "actual"
    assert job["input_tokens"] == 10
    assert job["output_tokens"] == 20
    assert job["cost_estimate"] == pytest.approx(cost)
    assert job["usage_source"] == "actual"
    assert preparation.projected_cost_upper_usd >= cost


def test_token_binding_mismatch_is_conservative(monkeypatch) -> None:
    preparation, consumed, ai_job_id = _started_attempt(monkeypatch)
    cost = actual_registry_cost_usd(
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        input_tokens=10,
        output_tokens=20,
    )
    assert cost is not None
    finalize_queued_ai_job(
        ai_job_id,
        status="success",
        response=_response(usage_source=AIUsageSource.actual, cost=cost),
        latency_ms=1,
    )

    result = reconcile_reserved_attempt(
        consumed.reservation_id,
        ai_job_id=ai_job_id,
        network_attempt=True,
        actual_input_tokens=1,
        actual_output_tokens=1,
        usage_source="actual",
        now=NOW + timedelta(seconds=4),
    )

    assert result.reconciliation_status == "conservative_usage_binding_mismatch"
    assert result.actual_input_tokens == max(preparation.projected_input_tokens, 10, 1)
    assert result.actual_output_tokens == max(preparation.projected_output_tokens, 20, 1)
    assert result.actual_cost_usd == pytest.approx(
        max(preparation.projected_cost_upper_usd, cost)
    )


def test_cost_binding_mismatch_is_conservative(monkeypatch) -> None:
    preparation, consumed, ai_job_id = _started_attempt(monkeypatch)
    expected_cost = actual_registry_cost_usd(
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        input_tokens=10,
        output_tokens=20,
    )
    assert expected_cost is not None
    mismatched_cost = 1.0
    finalize_queued_ai_job(
        ai_job_id,
        status="success",
        response=_response(usage_source=AIUsageSource.actual, cost=mismatched_cost),
        latency_ms=1,
    )

    result = reconcile_reserved_attempt(
        consumed.reservation_id,
        ai_job_id=ai_job_id,
        network_attempt=True,
        actual_input_tokens=10,
        actual_output_tokens=20,
        usage_source="actual",
        now=NOW + timedelta(seconds=4),
    )

    assert result.reconciliation_status == "conservative_cost_binding_mismatch"
    assert result.actual_input_tokens == 10
    assert result.actual_output_tokens == 20
    assert result.actual_cost_usd == pytest.approx(
        max(preparation.projected_cost_upper_usd, mismatched_cost, expected_cost)
    )


@pytest.mark.parametrize("reported_source", ["actual", "estimated", "mixed"])
def test_unverified_usage_is_normalized_to_reserved_upper_bound(
    monkeypatch, reported_source: str
) -> None:
    preparation, consumed, ai_job_id = _started_attempt(monkeypatch)
    response = _response(usage_source=AIUsageSource.estimated, cost=None)
    finalize_queued_ai_job(
        ai_job_id,
        status="success",
        response=response,
        latency_ms=1,
    )

    result = reconcile_reserved_attempt(
        consumed.reservation_id,
        ai_job_id=ai_job_id,
        network_attempt=True,
        actual_input_tokens=response.usage.input_tokens,
        actual_output_tokens=response.usage.output_tokens,
        usage_source=reported_source,
        now=NOW + timedelta(seconds=4),
    )

    expected_input_tokens = max(
        preparation.projected_input_tokens, response.usage.input_tokens
    )
    expected_output_tokens = max(
        preparation.projected_output_tokens, response.usage.output_tokens
    )
    assert result.reconciliation_status == "conservative_unverified_usage"
    assert result.actual_input_tokens == expected_input_tokens
    assert result.actual_output_tokens == expected_output_tokens
    assert result.actual_cost_usd == pytest.approx(preparation.projected_cost_upper_usd)
    with open_sqlite_connection() as connection:
        attempt = connection.execute(
            "SELECT * FROM egress_attempts WHERE id = ?",
            (result.egress_attempt_id,),
        ).fetchone()
        reservation = connection.execute(
            "SELECT * FROM egress_budget_reservations WHERE id = ?",
            (consumed.reservation_id,),
        ).fetchone()
        job = connection.execute(
            "SELECT input_tokens, output_tokens, cost_estimate, usage_source "
            "FROM ai_jobs WHERE id = ?",
            (ai_job_id,),
        ).fetchone()
    assert attempt["reconciliation_status"] == "conservative_unverified_usage"
    assert attempt["actual_input_tokens"] == expected_input_tokens
    assert attempt["actual_output_tokens"] == expected_output_tokens
    assert attempt["actual_cost_usd"] == pytest.approx(preparation.projected_cost_upper_usd)
    assert reservation["reconciliation_status"] == "conservative_unverified_usage"
    assert reservation["actual_input_tokens"] == expected_input_tokens
    assert reservation["actual_output_tokens"] == expected_output_tokens
    assert reservation["actual_cost_usd"] == pytest.approx(
        preparation.projected_cost_upper_usd
    )
    assert job["input_tokens"] == expected_input_tokens
    assert job["output_tokens"] == expected_output_tokens
    assert job["cost_estimate"] == pytest.approx(preparation.projected_cost_upper_usd)
    assert job["usage_source"] == "estimated"


def test_estimated_source_with_populated_cost_cannot_be_promoted_to_actual(monkeypatch) -> None:
    preparation, consumed, ai_job_id = _started_attempt(monkeypatch)
    exact_cost = actual_registry_cost_usd(
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        input_tokens=10,
        output_tokens=20,
    )
    assert exact_cost is not None
    response = _response(usage_source=AIUsageSource.estimated, cost=exact_cost)
    finalize_queued_ai_job(
        ai_job_id,
        status="success",
        response=response,
        latency_ms=1,
    )

    result = reconcile_reserved_attempt(
        consumed.reservation_id,
        ai_job_id=ai_job_id,
        network_attempt=True,
        actual_input_tokens=10,
        actual_output_tokens=20,
        usage_source="actual",
        now=NOW + timedelta(seconds=4),
    )

    assert result.reconciliation_status == "conservative_unverified_usage"
    assert result.actual_cost_usd == pytest.approx(preparation.projected_cost_upper_usd)
    with open_sqlite_connection() as connection:
        job = connection.execute(
            "SELECT usage_source, cost_estimate FROM ai_jobs WHERE id = ?",
            (ai_job_id,),
        ).fetchone()
    assert job["usage_source"] == "estimated"
    assert job["cost_estimate"] == pytest.approx(preparation.projected_cost_upper_usd)


def test_finalized_provider_error_without_usage_is_conservative(monkeypatch) -> None:
    preparation, consumed, ai_job_id = _started_attempt(monkeypatch)
    finalize_queued_ai_job(
        ai_job_id,
        status="provider_error",
        response=None,
        latency_ms=1,
        error_type="TimeoutError",
    )

    result = reconcile_reserved_attempt(
        consumed.reservation_id,
        ai_job_id=ai_job_id,
        network_attempt=True,
        usage_source="estimated",
        now=NOW + timedelta(seconds=4),
    )

    assert result.reconciliation_status == "conservative_missing_usage"
    assert result.actual_input_tokens == preparation.projected_input_tokens
    assert result.actual_output_tokens == preparation.projected_output_tokens
    assert result.actual_cost_usd == pytest.approx(preparation.projected_cost_upper_usd)
    with open_sqlite_connection() as connection:
        attempt = connection.execute(
            "SELECT * FROM egress_attempts WHERE id = ?",
            (result.egress_attempt_id,),
        ).fetchone()
        job = connection.execute(
            "SELECT input_tokens, output_tokens, cost_estimate FROM ai_jobs WHERE id = ?",
            (ai_job_id,),
        ).fetchone()
    assert attempt["reconciliation_status"] == "conservative_missing_usage"
    assert attempt["actual_cost_usd"] == pytest.approx(preparation.projected_cost_upper_usd)
    assert job["cost_estimate"] == pytest.approx(preparation.projected_cost_upper_usd)
