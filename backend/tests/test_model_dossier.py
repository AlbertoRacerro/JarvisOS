import sqlite3

import pytest
from fastapi import HTTPException

from app.core.errors import WORKSPACE_NOT_FOUND_MESSAGE
from app.modules.modeling import model_dossier
from app.modules.modeling import routes as modeling_routes


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        """
        CREATE TABLE workspaces (id TEXT PRIMARY KEY);
        CREATE TABLE model_specs (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            title TEXT NOT NULL,
            engineering_question TEXT NOT NULL,
            scope TEXT,
            maturity_status TEXT,
            assumptions_summary TEXT,
            inputs_summary TEXT,
            outputs_summary TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE model_versions (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            model_spec_id TEXT NOT NULL,
            version_label TEXT,
            implementation_kind TEXT,
            status TEXT,
            created_at TEXT NOT NULL,
            input_contract_sha256 TEXT
        );
        CREATE TABLE simulation_runs (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            model_version_id TEXT,
            run_label TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            project_knowledge_revision_id TEXT
        );
        CREATE TABLE artifacts (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            sha256 TEXT,
            source_ref TEXT
        );
        CREATE TABLE run_artifacts (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            simulation_run_id TEXT NOT NULL,
            artifact_id TEXT NOT NULL,
            role TEXT,
            created_at TEXT NOT NULL
        );
        CREATE TABLE evidence_records (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            source_run_id TEXT,
            report_artifact_id TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE freshness_marks (
            id TEXT PRIMARY KEY,
            workspace_id TEXT NOT NULL,
            record_kind TEXT NOT NULL,
            record_id TEXT NOT NULL
        );
        """
    )
    return connection


