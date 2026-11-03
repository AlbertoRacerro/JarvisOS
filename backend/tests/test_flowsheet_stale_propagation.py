import json
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))
    monkeypatch.setenv("DATABASE_URL", "must-not-enter-freshness")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.bootstrap import initialize_storage
    from app.main import create_app

    initialize_storage(seed_default=True)
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def _create_parameter(
    client: TestClient,
    *,
    name: str,
    value: str,
    unit: str,
    status: str,
    supersedes_parameter_id: str | None = None,
) -> str:
    payload = {
        "name": name,
        "value": value,
        "unit": unit,
        "value_status": "accepted" if status == "accepted" else "candidate",
        "status": status,
    }
    if supersedes_parameter_id is not None:
        payload["supersedes_parameter_id"] = supersedes_parameter_id
    response = client.post("/workspaces/main/parameters", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _seed_replacement_fixture(client: TestClient) -> dict[str, str]:
    old_parameter_id = _create_parameter(
        client,
        name="Tube length accepted",
        value="20",
        unit="m",
        status="accepted",
    )
    replacement_parameter_id = _create_parameter(
        client,
        name="Corrected tube length",
        value="22",
        unit="m",
        status="proposed",
        supersedes_parameter_id=old_parameter_id,
    )
    ids = {
        "old": old_parameter_id,
        "replacement": replacement_parameter_id,
        "model_spec": "ms-051",
        "implementation_artifact": "artifact-051-implementation",
        "model_version": "mv-051",
        "run": "run-051",
        "runner_job": "runner-051",
        "result_artifact": "artifact-051-result",
        "output_parameter": "parameter-051-output",
        "decision": "decision-051",
        "evidence": "evidence-051",
    }
    now = "2026-07-20T13:30:00+00:00"
    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO model_specs (id, workspace_id, title, engineering_question, created_at, updated_at)
            VALUES (?, 'main', '051 fixture', 'Trace freshness', ?, ?)
            """,
            (ids["model_spec"], now, now),
        )
        connection.execute(
            """
            INSERT INTO artifacts (
                id, workspace_id, filename, stored_path, artifact_type, mime_type,
                sha256, status, created_at
            ) VALUES (?, 'main', 'implementation.py', '/private/implementation.py',
                      'python_script', 'text/x-python', ?, 'registered', ?)
            """,
            (ids["implementation_artifact"], "1" * 64, now),
        )
        connection.execute(
            """
            INSERT INTO model_versions (
                id, workspace_id, model_spec_id, version_label, implementation_artifact_id,
                implementation_kind, status, created_at
            ) VALUES (?, 'main', ?, 'freshness-fixture-v0', ?, 'calc_v0', 'ready', ?)
            """,
            (ids["model_version"], ids["model_spec"], ids["implementation_artifact"], now),
        )
        input_payload = {
            "tube_length": {
                "value": 20.0,
                "unit": "m",
                "source_parameter_id": ids["old"],
            }
        }
        connection.execute(
            """
            INSERT INTO simulation_runs (
                id, workspace_id, model_version_id, run_label, status, input_payload,
                output_payload, created_at, completed_at
            ) VALUES (?, 'main', ?, '051 fixture run', 'succeeded', ?, ?, ?, ?)
            """,
            (
                ids["run"],
                ids["model_version"],
                json.dumps(input_payload, sort_keys=True),
                json.dumps({"private": 42}, sort_keys=True),
                now,
                now,
            ),
        )
        connection.execute(
            """
            INSERT INTO runner_jobs (
                id, workspace_id, simulation_run_id, runner_type, status, script_path,
                script_sha256, implementation_kind, working_dir, input_file, output_dir,
                timeout_seconds, max_stdout_bytes, max_stderr_bytes,
                max_output_json_bytes, max_artifact_bytes, created_at, updated_at
            ) VALUES (?, 'main', ?, 'python_local', 'succeeded', '/private/script.py', ?,
                      'calc_v0', '/private/work', '/private/input.json', '/private/output',
                      30, 1000, 1000, 1000, 1000, ?, ?)
            """,
            (ids["runner_job"], ids["run"], "2" * 64, now, now),
        )
        connection.execute(
            """
            INSERT INTO artifacts (
                id, workspace_id, filename, stored_path, artifact_type, mime_type,
                sha256, source_ref, status, created_at
            ) VALUES (?, 'main', 'result.json', '/private/result.json', 'json',
                      'application/json', ?, ?, 'registered', ?)
            """,
            (ids["result_artifact"], "3" * 64, f"simulation_run:{ids['run']}", now),
        )
        connection.execute(
            """
            INSERT INTO run_artifacts (
                id, workspace_id, simulation_run_id, artifact_id, role, created_at
            ) VALUES ('run-artifact-051', 'main', ?, ?, 'calc_result_json', ?)
            """,
            (ids["run"], ids["result_artifact"], now),
        )
        connection.execute(
            """
            INSERT INTO parameters (
                id, workspace_id, name, value, unit, value_status, status, origin,
                source_ref, created_at, updated_at
            ) VALUES (?, 'main', 'Derived output', '42', '1', 'candidate', 'proposed',
                      'calc', ?, ?, ?)
            """,
            (ids["output_parameter"], f"runner_job:{ids['runner_job']}", now, now),
        )
        connection.execute(
            """
            INSERT INTO decisions (
                id, workspace_id, title, decision_text, status, origin,
                linked_run_id, created_at, updated_at
            ) VALUES (?, 'main', 'Accepted historical decision', 'PRIVATE DECISION',
                      'accepted', 'user', ?, ?, ?)
            """,
            (ids["decision"], ids["run"], now, now),
        )
        connection.execute(
            """
            INSERT INTO evidence_records (
                id, workspace_id, kind, verdict, metrics_json, source_run_id,
                report_artifact_id, created_at
            ) VALUES (?, 'main', 'validation', 'pass', ?, ?, ?, ?)
            """,
            (
                ids["evidence"],
                json.dumps({"private_metric": 99}, sort_keys=True),
                ids["run"],
                ids["result_artifact"],
                now,
            ),
        )
        connection.commit()
    return ids


def test_replacement_promotion_atomically_marks_downstream_records(client: TestClient) -> None:
    ids = _seed_replacement_fixture(client)

    generic = client.post(f"/memory/parameter/{ids['replacement']}/promote")
    assert generic.status_code == 409, generic.text
    assert generic.json()["detail"]["code"] == "parameter_replacement_promotion_required"

    response = client.post(f"/memory/parameter/{ids['replacement']}/promote-replacement")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["accepted_parameter"]["status"] == "accepted"
    assert body["superseded_parameter"]["status"] == "superseded"
    assert body["accepted_parameter"]["supersedes_parameter_id"] == ids["old"]
    assert body["invalidation"]["affected_count"] >= 6
    assert body["invalidation"]["graph_digest"].startswith("sha256:")

    run_freshness = client.get(f"/workspaces/main/flowsheet/nodes/simulation_run:{ids['run']}/freshness")
    assert run_freshness.status_code == 200, run_freshness.text
    assert run_freshness.json()["latest_invalidation"]["path"] == [
        f"parameter:{ids['old']}",
        f"simulation_run:{ids['run']}",
    ]

    output_freshness = client.get(f"/workspaces/main/flowsheet/nodes/parameter:{ids['output_parameter']}/freshness")
    assert output_freshness.status_code == 200, output_freshness.text
    assert output_freshness.json()["latest_invalidation"]["path"] == [
        f"parameter:{ids['old']}",
        f"simulation_run:{ids['run']}",
        f"runner_job:{ids['runner_job']}",
        f"parameter:{ids['output_parameter']}",
    ]

    detail = client.get(f"/workspaces/main/flowsheet/invalidations/{body['invalidation']['id']}")
    assert detail.status_code == 200, detail.text
    detail_body = detail.json()
    assert [item["record_ref"] for item in detail_body["marks"]] == sorted(
        item["record_ref"] for item in detail_body["marks"]
    )
    rendered = detail.text
    for forbidden in ("PRIVATE DECISION", "private_metric", '"value":', "/private/"):
        assert forbidden not in rendered

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        run = connection.execute("SELECT status FROM simulation_runs WHERE id = ?", (ids["run"],)).fetchone()
        decision = connection.execute("SELECT status FROM decisions WHERE id = ?", (ids["decision"],)).fetchone()
        event = connection.execute(
            "SELECT payload FROM events WHERE event_type = 'ParameterReplacementAccepted'"
        ).fetchone()
        assert run["status"] == "succeeded"
        assert decision["status"] == "accepted"
        assert event is not None
        event_payload = json.loads(event["payload"])
        assert set(event_payload) == {
            "replacement_parameter_id",
            "superseded_parameter_id",
            "invalidation_id",
            "affected_count",
            "graph_digest",
            "cycle_count",
            "unresolved_diagnostic_count",
        }
        assert event_payload["replacement_parameter_id"] == ids["replacement"]
        assert event_payload["superseded_parameter_id"] == ids["old"]
        assert event_payload["invalidation_id"] == body["invalidation"]["id"]
        assert event_payload["affected_count"] == body["invalidation"]["affected_count"]
        assert event_payload["graph_digest"] == body["invalidation"]["graph_digest"]
        assert isinstance(event_payload["cycle_count"], int)
        assert event_payload["cycle_count"] >= 0
        assert isinstance(event_payload["unresolved_diagnostic_count"], int)
        assert event_payload["unresolved_diagnostic_count"] >= 0


def test_replacement_promotion_replay_is_idempotent(client: TestClient) -> None:
    ids = _seed_replacement_fixture(client)
    first = client.post(f"/memory/parameter/{ids['replacement']}/promote-replacement")
    second = client.post(f"/memory/parameter/{ids['replacement']}/promote-replacement")
    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first.json() == second.json()

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        invalidations = connection.execute("SELECT COUNT(*) AS count FROM freshness_invalidations").fetchone()
        events = connection.execute(
            "SELECT COUNT(*) AS count FROM events WHERE event_type = 'ParameterReplacementAccepted'"
        ).fetchone()
        assert invalidations["count"] == 1
        assert events["count"] == 1


def test_zero_descendant_replacement_creates_audit_batch(client: TestClient) -> None:
    old_id = _create_parameter(
        client,
        name="Unbound accepted parameter",
        value="1",
        unit="1",
        status="accepted",
    )
    replacement_id = _create_parameter(
        client,
        name="Replacement without descendants",
        value="2",
        unit="1",
        status="proposed",
        supersedes_parameter_id=old_id,
    )
    response = client.post(f"/memory/parameter/{replacement_id}/promote-replacement")
    assert response.status_code == 200, response.text
    assert response.json()["invalidation"]["affected_count"] == 0

    old_freshness = client.get(f"/workspaces/main/flowsheet/nodes/parameter:{old_id}/freshness")
    assert old_freshness.status_code == 200
    assert old_freshness.json()["state"] == "fresh"


def test_competing_replacement_loser_is_rejected_without_partial_state(client: TestClient) -> None:
    old_id = _create_parameter(
        client,
        name="Accepted source",
        value="10",
        unit="kg",
        status="accepted",
    )
    first_id = _create_parameter(
        client,
        name="Alternative one",
        value="11",
        unit="kg",
        status="proposed",
        supersedes_parameter_id=old_id,
    )
    second_id = _create_parameter(
        client,
        name="Alternative two",
        value="12",
        unit="kg",
        status="proposed",
        supersedes_parameter_id=old_id,
    )
    assert client.post(f"/memory/parameter/{first_id}/promote-replacement").status_code == 200
    loser = client.post(f"/memory/parameter/{second_id}/promote-replacement")
    assert loser.status_code == 409, loser.text
    assert loser.json()["detail"]["code"] == "parameter_already_replaced"

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        second = connection.execute("SELECT status FROM parameters WHERE id = ?", (second_id,)).fetchone()
        assert second["status"] == "proposed"


def test_same_name_without_explicit_replacement_keeps_ordinary_promotion(client: TestClient) -> None:
    _create_parameter(
        client,
        name="Shared name",
        value="1",
        unit="m",
        status="accepted",
    )
    ordinary_id = _create_parameter(
        client,
        name="Shared name",
        value="2",
        unit="m",
        status="proposed",
    )
    promoted = client.post(f"/memory/parameter/{ordinary_id}/promote")
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["status"] == "accepted"

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        count = connection.execute("SELECT COUNT(*) AS count FROM freshness_invalidations").fetchone()
        assert count["count"] == 0
