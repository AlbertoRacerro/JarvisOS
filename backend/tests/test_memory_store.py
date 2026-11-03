import sqlite3
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.core.database import open_sqlite_connection
from app.modules.events.service import utc_now


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.bootstrap import initialize_storage
    from app.main import create_app

    initialize_storage(seed_default=True)
    with TestClient(create_app()) as test_client:
        yield test_client
    get_settings.cache_clear()


def _insert_ai_job(job_id: str = "ai-job-1") -> str:
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO ai_jobs (id, created_at, status, task_kind, route_reason_json)
            VALUES (?, ?, 'succeeded', 'test', '{}')
            """,
            (job_id, utc_now()),
        )
        connection.commit()
    return job_id


def _row(table: str, record_id: str) -> sqlite3.Row:
    with open_sqlite_connection() as connection:
        row = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone()
    assert row is not None
    return row


def test_memorystore_columns_upgrade_legacy_database_without_data_loss(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("JARVISOS_DATA_ROOT", str(tmp_path / "JarvisOS"))
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.core.database import get_database_path, initialize_database

    database_path = get_database_path()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(database_path) as connection:
        connection.executescript(
            """
            CREATE TABLE workspaces (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, slug TEXT NOT NULL UNIQUE,
                description TEXT, status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE assumptions (
                id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, statement TEXT NOT NULL,
                scope TEXT, confidence TEXT, status TEXT NOT NULL DEFAULT 'proposed',
                source_ref TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, notes TEXT
            );
            CREATE TABLE parameters (
                id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, name TEXT NOT NULL,
                symbol TEXT, value TEXT, unit TEXT NOT NULL DEFAULT 'unspecified',
                value_status TEXT NOT NULL DEFAULT 'candidate', value_min REAL, value_max REAL,
                source_ref TEXT, confidence REAL, status TEXT NOT NULL DEFAULT 'draft',
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, notes TEXT
            );
            CREATE TABLE decisions (
                id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, title TEXT NOT NULL,
                decision_text TEXT NOT NULL, rationale TEXT, status TEXT NOT NULL DEFAULT 'draft',
                linked_run_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, notes TEXT
            );
            """
        )
        now = utc_now()
        connection.execute(
            "INSERT INTO workspaces (id, name, slug, created_at, updated_at) VALUES ('w1', 'W', 'w', ?, ?)",
            (now, now),
        )
        connection.execute(
            """
            INSERT INTO assumptions (id, workspace_id, statement, status, created_at, updated_at)
            VALUES ('a1', 'w1', 'Legacy assumption', 'proposed', ?, ?)
            """,
            (now, now),
        )
        connection.execute(
            """
            INSERT INTO parameters (id, workspace_id, name, unit, status, created_at, updated_at)
            VALUES ('p1', 'w1', 'Legacy parameter', 'm', 'draft', ?, ?)
            """,
            (now, now),
        )
        connection.execute(
            """
            INSERT INTO decisions (id, workspace_id, title, decision_text, status, created_at, updated_at)
            VALUES ('d1', 'w1', 'Legacy decision', 'Decide', 'draft', ?, ?)
            """,
            (now, now),
        )
        connection.commit()

    initialize_database()
    initialize_database()

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        for table, record_id in (("assumptions", "a1"), ("parameters", "p1"), ("decisions", "d1")):
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}
            assert {"origin", "source_ai_job_id", "promoted_at"}.issubset(columns)
            row = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone()
            assert row["origin"] == "user"
            assert row["source_ai_job_id"] is None
            assert row["promoted_at"] is None
        assert connection.execute("SELECT statement FROM assumptions WHERE id = 'a1'").fetchone()[0] == "Legacy assumption"
        assert connection.execute("SELECT name FROM parameters WHERE id = 'p1'").fetchone()[0] == "Legacy parameter"
        assert connection.execute("SELECT title FROM decisions WHERE id = 'd1'").fetchone()[0] == "Legacy decision"


def test_create_ai_proposals_requires_existing_source_ai_job(client: TestClient) -> None:
    missing = client.post("/memory/proposals", json={"record_kind": "assumption", "workspace_id": "main", "statement": "AI says"})
    assert missing.status_code == 400
    blank = client.post(
        "/memory/proposals",
        json={"record_kind": "assumption", "workspace_id": "main", "statement": "AI says", "source_ai_job_id": " "},
    )
    assert blank.status_code == 400
    absent = client.post(
        "/memory/proposals",
        json={"record_kind": "assumption", "workspace_id": "main", "statement": "AI says", "source_ai_job_id": "missing"},
    )
    assert absent.status_code == 400
    with open_sqlite_connection() as connection:
        count = connection.execute("SELECT COUNT(*) FROM assumptions WHERE statement = 'AI says'").fetchone()[0]
    assert count == 0


def test_create_list_promote_and_reject_memory_proposals(client: TestClient) -> None:
    ai_job_id = _insert_ai_job()
    created = client.post(
        "/memory/proposals",
        json={"record_kind": "parameter", "workspace_id": "main", "name": "AI length", "unit": "m", "source_ai_job_id": ai_job_id},
    )
    assert created.status_code == 201
    proposal = created.json()
    assert proposal["origin"] == "ai_proposed"
    assert proposal["status"] == "proposed"
    assert proposal["source_ai_job_id"] == ai_job_id
    assert proposal["promoted_at"] is None

    listed = client.get("/memory/proposals", params={"workspace_id": "main", "status": "proposed"})
    assert listed.status_code == 200
    assert any(item["id"] == proposal["id"] and item["record_kind"] == "parameter" for item in listed.json())

    promoted = client.post(f"/memory/parameter/{proposal['id']}/promote")
    assert promoted.status_code == 200
    assert promoted.json()["status"] == "accepted"
    assert promoted.json()["promoted_at"] is not None
    before = _row("parameters", proposal["id"])

    rejected_after_accept = client.post(f"/memory/parameter/{proposal['id']}/reject")
    assert rejected_after_accept.status_code == 400
    after = _row("parameters", proposal["id"])
    assert after["status"] == before["status"]
    assert after["promoted_at"] == before["promoted_at"]

    rejected = client.post(
        "/memory/proposals",
        json={
            "record_kind": "assumption",
            "workspace_id": "main",
            "statement": "AI assumption",
            "source_ai_job_id": ai_job_id,
        },
    ).json()
    reject_response = client.post(f"/memory/assumption/{rejected['id']}/reject")
    assert reject_response.status_code == 200
    assert reject_response.json()["status"] == "rejected"
    assert reject_response.json()["promoted_at"] is None
    assert client.post(f"/memory/assumption/{rejected['id']}/promote").status_code == 400
    assert client.post(f"/memory/assumption/{uuid4()}/promote").status_code == 404


def test_memory_parameter_proposals_validate_modeling_parameter_contract(client: TestClient) -> None:
    ai_job_id = _insert_ai_job()
    malformed_payloads = [
        {
            "record_kind": "parameter",
            "workspace_id": "main",
            "name": "Empty unit",
            "unit": "",
            "source_ai_job_id": ai_job_id,
        },
        {
            "record_kind": "parameter",
            "workspace_id": "main",
            "name": "Bad value status",
            "unit": "m",
            "value_status": "guessed",
            "source_ai_job_id": ai_job_id,
        },
        {
            "record_kind": "parameter",
            "workspace_id": "main",
            "name": "Bad bounds",
            "unit": "m",
            "value_min": 10,
            "value_max": 1,
            "source_ai_job_id": ai_job_id,
        },
    ]

    for payload in malformed_payloads:
        response = client.post("/memory/proposals", json=payload)
        assert response.status_code == 422

    with open_sqlite_connection() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM parameters WHERE name IN ('Empty unit', 'Bad value status', 'Bad bounds')"
        ).fetchone()[0]
    assert count == 0

    parameters_response = client.get("/workspaces/main/parameters")
    assert parameters_response.status_code == 200


def test_calc_parameter_proposals_validate_parameter_contract(client: TestClient) -> None:
    from pydantic import ValidationError

    from app.modules.memory.models import CalcParameterProposalCreate
    from app.modules.memory.service import create_calc_parameter_proposals

    with pytest.raises(ValidationError):
        CalcParameterProposalCreate(
            workspace_id="main",
            runner_job_id="runner-1",
            name="Bad calc status",
            value_status="guessed",
        )
    with pytest.raises(ValidationError):
        CalcParameterProposalCreate(
            workspace_id="main",
            runner_job_id="runner-1",
            name="Bad calc bounds",
            value_min=10,
            value_max=1,
        )

    records = create_calc_parameter_proposals(
        [
            CalcParameterProposalCreate(
                workspace_id="main",
                runner_job_id="runner-1",
                name="Valid calc output",
                unit="m",
                value_status="validated",
                value_min=1,
                value_max=10,
            )
        ]
    )
    assert len(records) == 1
    assert records[0].origin == "calc"
    assert records[0].source_ref == "runner_job:runner-1"
    assert client.get("/workspaces/main/parameters").status_code == 200


def test_legacy_status_normalization_and_superseded_unreachable(client: TestClient) -> None:
    now = utc_now()
    with open_sqlite_connection() as connection:
        connection.execute(
            """
            INSERT INTO assumptions (id, workspace_id, statement, status, origin, created_at, updated_at)
            VALUES ('legacy-a', 'main', 'Legacy draft assumption', 'draft', 'user', ?, ?)
            """,
            (now, now),
        )
        connection.execute(
            """
            INSERT INTO decisions (id, workspace_id, title, decision_text, status, origin, created_at, updated_at)
            VALUES ('legacy-d', 'main', 'Legacy draft decision', 'Decide', 'draft', 'user', ?, ?)
            """,
            (now, now),
        )
        connection.execute(
            """
            INSERT INTO parameters (id, workspace_id, name, unit, status, origin, created_at, updated_at)
            VALUES ('sup-p', 'main', 'Superseded parameter', 'm', 'superseded', 'user', ?, ?)
            """,
            (now, now),
        )
        connection.commit()

    listed = client.get("/memory/proposals", params={"workspace_id": "main", "status": "proposed"}).json()
    listed_ids = {item["id"] for item in listed}
    assert {"legacy-a", "legacy-d"}.issubset(listed_ids)
    promoted = client.post("/memory/assumption/legacy-a/promote")
    assert promoted.status_code == 200
    assert promoted.json()["status"] == "accepted"

    assert client.post("/memory/parameter/sup-p/promote").status_code == 400
    assert client.post("/memory/parameter/sup-p/reject").status_code == 400
    with open_sqlite_connection() as connection:
        statuses = connection.execute(
            """
            SELECT status FROM assumptions
            UNION ALL SELECT status FROM parameters
            UNION ALL SELECT status FROM decisions
            """
        ).fetchall()
    assert [row["status"] for row in statuses].count("superseded") == 1


def test_manual_modeling_path_defaults_to_user_origin(client: TestClient) -> None:
    assumption = client.post("/workspaces/main/assumptions", json={"statement": "Manual assumption"})
    parameter = client.post("/workspaces/main/parameters", json={"name": "Manual parameter", "unit": "m"})
    decision = client.post(
        "/workspaces/main/decisions",
        json={"title": "Manual decision", "decision_text": "Decide manually."},
    )
    assert assumption.status_code == 201
    assert parameter.status_code == 201
    assert decision.status_code == 201
    assert _row("assumptions", assumption.json()["id"])["origin"] == "user"
    assert _row("parameters", parameter.json()["id"])["origin"] == "user"
    assert _row("decisions", decision.json()["id"])["origin"] == "user"


def test_proposal_read_model_exposes_existing_decision_fields(client: TestClient) -> None:
    ai_job_id = _insert_ai_job("review-read-model")
    assumption = client.post(
        "/memory/proposals",
        json={
            "record_kind": "assumption",
            "workspace_id": "main",
            "source_ai_job_id": ai_job_id,
            "statement": "Assume seawater density",
            "scope": "hydraulics",
            "confidence": "medium",
            "source_ref": "paper:1",
        },
    )
    parameter = client.post(
        "/memory/proposals",
        json={
            "record_kind": "parameter",
            "workspace_id": "main",
            "source_ai_job_id": ai_job_id,
            "name": "Tube diameter",
            "symbol": "D",
            "value": "0.25",
            "unit": "m",
            "value_status": "literature",
            "value_min": 0.2,
            "value_max": 0.3,
            "confidence": 0.8,
            "source_ref": "paper:2",
        },
    )
    decision = client.post(
        "/memory/proposals",
        json={
            "record_kind": "decision",
            "workspace_id": "main",
            "source_ai_job_id": ai_job_id,
            "title": "Select tube",
            "decision_text": "Use candidate A",
            "rationale": "Lowest verified pressure drop",
            "linked_run_id": None,
        },
    )
    assert assumption.status_code == 201
    assert parameter.status_code == 201
    assert decision.status_code == 201

    listed = client.get("/memory/proposals", params={"workspace_id": "main", "status": "proposed"})
    assert listed.status_code == 200
    by_id = {item["id"]: item for item in listed.json()}

    assumption_read = by_id[assumption.json()["id"]]
    assert assumption_read["scope"] == "hydraulics"
    assert assumption_read["confidence"] == "medium"
    assert assumption_read["symbol"] is None
    assert assumption_read["rationale"] is None

    parameter_read = by_id[parameter.json()["id"]]
    assert parameter_read["symbol"] == "D"
    assert parameter_read["value"] == "0.25"
    assert parameter_read["unit"] == "m"
    assert parameter_read["value_status"] == "literature"
    assert parameter_read["value_min"] == 0.2
    assert parameter_read["value_max"] == 0.3
    assert parameter_read["confidence"] == 0.8
    assert parameter_read["scope"] is None

    decision_read = by_id[decision.json()["id"]]
    assert decision_read["rationale"] == "Lowest verified pressure drop"
    assert decision_read["linked_run_id"] is None
    assert decision_read["value"] is None
    assert decision_read["scope"] is None


def test_memory_module_does_not_import_ai_execution() -> None:
    service_source = (Path(__file__).resolve().parents[1] / "app/modules/memory/service.py").read_text()
    assert "app.modules.ai" not in service_source
    assert "run_ai_task" not in service_source
