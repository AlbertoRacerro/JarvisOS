"""Normal-language search keeps exact identity, ranking and workspace boundaries."""
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.database import initialize_database, open_sqlite_connection
from app.core.search_text import match_rank
from app.modules.project_search.routes import router


def test_normalization_is_additive_and_never_interprets_wildcards():
    assert match_rank("perdite carico", "Perdite di carico") == 4
    assert match_rank("CARICO perdite", "Perdite di carico") == 4
    assert match_rank("caffe", "Caffè") == 3
    assert match_rank("perdite carico", "perdite carico") == 0
    assert match_rank("perdite carico", "Perdite di carico", literal=True) == 99
    assert match_rank("perdite pressione", "Perdite di carico") == 99
    assert match_rank("%", "Arbitrary text") == 99
    assert match_rank("%", "Efficiency %") == 2


def test_human_search_ranks_before_owner_limit_and_retains_exact_navigation():
    initialize_database()
    now = "2026-09-15T00:00:00Z"
    with open_sqlite_connection() as connection:
        for workspace in ("human-search", "other-human-search"):
            connection.execute(
                "INSERT INTO workspaces (id, name, slug, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (workspace, workspace, workspace, now, now),
            )
        # More than the bounded owner read; the exact match sorts after these by ID.
        rows = [(f"a-{index:03d}", "human-search", "Perdite di carico", "active", now, now) for index in range(110)]
        rows += [
            ("z-exact", "human-search", "perdite carico", "active", now, now),
            ("foreign", "other-human-search", "perdite carico", "active", now, now),
        ]
        connection.executemany(
            "INSERT INTO requirements (id, workspace_id, statement, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)", rows,
        )
        connection.commit()
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.get("/workspaces/human-search/project-search", params={"q": "perdite carico", "limit": 3})
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert items[0]["stable_ref"] == "requirement:z-exact"
    assert items[0]["match_tier"] == "exact"
    assert items[1]["match_tier"] == "tokens"
    assert items[1]["route_params"]["recordId"] == "a-000"
    assert all(item["workspace_id"] == "human-search" for item in items)
