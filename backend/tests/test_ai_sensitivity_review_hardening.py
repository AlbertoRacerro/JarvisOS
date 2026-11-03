from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.modules.ai.gateway as gateway_module
from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.context_builder import (
    ContextSelectionSpec,
    build_workspace_context_bundle,
)
from app.modules.ai.sensitivity import (
    SensitivityPolicyError,
    approve_sanitized_derivative,
    build_external_context_preview,
    create_sanitized_derivative,
    create_sensitivity_label,
    get_sanitized_derivative,
)
from app.modules.ai.sensitivity_models import (
    SanitizedDerivativeCreate,
    SensitivityLabelCreate,
)
from app.modules.ai.sensitivity_routes import router as sensitivity_router
from app.modules.events.service import utc_now

WORKSPACE_ID = "main"


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(sensitivity_router)
    return TestClient(app)


def _bootstrap() -> None:
    initialize_database()
    now = utc_now()
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO workspaces (
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


def _update_decision(record_id: str, text: str) -> None:
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            UPDATE decisions
            SET decision_text = ?, updated_at = ?
            WHERE id = ?
            """,
            (text, utc_now(), record_id),
        )
        connection.commit()


def _requirement(text: str) -> str:
    record_id = str(uuid4())
    now = utc_now()
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO requirements (
                id, workspace_id, statement, rationale, status, notes,
                schema_version, created_at, updated_at
            ) VALUES (?, ?, ?, NULL, 'active', NULL, 1, ?, ?)
            """,
            (record_id, WORKSPACE_ID, text, now, now),
        )
        connection.commit()
    return record_id


def _label(record_id: str, level: str) -> None:
    create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=f"decision:{record_id}",
            level=level,
        )
    )


def _draft_derivative(
    record_id: str,
    *,
    content: str = "Generic sanitized design summary.",
    effective_level: str = "S1",
):
    return create_sanitized_derivative(
        SanitizedDerivativeCreate(
            workspace_id=WORKSPACE_ID,
            source_refs=[f"decision:{record_id}"],
            content=content,
            effective_level=effective_level,
            transformations=["Removed project-specific values"],
        )
    )


