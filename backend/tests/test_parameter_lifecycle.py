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


def _create_parameter(client: TestClient, *, name: str = "Lifecycle parameter") -> dict[str, object]:
    response = client.post(
        "/workspaces/main/parameters",
        json={
            "name": name,
            "value": "10",
            "unit": "m",
            "value_status": "accepted",
            "status": "accepted",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_default_parameter_reads_hide_noncurrent_and_history_reveals_them(client: TestClient) -> None:
    parameter = _create_parameter(client)

    transitioned = client.post(
        f"/parameters/{parameter['id']}/lifecycle",
        json={
            "workspace_id": "main",
            "action": "deactivate",
            "expected_lifecycle_state": "active",
            "expected_updated_at": parameter["updated_at"],
            "reason": "Not used by the current model.",
        },
    )
    assert transitioned.status_code == 200, transitioned.text
    inactive = transitioned.json()
    assert inactive["lifecycle_state"] == "inactive"

    current = client.get("/workspaces/main/parameters")
    history = client.get("/workspaces/main/parameters?include_noncurrent=true")
    assert current.status_code == 200
    assert history.status_code == 200
    assert parameter["id"] not in {item["id"] for item in current.json()}
    historical = {item["id"]: item for item in history.json()}
    assert historical[parameter["id"]]["lifecycle_state"] == "inactive"


def test_lifecycle_and_edit_mutations_are_cas_guarded(client: TestClient) -> None:
    parameter = _create_parameter(client)

    edited = client.patch(
        f"/parameters/{parameter['id']}",
        json={
            "workspace_id": "main",
            "expected_updated_at": parameter["updated_at"],
            "notes": "Operator note",
        },
    )
    assert edited.status_code == 200, edited.text
    revised = edited.json()
    assert revised["notes"] == "Operator note"
    assert revised["updated_at"] != parameter["updated_at"]

    stale = client.post(
        f"/parameters/{parameter['id']}/lifecycle",
        json={
            "workspace_id": "main",
            "action": "deactivate",
            "expected_lifecycle_state": "active",
            "expected_updated_at": parameter["updated_at"],
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "parameter_stale"

    transitioned = client.post(
        f"/parameters/{parameter['id']}/lifecycle",
        json={
            "workspace_id": "main",
            "action": "deactivate",
            "expected_lifecycle_state": "active",
            "expected_updated_at": revised["updated_at"],
        },
    )
    assert transitioned.status_code == 200
    inactive = transitioned.json()

    rejected_edit = client.patch(
        f"/parameters/{parameter['id']}",
        json={
            "workspace_id": "main",
            "expected_updated_at": inactive["updated_at"],
            "notes": "Must not mutate inactive authority",
        },
    )
    assert rejected_edit.status_code == 409
    assert rejected_edit.json()["detail"]["code"] == "parameter_not_active"


def test_replacement_validator_rejects_nonactive_source(client: TestClient) -> None:
    parameter = _create_parameter(client)
    transitioned = client.post(
        f"/parameters/{parameter['id']}/lifecycle",
        json={
            "workspace_id": "main",
            "action": "deactivate",
            "expected_lifecycle_state": "active",
            "expected_updated_at": parameter["updated_at"],
        },
    )
    assert transitioned.status_code == 200

    from app.core.database import open_sqlite_connection
    from app.modules.memory.replacement import (
        ParameterReplacementError,
        validate_parameter_replacement_proposal,
    )

    with open_sqlite_connection() as connection:
        with pytest.raises(ParameterReplacementError) as exc_info:
            validate_parameter_replacement_proposal(
                connection,
                workspace_id="main",
                supersedes_parameter_id=str(parameter["id"]),
                replacement_parameter_id="replacement-test",
                unit="m",
                value="11",
            )
    assert exc_info.value.code == "parameter_replacement_source_not_active"


def test_replacement_promotion_and_replay_keep_lifecycle_pair_coherent(client: TestClient) -> None:
    superseded = _create_parameter(client, name="Original parameter")
    replacement = _create_parameter(client, name="Replacement parameter")

    from app.core.database import open_sqlite_connection

    with open_sqlite_connection() as connection:
        connection.execute(
            """
            UPDATE parameters
            SET status = 'proposed', supersedes_parameter_id = ?
            WHERE id = ?
            """,
            (superseded["id"], replacement["id"]),
        )
        connection.commit()

    promoted = client.post(f"/memory/parameter/{replacement['id']}/promote-replacement")
    assert promoted.status_code == 200, promoted.text

    def _states() -> dict[str, tuple[str, str]]:
        with open_sqlite_connection() as connection:
            rows = connection.execute(
                "SELECT id, status, lifecycle_state FROM parameters WHERE id IN (?, ?)",
                (superseded["id"], replacement["id"]),
            ).fetchall()
        return {str(row["id"]): (str(row["status"]), str(row["lifecycle_state"])) for row in rows}

    assert _states() == {
        str(superseded["id"]): ("superseded", "superseded"),
        str(replacement["id"]): ("accepted", "active"),
    }

    replayed = client.post(f"/memory/parameter/{replacement['id']}/promote-replacement")
    assert replayed.status_code == 200, replayed.text
    assert replayed.json()["invalidation"]["id"] == promoted.json()["invalidation"]["id"]
    assert _states() == {
        str(superseded["id"]): ("superseded", "superseded"),
        str(replacement["id"]): ("accepted", "active"),
    }
