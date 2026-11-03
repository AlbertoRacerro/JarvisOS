import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.context_builder import canonical_digest
from app.modules.ai.egress_lifecycle import consume_confirmation_ticket
from app.modules.ai.egress_persistence import prepare_egress_attempt
from app.modules.ai.egress_policy import EXTERNAL_PROVIDER_OPERATION, load_default_egress_policy
from app.modules.ai.egress_sanitizer import (
    auto_approve_canonical_derivative,
    create_prompt_derivative,
    get_prompt_derivative,
    resolve_approved_prompt_derivative,
    review_sanitizer_audit_item,
)
from app.modules.ai.egress_service import EgressPacketMaterial, sha256_text
from app.modules.ai.models import AISettingsUpdate
from app.modules.ai.sensitivity import (
    SensitivityPolicyError,
    build_external_context_preview,
    create_sensitivity_label,
    get_sanitized_derivative,
    resolve_source_snapshot,
)
from app.modules.ai.sensitivity_models import SensitivityLabelCreate
from app.modules.ai.settings import ensure_ai_settings, update_ai_settings
from app.modules.events.service import utc_now

WORKSPACE_ID = "main"
NOW = datetime(2026, 7, 14, 11, 0, tzinfo=UTC)
CONFIG_DIGEST = sha256_text("sanitizer-config-v1")


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


def _policy_100_percent():
    return replace(load_default_egress_policy(), sample_rate_bps=10_000)


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


