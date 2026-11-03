from __future__ import annotations

from uuid import uuid4

import pytest

import app.modules.ai.sensitivity as sensitivity_module
from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.context_builder import ContextSelectionSpec
from app.modules.ai.sensitivity import (
    approve_sanitized_derivative,
    build_external_context_preview,
    create_sanitized_derivative,
    create_sensitivity_label,
    get_current_sensitivity_label,
    get_sanitized_derivative,
    preview_manual_context,
)
from app.modules.ai.sensitivity_models import (
    SanitizedDerivativeCreate,
    SensitivityLabelCreate,
)
from app.modules.events.service import utc_now

WORKSPACE_ID = "main"


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


def _label(record_id: str, level: str) -> None:
    create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=f"decision:{record_id}",
            level=level,
        )
    )


def _selection(*record_ids: str) -> ContextSelectionSpec:
    return ContextSelectionSpec(
        kinds=["decision"],
        ids=list(record_ids),
    )


def _approved_derivative(record_id: str, level: str):
    derivative = create_sanitized_derivative(
        SanitizedDerivativeCreate(
            workspace_id=WORKSPACE_ID,
            source_refs=[f"decision:{record_id}"],
            content=(
                "Confidential sanitized project summary."
                if level == "S2"
                else "Generic sanitized project summary."
            ),
            effective_level=level,
            transformations=["Removed project-specific details"],
        )
    )
    return approve_sanitized_derivative(WORKSPACE_ID, derivative.id)


def test_approved_s2_derivative_is_withheld_from_external_preview() -> None:
    _bootstrap()
    record_id = _decision("Zephyr proprietary geometry decision")
    _label(record_id, "S3")
    derivative = _approved_derivative(record_id, "S2")

    preview = build_external_context_preview(
        WORKSPACE_ID,
        32_000,
        _selection(record_id),
    )

    assert preview.blocks == []
    assert preview.included_count == 0
    assert preview.withheld_sources_manifest == [
        {
            "source_ref": f"decision:{record_id}",
            "effective_level": "S2",
            "derivative_id": derivative.id,
            "reason": "derivative_level_not_external_eligible",
        }
    ]


def test_approved_s2_derivative_is_withheld_from_manual_preview() -> None:
    _bootstrap()
    record_id = _decision("Zephyr proprietary geometry decision")
    _label(record_id, "S3")
    derivative = _approved_derivative(record_id, "S2")

    preview = preview_manual_context(
        WORKSPACE_ID,
        [
            {
                "source": f"derivative:{derivative.id}",
                "type": "sanitized_derivative",
                "id": derivative.id,
                "content": derivative.content,
            }
        ],
        32_000,
    )

    assert preview.blocks == []
    assert preview.withheld_sources_manifest == [
        {
            "source_ref": f"derivative:{derivative.id}",
            "effective_level": "S2",
            "derivative_id": derivative.id,
            "reason": "derivative_level_not_external_eligible",
        }
    ]


def test_latest_label_uses_insert_order_not_wall_clock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bootstrap()
    record_id = _decision("Generic public engineering note")
    timestamps = iter(
        [
            "2026-07-12T10:00:00+00:00",
            "2026-07-12T09:00:00+00:00",
        ]
    )
    monkeypatch.setattr(sensitivity_module, "utc_now", lambda: next(timestamps))

    _label(record_id, "S1")
    _label(record_id, "S4")

    current = get_current_sensitivity_label(
        WORKSPACE_ID,
        f"decision:{record_id}",
    )
    preview = build_external_context_preview(
        WORKSPACE_ID,
        32_000,
        _selection(record_id),
    )

    assert current is not None
    assert current.level == "S4"
    assert current.created_at == "2026-07-12T09:00:00+00:00"
    assert preview.blocks == []
    assert preview.withheld_sources_manifest[0]["effective_level"] == "S4"


def test_policy_version_change_invalidates_label_without_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bootstrap()
    record_id = _decision("Generic public engineering note")
    _label(record_id, "S1")
    monkeypatch.setattr(sensitivity_module, "POLICY_VERSION", "ip-egress-v2")

    label = get_current_sensitivity_label(
        WORKSPACE_ID,
        f"decision:{record_id}",
    )
    preview = build_external_context_preview(
        WORKSPACE_ID,
        32_000,
        _selection(record_id),
    )

    assert label is not None
    assert label.current is False
    assert label.stale_reason == "policy_version_mismatch"
    assert preview.blocks == []
    assert preview.withheld_sources_manifest[0]["reason"] == "stale_label"


def test_default_derivative_read_never_persists_staleness() -> None:
    _bootstrap()
    record_id = _decision("Zephyr proprietary geometry decision")
    _label(record_id, "S3")
    derivative = _approved_derivative(record_id, "S1")
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE decisions SET decision_text = ?, updated_at = ? WHERE id = ?",
            (
                "Changed Zephyr proprietary geometry decision",
                utc_now(),
                record_id,
            ),
        )
        before_events = connection.execute(
            "SELECT COUNT(*) AS count FROM events"
        ).fetchone()["count"]
        connection.commit()

    read = get_sanitized_derivative(WORKSPACE_ID, derivative.id)

    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT status, stale_reason FROM sanitized_derivatives WHERE id = ?",
            (derivative.id,),
        ).fetchone()
        after_events = connection.execute(
            "SELECT COUNT(*) AS count FROM events"
        ).fetchone()["count"]
    assert read.status == "approved"
    assert row is not None
    assert row["status"] == "approved"
    assert row["stale_reason"] is None
    assert after_events == before_events


def test_preview_eligibility_uses_one_read_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bootstrap()
    first = _decision("Generic public engineering note A")
    second = _decision("Generic public engineering note B")
    _label(first, "S1")
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE decisions SET updated_at = '9999-01-01T00:00:00+00:00' WHERE id = ?",
            (first,),
        )
        connection.execute(
            "UPDATE decisions SET updated_at = '0001-01-01T00:00:00+00:00' WHERE id = ?",
            (second,),
        )
        connection.commit()

    original = sensitivity_module._candidate_for_snapshot_in_connection
    calls = 0

    def mutate_after_first_candidate(*args, **kwargs):
        nonlocal calls
        result = original(*args, **kwargs)
        calls += 1
        if calls == 1:
            _label(first, "S4")
            _label(second, "S1")
        return result

    monkeypatch.setattr(
        sensitivity_module,
        "_candidate_for_snapshot_in_connection",
        mutate_after_first_candidate,
    )

    preview = build_external_context_preview(
        WORKSPACE_ID,
        32_000,
        _selection(first, second),
    )

    assert [block["source"] for block in preview.blocks] == [f"decision:{first}"]
    assert preview.withheld_sources_manifest == [
        {
            "source_ref": f"decision:{second}",
            "effective_level": "unknown",
            "reason": "missing_current_label",
        }
    ]
