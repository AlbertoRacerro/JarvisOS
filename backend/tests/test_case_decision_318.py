"""Acceptance tests for spec 318 (CASE-DECISION-1): a decision linked to an accepted case verdict."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.main import create_app

NOW = "2026-11-19T00:00:00Z"


@pytest.fixture
def client(isolated_data_root: None) -> TestClient:
    initialize_storage(seed_default=True)
    with TestClient(create_app()) as test_client:
        yield test_client


def _seed_workspace(slug: str) -> str:
    ws_id = str(uuid.uuid4())
    with open_sqlite_connection() as conn:
        conn.execute(
            "INSERT INTO workspaces (id, name, slug, description, status, created_at, updated_at) "
            "VALUES (?, ?, ?, 'Test workspace', 'active', ?, ?)",
            (ws_id, f"Workspace {slug}", slug, NOW, NOW),
        )
        conn.commit()
    return ws_id


def _seed_verdict(workspace_id: str, status: str, case_id: str = "case_a") -> str:
    verdict_id = str(uuid.uuid4())
    with open_sqlite_connection() as conn:
        conn.execute(
            """
            INSERT INTO case_verdicts (
                id, workspace_id, case_id, run_id, quantity, computed_value, computed_unit,
                published_value, published_unit, rel_tol, deviation, verdict, status, created_at, updated_at
            ) VALUES (?, ?, ?, 'run_001', 'q', 1.0, 'm', 1.0, 'm', 0.05, 0.0, 'pass', ?, ?, ?)
            """,
            (verdict_id, workspace_id, case_id, status, NOW, NOW),
        )
        conn.commit()
    return verdict_id


def _decision_count(workspace_id: str) -> int:
    with open_sqlite_connection(read_only=True) as conn:
        return conn.execute("SELECT COUNT(*) FROM decisions WHERE workspace_id = ?", (workspace_id,)).fetchone()[0]


def _payload(verdict_id: str | None) -> dict:
    return {
        "title": "Adopt the tube diameter",
        "decision_text": "The model matches the paper within tolerance.",
        "status": "accepted",
        "case_verdict_id": verdict_id,
    }


def test_decision_rejects_invalid_case_verdict(client: TestClient) -> None:
    ws_id = _seed_workspace("ws-a")
    other_ws = _seed_workspace("ws-b")
    foreign = _seed_verdict(other_ws, "accepted")
    recorded = _seed_verdict(ws_id, "recorded")
    superseded = _seed_verdict(ws_id, "superseded")

    for verdict_id in (foreign, recorded, superseded, "missing-verdict"):
        res = client.post(f"/workspaces/{ws_id}/decisions", json=_payload(verdict_id))
        assert res.status_code == 422, res.text
        assert res.json()["detail"]["code"] == "case_verdict_invalid"
    assert _decision_count(ws_id) == 0


def test_decision_links_accepted_case_verdict(client: TestClient) -> None:
    ws_id = _seed_workspace("ws-a")
    accepted = _seed_verdict(ws_id, "accepted")

    res = client.post(f"/workspaces/{ws_id}/decisions", json=_payload(accepted))
    assert res.status_code == 201, res.text
    body = res.json()
    assert body["case_verdict_id"] == accepted
    assert body["status"] == "accepted"

    listed = client.get(f"/workspaces/{ws_id}/decisions").json()
    assert [d["case_verdict_id"] for d in listed] == [accepted]


def test_decision_without_case_verdict_still_allowed(client: TestClient) -> None:
    ws_id = _seed_workspace("ws-a")
    res = client.post(f"/workspaces/{ws_id}/decisions", json=_payload(None))
    assert res.status_code == 201, res.text
    assert res.json()["case_verdict_id"] is None
