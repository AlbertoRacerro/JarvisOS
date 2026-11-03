from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.egress_persistence import (
    EgressStateError,
    prepare_egress_attempt,
)
from app.modules.ai.egress_policy import EXTERNAL_PROVIDER_OPERATION, load_default_egress_policy
from app.modules.ai.egress_service import EgressPacketMaterial, sha256_text
from app.modules.ai.models import AISettingsUpdate
from app.modules.ai.settings import ensure_ai_settings, update_ai_settings
from app.modules.events.service import utc_now

WORKSPACE_ID = "main"
NOW = datetime(2026, 7, 14, 9, 0, tzinfo=UTC)


def _bootstrap(monkeypatch, *, budget: float = 100.0) -> None:
    initialize_database()
    ensure_ai_settings()
    update_ai_settings(
        AISettingsUpdate(
            policy_mode="FAST_DEV",
            monthly_api_budget_usd=budget,
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


def _material(**overrides) -> EgressPacketMaterial:
    values = {
        "operation": EXTERNAL_PROVIDER_OPERATION,
        "task_kind": "general",
        "route_class": "external:cheap",
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-pro",
        "fallback_index": 0,
        "prompt": "Summarize the approved generic pump note.",
        "context_blocks": (
            {
                "source": "derivative:derivative-1",
                "content": "Generic pump sizing note.",
            },
        ),
        "prompt_level": "S1",
        "context_level": "S1",
        "final_level": "S1",
        "max_output_tokens": 128,
        "workspace_id": WORKSPACE_ID,
        "included_manifest": (
            {
                "derivative_id": "derivative-1",
                "effective_level": "S1",
                "source_ref": "decision:decision-1",
            },
        ),
        "source_digests": (
            ("decision:decision-1", sha256_text("source-body")),
        ),
    }
    values.update(overrides)
    return EgressPacketMaterial(**values)


def _counts() -> dict[str, int]:
    tables = (
        "egress_packets",
        "egress_decisions",
        "egress_confirmation_tickets",
        "egress_budget_reservations",
        "egress_attempts",
    )
    with open_sqlite_connection() as connection:
        return {
            table: int(
                connection.execute(f"SELECT COUNT(*) AS count FROM {table}").fetchone()[
                    "count"
                ]
            )
            for table in tables
        }


def _seed_recorded_network_attempt(preparation) -> None:
    ai_job_id = str(uuid4())
    reservation_id = str(uuid4())
    attempt_id = str(uuid4())
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO ai_jobs (
                id, created_at, status, task_kind, requested_route_class,
                selected_route_class, provider_id, model_id, route_reason_json,
                input_tokens, output_tokens, cost_estimate
            ) VALUES (?, ?, 'completed', 'general', 'external:cheap',
                      'external:cheap', 'deepseek', 'deepseek-v4-pro', '{}', 1, 1, 0.0)
            """,
            (ai_job_id, NOW.isoformat()),
        )
        connection.execute(
            """
            INSERT INTO egress_budget_reservations (
                id, decision_id, packet_digest, provider_id, model_id,
                projected_input_tokens, projected_output_tokens,
                projected_cost_upper_usd, state, created_at, expires_at,
                attempt_started_at, reconciled_at, egress_attempt_id,
                ai_job_id, actual_input_tokens, actual_output_tokens,
                actual_cost_usd, reconciliation_status
            ) VALUES (?, ?, ?, 'deepseek', 'deepseek-v4-pro', ?, ?, ?,
                      'reconciled', ?, ?, ?, ?, ?, ?, 1, 1, 0.0, 'actual')
            """,
            (
                reservation_id,
                preparation.decision_id,
                preparation.packet_digest,
                preparation.projected_input_tokens,
                preparation.projected_output_tokens,
                preparation.projected_cost_upper_usd,
                NOW.isoformat(),
                (NOW + timedelta(minutes=5)).isoformat(),
                NOW.isoformat(),
                NOW.isoformat(),
                attempt_id,
                ai_job_id,
            ),
        )
        connection.execute(
            """
            INSERT INTO egress_attempts (
                id, decision_id, packet_id, ai_job_id, reservation_id,
                route_class, provider_id, model_id, fallback_index,
                network_attempt, reconciliation_status,
                projected_input_tokens, projected_output_tokens,
                projected_cost_upper_usd, actual_input_tokens,
                actual_output_tokens, actual_cost_usd, created_at
            ) VALUES (?, ?, ?, ?, ?, 'external:cheap', 'deepseek',
                      'deepseek-v4-pro', 0, 1, 'actual', ?, ?, ?, 1, 1, 0.0, ?)
            """,
            (
                attempt_id,
                preparation.decision_id,
                preparation.packet_id,
                ai_job_id,
                reservation_id,
                preparation.projected_input_tokens,
                preparation.projected_output_tokens,
                preparation.projected_cost_upper_usd,
                NOW.isoformat(),
            ),
        )
        connection.commit()


def test_first_use_creates_pending_ticket_without_reservation(monkeypatch):
    _bootstrap(monkeypatch)

    result = prepare_egress_attempt(_material(), now=NOW)

    assert result.result == "pause"
    assert result.reason_code == "confirmation_required"
    assert result.trigger_ids == ("t1",)
    assert result.ticket_id is not None
    assert result.reservation_id is None
    assert result.confirmation_required is True
    assert _counts() == {
        "egress_packets": 1,
        "egress_decisions": 1,
        "egress_confirmation_tickets": 1,
        "egress_budget_reservations": 0,
        "egress_attempts": 0,
    }
    with open_sqlite_connection() as connection:
        ticket = connection.execute(
            "SELECT state, packet_digest, source_digests_json FROM egress_confirmation_tickets"
        ).fetchone()
        decision = connection.execute(
            "SELECT result, confirmation_required, trigger_ids_json FROM egress_decisions"
        ).fetchone()
    assert ticket["state"] == "pending"
    assert ticket["packet_digest"] == result.packet_digest
    assert "source-body" not in ticket["source_digests_json"]
    assert decision["result"] == "pause"
    assert decision["confirmation_required"] == 1
    assert decision["trigger_ids_json"] == '["t1"]'


def test_missing_credentials_is_hard_denial_with_no_ticket_or_reservation(monkeypatch):
    _bootstrap(monkeypatch)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    result = prepare_egress_attempt(_material(), now=NOW)

    assert result.result == "deny"
    assert result.reason_code == "provider_credentials_missing"
    assert result.ticket_id is None
    assert result.reservation_id is None
    assert _counts()["egress_packets"] == 1
    assert _counts()["egress_decisions"] == 1
    assert _counts()["egress_confirmation_tickets"] == 0
    assert _counts()["egress_budget_reservations"] == 0


def test_projected_global_budget_overshoot_fails_closed(monkeypatch):
    _bootstrap(monkeypatch, budget=0.000001)

    result = prepare_egress_attempt(_material(), now=NOW)

    assert result.result == "deny"
    assert result.reason_code == "global_monthly_cost_cap_exceeded"
    assert result.confirmation_required is False


def test_daily_soft_limit_and_workspace_flag_add_confirmable_triggers(monkeypatch):
    _bootstrap(monkeypatch)
    policy = load_default_egress_policy()
    policy = type(policy)(
        **{
            **policy.__dict__,
            "daily_soft_spend_usd": 0.000001,
        }
    )
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO workspace_egress_policy (
                workspace_id, ask_me, created_at, updated_at, updated_by
            ) VALUES (?, 1, ?, ?, 'test')
            """,
            (WORKSPACE_ID, NOW.isoformat(), NOW.isoformat()),
        )
        connection.commit()

    result = prepare_egress_attempt(_material(), policy=policy, now=NOW)

    assert result.result == "pause"
    assert result.trigger_ids == ("t1", "t2", "t5")


def test_prior_network_attempt_allows_silent_reservation(monkeypatch):
    _bootstrap(monkeypatch)
    first = prepare_egress_attempt(_material(), now=NOW)
    _seed_recorded_network_attempt(first)

    result = prepare_egress_attempt(_material(), now=NOW + timedelta(seconds=1))

    assert result.result == "allow"
    assert result.reason_code == "silent_allow"
    assert result.trigger_ids == ()
    assert result.ticket_id is None
    assert result.reservation_id is not None
    with open_sqlite_connection() as connection:
        reservation = connection.execute(
            "SELECT state, decision_id, packet_digest FROM egress_budget_reservations WHERE id = ?",
            (result.reservation_id,),
        ).fetchone()
    assert reservation["state"] == "active"
    assert reservation["decision_id"] == result.decision_id
    assert reservation["packet_digest"] == result.packet_digest


def test_identical_packet_is_reused_but_decisions_remain_immutable(monkeypatch):
    _bootstrap(monkeypatch)

    first = prepare_egress_attempt(_material(), now=NOW)
    second = prepare_egress_attempt(_material(), now=NOW + timedelta(seconds=1))

    assert first.packet_id == second.packet_id
    assert first.packet_digest == second.packet_digest
    assert first.decision_id != second.decision_id
    assert _counts()["egress_packets"] == 1
    assert _counts()["egress_decisions"] == 2


def test_immutable_packet_mismatch_is_detected(monkeypatch):
    _bootstrap(monkeypatch)
    first = prepare_egress_attempt(_material(), now=NOW)
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE egress_packets SET packet_json = '{\"tampered\":true}' WHERE id = ?",
            (first.packet_id,),
        )
        connection.commit()

    with pytest.raises(EgressStateError, match="immutable packet mismatch"):
        prepare_egress_attempt(_material(), now=NOW + timedelta(seconds=1))


