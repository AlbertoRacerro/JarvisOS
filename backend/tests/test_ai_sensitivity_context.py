from __future__ import annotations

from uuid import uuid4

import pytest

from app.core.database import initialize_database, open_sqlite_connection
from app.core.schema import SCHEMA_SENSITIVITY_CONTEXT_MIGRATION_ID
from app.modules.ai.context_builder import ContextBlockError, ContextSelectionSpec
from app.modules.ai.sensitivity import (
    SensitivityPolicyError,
    approve_sanitized_derivative,
    build_external_context_preview,
    create_sanitized_derivative,
    create_sensitivity_label,
    deterministic_floor,
    get_current_sensitivity_label,
    preview_manual_context,
    revalidate_sanitized_derivative,
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
            INSERT INTO workspaces (id, name, slug, description, status, created_at, updated_at)
            VALUES (?, 'Zephyr', 'main', NULL, 'active', ?, ?)
            """,
            (WORKSPACE_ID, now, now),
        )
        connection.commit()


def _decision(text: str, *, status: str = "accepted") -> str:
    record_id = str(uuid4())
    now = utc_now()
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO decisions (
                id, workspace_id, title, decision_text, rationale, status,
                linked_run_id, created_at, updated_at, notes
            ) VALUES (?, ?, 'Decision', ?, NULL, ?, NULL, ?, ?, NULL)
            """,
            (record_id, WORKSPACE_ID, text, status, now, now),
        )
        connection.commit()
    return record_id


def _update_decision(record_id: str, text: str) -> None:
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE decisions SET decision_text = ?, updated_at = ? WHERE id = ?",
            (text, utc_now(), record_id),
        )
        connection.commit()


def _selection(record_id: str) -> ContextSelectionSpec:
    return ContextSelectionSpec(kinds=["decision"], ids=[record_id])


def test_schema_migration_creates_policy_sidecars() -> None:
    _bootstrap()
    with open_sqlite_connection() as connection:
        migration = connection.execute(
            "SELECT status FROM schema_migrations WHERE migration_id = ?",
            (SCHEMA_SENSITIVITY_CONTEXT_MIGRATION_ID,),
        ).fetchone()
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert migration is not None
    assert migration["status"] == "applied"
    assert {"sensitivity_labels", "sanitized_derivatives"}.issubset(tables)


def test_legacy_unlabelled_record_is_unknown_and_withheld() -> None:
    _bootstrap()
    record_id = _decision("Generic pump sizing note")

    preview = build_external_context_preview(
        WORKSPACE_ID, 32_000, _selection(record_id)
    )

    assert preview.blocks == []
    assert preview.included_count == 0
    assert preview.withheld_count == 1
    assert preview.withheld_sources_manifest == [
        {
            "source_ref": f"decision:{record_id}",
            "effective_level": "unknown",
            "reason": "missing_current_label",
        }
    ]
    assert "content" not in preview.withheld_sources_manifest[0]


def test_current_s1_label_allows_raw_external_preview() -> None:
    _bootstrap()
    record_id = _decision("Generic public-domain pump sizing note")
    label = create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=f"decision:{record_id}",
            level="S1",
        )
    )

    preview = build_external_context_preview(
        WORKSPACE_ID, 32_000, _selection(record_id)
    )

    assert label.current is True
    assert preview.included_count == 1
    assert preview.withheld_count == 0
    assert preview.blocks[0]["source"] == f"decision:{record_id}"
    assert preview.included_sources_manifest[0]["label_id"] == label.id
    assert preview.included_sources_manifest[0]["effective_level"] == "S1"


def test_explicit_id_does_not_bypass_sensitive_withholding() -> None:
    _bootstrap()
    record_id = _decision("Zephyr proprietary geometry decision")
    create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=f"decision:{record_id}",
            level="S3",
        )
    )

    preview = build_external_context_preview(
        WORKSPACE_ID, 32_000, _selection(record_id)
    )

    assert preview.blocks == []
    assert preview.withheld_sources_manifest[0]["effective_level"] == "S3"
    assert preview.withheld_sources_manifest[0]["reason"] == "raw_level_not_external_eligible"


def test_source_mutation_makes_label_stale() -> None:
    _bootstrap()
    record_id = _decision("Initial generic note")
    create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=f"decision:{record_id}",
            level="S1",
        )
    )
    _update_decision(record_id, "Changed generic note")

    label = get_current_sensitivity_label(WORKSPACE_ID, f"decision:{record_id}")
    preview = build_external_context_preview(
        WORKSPACE_ID, 32_000, _selection(record_id)
    )

    assert label is not None
    assert label.current is False
    assert label.stale_reason == "content_digest_mismatch"
    assert preview.blocks == []
    assert preview.withheld_sources_manifest[0]["reason"] == "stale_label"


