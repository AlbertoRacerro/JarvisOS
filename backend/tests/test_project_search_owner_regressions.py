from __future__ import annotations

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.memory.literature_search import _escape_like_literal as escape_literature_literal
from app.modules.modeling.model_dossier_search import _escape_like_literal as escape_model_literal
from app.modules.modeling.project_search_owner import (
    _escape_like_literal as escape_project_literal,
)
from app.modules.modeling.project_search_owner import search_context_records_literal


def _insert_workspace(connection, workspace_id: str) -> None:
    connection.execute(
        "INSERT INTO workspaces (id, name, slug, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (workspace_id, workspace_id, workspace_id, "2026-09-12T00:00:00Z", "2026-09-12T00:00:00Z"),
    )


def test_owner_ranks_exact_before_contains_before_applying_match_bound() -> None:
    initialize_database()
    workspace_id = "ws-search-rank-before-cap"
    now = "2026-09-12T00:00:00Z"
    with open_sqlite_connection() as connection:
        _insert_workspace(connection, workspace_id)
        connection.executemany(
            "INSERT INTO requirements (id, workspace_id, statement, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            [
                (f"a-contains-{index:03d}", workspace_id, f"noise needle tail {index}", "active", now, now)
                for index in range(101)
            ],
        )
        connection.execute(
            "INSERT INTO requirements (id, workspace_id, statement, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("z-exact", workspace_id, "needle", "active", now, now),
        )
        connection.commit()

    selected = search_context_records_literal(
        workspace_id,
        kinds=["requirement"],
        statuses_by_kind={"requirement": ["active"]},
        query="needle",
        max_matches_per_kind=101,
    )

    assert selected["requirement"][0].id == "z-exact"
    assert any(record.id == "z-exact" for record in selected["requirement"])


def test_owner_filters_retired_decisions_before_applying_match_bound() -> None:
    initialize_database()
    workspace_id = "ws-search-decision-lifecycle-before-cap"
    now = "2026-09-12T00:00:00Z"
    with open_sqlite_connection() as connection:
        _insert_workspace(connection, workspace_id)
        connection.executemany(
            "INSERT INTO decisions (id, workspace_id, title, decision_text, status, basis_lifecycle_state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    f"a-retired-{index:03d}",
                    workspace_id,
                    f"noise needle tail {index}",
                    "retired decision",
                    "accepted",
                    "retired",
                    now,
                    now,
                )
                for index in range(101)
            ],
        )
        connection.execute(
            "INSERT INTO decisions (id, workspace_id, title, decision_text, status, basis_lifecycle_state, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("z-active-exact", workspace_id, "needle", "active decision", "accepted", "active", now, now),
        )
        connection.commit()

    selected = search_context_records_literal(
        workspace_id,
        kinds=["decision"],
        statuses_by_kind={"decision": None},
        query="needle",
        max_matches_per_kind=101,
    )

    assert [record.id for record in selected["decision"]] == ["z-active-exact"]


def test_owner_unicode_casefold_matches_non_ascii_text() -> None:
    initialize_database()
    workspace_id = "ws-search-unicode-casefold"
    now = "2026-09-12T00:00:00Z"
    with open_sqlite_connection() as connection:
        _insert_workspace(connection, workspace_id)
        connection.execute(
            "INSERT INTO requirements (id, workspace_id, statement, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("unicode-requirement", workspace_id, "Über Reaktor", "active", now, now),
        )
        connection.commit()

    selected = search_context_records_literal(
        workspace_id,
        kinds=["requirement"],
        statuses_by_kind={"requirement": ["active"]},
        query="über",
        max_matches_per_kind=10,
    )

    assert [record.id for record in selected["requirement"]] == ["unicode-requirement"]
    assert escape_project_literal("ÁCIDO_%") == "ácido\\_\\%"
    assert escape_model_literal("ÁCIDO_%") == "ácido\\_\\%"
    assert escape_literature_literal("ÁCIDO_%") == "ácido\\_\\%"