def test_provider_token_cap_includes_existing_ai_jobs(monkeypatch):
    _bootstrap(monkeypatch)
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO ai_jobs (
                id, created_at, status, task_kind, selected_route_class,
                provider_id, model_id, route_reason_json, input_tokens,
                output_tokens, cost_estimate
            ) VALUES (?, ?, 'completed', 'general', 'external:cheap',
                      'deepseek', 'deepseek-v4-pro', '{}', 999999, 0, 0.0)
            """,
            (str(uuid4()), NOW.isoformat()),
        )
        connection.commit()

    result = prepare_egress_attempt(_material(), now=NOW)

    assert result.result == "deny"
    assert result.reason_code == "provider_monthly_token_cap_exceeded"


def test_expired_active_reservation_is_released_before_new_budget_check(monkeypatch):
    _bootstrap(monkeypatch)
    first = prepare_egress_attempt(_material(), now=NOW)
    _seed_recorded_network_attempt(first)
    allowed = prepare_egress_attempt(_material(), now=NOW + timedelta(seconds=1))
    assert allowed.reservation_id is not None

    replacement = prepare_egress_attempt(
        _material(prompt="A distinct approved prompt."),
        now=NOW + timedelta(minutes=6),
    )

    assert replacement.result == "allow"
    with open_sqlite_connection() as connection:
        expired = connection.execute(
            "SELECT state, reconciliation_status FROM egress_budget_reservations WHERE id = ?",
            (allowed.reservation_id,),
        ).fetchone()
    assert expired["state"] == "expired"
    assert expired["reconciliation_status"] == "expired_before_start"
