"""BLUECAD spec-006 conformance tests — REVIEWER-OWNED.

Written by the reviewing tier before implementation (AGENTS.md
"Reviewer-owned conformance tests"). Implementation PRs must not add to,
modify, or delete this file.

Scope: the security contract of the BLUECAD artifact-content endpoint
(GET /workspaces/{id}/bluecad/artifacts/{artifact_id}/content). The viewer
itself is reviewed visually; this file pins the parts a rushed implementation
gets wrong — serving arbitrary artifacts, leaking existence, or path
traversal outside the data root.

Contract pinned (also stated in the spec-006 launch prompt):
- The endpoint serves bytes ONLY for artifacts whose role (stored in the
  'artifact_type' column) starts with 'bluecad_' AND whose 'stored_path'
  resolves under the data root.
- Every other case returns 404 (never 403 — do not leak existence), never
  500, never the file bytes.

Skips cleanly until the route is registered.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


def _content_route_registered(app: object) -> bool:
    return any("/bluecad/artifacts/" in getattr(route, "path", "") for route in app.routes)


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.bootstrap import initialize_storage
    from app.main import create_app

    initialize_storage(seed_default=True)
    app = create_app()
    if not _content_route_registered(app):
        pytest.skip("spec 006 artifact-content route not present yet")
    with TestClient(app) as test_client:
        yield test_client
    get_settings.cache_clear()


def _workspace_id(client: TestClient) -> str:
    response = client.get("/workspaces")
    assert response.status_code == 200
    first = response.json()[0]
    return first.get("id") or first.get("workspace_id")


def _insert_artifact(workspace_id: str, *, role: str, stored_path: Path, contents: bytes) -> str:
    """Insert an artifacts row directly (stable schema) and write its file."""
    from app.core.database import open_sqlite_connection

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
                role,
                "model/gltf-binary",
                None,
                "conformance",
                "2026-07-04T00:00:00Z",
                "reviewer conformance fixture",
            ),
        )
        connection.commit()
    return artifact_id


def _data_root() -> Path:
    from app.core.paths import resolve_paths

    return resolve_paths().data_root


def _content_url(workspace_id: str, artifact_id: str) -> str:
    return f"/workspaces/{workspace_id}/bluecad/artifacts/{artifact_id}/content"


def test_serves_bluecad_artifact_under_data_root(client: TestClient) -> None:
    workspace_id = _workspace_id(client)
    payload = b"glTF-binary-bytes-\x00\x01\x02"
    artifact_id = _insert_artifact(
        workspace_id,
        role="bluecad_glb",
        stored_path=_data_root() / "artifacts" / "conf" / "model.glb",
        contents=payload,
    )
    response = client.get(_content_url(workspace_id, artifact_id))
    assert response.status_code == 200, response.text
    assert response.content == payload
    assert "gltf" in response.headers.get("content-type", "").lower()


def test_non_bluecad_role_is_404_not_served(client: TestClient) -> None:
    workspace_id = _workspace_id(client)
    artifact_id = _insert_artifact(
        workspace_id,
        role="python_script",
        stored_path=_data_root() / "artifacts" / "conf" / "script.py",
        contents=b"print('secret')",
    )
    response = client.get(_content_url(workspace_id, artifact_id))
    assert response.status_code == 404, "non-bluecad artifacts must not be served (and 404, not 403)"
    assert b"secret" not in response.content


def test_unknown_artifact_id_is_404(client: TestClient) -> None:
    workspace_id = _workspace_id(client)
    response = client.get(_content_url(workspace_id, str(uuid.uuid4())))
    assert response.status_code == 404


def test_stored_path_outside_data_root_is_404(client: TestClient, tmp_path) -> None:
    """A bluecad-role artifact whose stored_path escapes the data root must
    not be served — defends against traversal / poisoned rows."""
    workspace_id = _workspace_id(client)
    outside = tmp_path / "outside_data_root" / "evil.glb"
    artifact_id = _insert_artifact(
        workspace_id,
        role="bluecad_glb",
        stored_path=outside,
        contents=b"should-never-be-served",
    )
    response = client.get(_content_url(workspace_id, artifact_id))
    assert response.status_code == 404, "artifacts outside the data root must not be served"
    assert b"should-never-be-served" not in response.content


def test_no_generic_artifact_listing_endpoint(client: TestClient) -> None:
    """The slice must not add a generic artifact listing/download surface."""
    workspace_id = _workspace_id(client)
    listing = client.get(f"/workspaces/{workspace_id}/bluecad/artifacts")
    assert listing.status_code in (404, 405), "no generic artifact listing endpoint may exist"
