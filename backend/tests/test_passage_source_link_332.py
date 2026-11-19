from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.core.paths import build_paths
from tests.test_literature_knowledge import _artifact, _source_url, _workspace_ids, client  # noqa: F401

TEXT = "# Fermentazione\n\nIl processo aerobico produce biomassa.\n"


def _setup(client: TestClient, tmp_path: Path, folder_text: str) -> tuple[str, str]:  # noqa: F811
    workspace_id, _ = _workspace_ids(client)
    artifact = _artifact(
        workspace_id, build_paths().artifacts_dir / "literature" / "paper.md",
        mime_type="text/markdown", payload=TEXT.encode(),
    )
    client.post(_source_url(workspace_id), json={"title": "Paper sulla fermentazione", "source_kind": "paper",
                                                 "artifact_id": artifact})
    folder = tmp_path / "docs"
    (folder / "sub").mkdir(parents=True)
    (folder / "sub" / "copy.md").write_text(folder_text, encoding="utf-8")
    endpoint = f"/workspaces/{workspace_id}/memory"
    assert client.put(f"{endpoint}/documents", json={"folder": str(folder)}).status_code == 200
    assert client.post(f"{endpoint}/documents/refresh").status_code == 200
    return workspace_id, endpoint


def _ask(client: TestClient, endpoint: str, expected: str) -> dict:  # noqa: F811
    response = client.post(f"{endpoint}/retrieval-eval", json={
        "questions": [{"query": "processo aerobico biomassa", "expected_ids": [expected]}]})
    assert response.status_code == 200, response.text
    return response.json()


def test_identical_folder_file_is_credited_to_source(client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    _, endpoint = _setup(client, tmp_path, TEXT)
    result = _ask(client, endpoint, "Paper sulla fermentazione")
    question = result["questions"][0]
    assert question["accepted_ids"] == ["sub/copy.md"]
    assert question["first_matching_rank"] == 1
    assert str(tmp_path) not in str(result)


def test_one_byte_change_adds_nothing(client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    _, endpoint = _setup(client, tmp_path, TEXT + "x")
    question = _ask(client, endpoint, "Paper sulla fermentazione")["questions"][0]
    assert question["accepted_ids"] == []
    assert question["first_matching_rank"] is None


def test_expected_that_is_not_a_source_adds_nothing(client: TestClient, tmp_path: Path) -> None:  # noqa: F811
    _, endpoint = _setup(client, tmp_path, TEXT)
    assert _ask(client, endpoint, "Altro titolo")["questions"][0]["accepted_ids"] == []