def test_s2_or_higher_source_cannot_be_downgraded_in_place() -> None:
    _bootstrap()
    record_id = _decision("Zephyr proprietary geometry decision")
    create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=f"decision:{record_id}",
            level="S3",
        )
    )

    with pytest.raises(SensitivityPolicyError, match="cannot be downgraded"):
        create_sensitivity_label(
            SensitivityLabelCreate(
                workspace_id=WORKSPACE_ID,
                subject_ref=f"decision:{record_id}",
                level="S1",
            )
        )


def test_deterministic_secret_floor_overrides_human_label() -> None:
    _bootstrap()
    record_id = _decision("api_key=super-secret-value")

    label = create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=f"decision:{record_id}",
            level="S0",
        )
    )

    assert deterministic_floor("api_key=super-secret-value") == "S4"
    assert label.level == "S4"
    assert label.classification_source == "deterministic_floor"


def test_approved_derivative_replaces_raw_s3_source_with_provenance() -> None:
    _bootstrap()
    record_id = _decision("Zephyr proprietary geometry decision")
    source_ref = f"decision:{record_id}"
    create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=source_ref,
            level="S3",
        )
    )
    derivative = create_sanitized_derivative(
        SanitizedDerivativeCreate(
            workspace_id=WORKSPACE_ID,
            source_refs=[source_ref],
            content="A generic tubular photobioreactor design is under study.",
            effective_level="S1",
            transformations=["Removed dimensions and project-specific geometry"],
        )
    )
    derivative = approve_sanitized_derivative(WORKSPACE_ID, derivative.id)

    preview = build_external_context_preview(
        WORKSPACE_ID, 32_000, _selection(record_id)
    )

    assert derivative.status == "approved"
    assert derivative.source_digests[source_ref]
    assert preview.included_count == 1
    assert preview.withheld_count == 0
    assert preview.blocks[0]["source"] == f"derivative:{derivative.id}"
    manifest = preview.included_sources_manifest[0]
    assert manifest["source_refs"] == [source_ref]
    assert manifest["derivative_id"] == derivative.id
    assert manifest["effective_level"] == "S1"


def test_source_mutation_marks_approved_derivative_stale() -> None:
    _bootstrap()
    record_id = _decision("Zephyr proprietary geometry decision")
    source_ref = f"decision:{record_id}"
    create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=source_ref,
            level="S3",
        )
    )
    derivative = create_sanitized_derivative(
        SanitizedDerivativeCreate(
            workspace_id=WORKSPACE_ID,
            source_refs=[source_ref],
            content="Generic sanitized design summary.",
            effective_level="S1",
            transformations=["Removed proprietary values"],
        )
    )
    approve_sanitized_derivative(WORKSPACE_ID, derivative.id)
    _update_decision(record_id, "Changed Zephyr proprietary geometry decision")

    refreshed = revalidate_sanitized_derivative(WORKSPACE_ID, derivative.id)
    preview = build_external_context_preview(
        WORKSPACE_ID, 32_000, _selection(record_id)
    )

    assert refreshed.status == "stale"
    assert refreshed.stale_reason == f"source_digest_mismatch:{source_ref}"
    assert preview.blocks == []


def test_s4_source_derivative_cannot_be_declared_s2() -> None:
    _bootstrap()
    record_id = _decision("password=super-secret-value")
    source_ref = f"decision:{record_id}"
    create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=source_ref,
            level="S4",
        )
    )

    with pytest.raises(SensitivityPolicyError, match="S4 source"):
        create_sanitized_derivative(
            SanitizedDerivativeCreate(
                workspace_id=WORKSPACE_ID,
                source_refs=[source_ref],
                content="Generic account configuration concept.",
                effective_level="S2",
                transformations=["Removed credentials"],
            )
        )


def test_manual_block_cannot_self_declare_external_safe_level() -> None:
    _bootstrap()

    with pytest.raises(ContextBlockError, match="unknown keys"):
        preview_manual_context(
            WORKSPACE_ID,
            [
                {
                    "source": "manual:user",
                    "content": "Private project content",
                    "sensitivity": "S0",
                }
            ],
            32_000,
        )


