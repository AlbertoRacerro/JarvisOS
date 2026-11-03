from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))

    from app.core.config import get_settings

    get_settings.cache_clear()

    from app.core.bootstrap import initialize_storage
    from app.main import create_app

    initialize_storage(seed_default=True)

    with TestClient(create_app()) as test_client:
        yield test_client

    get_settings.cache_clear()


def _ids(client: TestClient, **params: object) -> list[str]:
    return [item["id"] for item in client.get("/workspaces", params=params).json()]


def test_archive_hides_project_and_restore_brings_it_back(client: TestClient) -> None:
    created = client.post("/workspaces", json={"name": "Smoke", "slug": "smoke-1"}).json()

    archived = client.post(f"/workspaces/{created['id']}/archive")
    assert archived.status_code == 200
    assert archived.json()["status"] == "archived"
    assert created["id"] not in _ids(client)
    assert created["id"] in _ids(client, include_archived=True)
    assert client.get(f"/workspaces/{created['id']}").status_code == 200

    restored = client.post(f"/workspaces/{created['id']}/restore")
    assert restored.status_code == 200
    assert restored.json()["status"] == "active"
    assert created["id"] in _ids(client)


def test_archive_is_idempotent_and_logged_once(client: TestClient) -> None:
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import count_events_by_type

    created = client.post("/workspaces", json={"name": "Smoke", "slug": "smoke-2"}).json()
    assert client.post(f"/workspaces/{created['id']}/archive").status_code == 200
    assert client.post(f"/workspaces/{created['id']}/archive").status_code == 200

    with open_sqlite_connection() as connection:
        assert count_events_by_type(connection, "WorkspaceArchived") == 1


def test_last_active_project_cannot_be_archived(client: TestClient) -> None:
    response = client.post("/workspaces/main/archive")

    assert response.status_code == 409
    assert "main" in _ids(client)


def test_unknown_project_is_not_found(client: TestClient) -> None:
    assert client.post("/workspaces/missing/archive").status_code == 404
    assert client.post("/workspaces/missing/restore").status_code == 404
    assert client.post("/workspaces/missing/rename", json={"name": "X"}).status_code == 404


def test_rename_changes_only_the_display_name(client: TestClient) -> None:
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import count_events_by_type

    renamed = client.post("/workspaces/main/rename", json={"name": "  Zephyr  "})

    assert renamed.status_code == 200
    assert renamed.json()["name"] == "Zephyr"
    assert renamed.json()["slug"] == "main"
    assert client.get("/workspaces/main").json()["name"] == "Zephyr"
    assert client.post("/workspaces/main/rename", json={"name": "Zephyr"}).status_code == 200
    with open_sqlite_connection() as connection:
        assert count_events_by_type(connection, "WorkspaceRenamed") == 1


def test_rename_refuses_a_blank_name(client: TestClient) -> None:
    assert client.post("/workspaces/main/rename", json={"name": "   "}).status_code == 422
    assert client.post("/workspaces/main/rename", json={"name": ""}).status_code == 422