def _seed_spec_version(
    connection: sqlite3.Connection,
    *,
    workspace_id: str = "ws-a",
    spec_id: str = "spec-a",
    version_id: str = "version-a1",
) -> None:
    connection.execute("INSERT INTO workspaces(id) VALUES (?)", (workspace_id,))
    connection.execute(
        """
        INSERT INTO model_specs(
            id, workspace_id, title, engineering_question, created_at
        ) VALUES (?, ?, ?, ?, ?)
        """,
        (spec_id, workspace_id, "Model", "Question", "2026-01-01T00:00:00Z"),
    )
    connection.execute(
        """
        INSERT INTO model_versions(
            id, workspace_id, model_spec_id, version_label, implementation_kind,
            status, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            version_id,
            workspace_id,
            spec_id,
            "v1",
            "batch_growth_v0",
            "active",
            "2026-01-02T00:00:00Z",
        ),
    )
    connection.commit()


def test_dossier_composes_only_exact_version_evidence_and_proven_staleness(monkeypatch) -> None:
    connection = _connection()
    monkeypatch.setattr(model_dossier, "open_sqlite_connection", lambda: connection)

    connection.executemany("INSERT INTO workspaces(id) VALUES (?)", [("ws-a",), ("ws-b",)])
    connection.executemany(
        """
        INSERT INTO model_specs(
            id, workspace_id, title, engineering_question, scope, maturity_status,
            assumptions_summary, inputs_summary, outputs_summary, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "spec-a",
                "ws-a",
                "A",
                "Question A",
                "scope",
                "validated",
                "assumptions",
                "inputs",
                "outputs",
                "2026-01-01T00:00:00Z",
            ),
            (
                "spec-b",
                "ws-b",
                "B",
                "Question B",
                None,
                None,
                None,
                None,
                None,
                "2026-01-01T00:00:00Z",
            ),
        ],
    )
    connection.executemany(
        """
        INSERT INTO model_versions(
            id, workspace_id, model_spec_id, version_label, implementation_kind,
            status, created_at, input_contract_sha256
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "version-a1",
                "ws-a",
                "spec-a",
                "v1",
                "batch_growth_v0",
                "active",
                "2026-01-02T00:00:00Z",
                "digest-a1",
            ),
            (
                "version-a2",
                "ws-a",
                "spec-a",
                "v2",
                "batch_growth_v0",
                "active",
                "2026-01-03T00:00:00Z",
                "digest-a2",
            ),
            (
                "version-b1",
                "ws-b",
                "spec-b",
                "v1",
                "batch_growth_v0",
                "active",
                "2026-01-02T00:00:00Z",
                "digest-b1",
            ),
        ],
    )
    connection.executemany(
        """
        INSERT INTO simulation_runs(
            id, workspace_id, model_version_id, run_label, status, created_at,
            started_at, completed_at, project_knowledge_revision_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "run-a1",
                "ws-a",
                "version-a1",
                "run 1",
                "succeeded",
                "2026-01-04T00:00:00Z",
                None,
                None,
                "pk-rev-1",
            ),
            (
                "run-a2",
                "ws-a",
                "version-a2",
                "run 2",
                "succeeded",
                "2026-01-05T00:00:00Z",
                None,
                None,
                "pk-rev-2",
            ),
            (
                "run-b1",
                "ws-b",
                "version-b1",
                "run b",
                "succeeded",
                "2026-01-04T00:00:00Z",
                None,
                None,
                "pk-rev-b",
            ),
        ],
    )
    connection.executemany(
        "INSERT INTO artifacts(id, workspace_id, sha256, source_ref) VALUES (?, ?, ?, ?)",
        [
            ("report-a1", "ws-a", "sha-a1", "artifact://report-a1"),
            ("report-a2", "ws-a", "sha-a2", "artifact://report-a2"),
            ("report-b1", "ws-b", "sha-b1", "artifact://report-b1"),
        ],
    )
    connection.executemany(
        """
        INSERT INTO evidence_records(
            id, workspace_id, kind, source_run_id, report_artifact_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            (
                "evidence-a1",
                "ws-a",
                "validation",
                "run-a1",
                "report-a1",
                "2026-01-06T00:00:00Z",
            ),
            (
                "evidence-a2",
                "ws-a",
                "validation",
                "run-a2",
                "report-a2",
                "2026-01-07T00:00:00Z",
            ),
            (
                "evidence-b1",
                "ws-b",
                "validation",
                "run-b1",
                "report-b1",
                "2026-01-06T00:00:00Z",
            ),
        ],
    )
    connection.execute(
        "INSERT INTO freshness_marks(id, workspace_id, record_kind, record_id) VALUES (?, ?, ?, ?)",
        ("mark-a1", "ws-a", "evidence", "evidence-a1"),
    )
    connection.commit()

    dossier = model_dossier.get_model_dossier("ws-a", "version-a1")

    assert dossier is not None
    assert dossier.identity.model_version_id == "version-a1"
    assert [run.run_id for run in dossier.runs] == ["run-a1"]
    assert dossier.runs[0].project_knowledge_revision_id == "pk-rev-1"
    assert [evidence.evidence_id for evidence in dossier.evidence] == ["evidence-a1"]
    assert dossier.evidence[0].freshness == "stale"
    assert dossier.evidence[0].source_ref == "artifact://report-a1"
    assert dossier.evidence[0].availability == "available"
    assert model_dossier.get_model_dossier("ws-b", "version-a1") is None


def test_dossier_does_not_invent_freshness_without_exact_mark(monkeypatch) -> None:
    connection = _connection()
    monkeypatch.setattr(model_dossier, "open_sqlite_connection", lambda: connection)
    _seed_spec_version(connection)
    connection.execute(
        """
        INSERT INTO simulation_runs(id, workspace_id, model_version_id, status, created_at)
        VALUES ('run-a1', 'ws-a', 'version-a1', 'succeeded', '2026-01-03T00:00:00Z')
        """
    )
    connection.execute(
        """
        INSERT INTO artifacts(id, workspace_id, sha256, source_ref)
        VALUES ('report-a1', 'ws-a', 'sha-a1', 'artifact://report-a1')
        """
    )
    connection.execute(
        """
        INSERT INTO evidence_records(
            id, workspace_id, kind, source_run_id, report_artifact_id, created_at
        ) VALUES (
            'evidence-a1', 'ws-a', 'validation', 'run-a1', 'report-a1',
            '2026-01-04T00:00:00Z'
        )
        """
    )
    connection.commit()

    dossier = model_dossier.get_model_dossier("ws-a", "version-a1")

    assert dossier is not None
    assert dossier.evidence[0].freshness is None


def test_dossier_index_proves_empty_state_deterministic_order_and_hard_bounds(monkeypatch) -> None:
    connection = _connection()
    monkeypatch.setattr(model_dossier, "open_sqlite_connection", lambda: connection)
    monkeypatch.setattr(model_dossier, "MODEL_DOSSIER_MAX_MODELS", 2)
    monkeypatch.setattr(model_dossier, "MODEL_DOSSIER_MAX_VERSIONS_PER_MODEL", 2)

    connection.execute("INSERT INTO workspaces(id) VALUES ('ws-a')")
    connection.executemany(
        """
        INSERT INTO model_specs(id, workspace_id, title, engineering_question, created_at)
        VALUES (?, 'ws-a', ?, 'Q', ?)
        """,
        [
            ("spec-a", "A", "2026-01-01T00:00:00Z"),
            ("spec-b", "B", "2026-01-02T00:00:00Z"),
            ("spec-c", "C", "2026-01-03T00:00:00Z"),
        ],
    )
    connection.executemany(
        """
        INSERT INTO model_versions(
            id, workspace_id, model_spec_id, version_label, status, created_at
        ) VALUES (?, 'ws-a', 'spec-c', ?, 'active', ?)
        """,
        [
            ("version-c1", "v1", "2026-01-04T00:00:00Z"),
            ("version-c2", "v2", "2026-01-05T00:00:00Z"),
            ("version-c3", "v3", "2026-01-06T00:00:00Z"),
        ],
    )
    connection.commit()

    index = model_dossier.list_model_dossier_index("ws-a")

    assert [item.model_spec_id for item in index] == ["spec-c", "spec-b"]
    assert [version.model_version_id for version in index[0].versions] == ["version-c3", "version-c2"]
    assert index[1].versions == []
    with pytest.raises(ValueError, match=WORKSPACE_NOT_FOUND_MESSAGE):
        model_dossier.list_model_dossier_index("ws-missing")


def test_dossier_detail_proves_zero_run_child_bounds_and_missing_artifact(monkeypatch) -> None:
    connection = _connection()
    monkeypatch.setattr(model_dossier, "open_sqlite_connection", lambda: connection)
    monkeypatch.setattr(model_dossier, "MODEL_DOSSIER_MAX_RUNS", 2)
    monkeypatch.setattr(model_dossier, "MODEL_DOSSIER_MAX_ARTIFACTS", 2)
    monkeypatch.setattr(model_dossier, "MODEL_DOSSIER_MAX_EVIDENCE", 2)
    _seed_spec_version(connection)

    zero_run = model_dossier.get_model_dossier("ws-a", "version-a1")
    assert zero_run is not None
    assert zero_run.runs == []
    assert zero_run.artifacts == []
    assert zero_run.evidence == []

    connection.executemany(
        """
        INSERT INTO simulation_runs(
            id, workspace_id, model_version_id, run_label, status, created_at
        ) VALUES (?, 'ws-a', 'version-a1', NULL, 'succeeded', ?)
        """,
        [
            ("run-1", "2026-01-03T00:00:00Z"),
            ("run-2", "2026-01-04T00:00:00Z"),
            ("run-3", "2026-01-05T00:00:00Z"),
        ],
    )
    connection.executemany(
        "INSERT INTO artifacts(id, workspace_id, sha256, source_ref) VALUES (?, 'ws-a', ?, ?)",
        [
            ("artifact-1", "sha-1", None),
            ("artifact-2", "sha-2", "artifact://2"),
        ],
    )
    connection.executemany(
        """
        INSERT INTO run_artifacts(
            id, workspace_id, simulation_run_id, artifact_id, role, created_at
        ) VALUES (?, 'ws-a', ?, ?, ?, ?)
        """,
        [
            ("link-1", "run-1", "artifact-1", None, "2026-01-06T00:00:00Z"),
            ("link-2", "run-2", "artifact-2", "report", "2026-01-07T00:00:00Z"),
            ("link-3", "run-3", "artifact-missing", "report", "2026-01-08T00:00:00Z"),
        ],
    )
    connection.executemany(
        """
        INSERT INTO evidence_records(
            id, workspace_id, kind, source_run_id, report_artifact_id, created_at
        ) VALUES (?, 'ws-a', 'validation', ?, ?, ?)
        """,
        [
            ("evidence-1", "run-1", "artifact-1", "2026-01-09T00:00:00Z"),
            ("evidence-2", "run-2", "artifact-2", "2026-01-10T00:00:00Z"),
            ("evidence-3", "run-3", "artifact-missing", "2026-01-11T00:00:00Z"),
        ],
    )
    connection.commit()

    dossier = model_dossier.get_model_dossier("ws-a", "version-a1")

    assert dossier is not None
    assert [run.run_id for run in dossier.runs] == ["run-3", "run-2"]
    assert [artifact.artifact_id for artifact in dossier.artifacts] == ["artifact-missing", "artifact-2"]
    assert dossier.artifacts[0].availability == "unavailable"
    assert dossier.artifacts[0].source_ref is None
    assert [evidence.evidence_id for evidence in dossier.evidence] == ["evidence-3", "evidence-2"]
    assert dossier.evidence[0].availability == "unavailable"
    assert dossier.evidence[0].source_ref is None


def test_dossier_routes_prove_workspace_and_exact_version_404_mapping(monkeypatch) -> None:
    monkeypatch.setattr(modeling_routes, "list_model_dossier_index", lambda workspace_id: [])
    assert modeling_routes.list_model_dossiers_endpoint("ws-a") == []

    def _missing_workspace(workspace_id: str):
        raise ValueError(WORKSPACE_NOT_FOUND_MESSAGE)

    monkeypatch.setattr(modeling_routes, "list_model_dossier_index", _missing_workspace)
    with pytest.raises(HTTPException) as exc_info:
        modeling_routes.list_model_dossiers_endpoint("ws-missing")
    assert exc_info.value.status_code == 404

    monkeypatch.setattr(modeling_routes, "get_model_dossier", lambda workspace_id, version_id: None)
    with pytest.raises(HTTPException) as exc_info:
        modeling_routes.get_model_dossier_endpoint("ws-a", "version-missing")
    assert exc_info.value.status_code == 404


def test_dossier_read_paths_are_rejected_if_they_attempt_sql_writes(monkeypatch) -> None:
    connection = _connection()
    _seed_spec_version(connection)

    write_actions = {
        sqlite3.SQLITE_DELETE,
        sqlite3.SQLITE_INSERT,
        sqlite3.SQLITE_UPDATE,
    }

    def _read_only_authorizer(action, arg1, arg2, database_name, trigger_name):
        del arg1, arg2, database_name, trigger_name
        return sqlite3.SQLITE_DENY if action in write_actions else sqlite3.SQLITE_OK

    connection.set_authorizer(_read_only_authorizer)
    monkeypatch.setattr(model_dossier, "open_sqlite_connection", lambda: connection)

    assert len(model_dossier.list_model_dossier_index("ws-a")) == 1
    assert model_dossier.get_model_dossier("ws-a", "version-a1") is not None
