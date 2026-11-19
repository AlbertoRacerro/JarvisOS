from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.database import initialize_database
from app.core.paths import build_paths
from app.main import create_app
from app.modules.engineering.bench_runs_view import bench_root
from app.modules.workspaces.models import WorkspaceCreate
from app.modules.workspaces.service import create_workspace


def _workspace() -> str:
    initialize_database()
    return create_workspace(
        WorkspaceCreate(name="retrieval-eval", slug=f"retrieval-{uuid4().hex[:8]}", status="active")
    ).id


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as test_client:
        yield test_client


def test_retrieval_eval_validates_without_creating_runs(client: TestClient) -> None:
    workspace_id = _workspace()
    endpoint = f"/workspaces/{workspace_id}/memory/retrieval-eval"
    for payload in ({}, {"questions": []}, {"questions": [{"query": " ", "expected_ids": ["x"]}]},
                    {"questions": [{"query": "q", "expected_ids": []}]},
                    {"questions": [{"query": "q", "expected_ids": ["x"]}], "mode": "bad"}):
        response = client.post(endpoint, json=payload)
        assert response.status_code == 422
    assert not bench_root(workspace_id).exists()
    assert client.post("/workspaces/missing/memory/retrieval-eval", json={"questions": []}).status_code == 404


def test_retrieval_eval_persists_path_free_result(client: TestClient, tmp_path: Path) -> None:
    workspace_id = _workspace()
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "guide.md").write_text("# Fermentazione\n\nProcesso aerobico.\n", encoding="utf-8")
    endpoint = f"/workspaces/{workspace_id}/memory"
    assert client.put(f"{endpoint}/documents", json={"folder": str(folder)}).status_code == 200
    assert client.post(f"{endpoint}/documents/refresh").status_code == 200

    response = client.post(
        f"{endpoint}/retrieval-eval",
        json={"questions": [{"query": "fermentazione", "expected_ids": ["Fermentazione"]}]},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["evaluator_id"] == "retrieval.eval"
    assert result["verdict"] == "usable"
    outputs = {item["name"]: item["value"]["value"] for item in result["outputs"]}
    assert outputs["top3_hit_rate"] == 1.0
    assert next(check for check in result["checks"] if check["id"] == "RETRIEVAL_EXPECTED_IN_TOP3")["status"] == "pass"
    assert result["run_id"].startswith("retrieval-")
    assert str(build_paths().data_root) not in response.text
    assert (bench_root(workspace_id) / result["run_id"] / "result.json").is_file()


def test_retrieval_eval_matches_what_search_shows(client: TestClient, tmp_path: Path) -> None:
    workspace_id = _workspace()
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "guide.md").write_text("# Fermentazione\n\nProcesso aerobico.\n", encoding="utf-8")
    endpoint = f"/workspaces/{workspace_id}/memory"
    client.put(f"{endpoint}/documents", json={"folder": str(folder)})
    client.post(f"{endpoint}/documents/refresh")
    for expected, top3 in (("guide.md", 1.0), ("guide.md#Fermentazione", 1.0), ("Altro", 0.0)):
        result = client.post(
            f"{endpoint}/retrieval-eval",
            json={"questions": [{"query": "fermentazione", "expected_ids": [expected]}]},
        ).json()
        outputs = {item["name"]: item["value"]["value"] for item in result["outputs"]}
        assert outputs["top3_hit_rate"] == top3, expected


def test_retrieval_eval_missed_expected_id_warns(client: TestClient, tmp_path: Path) -> None:
    workspace_id = _workspace()
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "guide.md").write_text("# Fermentazione\n\nProcesso aerobico.\n", encoding="utf-8")
    endpoint = f"/workspaces/{workspace_id}/memory"
    assert client.put(f"{endpoint}/documents", json={"folder": str(folder)}).status_code == 200
    assert client.post(f"{endpoint}/documents/refresh").status_code == 200

    response = client.post(
        f"{endpoint}/retrieval-eval",
        json={"questions": [{"query": "fermentazione", "expected_ids": ["missing.md"]}]},
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["verdict"] == "usable_with_warnings"
    assert next(check for check in result["checks"] if check["id"] == "RETRIEVAL_EXPECTED_IN_TOP3")["status"] == "warn"
    assert result["outputs"]