def test_manual_context_accepts_only_exact_approved_derivative() -> None:
    _bootstrap()
    record_id = _decision("Zephyr proprietary geometry decision")
    source_ref = f"decision:{record_id}"
    create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=source_ref,
            level="S3",
        )
    )
    derivative = create_sanitized_derivative(
        SanitizedDerivativeCreate(
            workspace_id=WORKSPACE_ID,
            source_refs=[source_ref],
            content="Generic sanitized design summary.",
            effective_level="S1",
            transformations=["Removed proprietary values"],
        )
    )
    derivative = approve_sanitized_derivative(WORKSPACE_ID, derivative.id)

    accepted = preview_manual_context(
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
    altered = preview_manual_context(
        WORKSPACE_ID,
        [
            {
                "source": f"derivative:{derivative.id}",
                "id": derivative.id,
                "content": derivative.content + " altered",
            }
        ],
        32_000,
    )

    assert accepted.included_count == 1
    assert accepted.withheld_count == 0
    assert altered.included_count == 0
    assert altered.withheld_sources_manifest[0]["reason"] == "derivative_content_digest_mismatch"
    assert "content" not in altered.withheld_sources_manifest[0]


def test_sensitivity_withholding_is_distinct_from_budget_dropping() -> None:
    _bootstrap()
    first = _decision("Generic note one")
    second = _decision("Generic note two")
    third = _decision("Zephyr proprietary geometry decision")
    for record_id in (first, second):
        create_sensitivity_label(
            SensitivityLabelCreate(
                workspace_id=WORKSPACE_ID,
                subject_ref=f"decision:{record_id}",
                level="S1",
            )
        )
    create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=f"decision:{third}",
            level="S3",
        )
    )

    preview = build_external_context_preview(
        WORKSPACE_ID,
        180,
        ContextSelectionSpec(kinds=["decision"], ids=[first, second, third]),
    )

    assert preview.withheld_count == 1
    assert preview.withheld_sources_manifest[0]["source_ref"] == f"decision:{third}"
    assert preview.dropped_count >= 1
    assert preview.included_count + preview.dropped_count == 2


def _derivative_db_state(derivative_id: str) -> tuple[str, str | None, int]:
    with open_sqlite_connection() as connection:
        row = connection.execute(
            "SELECT status, stale_reason FROM sanitized_derivatives WHERE id = ?",
            (derivative_id,),
        ).fetchone()
        event_count = connection.execute("SELECT COUNT(*) AS count FROM events").fetchone()["count"]
    assert row is not None
    return row["status"], row["stale_reason"], int(event_count)


def test_external_preview_relabel_staleness_is_read_only() -> None:
    _bootstrap()
    record_id = _decision("Zephyr proprietary geometry decision")
    source_ref = f"decision:{record_id}"
    create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=source_ref,
            level="S3",
        )
    )
    derivative = create_sanitized_derivative(
        SanitizedDerivativeCreate(
            workspace_id=WORKSPACE_ID,
            source_refs=[source_ref],
            content="Confidential sanitized project summary.",
            effective_level="S2",
            transformations=["Removed secret material"],
        )
    )
    derivative = approve_sanitized_derivative(WORKSPACE_ID, derivative.id)
    create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=source_ref,
            level="S4",
        )
    )
    before = _derivative_db_state(derivative.id)

    preview = build_external_context_preview(WORKSPACE_ID, 32_000, _selection(record_id))
    after = _derivative_db_state(derivative.id)

    assert preview.blocks == []
    assert preview.withheld_sources_manifest[0]["reason"] == "raw_level_not_external_eligible"
    assert before == after
    refreshed = revalidate_sanitized_derivative(WORKSPACE_ID, derivative.id)
    assert refreshed.status == "stale"
    assert refreshed.stale_reason == f"source_level_incompatible:{source_ref}:S4"


def test_manual_preview_stale_derivative_is_read_only() -> None:
    _bootstrap()
    record_id = _decision("Zephyr proprietary geometry decision")
    source_ref = f"decision:{record_id}"
    create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=source_ref,
            level="S3",
        )
    )
    derivative = create_sanitized_derivative(
        SanitizedDerivativeCreate(
            workspace_id=WORKSPACE_ID,
            source_refs=[source_ref],
            content="Generic sanitized design summary.",
            effective_level="S1",
            transformations=["Removed proprietary values"],
        )
    )
    derivative = approve_sanitized_derivative(WORKSPACE_ID, derivative.id)
    _update_decision(record_id, "Changed Zephyr proprietary geometry decision")
    before = _derivative_db_state(derivative.id)

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
    after = _derivative_db_state(derivative.id)

    assert preview.blocks == []
    assert preview.withheld_sources_manifest[0]["reason"] == "derivative_stale"
    assert before == after
