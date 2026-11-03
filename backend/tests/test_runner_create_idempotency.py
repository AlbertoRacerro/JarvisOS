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


def _create_implementation(client: TestClient) -> str:
    spec_response = client.post(
        "/workspaces/main/model-specs",
        json={
            "title": "Runner idempotency fixture",
            "engineering_question": "Can one intended create survive response-loss retry?",
        },
    )
    assert spec_response.status_code == 201

    implementation_response = client.post(
        "/workspaces/main/model-implementations",
        json={
            "model_spec_id": spec_response.json()["id"],
            "version_label": "runner-idempotency-v0",
            "implementation_kind": "batch_growth_v0",
        },
    )
    assert implementation_response.status_code == 201
    return str(implementation_response.json()["id"])


def _input(mu_max: float = 0.4) -> dict[str, object]:
    return {
        "schema_version": 1,
        "parameters": {"mu_max": mu_max, "X0": 0.05, "t_final": 2, "dt": 1},
        "input_artifact_ids": [],
    }


def _create_payload(model_version_id: str, request_key: str, *, mu_max: float = 0.4) -> dict[str, object]:
    return {
        "model_version_id": model_version_id,
        "run_label": "idempotent-run",
        "timeout_seconds": 10,
        "input_set": _input(mu_max),
        "request_key": request_key,
    }


def test_same_request_key_and_payload_returns_same_run_without_duplicate(client: TestClient) -> None:
    implementation_id = _create_implementation(client)
    payload = _create_payload(implementation_id, "run-create-test-0001")

    first = client.post("/workspaces/main/runner-jobs", json=payload)
    second = client.post("/workspaces/main/runner-jobs", json=payload)

    assert first.status_code == 201
    assert second.status_code == 201
    assert second.json()["runner_job"]["id"] == first.json()["runner_job"]["id"]
    assert second.json()["simulation_run"]["id"] == first.json()["simulation_run"]["id"]

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        job_count = connection.execute(
            "SELECT COUNT(*) AS count FROM runner_jobs WHERE workspace_id = ? AND request_key = ?",
            ("main", payload["request_key"]),
        ).fetchone()["count"]
        run_count = connection.execute(
            "SELECT COUNT(*) AS count FROM simulation_runs WHERE workspace_id = ? AND run_label = ?",
            ("main", payload["run_label"]),
        ).fetchone()["count"]

    assert job_count == 1
    assert run_count == 1


def test_create_replay_after_execution_returns_terminal_job_without_duplicate(client: TestClient) -> None:
    implementation_id = _create_implementation(client)
    payload = _create_payload(implementation_id, "run-create-test-terminal-0001")

    created = client.post("/workspaces/main/runner-jobs", json=payload)
    assert created.status_code == 201
    runner_job_id = created.json()["runner_job"]["id"]
    simulation_run_id = created.json()["simulation_run"]["id"]

    executed = client.post(f"/runner-jobs/{runner_job_id}/run")
    assert executed.status_code == 200
    assert executed.json()["runner_job"]["status"] == "succeeded"

    replay = client.post("/workspaces/main/runner-jobs", json=payload)
    assert replay.status_code == 201
    assert replay.json()["runner_job"]["id"] == runner_job_id
    assert replay.json()["runner_job"]["status"] == "succeeded"
    assert replay.json()["simulation_run"]["id"] == simulation_run_id
    assert replay.json()["simulation_run"]["status"] == "succeeded"

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        job_count = connection.execute(
            "SELECT COUNT(*) AS count FROM runner_jobs WHERE workspace_id = ? AND request_key = ?",
            ("main", payload["request_key"]),
        ).fetchone()["count"]
        run_count = connection.execute(
            "SELECT COUNT(*) AS count FROM simulation_runs WHERE workspace_id = ? AND run_label = ?",
            ("main", payload["run_label"]),
        ).fetchone()["count"]

    assert job_count == 1
    assert run_count == 1


def test_same_request_key_with_different_payload_fails_closed(client: TestClient) -> None:
    implementation_id = _create_implementation(client)
    request_key = "run-create-test-0002"

    first = client.post(
        "/workspaces/main/runner-jobs",
        json=_create_payload(implementation_id, request_key, mu_max=0.4),
    )
    conflict = client.post(
        "/workspaces/main/runner-jobs",
        json=_create_payload(implementation_id, request_key, mu_max=0.5),
    )

    assert first.status_code == 201
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "runner_request_key_conflict"

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        job_count = connection.execute(
            "SELECT COUNT(*) AS count FROM runner_jobs WHERE workspace_id = ? AND request_key = ?",
            ("main", request_key),
        ).fetchone()["count"]
    assert job_count == 1


def test_runner_request_key_schema_is_nullable_and_workspace_scoped(client: TestClient) -> None:
    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        columns = {
            row["name"]: row
            for row in connection.execute("PRAGMA table_info(runner_jobs)").fetchall()
        }
        indexes = {
            row["name"]
            for row in connection.execute("PRAGMA index_list(runner_jobs)").fetchall()
        }

    assert "request_key" in columns
    assert columns["request_key"]["notnull"] == 0
    assert "idx_runner_jobs_workspace_request_key" in indexes
