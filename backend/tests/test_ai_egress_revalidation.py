from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai import sensitivity
from app.modules.ai.egress_lifecycle import consume_confirmation_ticket
from app.modules.ai.egress_persistence import EgressStateError, prepare_egress_attempt
from app.modules.ai.egress_policy import EXTERNAL_PROVIDER_OPERATION
from app.modules.ai.egress_sanitizer import (
    auto_approve_canonical_derivative,
    create_prompt_derivative,
    get_prompt_derivative,
)
from app.modules.ai.egress_service import EgressPacketMaterial, sha256_text
from app.modules.ai.models import AISettingsUpdate
from app.modules.ai.sensitivity_models import SensitivityLabelCreate
from app.modules.ai.settings import ensure_ai_settings, update_ai_settings
from app.modules.events.service import utc_now

WORKSPACE_ID = "main"
NOW = datetime(2026, 7, 15, 6, 30, tzinfo=UTC)
CONFIG_DIGEST = sha256_text("ticket-revalidation-test-config")


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


def _decision(text: str) -> str:
    record_id = str(uuid4())
    now = utc_now()
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO decisions (
                id, workspace_id, title, decision_text, rationale, status,
                linked_run_id, created_at, updated_at, notes
            ) VALUES (?, ?, 'Decision', ?, NULL, 'accepted', NULL, ?, ?, NULL)
            """,
            (record_id, WORKSPACE_ID, text, now, now),
        )
        connection.commit()
    return record_id


def _base_material(**overrides) -> EgressPacketMaterial:
    values = {
        "operation": EXTERNAL_PROVIDER_OPERATION,
        "task_kind": "general",
        "route_class": "external:cheap",
        "provider_id": "deepseek",
        "model_id": "deepseek-v4-pro",
        "fallback_index": 0,
        "prompt": "Explain a generic engineering method.",
        "context_blocks": (),
        "prompt_level": "S1",
        "context_level": "S0",
        "final_level": "S1",
        "max_output_tokens": 64,
        "workspace_id": WORKSPACE_ID,
    }
    values.update(overrides)
    return EgressPacketMaterial(**values)


def _canonical_derivative_material() -> tuple[EgressPacketMaterial, str, str]:
    decision_id = _decision("Zephyr proprietary geometry decision")
    source_ref = f"decision:{decision_id}"
    sensitivity.create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=source_ref,
            level="S3",
        )
    )
    approval = auto_approve_canonical_derivative(
        workspace_id=WORKSPACE_ID,
        source_refs=[source_ref],
        derivative_content="Generic tubular photobioreactor concept.",
        final_level="S1",
        transformations=["Removed project-specific geometry"],
        sanitizer_kind="deterministic",
        sanitizer_version="canonical-redactor-v1",
        sanitizer_config_digest=CONFIG_DIGEST,
    )
    derivative = sensitivity.get_sanitized_derivative(
        WORKSPACE_ID,
        approval.derivative_id,
    )
    block = {
        "source": f"derivative:{derivative.id}",
        "type": "sanitized_derivative",
        "id": derivative.id,
        "content": derivative.content,
    }
    manifest = {
        "source_ref": f"derivative:{derivative.id}",
        "source_refs": derivative.source_refs,
        "content_digest": derivative.content_digest,
        "effective_level": derivative.effective_level,
        "label_id": None,
        "derivative_id": derivative.id,
        "inclusion_reason": "approved_derivative",
    }
    material = _base_material(
        context_blocks=(block,),
        context_level="S1",
        included_manifest=(manifest,),
        source_digests=tuple(sorted(derivative.source_digests.items())),
    )
    return material, decision_id, derivative.id


def _assert_revoked_without_reservation(ticket_id: str) -> None:
    with open_sqlite_connection() as connection:
        ticket = connection.execute(
            "SELECT state, revocation_reason FROM egress_confirmation_tickets WHERE id = ?",
            (ticket_id,),
        ).fetchone()
        reservations = connection.execute(
            "SELECT COUNT(*) AS count FROM egress_budget_reservations"
        ).fetchone()["count"]
    assert ticket["state"] == "revoked"
    assert ticket["revocation_reason"] == "ticket_binding_or_policy_drift"
    assert reservations == 0


def test_current_canonical_derivative_ticket_consumes(monkeypatch) -> None:
    _bootstrap(monkeypatch)
    material, _decision_id, _derivative_id = _canonical_derivative_material()
    preparation = prepare_egress_attempt(material, now=NOW)

    result = consume_confirmation_ticket(
        preparation.ticket_id,
        now=NOW + timedelta(seconds=1),
    )

    assert result.authorized is True
    assert result.reason_code == "ticket_consumed"
    assert result.reservation_id is not None


def test_source_mutation_revokes_pending_ticket_before_reservation(monkeypatch) -> None:
    _bootstrap(monkeypatch)
    material, decision_id, _derivative_id = _canonical_derivative_material()
    preparation = prepare_egress_attempt(material, now=NOW)
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE decisions SET decision_text = ?, updated_at = ? WHERE id = ?",
            ("Zephyr changed proprietary geometry decision", utc_now(), decision_id),
        )
        connection.commit()

    result = consume_confirmation_ticket(
        preparation.ticket_id,
        now=NOW + timedelta(seconds=1),
    )

    assert result.authorized is False
    assert result.reason_code == "ticket_binding_or_policy_drift"
    assert result.reservation_id is None
    _assert_revoked_without_reservation(preparation.ticket_id)


def test_canonical_derivative_revocation_revokes_pending_ticket(monkeypatch) -> None:
    _bootstrap(monkeypatch)
    material, _decision_id, derivative_id = _canonical_derivative_material()
    preparation = prepare_egress_attempt(material, now=NOW)
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE sanitized_derivatives SET status = 'revoked', stale_reason = 'operator_rejected' WHERE id = ?",
            (derivative_id,),
        )
        connection.commit()

    result = consume_confirmation_ticket(
        preparation.ticket_id,
        now=NOW + timedelta(seconds=1),
    )

    assert result.authorized is False
    assert result.reason_code == "ticket_binding_or_policy_drift"
    _assert_revoked_without_reservation(preparation.ticket_id)


def test_prompt_derivative_revocation_revokes_pending_ticket(monkeypatch) -> None:
    _bootstrap(monkeypatch)
    approval = create_prompt_derivative(
        raw_prompt="private project geometry question",
        derivative_content="Generic engineering question.",
        final_level="S1",
        transformations=["Removed project identity"],
        sanitizer_kind="deterministic",
        sanitizer_version="prompt-redactor-v1",
        sanitizer_config_digest=CONFIG_DIGEST,
        workspace_id=WORKSPACE_ID,
    )
    derivative = get_prompt_derivative(
        approval.derivative_id,
        workspace_id=WORKSPACE_ID,
    )
    material = _base_material(
        prompt=derivative.derivative_content,
        prompt_derivative_id=derivative.id,
    )
    preparation = prepare_egress_attempt(material, now=NOW)
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE egress_prompt_derivatives SET status = 'revoked', revocation_reason = 'operator_rejected' WHERE id = ?",
            (derivative.id,),
        )
        connection.commit()

    result = consume_confirmation_ticket(
        preparation.ticket_id,
        now=NOW + timedelta(seconds=1),
    )

    assert result.authorized is False
    assert result.reason_code == "ticket_binding_or_policy_drift"
    _assert_revoked_without_reservation(preparation.ticket_id)


def test_direct_source_label_upgrade_revokes_pending_ticket(monkeypatch) -> None:
    _bootstrap(monkeypatch)
    decision_id = _decision("Generic approved pump decision")
    source_ref = f"decision:{decision_id}"
    label = sensitivity.create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=source_ref,
            level="S1",
        )
    )
    snapshot = sensitivity.resolve_source_snapshot(WORKSPACE_ID, source_ref)
    material = _base_material(
        context_blocks=(snapshot.block,),
        context_level="S1",
        included_manifest=(
            {
                "source_ref": source_ref,
                "content_digest": snapshot.content_digest,
                "effective_level": "S1",
                "label_id": label.id,
                "derivative_id": None,
                "inclusion_reason": "current_label",
            },
        ),
        source_digests=((source_ref, snapshot.content_digest),),
    )
    preparation = prepare_egress_attempt(material, now=NOW)
    sensitivity.create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=source_ref,
            level="S2",
        )
    )

    result = consume_confirmation_ticket(
        preparation.ticket_id,
        now=NOW + timedelta(seconds=1),
    )

    assert result.authorized is False
    assert result.reason_code == "ticket_binding_or_policy_drift"
    _assert_revoked_without_reservation(preparation.ticket_id)


def test_concurrent_ticket_consumption_has_one_cas_winner(monkeypatch) -> None:
    _bootstrap(monkeypatch)
    preparation = prepare_egress_attempt(_base_material(), now=NOW)

    def consume_once():
        try:
            return consume_confirmation_ticket(
                preparation.ticket_id,
                now=NOW + timedelta(seconds=1),
            )
        except EgressStateError as exc:
            return exc

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _index: consume_once(), range(2)))

    authorized = [result for result in results if not isinstance(result, Exception)]
    rejected = [result for result in results if isinstance(result, EgressStateError)]
    assert len(authorized) == 1
    assert authorized[0].authorized is True
    assert len(rejected) == 1
    assert "not pending: consumed" in str(rejected[0])
    with open_sqlite_connection() as connection:
        reservations = connection.execute(
            "SELECT COUNT(*) AS count FROM egress_budget_reservations"
        ).fetchone()["count"]
    assert reservations == 1