def _insert_ai_job(
    *,
    route: str,
    output_text: str,
    status: str = "success",
    task_kind: str = "sanitizer",
) -> str:
    ai_job_id = str(uuid4())
    provider_id = "local_ollama" if route.startswith("local:") else "deepseek"
    model_id = "qwen3:8b" if route.startswith("local:") else "deepseek-v4-pro"
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO ai_jobs (
                id, created_at, status, task_kind, selected_route_class,
                provider_id, model_id, route_reason_json, output_digest
            ) VALUES (?, ?, ?, ?, ?, ?, ?, '{}', ?)
            """,
            (
                ai_job_id,
                NOW.isoformat(),
                status,
                task_kind,
                route,
                provider_id,
                model_id,
                canonical_digest({"text": output_text}),
            ),
        )
        connection.commit()
    return ai_job_id


def _prompt_material(*, prompt: str, derivative_id: str) -> EgressPacketMaterial:
    return EgressPacketMaterial(
        operation=EXTERNAL_PROVIDER_OPERATION,
        task_kind="general",
        route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        fallback_index=0,
        prompt=prompt,
        context_blocks=(),
        prompt_level="S1",
        context_level="S0",
        final_level="S1",
        max_output_tokens=128,
        workspace_id=WORKSPACE_ID,
        prompt_derivative_id=derivative_id,
    )


def test_prompt_derivative_preserves_exact_prompt_digest_and_creates_audit(monkeypatch):
    _bootstrap(monkeypatch)
    raw_prompt = "  private project question with intentional spacing  "

    approval = create_prompt_derivative(
        raw_prompt=raw_prompt,
        derivative_content="Generic public-domain engineering question.",
        final_level="S1",
        transformations=["Removed project identity"],
        sanitizer_kind="deterministic",
        sanitizer_version="prompt-redactor-v1",
        sanitizer_config_digest=CONFIG_DIGEST,
        workspace_id=WORKSPACE_ID,
        policy=_policy_100_percent(),
        now=NOW,
    )
    derivative = get_prompt_derivative(
        approval.derivative_id,
        workspace_id=WORKSPACE_ID,
    )

    assert approval.audit_item_id is not None
    assert derivative.raw_prompt_digest == sha256_text(raw_prompt)
    assert derivative.derivative_content == "Generic public-domain engineering question."
    assert raw_prompt not in repr(derivative)
    resolved = resolve_approved_prompt_derivative(
        raw_prompt=raw_prompt,
        workspace_id=WORKSPACE_ID,
        policy=_policy_100_percent(),
    )
    assert resolved is not None
    assert resolved.id == approval.derivative_id


def test_prompt_derivative_rejects_raw_secret_and_surviving_sensitive_output(monkeypatch):
    _bootstrap(monkeypatch)

    with pytest.raises(SensitivityPolicyError, match="secret marker"):
        create_prompt_derivative(
            raw_prompt="api_key=super-secret-value",
            derivative_content="Generic question.",
            final_level="S1",
            transformations=["Removed credential"],
            sanitizer_kind="deterministic",
            sanitizer_version="prompt-redactor-v1",
            sanitizer_config_digest=CONFIG_DIGEST,
            workspace_id=WORKSPACE_ID,
        )

    with pytest.raises(SensitivityPolicyError, match="external-ineligible"):
        create_prompt_derivative(
            raw_prompt="private project question",
            derivative_content="Zephyr proprietary geometry remains here.",
            final_level="S1",
            transformations=["Incomplete rewrite"],
            sanitizer_kind="deterministic",
            sanitizer_version="prompt-redactor-v1",
            sanitizer_config_digest=CONFIG_DIGEST,
            workspace_id=WORKSPACE_ID,
        )


def test_model_backed_sanitizer_requires_exact_local_ai_job_provenance(monkeypatch):
    _bootstrap(monkeypatch)
    local_output = "Generic engineering question."
    external_output = "Another generic engineering question."
    local_job = _insert_ai_job(route="local:fast", output_text=local_output)
    external_job = _insert_ai_job(route="external:cheap", output_text=external_output)

    approval = create_prompt_derivative(
        raw_prompt="private project question",
        derivative_content=local_output,
        final_level="S1",
        transformations=["Local model removed project details"],
        sanitizer_kind="model_local",
        sanitizer_version="local-sanitizer-v1",
        sanitizer_config_digest=CONFIG_DIGEST,
        sanitizer_ai_job_id=local_job,
        workspace_id=WORKSPACE_ID,
    )
    assert approval.sanitizer_ai_job_id == local_job

    with pytest.raises(SensitivityPolicyError, match="dedicated local-route"):
        create_prompt_derivative(
            raw_prompt="another private project question",
            derivative_content=external_output,
            final_level="S1",
            transformations=["Attempted external sanitizer"],
            sanitizer_kind="model_local",
            sanitizer_version="local-sanitizer-v1",
            sanitizer_config_digest=CONFIG_DIGEST,
            sanitizer_ai_job_id=external_job,
            workspace_id=WORKSPACE_ID,
        )


def test_model_backed_sanitizer_rejects_output_digest_mismatch(monkeypatch):
    _bootstrap(monkeypatch)
    ai_job_id = _insert_ai_job(
        route="local:fast",
        output_text="Different generic output.",
    )

    with pytest.raises(SensitivityPolicyError, match="matching output provenance"):
        create_prompt_derivative(
            raw_prompt="private project question",
            derivative_content="Generic engineering question.",
            final_level="S1",
            transformations=["Claimed local rewrite"],
            sanitizer_kind="model_local",
            sanitizer_version="local-sanitizer-v1",
            sanitizer_config_digest=CONFIG_DIGEST,
            sanitizer_ai_job_id=ai_job_id,
            workspace_id=WORKSPACE_ID,
        )

    with open_sqlite_connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) AS count FROM egress_prompt_derivatives"
        ).fetchone()["count"]
    assert count == 0


def test_auto_canonical_derivative_persists_provenance_and_is_previewable(monkeypatch):
    _bootstrap(monkeypatch)
    decision_id = _decision("Zephyr proprietary geometry decision")
    source_ref = f"decision:{decision_id}"
    create_sensitivity_label(
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
        transformations=["Removed project dimensions and geometry"],
        sanitizer_kind="deterministic",
        sanitizer_version="canonical-redactor-v1",
        sanitizer_config_digest=CONFIG_DIGEST,
        policy=_policy_100_percent(),
        now=NOW,
    )

    derivative = get_sanitized_derivative(WORKSPACE_ID, approval.derivative_id)
    assert derivative.status == "approved"
    assert derivative.effective_level == "S1"
    assert approval.audit_item_id is not None
    with open_sqlite_connection() as connection:
        row = connection.execute(
            """
            SELECT sanitizer_kind, sanitizer_version, sanitizer_config_digest,
                   sanitizer_ai_job_id, approval_source, auto_approved
            FROM sanitized_derivatives WHERE id = ?
            """,
            (approval.derivative_id,),
        ).fetchone()
    assert row["sanitizer_kind"] == "deterministic"
    assert row["sanitizer_version"] == "canonical-redactor-v1"
    assert row["sanitizer_config_digest"] == CONFIG_DIGEST
    assert row["sanitizer_ai_job_id"] is None
    assert row["approval_source"] == "policy-sanitizer-v1"
    assert row["auto_approved"] == 1

    from app.modules.ai.context_builder import ContextSelectionSpec

    preview = build_external_context_preview(
        WORKSPACE_ID,
        32_000,
        ContextSelectionSpec(kinds=["decision"], ids=[decision_id]),
    )
    assert preview.included_count == 1
    assert preview.blocks[0]["source"] == f"derivative:{approval.derivative_id}"


def test_exact_auto_derivative_is_reused_without_duplicate_audit(monkeypatch):
    _bootstrap(monkeypatch)
    decision_id = _decision("Zephyr proprietary geometry decision")
    source_ref = f"decision:{decision_id}"
    create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=source_ref,
            level="S3",
        )
    )
    kwargs = {
        "workspace_id": WORKSPACE_ID,
        "source_refs": [source_ref],
        "derivative_content": "Generic tubular concept.",
        "final_level": "S1",
        "transformations": ["Removed project-specific geometry"],
        "sanitizer_kind": "deterministic",
        "sanitizer_version": "canonical-redactor-v1",
        "sanitizer_config_digest": CONFIG_DIGEST,
        "policy": _policy_100_percent(),
        "now": NOW,
    }

    first = auto_approve_canonical_derivative(**kwargs)
    second = auto_approve_canonical_derivative(**kwargs)

    assert first.reused is False
    assert second.reused is True
    assert second.derivative_id == first.derivative_id
    assert second.audit_item_id == first.audit_item_id
    with open_sqlite_connection() as connection:
        derivative_count = connection.execute(
            "SELECT COUNT(*) AS count FROM sanitized_derivatives"
        ).fetchone()["count"]
        audit_count = connection.execute(
            "SELECT COUNT(*) AS count FROM sanitizer_audit_items"
        ).fetchone()["count"]
    assert derivative_count == 1
    assert audit_count == 1


def test_prompt_audit_rejection_revokes_ticket_and_releases_active_reservation(monkeypatch):
    _bootstrap(monkeypatch)
    approval = create_prompt_derivative(
        raw_prompt="private project question",
        derivative_content="Generic engineering question.",
        final_level="S1",
        transformations=["Removed project identity"],
        sanitizer_kind="deterministic",
        sanitizer_version="prompt-redactor-v1",
        sanitizer_config_digest=CONFIG_DIGEST,
        workspace_id=WORKSPACE_ID,
        policy=_policy_100_percent(),
        now=NOW,
    )
    preparation = prepare_egress_attempt(
        _prompt_material(
            prompt="Generic engineering question.",
            derivative_id=approval.derivative_id,
        ),
        policy=_policy_100_percent(),
        now=NOW + timedelta(seconds=1),
    )
    consumed = consume_confirmation_ticket(
        preparation.ticket_id,
        policy=_policy_100_percent(),
        now=NOW + timedelta(seconds=2),
    )
    assert consumed.authorized is True

    result = review_sanitizer_audit_item(
        approval.audit_item_id,
        disposition="rejected",
        notes="Project meaning survived the rewrite.",
        now=NOW + timedelta(seconds=3),
    )

    assert result.invalidated_packet_count == 1
    assert result.revoked_ticket_count == 0
    assert result.released_reservation_count == 1
    assert get_prompt_derivative(
        approval.derivative_id,
        workspace_id=WORKSPACE_ID,
    ).status == "revoked"
    with open_sqlite_connection() as connection:
        reservation = connection.execute(
            "SELECT state, reconciliation_status FROM egress_budget_reservations WHERE id = ?",
            (consumed.reservation_id,),
        ).fetchone()
    assert reservation["state"] == "released"
    assert reservation["reconciliation_status"] == "sanitizer_audit_rejected_before_start"
    assert (
        resolve_approved_prompt_derivative(
            raw_prompt="private project question",
            workspace_id=WORKSPACE_ID,
            policy=_policy_100_percent(),
        )
        is None
    )


def test_canonical_audit_rejection_revokes_derivative_and_pending_ticket(monkeypatch):
    _bootstrap(monkeypatch)
    decision_id = _decision("Zephyr proprietary geometry decision")
    source_ref = f"decision:{decision_id}"
    create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=source_ref,
            level="S3",
        )
    )
    approval = auto_approve_canonical_derivative(
        workspace_id=WORKSPACE_ID,
        source_refs=[source_ref],
        derivative_content="Generic tubular concept.",
        final_level="S1",
        transformations=["Removed project-specific geometry"],
        sanitizer_kind="deterministic",
        sanitizer_version="canonical-redactor-v1",
        sanitizer_config_digest=CONFIG_DIGEST,
        policy=_policy_100_percent(),
        now=NOW,
    )
    snapshot = resolve_source_snapshot(WORKSPACE_ID, source_ref)
    material = EgressPacketMaterial(
        operation=EXTERNAL_PROVIDER_OPERATION,
        task_kind="general",
        route_class="external:cheap",
        provider_id="deepseek",
        model_id="deepseek-v4-pro",
        fallback_index=0,
        prompt="Summarize the generic concept.",
        context_blocks=(
            {
                "source": f"derivative:{approval.derivative_id}",
                "type": "sanitized_derivative",
                "id": approval.derivative_id,
                "content": "Generic tubular concept.",
            },
        ),
        prompt_level="S1",
        context_level="S1",
        final_level="S1",
        max_output_tokens=128,
        workspace_id=WORKSPACE_ID,
        included_manifest=(
            {
                "source_ref": f"derivative:{approval.derivative_id}",
                "source_refs": [source_ref],
                "content_digest": approval.derivative_digest,
                "effective_level": "S1",
                "derivative_id": approval.derivative_id,
            },
        ),
        source_digests=((source_ref, snapshot.content_digest),),
    )
    preparation = prepare_egress_attempt(
        material,
        policy=_policy_100_percent(),
        now=NOW + timedelta(seconds=1),
    )
    assert preparation.ticket_id is not None

    result = review_sanitizer_audit_item(
        approval.audit_item_id,
        disposition="rejected",
        now=NOW + timedelta(seconds=2),
    )

    assert result.invalidated_packet_count == 1
    assert result.revoked_ticket_count == 1
    assert get_sanitized_derivative(WORKSPACE_ID, approval.derivative_id).status == "revoked"
    with open_sqlite_connection() as connection:
        ticket = connection.execute(
            "SELECT state, revocation_reason FROM egress_confirmation_tickets WHERE id = ?",
            (preparation.ticket_id,),
        ).fetchone()
    assert ticket["state"] == "revoked"
    assert ticket["revocation_reason"] == "sanitizer_audit_rejected"


def test_audit_acceptance_keeps_derivative_current_and_event_payloads_body_free(monkeypatch):
    _bootstrap(monkeypatch)
    raw_prompt = "private project question"
    derivative_body = "Generic engineering question."
    notes = "Reviewed and safe."
    approval = create_prompt_derivative(
        raw_prompt=raw_prompt,
        derivative_content=derivative_body,
        final_level="S1",
        transformations=["Removed project identity"],
        sanitizer_kind="deterministic",
        sanitizer_version="prompt-redactor-v1",
        sanitizer_config_digest=CONFIG_DIGEST,
        workspace_id=WORKSPACE_ID,
        policy=_policy_100_percent(),
        now=NOW,
    )

    result = review_sanitizer_audit_item(
        approval.audit_item_id,
        disposition="accepted",
        notes=notes,
        now=NOW + timedelta(seconds=1),
    )

    assert result.state == "accepted"
    assert get_prompt_derivative(
        approval.derivative_id,
        workspace_id=WORKSPACE_ID,
    ).status == "approved"
    with open_sqlite_connection() as connection:
        payloads = [
            row["payload"]
            for row in connection.execute(
                "SELECT payload FROM events ORDER BY created_at, id"
            ).fetchall()
            if row["payload"] is not None
        ]
    serialized = json.dumps(payloads)
    assert raw_prompt not in serialized
    assert derivative_body not in serialized
    assert notes not in serialized
    assert sha256_text(notes) in serialized
