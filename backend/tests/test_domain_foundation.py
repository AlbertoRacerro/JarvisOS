import json
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


def _event_count(event_type: str) -> int:
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import count_events_by_type

    with open_sqlite_connection() as connection:
        return count_events_by_type(connection, event_type)


def _latest_event_payload(event_type: str) -> dict[str, object]:
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import list_events_by_type

    with open_sqlite_connection() as connection:
        events = list_events_by_type(connection, event_type)

    assert events
    return json.loads(events[-1]["payload"])


def test_database_initialization_and_default_workspace(client: TestClient) -> None:
    info = client.get("/system/info").json()

    assert info["database"]["initialized"] is True
    assert info["database"]["ready"] is True

    response = client.get("/workspaces")

    assert response.status_code == 200
    workspaces = response.json()
    assert any(workspace["id"] == "main" for workspace in workspaces)

    init_again = client.post("/system/initialize")
    assert init_again.status_code == 200

    workspaces_after_second_init = client.get("/workspaces").json()
    assert [workspace["id"] for workspace in workspaces_after_second_init].count("main") == 1
    assert _event_count("WorkspaceCreated") == 1


def test_create_and_list_workspaces(client: TestClient) -> None:
    response = client.post(
        "/workspaces",
        json={
            "name": "Test Workspace",
            "slug": "test-workspace",
            "description": "Created by tests.",
        },
    )

    assert response.status_code == 201
    created = response.json()
    assert created["slug"] == "test-workspace"

    list_response = client.get("/workspaces")
    assert any(workspace["id"] == created["id"] for workspace in list_response.json())
    get_response = client.get(f"/workspaces/{created['id']}")
    assert get_response.status_code == 200
    assert get_response.json()["id"] == created["id"]
    assert _event_count("WorkspaceCreated") >= 2


def test_create_and_list_model_specs(client: TestClient) -> None:
    response = client.post(
        "/workspaces/main/model-specs",
        json={
            "title": "PBR Buoyancy Estimate",
            "engineering_question": "How much buoyancy margin is required?",
            "scope": "Early sizing",
        },
    )

    assert response.status_code == 201
    created = response.json()
    assert created["schema_version"] == 1

    list_response = client.get("/workspaces/main/model-specs")
    assert any(item["id"] == created["id"] for item in list_response.json())
    get_response = client.get(f"/model-specs/{created['id']}")
    assert get_response.status_code == 200
    assert get_response.json()["id"] == created["id"]
    assert _event_count("ModelSpecCreated") == 1
    assert _latest_event_payload("ModelSpecCreated")["title"] == "PBR Buoyancy Estimate"


def test_create_and_list_assumptions(client: TestClient) -> None:
    response = client.post(
        "/workspaces/main/assumptions",
        json={"statement": "Water density is approximately 1025 kg/m3.", "confidence": "medium"},
    )

    assert response.status_code == 201
    created = response.json()
    assert created["statement"].startswith("Water density")

    list_response = client.get("/workspaces/main/assumptions")
    assert any(item["id"] == created["id"] for item in list_response.json())
    assert _event_count("AssumptionCreated") == 1
    assert "Water density" in str(_latest_event_payload("AssumptionCreated")["statement"])


def test_create_and_list_parameters(client: TestClient) -> None:
    response = client.post(
        "/workspaces/main/parameters",
        json={"name": "Seawater density", "symbol": "rho", "value": "1025", "unit": "kg/m3"},
    )

    assert response.status_code == 201
    created = response.json()
    assert created["symbol"] == "rho"

    list_response = client.get("/workspaces/main/parameters")
    assert any(item["id"] == created["id"] for item in list_response.json())
    assert _event_count("ParameterCreated") == 1
    assert _latest_event_payload("ParameterCreated")["symbol"] == "rho"


def test_create_and_list_simulation_runs(client: TestClient) -> None:
    response = client.post(
        "/workspaces/main/simulation-runs",
        json={"run_label": "scratch-001", "status": "planned", "input_payload": "{}"},
    )

    assert response.status_code == 201
    created = response.json()
    assert created["run_label"] == "scratch-001"

    list_response = client.get("/workspaces/main/simulation-runs")
    assert any(item["id"] == created["id"] for item in list_response.json())
    assert _event_count("SimulationRunCreated") == 1
    assert _latest_event_payload("SimulationRunCreated")["run_label"] == "scratch-001"


