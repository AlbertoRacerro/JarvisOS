from __future__ import annotations

from uuid import uuid4

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.ai.context_builder import ContextSelectionSpec
from app.modules.ai.sensitivity import (
    approve_sanitized_derivative,
    build_external_context_preview,
    create_sanitized_derivative,
    create_sensitivity_label,
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


def _set_updated_at(record_id: str, value: str) -> None:
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE decisions SET updated_at = ? WHERE id = ?",
            (value, record_id),
        )
        connection.commit()


def _label(record_id: str, level: str) -> None:
    create_sensitivity_label(
        SensitivityLabelCreate(
            workspace_id=WORKSPACE_ID,
            subject_ref=f"decision:{record_id}",
            level=level,
        )
    )


def _approve(source_ids: list[str], content: str = "Generic combined engineering summary."):
    derivative = create_sanitized_derivative(
        SanitizedDerivativeCreate(
            workspace_id=WORKSPACE_ID,
            source_refs=[f"decision:{record_id}" for record_id in source_ids],
            content=content,
            effective_level="S1",
            transformations=["Removed project-specific details"],
        )
    )
    return approve_sanitized_derivative(WORKSPACE_ID, derivative.id)


def _selection(*record_ids: str) -> ContextSelectionSpec:
    return ContextSelectionSpec(kinds=["decision"], ids=list(record_ids))


def _manual_block(derivative) -> dict[str, str]:
    return {
        "source": f"derivative:{derivative.id}",
        "type": "sanitized_derivative",
        "id": derivative.id,
        "content": derivative.content,
    }


def test_multisource_derivative_replaces_all_of_its_selected_sources() -> None:
    _bootstrap()
    public_id = _decision("Generic public engineering note")
    sensitive_id = _decision("Zephyr proprietary geometry decision")
    _label(public_id, "S1")
    _label(sensitive_id, "S3")
    derivative = _approve([public_id, sensitive_id])
    _set_updated_at(public_id, "9999-01-01T00:00:00+00:00")
    _set_updated_at(sensitive_id, "0001-01-01T00:00:00+00:00")

    preview = build_external_context_preview(
        WORKSPACE_ID,
        32_000,
        _selection(public_id, sensitive_id),
    )

    assert [block["source"] for block in preview.blocks] == [
        f"derivative:{derivative.id}"
    ]
    assert set(preview.included_sources_manifest[0]["source_refs"]) == {
        f"decision:{public_id}",
        f"decision:{sensitive_id}",
    }
    assert preview.withheld_count == 0


def test_overlapping_derivatives_are_not_partially_combined() -> None:
    _bootstrap()
    first = _decision("Zephyr proprietary geometry decision A")
    second = _decision("Zephyr proprietary geometry decision B")
    third = _decision("Zephyr proprietary geometry decision C")
    for record_id in (first, second, third):
        _label(record_id, "S3")
    first_derivative = _approve([first, second], "Generic summary A and B.")
    _approve([second, third], "Generic summary B and C.")
    _set_updated_at(first, "9999-01-01T00:00:00+00:00")
    _set_updated_at(second, "5000-01-01T00:00:00+00:00")
    _set_updated_at(third, "0001-01-01T00:00:00+00:00")

    preview = build_external_context_preview(
        WORKSPACE_ID,
        32_000,
        _selection(first, second, third),
    )

    assert [block["source"] for block in preview.blocks] == [
        f"derivative:{first_derivative.id}"
    ]
    assert preview.withheld_sources_manifest == [
        {
            "source_ref": f"decision:{third}",
            "effective_level": "S3",
            "reason": "raw_level_not_external_eligible",
        }
    ]


def test_manual_preview_withholds_overlapping_derivative() -> None:
    _bootstrap()
    first = _decision("Zephyr proprietary geometry decision A")
    second = _decision("Zephyr proprietary geometry decision B")
    third = _decision("Zephyr proprietary geometry decision C")
    for record_id in (first, second, third):
        _label(record_id, "S3")
    first_derivative = _approve([first, second], "Generic summary A and B.")
    overlapping = _approve([second, third], "Generic summary B and C.")

    preview = preview_manual_context(
        WORKSPACE_ID,
        [_manual_block(first_derivative), _manual_block(overlapping)],
        32_000,
    )

    assert [block["source"] for block in preview.blocks] == [
        f"derivative:{first_derivative.id}"
    ]
    assert preview.withheld_sources_manifest == [
        {
            "source_ref": f"derivative:{overlapping.id}",
            "effective_level": "S1",
            "derivative_id": overlapping.id,
            "reason": "derivative_source_overlap",
        }
    ]


def test_refresh_true_remains_explicit_revalidation_compatibility() -> None:
    _bootstrap()
    record_id = _decision("Zephyr proprietary geometry decision")
    _label(record_id, "S3")
    derivative = _approve([record_id])
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE decisions SET decision_text = ?, updated_at = ? WHERE id = ?",
            (
                "Changed Zephyr proprietary geometry decision",
                utc_now(),
                record_id,
            ),
        )
        connection.commit()

    refreshed = get_sanitized_derivative(
        WORKSPACE_ID,
        derivative.id,
        refresh=True,
    )

    assert refreshed.status == "stale"
    assert refreshed.stale_reason == f"source_digest_mismatch:decision:{record_id}"
