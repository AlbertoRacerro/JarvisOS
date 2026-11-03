from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.core.database import open_sqlite_connection
from app.core.paths import build_paths


@pytest.fixture
def client(isolated_data_root) -> TestClient:
    from app.core.bootstrap import initialize_storage
    from app.main import create_app

    initialize_storage(seed_default=True)
    with TestClient(create_app()) as test_client:
        yield test_client


def _workspace_id(client: TestClient) -> str:
    response = client.get("/workspaces")
    assert response.status_code == 200
    return response.json()[0]["id"]


def _insert_artifact(workspace_id: str, *, artifact_type: str, stored_path: Path, contents: bytes) -> str:
    stored_path.parent.mkdir(parents=True, exist_ok=True)
    stored_path.write_bytes(contents)
    artifact_id = str(uuid.uuid4())
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO artifacts (
                id, workspace_id, filename, stored_path, artifact_type, mime_type,
                sha256, source_ref, status, created_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'registered', ?, ?)
            """,
            (
                artifact_id,
                workspace_id,
                stored_path.name,
                str(stored_path),
                artifact_type,
                "model/gltf-binary",
                None,
                "test",
                "2026-07-04T00:00:00Z",
                "spec 006 fixture",
            ),
        )
        connection.commit()
    return artifact_id


def _content_path(workspace_id: str, artifact_id: str) -> str:
    return f"/workspaces/{workspace_id}/bluecad/artifacts/{artifact_id}/content"


def test_bluecad_glb_content_serves_bytes(client: TestClient) -> None:
    workspace_id = _workspace_id(client)
    payload = b"glb\x00payload"
    artifact_id = _insert_artifact(
        workspace_id,
        artifact_type="bluecad_glb",
        stored_path=build_paths().data_root / "artifacts" / "bluecad" / "part.glb",
        contents=payload,
    )

    response = client.get(_content_path(workspace_id, artifact_id))

    assert response.status_code == 200
    assert response.content == payload
    assert response.headers["content-type"].startswith("model/gltf-binary")


def test_bluecad_content_unknown_artifact_returns_404(client: TestClient) -> None:
    workspace_id = _workspace_id(client)

    response = client.get(_content_path(workspace_id, str(uuid.uuid4())))

    assert response.status_code == 404


def test_bluecad_content_non_bluecad_artifact_returns_404(client: TestClient) -> None:
    workspace_id = _workspace_id(client)
    artifact_id = _insert_artifact(
        workspace_id,
        artifact_type="runner_output",
        stored_path=build_paths().data_root / "artifacts" / "runner" / "result.glb",
        contents=b"not-bluecad",
    )

    response = client.get(_content_path(workspace_id, artifact_id))

    assert response.status_code == 404
    assert b"not-bluecad" not in response.content


def test_bluecad_content_outside_data_root_returns_404(client: TestClient, tmp_path: Path) -> None:
    workspace_id = _workspace_id(client)
    artifact_id = _insert_artifact(
        workspace_id,
        artifact_type="bluecad_glb",
        stored_path=tmp_path / "outside" / "escape.glb",
        contents=b"outside-root",
    )

    response = client.get(_content_path(workspace_id, artifact_id))

    assert response.status_code == 404
    assert b"outside-root" not in response.content