def test_create_and_list_decisions(client: TestClient) -> None:
    response = client.post(
        "/workspaces/main/decisions",
        json={
            "title": "Use SQLite for early local persistence",
            "decision_text": "Keep the first database local and simple.",
        },
    )

    assert response.status_code == 201
    created = response.json()
    assert created["title"].startswith("Use SQLite")

    list_response = client.get("/workspaces/main/decisions")
    assert any(item["id"] == created["id"] for item in list_response.json())
    assert _event_count("DecisionCreated") == 1
    assert _latest_event_payload("DecisionCreated")["title"] == "Use SQLite for early local persistence"


def test_parameter_validation_requires_schema_freeze_fields(client: TestClient) -> None:
    missing_unit = client.post(
        "/workspaces/main/parameters",
        json={"name": "Seawater density", "symbol": "rho", "value": "1025"},
    )
    assert missing_unit.status_code == 422
    assert "unit" in missing_unit.text

    bad_status = client.post(
        "/workspaces/main/parameters",
        json={
            "name": "Seawater density",
            "value": "1025",
            "unit": "kg/m3",
            "value_status": "guessed",
        },
    )
    assert bad_status.status_code == 422
    assert "value_status" in bad_status.text

    bad_bounds = client.post(
        "/workspaces/main/parameters",
        json={"name": "Seawater density", "unit": "kg/m3", "value_min": 1030, "value_max": 1020},
    )
    assert bad_bounds.status_code == 422
    assert "value_min must be less than or equal to value_max" in bad_bounds.text


def test_create_parameter_with_all_schema_freeze_fields(client: TestClient) -> None:
    response = client.post(
        "/workspaces/main/parameters",
        json={
            "name": "Seawater density",
            "symbol": "rho",
            "value": "1025",
            "unit": "kg/m3",
            "value_status": "measured",
            "value_min": 1020,
            "value_max": 1030,
            "source_ref": "user estimate",
        },
    )

    assert response.status_code == 201
    created = response.json()
    assert created["unit"] == "kg/m3"
    assert created["value_status"] == "measured"
    assert created["value_min"] == 1020
    assert created["value_max"] == 1030
    assert created["source_ref"] == "user estimate"


def test_minimal_parameter_rows_list_with_defaults(client: TestClient) -> None:
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import utc_now

    now = utc_now()
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO parameters (id, workspace_id, name, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            ("legacy-param", "main", "Legacy parameter", now, now),
        )
        connection.commit()

    response = client.get("/workspaces/main/parameters")

    assert response.status_code == 200
    legacy = next(item for item in response.json() if item["id"] == "legacy-param")
    assert legacy["unit"] == "unspecified"
    assert legacy["value_status"] == "candidate"


def test_create_assumption_status_and_confidence(client: TestClient) -> None:
    response = client.post(
        "/workspaces/main/assumptions",
        json={"statement": "Use calm sea during initial tests.", "status": "accepted", "confidence": "high"},
    )

    assert response.status_code == 201
    created = response.json()
    assert created["status"] == "accepted"
    assert created["confidence"] == "high"

    bad_status = client.post(
        "/workspaces/main/assumptions",
        json={"statement": "Invalid assumption.", "status": "draft"},
    )
    assert bad_status.status_code == 422

    bad_confidence = client.post(
        "/workspaces/main/assumptions",
        json={"statement": "Invalid confidence.", "confidence": "certain"},
    )
    assert bad_confidence.status_code == 422


def test_requirement_crud(client: TestClient) -> None:
    create_response = client.post(
        "/workspaces/main/requirements",
        json={"statement": "The buoy shall remain afloat.", "rationale": "Safety-critical behavior."},
    )

    assert create_response.status_code == 201
    created = create_response.json()
    assert created["status"] == "draft"
    assert created["schema_version"] == 1

    get_response = client.get(f"/requirements/{created['id']}")
    assert get_response.status_code == 200
    assert get_response.json()["id"] == created["id"]

    list_response = client.get("/workspaces/main/requirements")
    assert list_response.status_code == 200
    assert any(item["id"] == created["id"] for item in list_response.json())

    update_response = client.patch(
        f"/requirements/{created['id']}",
        json={
            "workspace_id": "main",
            "expected_updated_at": created["updated_at"],
            "rationale": "Updated safety rationale.",
        },
    )
    assert update_response.status_code == 200
    assert update_response.json()["status"] == "draft"
    assert update_response.json()["rationale"] == "Updated safety rationale."
    assert _event_count("RequirementCreated") == 1
