from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.core.paths import build_paths
from tests.test_literature_knowledge import _workspace_ids, client  # noqa: F401

PAPER = b"# Paper\nintro\nk = 1.2 x 10^-3 s^-1 at 300 K\n"


def _upload(client: TestClient, workspace_id: str, name: str, data: bytes):  # noqa: F811
    return client.post(
        f"/workspaces/{workspace_id}/literature/files",
        params={"filename": name},
        content=data,
        headers={"Content-Type": "application/octet-stream"},
    )


def _files(workspace_id: str) -> list[Path]:
    base = build_paths().artifacts_dir / "literature" / workspace_id
    return sorted(base.iterdir()) if base.exists() else []


def _source(client: TestClient, workspace_id: str, **extra):  # noqa: F811
    response = client.post(
        f"/workspaces/{workspace_id}/literature/sources",
        json={"title": "Kinetics", "source_kind": "paper", **extra},
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_upload_creates_available_backing_with_same_bytes(client: TestClient) -> None:  # noqa: F811
    ws, _ = _workspace_ids(client)
    response = _upload(client, ws, "..\\sub/paper.md", PAPER)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["filename"] == "paper.md"
    assert body["mime_type"] == "text/markdown"
    assert body["size"] == len(PAPER)
    assert "stored_path" not in body and str(build_paths().data_root) not in response.text
    source = _source(client, ws, artifact_id=body["artifact_id"])
    assert source["backing"]["availability"] == "available"
    content = client.get(f"/workspaces/{ws}/literature/sources/{source['id']}/content")
    assert content.status_code == 200
    assert content.content == PAPER


def test_refused_uploads_leave_no_file(client: TestClient) -> None:  # noqa: F811
    ws, _ = _workspace_ids(client)
    cases = [
        ("tool.exe", b"MZ", 415),
        ("paper.pdf", b"not a pdf", 422),
        ("paper.txt", b"\xff\xfe\x00bad", 422),
        ("paper.txt", b"x" * (20 * 1024 * 1024 + 1), 413),
        ("paper.txt", b"", 400),
        ("n" * 256 + ".txt", b"x", 400),
    ]
    for name, data, status in cases:
        response = _upload(client, ws, name, data)
        assert response.status_code == status, (name, response.text)
        assert isinstance(response.json()["detail"]["code"], str)
    assert _files(ws) == []
    missing = _upload(client, "no-such-workspace", "paper.txt", b"x")
    assert missing.status_code == 404
    assert not (build_paths().artifacts_dir / "literature" / "no-such-workspace").exists()


def test_same_bytes_store_one_file(client: TestClient) -> None:  # noqa: F811
    ws, _ = _workspace_ids(client)
    first = _upload(client, ws, "a.md", PAPER).json()
    second = _upload(client, ws, "b.md", PAPER).json()
    assert first["sha256"] == second["sha256"]
    assert first["artifact_id"] != second["artifact_id"]
    assert len(_files(ws)) == 1


def test_symlinked_target_directory_is_refused(client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    ws, _ = _workspace_ids(client)
    base = build_paths().artifacts_dir / "literature"
    base.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (base / ws).symlink_to(outside, target_is_directory=True)
    response = _upload(client, ws, "a.md", PAPER)
    assert response.status_code == 500
    assert response.json()["detail"]["code"] == "literature_upload_unsafe"
    assert list(outside.iterdir()) == []


def test_attach_backing_once_and_same_workspace_only(client: TestClient) -> None:  # noqa: F811
    ws, other = _workspace_ids(client)
    source = _source(client, ws)
    assert source["backing"] is None
    artifact = _upload(client, ws, "a.md", PAPER).json()["artifact_id"]
    url = f"/workspaces/{ws}/literature/sources/{source['id']}/backing"
    foreign = _upload(client, other, "a.md", PAPER).json()["artifact_id"]
    refused = client.post(url, json={"artifact_id": foreign})
    assert refused.status_code == 404
    attached = client.post(url, json={"artifact_id": artifact})
    assert attached.status_code == 200
    assert attached.json()["backing"]["availability"] == "available"
    again = client.post(url, json={"artifact_id": artifact})
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "literature_backing_exists"


def test_datum_verdicts_through_routes(client: TestClient) -> None:  # noqa: F811
    ws, _ = _workspace_ids(client)
    artifact = _upload(client, ws, "a.md", PAPER).json()["artifact_id"]
    source = _source(client, ws, artifact_id=artifact)
    url = f"/workspaces/{ws}/literature/sources/{source['id']}/entries"
    base = {"entry_kind": "datum", "unit": "1/s", "locator_kind": "line", "locator_start": 3, "locator_end": 3}
    good = client.post(url, json={**base, "value_number": 0.0012})
    assert good.status_code == 201 and good.json()["verdict"] == "usable"
    wrong = client.post(url, json={**base, "value_number": 0.0021})
    assert wrong.status_code == 201 and wrong.json()["verdict"] == "not_usable"
    outside = client.post(url, json={**base, "value_number": 1, "locator_start": 9, "locator_end": 9})
    assert outside.status_code == 422
