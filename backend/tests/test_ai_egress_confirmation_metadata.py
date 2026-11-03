from __future__ import annotations

import pytest

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.egress_confirmation import run_confirmation_ticket
from app.modules.ai.egress_persistence import EgressStateError, prepare_egress_attempt
from app.modules.ai.egress_policy import EXTERNAL_PROVIDER_OPERATION
from app.modules.ai.egress_service import EgressPacketMaterial
from app.modules.ai.models import AISettingsUpdate
from app.modules.ai.settings import ensure_ai_settings, update_ai_settings
from app.modules.ai.token_flow_service import TokenFlowConflictError


def test_malformed_confirmation_metadata_fails_before_consumption(monkeypatch) -> None:
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
    preparation = prepare_egress_attempt(
        EgressPacketMaterial(
            operation=EXTERNAL_PROVIDER_OPERATION,
            task_kind="general",
            route_class="external:cheap",
            provider_id="deepseek",
            model_id="deepseek-v4-pro",
            fallback_index=0,
            prompt="Explain a generic pump sizing method.",
            context_blocks=(),
            prompt_level="S1",
            context_level="S0",
            final_level="S1",
            max_output_tokens=64,
        )
    )
    assert preparation.ticket_id is not None
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE egress_confirmation_tickets SET trigger_ids_json = ? WHERE id = ?",
            ("{not-json}", preparation.ticket_id),
        )
        connection.commit()

    with pytest.raises(EgressStateError, match="trigger metadata is malformed"):
        run_confirmation_ticket(preparation.ticket_id, adapters={})

    with open_sqlite_connection() as connection:
        ticket = connection.execute(
            "SELECT state, version FROM egress_confirmation_tickets WHERE id = ?",
            (preparation.ticket_id,),
        ).fetchone()
        reservations = connection.execute("SELECT COUNT(*) AS count FROM egress_budget_reservations").fetchone()[
            "count"
        ]
        jobs = connection.execute("SELECT COUNT(*) AS count FROM ai_jobs").fetchone()["count"]
    assert tuple(ticket) == ("pending", 0)
    assert reservations == 0
    assert jobs == 0


def test_ticket_without_canonical_flow_fails_before_consumption(monkeypatch) -> None:
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
    preparation = prepare_egress_attempt(
        EgressPacketMaterial(
            operation=EXTERNAL_PROVIDER_OPERATION,
            task_kind="general",
            route_class="external:cheap",
            provider_id="deepseek",
            model_id="deepseek-v4-pro",
            fallback_index=0,
            prompt="Explain a generic pump sizing method.",
            context_blocks=(),
            prompt_level="S1",
            context_level="S0",
            final_level="S1",
            max_output_tokens=64,
        )
    )
    assert preparation.ticket_id is not None

    with pytest.raises(
        TokenFlowConflictError,
        match="exactly one canonical paused flow",
    ):
        run_confirmation_ticket(preparation.ticket_id, adapters={})

    with open_sqlite_connection() as connection:
        ticket = connection.execute(
            "SELECT state, version FROM egress_confirmation_tickets WHERE id = ?",
            (preparation.ticket_id,),
        ).fetchone()
        reservations = connection.execute("SELECT COUNT(*) AS count FROM egress_budget_reservations").fetchone()[
            "count"
        ]
        jobs = connection.execute("SELECT COUNT(*) AS count FROM ai_jobs").fetchone()["count"]
        flows = connection.execute("SELECT COUNT(*) AS count FROM ai_flows").fetchone()["count"]
    assert tuple(ticket) == ("pending", 0)
    assert reservations == 0
    assert jobs == 0
    assert flows == 0
