from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai import egress_persistence as persistence
from app.modules.ai.egress_lifecycle import (
    consume_confirmation_ticket,
    reconcile_reserved_attempt,
    start_reserved_attempt,
)
from app.modules.ai.egress_persistence import prepare_egress_attempt
from app.modules.ai.egress_policy import EXTERNAL_PROVIDER_OPERATION
from app.modules.ai.egress_service import EgressPacketMaterial
from app.modules.ai.models import AISettingsUpdate
from app.modules.ai.settings import ensure_ai_settings, update_ai_settings
from app.modules.ai.usage_cost import actual_registry_cost_usd
from app.modules.events.service import utc_now

WORKSPACE_ID = "main"
NOW = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)


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
        prompt="Explain one generic pump sizing method.",
        context_blocks=(),
        prompt_level="S1",
        context_level="S0",
        final_level="S1",
        max_output_tokens=128,
        workspace_id=WORKSPACE_ID,
    )


def _insert_ai_job() -> str:
    ai_job_id = str(uuid4())
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO ai_jobs (
                id, created_at, status, task_kind, requested_route_class,
                selected_route_class, provider_id, model_id, route_reason_json
            ) VALUES (?, ?, 'queued', 'general', 'external:cheap',
                      'external:cheap', 'deepseek', 'deepseek-v4-pro', '{}')
            """,
            (ai_job_id, NOW.isoformat()),
        )
        connection.commit()
    return ai_job_id


def _finalize_ai_job_usage(
    ai_job_id: str, *, input_tokens: int, output_tokens: int
) -> float:
    cost = actual_registry_cost_usd(
        provider_id="deepseek", model_id="deepseek-v4-pro",
        input_tokens=input_tokens, output_tokens=output_tokens,
    )
    assert cost is not None
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            UPDATE ai_jobs
            SET status = 'success', input_tokens = ?, output_tokens = ?,
                cost_estimate = ?, usage_source = 'actual', error_type = NULL
            WHERE id = ? AND status = 'queued'
            """,
            (input_tokens, output_tokens, cost, ai_job_id),
        )
        connection.commit()
    return cost


def _consume_and_start(monkeypatch):
    _bootstrap(monkeypatch)
    material = _material()
    preparation = prepare_egress_attempt(material, now=NOW)
    assert preparation.ticket_id is not None
    consumed = consume_confirmation_ticket(
        preparation.ticket_id,
        now=NOW + timedelta(seconds=1),
    )
    assert consumed.authorized is True
    ai_job_id = _insert_ai_job()
    start_reserved_attempt(
        consumed.reservation_id,
        ai_job_id=ai_job_id,
        now=NOW + timedelta(seconds=2),
    )
    return material, preparation, consumed, ai_job_id


def test_reconciled_actual_usage_becomes_ai_jobs_budget_authority(monkeypatch) -> None:
    material, preparation, consumed, ai_job_id = _consume_and_start(monkeypatch)
    _finalize_ai_job_usage(ai_job_id, input_tokens=10, output_tokens=20)

    result = reconcile_reserved_attempt(
        consumed.reservation_id,
        ai_job_id=ai_job_id,
        network_attempt=True,
        actual_input_tokens=10,
        actual_output_tokens=20,
        usage_source="actual",
        now=NOW + timedelta(seconds=3),
    )

    with open_sqlite_connection() as connection:
        job = connection.execute(
            "SELECT input_tokens, output_tokens, cost_estimate FROM ai_jobs WHERE id = ?",
            (ai_job_id,),
        ).fetchone()
        snapshot = persistence._budget_snapshot(
            connection,
            provider_id="deepseek",
            now_dt=NOW + timedelta(seconds=4),
            now_iso=(NOW + timedelta(seconds=4)).isoformat(),
        )
    assert job["input_tokens"] == result.actual_input_tokens == 10
    assert job["output_tokens"] == result.actual_output_tokens == 20
    assert job["cost_estimate"] == pytest.approx(result.actual_cost_usd)
    assert snapshot.global_actual_cost_usd == pytest.approx(result.actual_cost_usd)
    assert snapshot.provider_actual_cost_usd == pytest.approx(result.actual_cost_usd)
    assert snapshot.provider_actual_tokens == 30

    epsilon = result.actual_cost_usd / 2
    update_ai_settings(
        AISettingsUpdate(
            monthly_api_budget_usd=(
                result.actual_cost_usd
                + preparation.projected_cost_upper_usd
                - epsilon
            )
        )
    )
    second = prepare_egress_attempt(
        material,
        now=NOW + timedelta(seconds=5),
    )
    assert second.result == "deny"
    assert second.reason_code == "global_monthly_cost_cap_exceeded"


def test_missing_usage_conservative_upper_bound_remains_budgeted(monkeypatch) -> None:
    material, preparation, consumed, ai_job_id = _consume_and_start(monkeypatch)

    result = reconcile_reserved_attempt(
        consumed.reservation_id,
        ai_job_id=ai_job_id,
        network_attempt=True,
        now=NOW + timedelta(seconds=3),
    )

    assert result.reconciliation_status == "conservative_missing_usage"
    assert result.actual_input_tokens == preparation.projected_input_tokens
    assert result.actual_output_tokens == preparation.projected_output_tokens
    assert result.actual_cost_usd == preparation.projected_cost_upper_usd
    with open_sqlite_connection() as connection:
        job = connection.execute(
            "SELECT input_tokens, output_tokens, cost_estimate FROM ai_jobs WHERE id = ?",
            (ai_job_id,),
        ).fetchone()
    assert job["input_tokens"] == preparation.projected_input_tokens
    assert job["output_tokens"] == preparation.projected_output_tokens
    assert job["cost_estimate"] == pytest.approx(preparation.projected_cost_upper_usd)

    update_ai_settings(
        AISettingsUpdate(
            monthly_api_budget_usd=1.5 * preparation.projected_cost_upper_usd,
        )
    )
    second = prepare_egress_attempt(
        material,
        now=NOW + timedelta(seconds=5),
    )
    assert second.result == "deny"
    assert second.reason_code == "global_monthly_cost_cap_exceeded"


def test_initialize_database_backfills_existing_reconciled_usage(monkeypatch) -> None:
    _material_value, _preparation, consumed, ai_job_id = _consume_and_start(monkeypatch)
    _finalize_ai_job_usage(ai_job_id, input_tokens=7, output_tokens=11)
    result = reconcile_reserved_attempt(
        consumed.reservation_id,
        ai_job_id=ai_job_id,
        network_attempt=True,
        actual_input_tokens=7,
        actual_output_tokens=11,
        now=NOW + timedelta(seconds=3),
    )
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE ai_jobs SET input_tokens = 0, output_tokens = 0, cost_estimate = NULL WHERE id = ?",
            (ai_job_id,),
        )
        connection.commit()

    initialize_database()

    with open_sqlite_connection() as connection:
        job = connection.execute(
            "SELECT input_tokens, output_tokens, cost_estimate FROM ai_jobs WHERE id = ?",
            (ai_job_id,),
        ).fetchone()
    assert job["input_tokens"] == 7
    assert job["output_tokens"] == 11
    assert job["cost_estimate"] == pytest.approx(result.actual_cost_usd)
