from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.egress_lifecycle import consume_confirmation_ticket
from app.modules.ai.egress_persistence import prepare_egress_attempt
from app.modules.ai.egress_policy import EXTERNAL_PROVIDER_OPERATION
from app.modules.ai.egress_service import EgressPacketMaterial, sha256_text
from app.modules.ai.models import AISettingsUpdate
from app.modules.ai.settings import ensure_ai_settings, update_ai_settings
from app.modules.events.service import utc_now

WORKSPACE_ID = "main"
MODEL_ID = "deepseek-v4-flash-0731"
NOW = datetime(2026, 8, 4, 9, 0, tzinfo=UTC)


def _bootstrap(monkeypatch: pytest.MonkeyPatch) -> None:
    initialize_database()
    ensure_ai_settings()
    update_ai_settings(
        AISettingsUpdate(
            policy_mode="FAST_DEV",
            monthly_api_budget_usd=100.0,
            paid_ai_enabled=True,
            provider_mode="scaleway",
            scaleway_enabled=True,
            scaleway_smoke_test_enabled=True,
            scaleway_live_smoke_test_enabled=True,
        )
    )
    monkeypatch.setenv("SCALEWAY_API_KEY", "test-only-secret")
    monkeypatch.delenv("SCALEWAY_MODEL", raising=False)
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


def _set_legacy_usage_and_caps(
    *,
    input_tokens: int,
    output_tokens: int,
    monthly_cap: int,
    hard_stop_cap: int,
) -> None:
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            UPDATE ai_settings
            SET scaleway_input_tokens_month_to_date = ?,
                scaleway_output_tokens_month_to_date = ?,
                scaleway_monthly_token_cap = ?,
                scaleway_hard_stop_token_cap = ?
            WHERE id = 'default'
            """,
            (input_tokens, output_tokens, monthly_cap, hard_stop_cap),
        )
        connection.commit()


def _material(**overrides: object) -> EgressPacketMaterial:
    values: dict[str, object] = {
        "operation": EXTERNAL_PROVIDER_OPERATION,
        "task_kind": "decision_support",
        "route_class": "external:scaleway",
        "provider_id": "scaleway",
        "model_id": MODEL_ID,
        "fallback_index": 0,
        "prompt": "Reply with the word OK.",
        "context_blocks": (
            {
                "source": "derivative:derivative-1",
                "content": "Harmless public checkpoint note.",
            },
        ),
        "prompt_level": "S1",
        "context_level": "S1",
        "final_level": "S1",
        "max_output_tokens": 8,
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


def _source_free_material(**overrides: object) -> EgressPacketMaterial:
    return _material(
        context_blocks=(),
        included_manifest=(),
        source_digests=(),
        **overrides,
    )


def _reservation_count() -> int:
    with open_sqlite_connection() as connection:
        return int(
            connection.execute(
                "SELECT COUNT(*) AS count FROM egress_budget_reservations"
            ).fetchone()["count"]
        )


def _prepare_first_use_ticket() -> str:
    prepared = prepare_egress_attempt(_source_free_material(), now=NOW)
    assert prepared.result == "pause"
    assert prepared.ticket_id is not None
    assert _reservation_count() == 0
    return prepared.ticket_id


def test_projected_request_cannot_cross_legacy_monthly_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bootstrap(monkeypatch)
    _set_legacy_usage_and_caps(
        input_tokens=99,
        output_tokens=0,
        monthly_cap=100,
        hard_stop_cap=1_000,
    )

    result = prepare_egress_attempt(_material(), now=NOW)

    assert result.result == "deny"
    assert result.reason_code == "scaleway_monthly_token_cap_exceeded"
    assert result.reservation_id is None
    assert _reservation_count() == 0


def test_projected_request_combines_legacy_and_routed_usage_at_hard_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bootstrap(monkeypatch)
    _set_legacy_usage_and_caps(
        input_tokens=25,
        output_tokens=15,
        monthly_cap=1_000,
        hard_stop_cap=100,
    )
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO ai_jobs (
                id, created_at, status, task_kind, selected_route_class,
                provider_id, model_id, route_reason_json, input_tokens,
                output_tokens, cost_estimate
            ) VALUES (?, ?, 'completed', 'decision_support',
                      'external:scaleway', 'scaleway', ?, '{}', 40, 10, 0.0)
            """,
            (str(uuid4()), NOW.isoformat(), MODEL_ID),
        )
        connection.commit()

    result = prepare_egress_attempt(_material(), now=NOW)

    assert result.result == "deny"
    assert result.reason_code == "scaleway_hard_stop_token_cap_exceeded"
    assert result.reservation_id is None


@pytest.mark.parametrize(
    ("settings_update", "blocking_reason"),
    [
        ({"provider_mode": "fake"}, "scaleway_provider_mode_required"),
        ({"scaleway_enabled": False}, "scaleway_disabled"),
    ],
)
def test_ticket_consumption_revalidates_route_switches_atomically(
    monkeypatch: pytest.MonkeyPatch,
    settings_update: dict[str, object],
    blocking_reason: str,
) -> None:
    _bootstrap(monkeypatch)
    _set_legacy_usage_and_caps(
        input_tokens=0,
        output_tokens=0,
        monthly_cap=1_000,
        hard_stop_cap=1_000,
    )
    ticket_id = _prepare_first_use_ticket()

    update_ai_settings(AISettingsUpdate(**settings_update))
    consumed = consume_confirmation_ticket(ticket_id, now=NOW)

    assert consumed.authorized is False
    assert consumed.reason_code == blocking_reason
    assert consumed.reservation_id is None
    assert _reservation_count() == 0


@pytest.mark.parametrize(
    "settings_update",
    [
        {"scaleway_smoke_test_enabled": False},
        {"scaleway_live_smoke_test_enabled": False},
    ],
)
def test_ticket_consumption_ignores_wrapper_only_smoke_switches(
    monkeypatch: pytest.MonkeyPatch,
    settings_update: dict[str, object],
) -> None:
    _bootstrap(monkeypatch)
    _set_legacy_usage_and_caps(
        input_tokens=0,
        output_tokens=0,
        monthly_cap=1_000,
        hard_stop_cap=1_000,
    )
    ticket_id = _prepare_first_use_ticket()

    update_ai_settings(AISettingsUpdate(**settings_update))
    consumed = consume_confirmation_ticket(ticket_id, now=NOW)

    assert consumed.authorized is True
    assert consumed.reason_code == "ticket_consumed"
    assert consumed.reservation_id is not None
    assert _reservation_count() == 1


def test_active_routed_reservation_is_included_in_next_scaleway_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bootstrap(monkeypatch)
    _set_legacy_usage_and_caps(
        input_tokens=0,
        output_tokens=0,
        monthly_cap=100,
        hard_stop_cap=200,
    )

    ticket_id = _prepare_first_use_ticket()
    consumed = consume_confirmation_ticket(ticket_id, now=NOW)
    assert consumed.authorized is True
    assert consumed.reservation_id is not None
    assert _reservation_count() == 1

    result = prepare_egress_attempt(
        _source_free_material(prompt="Reply with the word YES."),
        now=NOW,
    )

    assert result.result == "deny"
    assert result.reason_code == "scaleway_monthly_token_cap_exceeded"
    assert result.reservation_id is None
    assert _reservation_count() == 1