def _event_count(event_type: str | None = None) -> int:
    with open_sqlite_connection() as connection:
        if event_type is None:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM events"
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM events
                WHERE event_type = ?
                """,
                (event_type,),
            ).fetchone()
    assert row is not None
    return int(row["count"])


def test_preview_routes_map_missing_workspace_to_404() -> None:
    initialize_database()
    with _client() as client:
        external = client.post(
            "/ai/sensitivity/context-preview",
            json={
                "workspace_id": "missing",
                "budget_chars": 32_000,
                "selection": {"kinds": ["decision"]},
            },
        )
        manual = client.post(
            "/ai/sensitivity/manual-context-preview",
            json={
                "workspace_id": "missing",
                "budget_chars": 32_000,
                "context_blocks": [
                    {
                        "source": "manual:user",
                        "content": "Generic context",
                    }
                ],
            },
        )

    assert external.status_code == 404
    assert external.json() == {"detail": "Workspace not found."}
    assert manual.status_code == 404
    assert manual.json() == {"detail": "Workspace not found."}


def test_failed_draft_approval_persists_stale_state_and_event() -> None:
    _bootstrap()
    record_id = _decision("Zephyr proprietary geometry decision")
    _label(record_id, "S3")
    derivative = _draft_derivative(record_id)
    _update_decision(
        record_id,
        "Changed Zephyr proprietary geometry decision",
    )
    before_events = _event_count(
        "SanitizedDerivativeMarkedStale"
    )

    with pytest.raises(
        SensitivityPolicyError,
        match="Derivative sources are stale",
    ):
        approve_sanitized_derivative(
            WORKSPACE_ID,
            derivative.id,
        )

    stored = get_sanitized_derivative(
        WORKSPACE_ID,
        derivative.id,
        refresh=False,
    )
    assert stored.status == "stale"
    assert stored.stale_reason == (
        f"source_digest_mismatch:decision:{record_id}"
    )
    assert _event_count(
        "SanitizedDerivativeMarkedStale"
    ) == before_events + 1


def test_derivative_get_is_read_only_and_revalidate_is_explicit() -> None:
    _bootstrap()
    record_id = _decision("Zephyr proprietary geometry decision")
    _label(record_id, "S3")
    derivative = approve_sanitized_derivative(
        WORKSPACE_ID,
        _draft_derivative(record_id).id,
    )
    _update_decision(
        record_id,
        "Changed Zephyr proprietary geometry decision",
    )
    before_events = _event_count()

    with _client() as client:
        read_response = client.get(
            f"/ai/sensitivity/derivatives/{derivative.id}",
            params={"workspace_id": WORKSPACE_ID},
        )
        after_get_events = _event_count()
        persisted_after_get = get_sanitized_derivative(
            WORKSPACE_ID,
            derivative.id,
            refresh=False,
        )
        revalidate_response = client.post(
            f"/ai/sensitivity/derivatives/{derivative.id}/revalidate",
            params={"workspace_id": WORKSPACE_ID},
        )

    assert read_response.status_code == 200
    assert read_response.json()["status"] == "approved"
    assert persisted_after_get.status == "approved"
    assert after_get_events == before_events
    assert revalidate_response.status_code == 200
    assert revalidate_response.json()["status"] == "stale"
    assert _event_count(
        "SanitizedDerivativeMarkedStale"
    ) == 1


def test_derivative_inherits_source_budget_priority() -> None:
    _bootstrap()
    public_requirement_id = _requirement(
        "Public requirement " + "p" * 260
    )
    sensitive_id = _decision(
        "Zephyr proprietary geometry decision " + "s" * 260
    )
    create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=f"requirement:{public_requirement_id}",
            level="S1",
        )
    )
    _label(sensitive_id, "S3")
    derivative = approve_sanitized_derivative(
        WORKSPACE_ID,
        _draft_derivative(
            sensitive_id,
            content="Sanitized design note " + "d" * 260,
        ).id,
    )

    preview = build_external_context_preview(
        WORKSPACE_ID,
        500,
        ContextSelectionSpec(
            kinds=["decision", "requirement"],
            ids=[public_requirement_id, sensitive_id],
        ),
    )

    assert preview.included_count == 1
    assert preview.blocks[0]["source"] == (
        f"derivative:{derivative.id}"
    )
    assert preview.dropped_count == 1
    assert preview.dropped_sources_manifest[0][
        "source_ref"
    ] == f"requirement:{public_requirement_id}"


def test_sensitivity_preview_matches_raw_pack_and_calls_no_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bootstrap()
    record_id = _decision("Generic public engineering note")
    _label(record_id, "S1")
    selection = ContextSelectionSpec(
        kinds=["decision"],
        ids=[record_id],
    )

    def forbidden_provider_call(*args, **kwargs):
        raise AssertionError(
            "Sensitivity preview must not call the AI gateway."
        )

    monkeypatch.setattr(
        gateway_module.AIGateway,
        "run_task",
        forbidden_provider_call,
    )
    raw = build_workspace_context_bundle(
        WORKSPACE_ID,
        budget_chars=32_000,
        selection=selection,
    )
    protected = build_external_context_preview(
        WORKSPACE_ID,
        32_000,
        selection,
    )

    assert protected.blocks == raw.blocks
    assert protected.context_digest == raw.context_digest
    assert protected.included_count == raw.included_count
    assert protected.dropped_count == raw.dropped_count


def test_route_status_contract_for_policy_and_validation_errors() -> None:
    _bootstrap()
    record_id = _decision("Zephyr proprietary geometry decision")
    _label(record_id, "S3")

    with _client() as client:
        downgrade = client.post(
            "/ai/sensitivity/labels",
            json={
                "workspace_id": WORKSPACE_ID,
                "subject_ref": f"decision:{record_id}",
                "level": "S1",
            },
        )
        malformed = client.post(
            "/ai/sensitivity/manual-context-preview",
            json={
                "workspace_id": WORKSPACE_ID,
                "context_blocks": [
                    {
                        "source": "manual:user",
                        "content": "Private context",
                        "sensitivity": "S0",
                    }
                ],
            },
        )

    assert downgrade.status_code == 409
    assert malformed.status_code == 422
