from __future__ import annotations

import pytest


def _init(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "parameter-lifecycle-review-fixes"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.bootstrap import initialize_storage

    initialize_storage(seed_default=True)


def _parameter(name: str):
    from app.modules.modeling.models import ParameterCreate
    from app.modules.modeling.service import create_parameter

    return create_parameter(
        "main",
        ParameterCreate(
            name=name,
            value="10",
            unit="m",
            value_status="accepted",
            status="accepted",
        ),
    )


def test_noncurrent_replacement_proposal_cannot_be_promoted(monkeypatch, tmp_path) -> None:
    _init(monkeypatch, tmp_path)
    from app.core.database import open_sqlite_connection
    from app.modules.memory.replacement import ParameterReplacementError
    from app.modules.memory.service import promote_parameter_replacement

    source = _parameter("Source")
    replacement = _parameter("Replacement")
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            UPDATE parameters
            SET status = 'proposed', lifecycle_state = 'archived', supersedes_parameter_id = ?
            WHERE id = ?
            """,
            (source.id, replacement.id),
        )
        connection.commit()

    with pytest.raises(ParameterReplacementError) as caught:
        promote_parameter_replacement(replacement.id)

    assert caught.value.code == "parameter_replacement_target_not_active"
    with open_sqlite_connection() as connection:
        rows = connection.execute(
            "SELECT id, status, lifecycle_state FROM parameters WHERE id IN (?, ?)",
            (source.id, replacement.id),
        ).fetchall()
    states = {str(row["id"]): (str(row["status"]), str(row["lifecycle_state"])) for row in rows}
    assert states[source.id] == ("accepted", "active")
    assert states[replacement.id] == ("proposed", "archived")


def test_context_selector_filters_parameter_lifecycle_before_limit_and_ids(monkeypatch, tmp_path) -> None:
    _init(monkeypatch, tmp_path)
    from app.core.database import open_sqlite_connection
    from app.modules.modeling.service import select_context_records

    active = _parameter("Active older")
    archived = _parameter("Archived newer")
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE parameters SET lifecycle_state = 'archived', updated_at = ? WHERE id = ?",
            ("2099-01-01T00:00:00+00:00", archived.id),
        )
        connection.commit()

    selected = select_context_records(
        "main",
        kinds=["parameter"],
        statuses_by_kind={"parameter": ["accepted"]},
        ids=None,
        query=None,
        max_items_per_kind=1,
    )
    assert [record.id for record in selected["parameter"]] == [active.id]

    explicit = select_context_records(
        "main",
        kinds=["parameter"],
        statuses_by_kind={"parameter": ["accepted"]},
        ids=[archived.id],
        query=None,
        max_items_per_kind=10,
    )
    assert explicit["parameter"] == []
