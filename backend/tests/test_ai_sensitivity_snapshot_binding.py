from __future__ import annotations

from uuid import uuid4

import pytest

import app.modules.ai.sensitivity as sensitivity_module
from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.context_builder import ContextSelectionSpec
from app.modules.ai.sensitivity import (
    build_external_context_preview,
    create_sensitivity_label,
)
from app.modules.ai.sensitivity_models import SensitivityLabelCreate
from app.modules.events.service import utc_now

WORKSPACE_ID = "main"


def _bootstrap() -> None:
    initialize_database()
    now = utc_now()
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO workspaces (id, name, slug, description, status, created_at, updated_at)
            VALUES (?, 'Zephyr', 'main', NULL, 'active', ?, ?)
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


def _parameter() -> str:
    record_id = str(uuid4())
    now = utc_now()
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO parameters (
                id, workspace_id, name, symbol, value, unit, value_status,
                value_min, value_max, source_ref, confidence, status,
                created_at, updated_at, notes
            ) VALUES (?, ?, 'Flow rate', 'F', '10', 'kg/s', 'accepted',
                      NULL, NULL, 'public-reference', NULL, 'draft', ?, ?, 'needle-query')
            """,
            (record_id, WORKSPACE_ID, now, now),
        )
        connection.commit()
    return record_id


def _evidence() -> str:
    record_id = str(uuid4())
    artifact_id = str(uuid4())
    now = utc_now()
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO artifacts (
                id, workspace_id, filename, stored_path, artifact_type, created_at
            ) VALUES (?, ?, 'snapshot-evidence.json', ?, 'test_report', ?)
            """,
            (
                artifact_id,
                WORKSPACE_ID,
                f"test-artifacts/{artifact_id}.json",
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO evidence_records (
                id, workspace_id, kind, verdict, metrics_json, source_run_id,
                candidate_id, attempt_id, report_artifact_id, created_at
            ) VALUES (?, ?, 'validation_v0', 'pass', '{}', NULL, NULL, NULL, ?, ?)
            """,
            (record_id, WORKSPACE_ID, artifact_id, now),
        )
        connection.commit()
    return record_id


def _label(subject_ref: str) -> None:
    create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=subject_ref,
            level="S1",
        )
    )


def test_parameter_status_and_query_change_do_not_mix_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bootstrap()
    record_id = _parameter()
    subject_ref = f"parameter:{record_id}"
    _label(subject_ref)
    original_select = sensitivity_module.select_context_records

    def select_then_change_predicates(*args, **kwargs):
        selected = original_select(*args, **kwargs)
        with open_sqlite_connection() as connection:
            connection.execute(
                """
                UPDATE parameters
                SET value_status = 'candidate', notes = 'no-longer-matches'
                WHERE id = ?
                """,
                (record_id,),
            )
            connection.commit()
        return selected

    monkeypatch.setattr(
        sensitivity_module,
        "select_context_records",
        select_then_change_predicates,
    )

    preview = build_external_context_preview(
        WORKSPACE_ID,
        32_000,
        ContextSelectionSpec(kinds=["parameter"], query="needle-query"),
    )

    assert [block["source"] for block in preview.blocks] == [subject_ref]
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT value_status, notes FROM parameters WHERE id = ?",
            (record_id,),
        ).fetchone()
    assert row is not None
    assert row["value_status"] == "candidate"
    assert row["notes"] == "no-longer-matches"


def test_evidence_verdict_change_does_not_mix_snapshots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bootstrap()
    record_id = _evidence()
    subject_ref = f"evidence:{record_id}"
    _label(subject_ref)
    original_select = sensitivity_module.select_evidence_records

    def select_then_change_verdict(*args, **kwargs):
        selected = original_select(*args, **kwargs)
        with open_sqlite_connection() as connection:
            connection.execute(
                "UPDATE evidence_records SET verdict = 'fail' WHERE id = ?",
                (record_id,),
            )
            connection.commit()
        return selected

    monkeypatch.setattr(
        sensitivity_module,
        "select_evidence_records",
        select_then_change_verdict,
    )

    preview = build_external_context_preview(
        WORKSPACE_ID,
        32_000,
        ContextSelectionSpec(kinds=["evidence"]),
    )

    assert [block["source"] for block in preview.blocks] == [subject_ref]
    assert "verdict=pass" in preview.blocks[0]["content"]
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT verdict FROM evidence_records WHERE id = ?",
            (record_id,),
        ).fetchone()
    assert row is not None
    assert row["verdict"] == "fail"


def test_source_deletion_after_selection_uses_coherent_old_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _bootstrap()
    record_id = _decision("Original generic pump note")
    subject_ref = f"decision:{record_id}"
    _label(subject_ref)
    original_select = sensitivity_module.select_context_records

    def select_then_delete(*args, **kwargs):
        selected = original_select(*args, **kwargs)
        with open_sqlite_connection() as connection:
            connection.execute("DELETE FROM decisions WHERE id = ?", (record_id,))
            connection.commit()
        return selected

    monkeypatch.setattr(
        sensitivity_module,
        "select_context_records",
        select_then_delete,
    )

    preview = build_external_context_preview(
        WORKSPACE_ID,
        32_000,
        ContextSelectionSpec(kinds=["decision"], ids=[record_id]),
    )

    assert [block["source"] for block in preview.blocks] == [subject_ref]
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT id FROM decisions WHERE id = ?",
            (record_id,),
        ).fetchone()
    assert row is None
