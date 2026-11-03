from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.project_search.routes import router


def _insert_workspace(connection, workspace_id: str) -> None:
    connection.execute(
        "INSERT INTO workspaces (id, name, slug, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (workspace_id, workspace_id, workspace_id, "2026-09-12T00:00:00Z", "2026-09-12T00:00:00Z"),
    )


def _snapshot(connection, workspace_id: str) -> dict[str, list[tuple[object, ...]]]:
    tables = (
        "requirements",
        "parameters",
        "assumptions",
        "decisions",
        "model_specs",
        "model_versions",
        "literature_sources",
        "literature_entries",
    )
    return {
        table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} WHERE workspace_id = ? ORDER BY id", (workspace_id,)).fetchall()]
        for table in tables
    }


def test_real_project_search_is_workspace_scoped_deduplicated_read_only_and_cardinality_safe() -> None:
    initialize_database()
    now = "2026-09-12T00:00:00Z"
    with open_sqlite_connection() as connection:
        _insert_workspace(connection, "ws-search")
        _insert_workspace(connection, "ws-other")

        connection.execute(
            "INSERT INTO requirements (id, workspace_id, statement, rationale, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("req-hit", "ws-search", "Reactor requirement", "reactor design", "active", now, now),
        )
        connection.execute(
            "INSERT INTO parameters (id, workspace_id, name, symbol, value, unit, value_status, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("param-hit", "ws-search", "Reactor pressure", "P", "10", "bar", "accepted", "draft", now, now),
        )
        connection.execute(
            "INSERT INTO assumptions (id, workspace_id, statement, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("assumption-hit", "ws-search", "Reactor is adiabatic", "accepted", now, now),
        )
        connection.execute(
            "INSERT INTO decisions (id, workspace_id, title, decision_text, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("decision-hit", "ws-search", "Reactor material", "Use alloy", "accepted", now, now),
        )
        connection.execute(
            "INSERT INTO model_specs (id, workspace_id, title, engineering_question, status, maturity_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("model-versionless", "ws-search", "Reactor kinetics", "Predict reactor conversion", "draft", "draft", now, now),
        )
        connection.execute(
            "INSERT INTO model_specs (id, workspace_id, title, engineering_question, status, maturity_status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("model-versioned", "ws-search", "Reactor dynamic model", "Predict reactor dynamics", "draft", "draft", now, now),
        )
        connection.execute(
            "INSERT INTO model_versions (id, workspace_id, model_spec_id, version_label, implementation_kind, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("model-v1", "ws-search", "model-versioned", "reactor-v1", "python", "active", now),
        )
        connection.execute(
            "INSERT INTO literature_sources (id, workspace_id, title, source_kind, state, citation, publisher, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("lit-source", "ws-search", "Reactor handbook", "book", "accepted", "Reactor Handbook", "Press", now, now),
        )
        connection.execute(
            "INSERT INTO literature_entries (id, workspace_id, source_id, entry_kind, statement, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("lit-entry", "ws-search", "lit-source", "claim", "Reactor residence time matters", "accepted", now, now),
        )

        # A different workspace contains an indistinguishable literal match and must never leak.
        connection.execute(
            "INSERT INTO requirements (id, workspace_id, statement, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            ("other-req", "ws-other", "Reactor requirement", "active", now, now),
        )

        # Total owner cardinality may exceed 100 without making a valid bounded match unavailable.
        connection.executemany(
            "INSERT INTO requirements (id, workspace_id, statement, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            [(f"noise-{index:03d}", "ws-search", f"Unrelated requirement {index}", "active", now, now) for index in range(101)],
        )
        connection.commit()
        before = _snapshot(connection, "ws-search")

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    response = client.get("/workspaces/ws-search/project-search", params={"q": "reactor", "limit": 100})
    assert response.status_code == 200, response.text
    payload = response.json()
    items = payload["items"]

    stable_refs = [item["stable_ref"] for item in items]
    assert len(stable_refs) == len(set(stable_refs))
    assert all(item["workspace_id"] == "ws-search" for item in items)
    assert "requirement:other-req" not in stable_refs
    assert "requirement:req-hit" in stable_refs
    assert "parameter:param-hit" in stable_refs
    assert "assumption:assumption-hit" in stable_refs
    assert "decision:decision-hit" in stable_refs
    assert "model_spec:model-versionless" in stable_refs
    assert "model_version:model-v1" in stable_refs
    assert "literature_source:lit-source" in stable_refs
    assert "literature_entry:lit-entry" in stable_refs

    versionless = next(item for item in items if item["stable_ref"] == "model_spec:model-versionless")
    assert versionless["version_or_revision"] is None
    assert versionless["route_params"] == {"modelSpecId": "model-versionless"}

    with open_sqlite_connection() as connection:
        after = _snapshot(connection, "ws-search")
    assert after == before
