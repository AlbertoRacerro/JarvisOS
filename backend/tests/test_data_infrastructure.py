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


@pytest.fixture
def uninitialized_client(tmp_path, monkeypatch) -> Iterator[TestClient]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))

    from app.core.config import get_settings

    get_settings.cache_clear()

    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client

    get_settings.cache_clear()


def test_schema_migration_table_records_baseline_and_current_version(client: TestClient) -> None:
    from app.core.cad_link_schema import CAD_LINK_SCHEMA_MIGRATION_RECORD
    from app.core.database import open_sqlite_connection
    from app.core.egress_schema import EGRESS_SCHEMA_MIGRATION_ID
    from app.core.grade_schema import GRADE_SCHEMA_MIGRATION_ID
    from app.core.schema import (
        CURRENT_SCHEMA_MIGRATION_ID,
        SCHEMA_BASELINE_MIGRATION_ID,
        SCHEMA_FRESHNESS_INVALIDATION_MIGRATION_ID,
    )
    from app.core.token_flow_schema import TOKEN_FLOW_SCHEMA_MIGRATION_ID

    with open_sqlite_connection() as connection:
        rows = connection.execute(
            "SELECT migration_id, name, status FROM schema_migrations ORDER BY migration_id ASC"
        ).fetchall()

    migrations = {row["migration_id"]: dict(row) for row in rows}
    assert SCHEMA_BASELINE_MIGRATION_ID in migrations
    assert SCHEMA_FRESHNESS_INVALIDATION_MIGRATION_ID in migrations
    assert GRADE_SCHEMA_MIGRATION_ID in migrations
    assert CAD_LINK_SCHEMA_MIGRATION_RECORD["migration_id"] in migrations
    assert EGRESS_SCHEMA_MIGRATION_ID in migrations
    assert TOKEN_FLOW_SCHEMA_MIGRATION_ID in migrations
    assert migrations[EGRESS_SCHEMA_MIGRATION_ID]["status"] == "applied"
    assert migrations[TOKEN_FLOW_SCHEMA_MIGRATION_ID]["status"] == "applied"
    assert migrations[GRADE_SCHEMA_MIGRATION_ID]["status"] == "applied"
    assert migrations[CAD_LINK_SCHEMA_MIGRATION_RECORD["migration_id"]]["status"] == "applied"
    assert CURRENT_SCHEMA_MIGRATION_ID in migrations
    assert migrations[CURRENT_SCHEMA_MIGRATION_ID]["status"] == "applied"

    info = client.get("/system/info").json()
    assert info["database"]["schema"]["current_migration_id"] == CURRENT_SCHEMA_MIGRATION_ID
    assert info["database"]["schema"]["current_migration_status"] == "applied"
    assert info["database"]["schema"]["applied_migration_count"] >= 2


def test_system_info_reports_bootstrap_action_when_database_is_uninitialized(
    uninitialized_client: TestClient,
) -> None:
    response = uninitialized_client.get("/system/info")

    assert response.status_code == 200
    database = response.json()["database"]
    assert database["initialized"] is False
    assert database["ready"] is False
    assert database["bootstrap_required"] is True
    assert database["bootstrap_action"] == "POST /system/initialize"
    assert database["schema"]["current_migration_id"] is None


def test_schema_indexes_exist(client: TestClient) -> None:
    from app.core.database import open_sqlite_connection

    expected = {
        "idx_events_workspace_created_at",
        "idx_artifacts_workspace_created_at",
        "idx_run_artifacts_workspace_run",
        "idx_simulation_runs_workspace_created_at",
        "idx_simulation_runs_workspace_status",
        "idx_runner_jobs_workspace_status",
        "idx_model_versions_workspace_model_spec",
    }
    with open_sqlite_connection() as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'idx_%'"
        ).fetchall()

    assert expected.issubset({row["name"] for row in rows})


def test_repository_row_projection_helpers(client: TestClient) -> None:
    from app.core.database import open_sqlite_connection
    from app.core.repository import optional_row_to_model, rows_to_models
    from app.modules.workspaces.models import WorkspaceRead

    with open_sqlite_connection() as connection:
        rows = connection.execute("SELECT * FROM workspaces ORDER BY created_at ASC").fetchall()
        missing = connection.execute("SELECT * FROM workspaces WHERE id = ?", ("missing",)).fetchone()

    workspaces = rows_to_models(rows, WorkspaceRead)
    assert any(workspace.id == "main" for workspace in workspaces)
    assert optional_row_to_model(missing, WorkspaceRead) is None


def test_redaction_helper_masks_obvious_secret_values() -> None:
    from app.modules.events.service import redact_event_payload

    payload = {
        "api_key": "sk-secret-value",
        "authorization": "Bearer live-token",
        "message": "password=abc123 token is xyz789 and see .env",
        "safe_token_counter": 42,
        "nested": {
            "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----",
            "note": "privacy_policy_secret_blocked",
        },
    }

    redacted = redact_event_payload(payload)
    text = json.dumps(redacted)
    assert "sk-secret-value" not in text
    assert "live-token" not in text
    assert "abc123" not in text
    assert "xyz789" not in text
    assert ".env" not in text
    assert "BEGIN PRIVATE KEY" not in text
    assert redacted["safe_token_counter"] == 42
    assert redacted["nested"]["note"] == "privacy_policy_secret_blocked"


def test_event_persistence_applies_redaction(client: TestClient) -> None:
    from app.core.database import open_sqlite_connection
    from app.modules.events.service import log_event

    with open_sqlite_connection() as connection:
        log_event(
            connection,
            event_type="RedactionSmoke",
            actor="local-user",
            target_type="Test",
            target_id="redaction-test",
            workspace_id="main",
            payload={"message": "api key is secret-value-123", "token_usage_month_to_date": 7},
        )
        connection.commit()
        row = connection.execute("SELECT payload FROM events WHERE event_type = 'RedactionSmoke'").fetchone()

    payload = json.loads(row["payload"])
    payload_text = json.dumps(payload)
    assert "secret-value-123" not in payload_text
    assert payload["token_usage_month_to_date"] == 7
