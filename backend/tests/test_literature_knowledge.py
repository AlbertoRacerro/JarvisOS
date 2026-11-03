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


def _workspace_ids(client: TestClient) -> tuple[str, str]:
    first = client.get("/workspaces").json()[0]["id"]
    with open_sqlite_connection() as connection:
        second = str(uuid.uuid4())
        now = "2026-09-10T00:00:00Z"
        connection.execute(
            "INSERT INTO workspaces (id, name, slug, description, status, created_at, updated_at) VALUES (?, ?, ?, ?, 'active', ?, ?)",
            (second, "Other", f"other-{second[:8]}", "cross-workspace fixture", now, now),
        )
        connection.commit()
    return first, second


def _artifact(
    workspace_id: str,
    path: Path,
    *,
    mime_type: str = "application/pdf",
    payload: bytes = b"paper",
    artifact_type: str = "literature_source",
    status: str = "registered",
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    artifact_id = str(uuid.uuid4())
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO artifacts (id, workspace_id, filename, stored_path, artifact_type, mime_type, sha256, source_ref, status, created_at, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'test', ?, '2026-09-10T00:00:00Z', 'spec 114 fixture')
            """,
            (artifact_id, workspace_id, path.name, str(path), artifact_type, mime_type, "0" * 64, status),
        )
        connection.commit()
    return artifact_id


def _source_url(workspace_id: str) -> str:
    return f"/workspaces/{workspace_id}/literature/sources"


def test_source_entry_provenance_used_by_and_safe_content(client: TestClient) -> None:
    workspace_id, _ = _workspace_ids(client)
    artifact_id = _artifact(
        workspace_id,
        build_paths().artifacts_dir / "literature" / "paper.md",
        mime_type="text/markdown",
        payload=b"# Reactor hydrodynamics\nSuperficial liquid velocity: 0.42 m/s\nReported condition.\n",
    )
    payload = {
        "title": "Reactor hydrodynamics",
        "source_kind": "paper",
        "state": "accepted",
        "artifact_id": artifact_id,
        "citation": "Example et al. (2026)",
        "published_year": 2026,
        "request_key": "source-1",
    }
    created = client.post(_source_url(workspace_id), json=payload)
    assert created.status_code == 201
    source = created.json()
    assert "stored_path" not in str(source)
    assert source["backing"]["availability"] == "available"
    assert source["backing"]["content_available"] is True

    replay = client.post(_source_url(workspace_id), json=payload)
    assert replay.status_code == 201
    assert replay.json()["id"] == source["id"]

    entry_response = client.post(
        f"{_source_url(workspace_id)}/{source['id']}/entries",
        json={
            "entry_kind": "datum",
            "value_number": 0.42,
            "unit": "m/s",
            "status": "accepted",
            "locator_kind": "line",
            "locator_start": 2,
            "context_text": "Superficial liquid velocity at the reported condition.",
            "request_key": "datum-1",
        },
    )
    assert entry_response.status_code == 201
    entry_id = entry_response.json()["id"]
    with open_sqlite_connection() as connection:
        now = "2026-09-10T00:00:00Z"
        connection.execute(
            "INSERT INTO parameters (id, workspace_id, name, source_ref, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), workspace_id, "Liquid velocity", f"literature_entry:{entry_id}", now, now),
        )
        connection.commit()

    detail = client.get(f"{_source_url(workspace_id)}/{source['id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["entries"][0]["provenance_ref"] == f"literature_entry:{entry_id}"
    assert body["entries"][0]["used_by"][0]["kind"] == "parameter"
    assert body["entries"][0]["used_by"][0]["title"] == "Liquid velocity"
    assert "stored_path" not in str(body)

    content = client.get(f"{_source_url(workspace_id)}/{source['id']}/content")
    assert content.status_code == 200
    assert b"Superficial liquid velocity" in content.content
    assert content.headers["content-type"].startswith("text/markdown")
    assert content.headers["content-disposition"].startswith("inline")


def test_request_key_replay_rejects_material_source_payload_change(client: TestClient) -> None:
    workspace_id, _ = _workspace_ids(client)
    original = {
        "title": "Metadata source",
        "source_kind": "report",
        "state": "raw",
        "citation": "Original citation",
        "publisher": "Publisher A",
        "published_year": 2025,
        "request_key": "request-replay",
    }
    created = client.post(_source_url(workspace_id), json=original)
    assert created.status_code == 201

    changed = {**original, "state": "accepted"}
    replay = client.post(_source_url(workspace_id), json=changed)
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "literature_request_key_conflict"


def test_artifact_replay_rejects_material_source_payload_change(client: TestClient) -> None:
    workspace_id, _ = _workspace_ids(client)
    artifact_id = _artifact(
        workspace_id,
        build_paths().artifacts_dir / "literature" / "identity.pdf",
    )
    original = {
        "title": "Artifact source",
        "source_kind": "paper",
        "state": "raw",
        "artifact_id": artifact_id,
        "citation": "Original citation",
        "publisher": "Publisher A",
        "published_year": 2025,
    }
    created = client.post(_source_url(workspace_id), json=original)
    assert created.status_code == 201

    changed = {**original, "citation": "Different citation"}
    replay = client.post(_source_url(workspace_id), json=changed)
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "literature_duplicate_artifact_conflict"


def test_cross_workspace_artifact_is_rejected(client: TestClient) -> None:
    workspace_id, other_workspace_id = _workspace_ids(client)
    artifact_id = _artifact(
        other_workspace_id,
        build_paths().artifacts_dir / "literature" / "other.pdf",
    )
    response = client.post(
        _source_url(workspace_id),
        json={"title": "Wrong workspace", "source_kind": "paper", "artifact_id": artifact_id},
    )
    assert response.status_code == 404
    assert "stored_path" not in response.text


@pytest.mark.parametrize(
    ("artifact_type", "status"),
    [("runner_output", "registered"), ("literature_source", "superseded")],
)
def test_wrong_kind_or_noncurrent_artifact_is_rejected(
    client: TestClient,
    artifact_type: str,
    status: str,
) -> None:
    workspace_id, _ = _workspace_ids(client)
    artifact_id = _artifact(
        workspace_id,
        build_paths().artifacts_dir / "literature" / f"{artifact_type}-{status}.pdf",
        artifact_type=artifact_type,
        status=status,
        payload=b"SENTINEL-DO-NOT-SERVE",
    )
    response = client.post(
        _source_url(workspace_id),
        json={"title": "Ineligible backing", "source_kind": "report", "artifact_id": artifact_id},
    )
    assert response.status_code == 409
    assert "stored_path" not in response.text
    assert "SENTINEL-DO-NOT-SERVE" not in response.text


def test_accepted_source_becomes_visibly_unavailable_when_backing_is_invalidated(client: TestClient) -> None:
    workspace_id, _ = _workspace_ids(client)
    artifact_id = _artifact(
        workspace_id,
        build_paths().artifacts_dir / "literature" / "accepted.pdf",
        payload=b"accepted source",
    )
    source = client.post(
        _source_url(workspace_id),
        json={"title": "Accepted source", "source_kind": "report", "state": "accepted", "artifact_id": artifact_id},
    ).json()
    with open_sqlite_connection() as connection:
        connection.execute("UPDATE artifacts SET status = 'superseded' WHERE id = ?", (artifact_id,))
        connection.commit()

    detail = client.get(f"{_source_url(workspace_id)}/{source['id']}")
    assert detail.status_code == 200
    body = detail.json()
    assert body["state"] == "accepted"
    assert body["backing"]["availability"] == "ineligible"
    assert body["backing"]["content_available"] is False
    assert body["backing"]["content_url"] is None
    assert "stored_path" not in str(body)
    content = client.get(f"{_source_url(workspace_id)}/{source['id']}/content")
    assert content.status_code == 404
    assert b"accepted source" not in content.content


@pytest.mark.parametrize(
    ("path_kind", "mime_type", "availability"),
    [
        ("outside", "application/pdf", "unsafe"),
        ("secrets", "application/pdf", "unsafe"),
        ("inside", "application/octet-stream", "unsupported"),
    ],
)
def test_unsafe_backing_content_is_never_served(
    client: TestClient,
    tmp_path: Path,
    path_kind: str,
    mime_type: str,
    availability: str,
) -> None:
    workspace_id, _ = _workspace_ids(client)
    if path_kind == "outside":
        path = tmp_path / "outside" / "secret.pdf"
    elif path_kind == "secrets":
        path = build_paths().secrets_dir / "secret.pdf"
    else:
        path = build_paths().artifacts_dir / "literature" / "secret.bin"
    artifact_id = _artifact(workspace_id, path, mime_type=mime_type, payload=b"SENTINEL-DO-NOT-LEAK")
    source = client.post(
        _source_url(workspace_id),
        json={"title": "Unsafe backing", "source_kind": "paper", "artifact_id": artifact_id},
    ).json()
    assert source["backing"]["availability"] == availability
    assert source["backing"]["content_available"] is False
    assert source["backing"]["content_url"] is None
    assert "stored_path" not in str(source)
    response = client.get(f"{_source_url(workspace_id)}/{source['id']}/content")
    assert response.status_code == 404
    assert b"SENTINEL-DO-NOT-LEAK" not in response.content


def test_locator_must_be_real_and_supported(client: TestClient) -> None:
    workspace_id, _ = _workspace_ids(client)
    text_artifact = _artifact(
        workspace_id,
        build_paths().artifacts_dir / "literature" / "lines.txt",
        mime_type="text/plain",
        payload=b"one\ntwo\nthree\n",
    )
    text_source = client.post(
        _source_url(workspace_id),
        json={"title": "Lines", "source_kind": "report", "artifact_id": text_artifact},
    ).json()
    base = f"{_source_url(workspace_id)}/{text_source['id']}/entries"
    out_of_range = client.post(
        base,
        json={"entry_kind": "claim", "statement": "outside", "locator_kind": "line", "locator_start": 4},
    )
    assert out_of_range.status_code == 422
    assert out_of_range.json()["detail"]["code"] == "literature_locator_out_of_range"

    pdf_artifact = _artifact(
        workspace_id,
        build_paths().artifacts_dir / "literature" / "unsupported-page.pdf",
        payload=b"%PDF-1.4 minimal",
    )
    pdf_source = client.post(
        _source_url(workspace_id),
        json={"title": "PDF", "source_kind": "paper", "artifact_id": pdf_artifact},
    ).json()
    unsupported = client.post(
        f"{_source_url(workspace_id)}/{pdf_source['id']}/entries",
        json={"entry_kind": "claim", "statement": "page claim", "locator_kind": "page", "locator_start": 1},
    )
    assert unsupported.status_code == 422
    assert unsupported.json()["detail"]["code"] == "literature_locator_unsupported"


def test_contract_rejects_ambiguous_or_invalid_entries(client: TestClient) -> None:
    workspace_id, _ = _workspace_ids(client)
    source = client.post(
        _source_url(workspace_id),
        json={"title": "Metadata source", "source_kind": "report", "request_key": "metadata"},
    ).json()
    base = f"{_source_url(workspace_id)}/{source['id']}/entries"
    assert client.post(base, json={"entry_kind": "claim", "statement": ""}).status_code == 422
    assert client.post(base, json={"entry_kind": "datum"}).status_code == 422
    assert client.post(base, json={"entry_kind": "claim", "statement": "x", "locator_start": 2}).status_code == 422
    assert client.post(base, json={"entry_kind": "claim", "statement": "x", "locator_kind": "line"}).status_code == 422
    assert client.post(base, json={"entry_kind": "claim", "statement": "x", "unknown": True}).status_code == 422
    assert client.get(f"{_source_url(workspace_id)}?limit=101").status_code == 422
