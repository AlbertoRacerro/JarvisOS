from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.database import initialize_database, open_sqlite_connection
from app.modules.bluecad.evidence import (
    get_evidence_record,
    record_fem_static_evidence,
    record_mesh_quality_evidence,
    record_validation_evidence,
)
from app.modules.bluecad.ledger import create_candidate_record, register_artifact, start_attempt
from app.modules.bluecad.models import BluecadLoopConfig
from app.modules.events.service import utc_now


def _init() -> None:
    from app.core.bootstrap import initialize_storage

    initialize_storage(seed_default=True)


def _artifact(tmp_path: Path, workspace_id: str = "main") -> str:
    path = tmp_path / "report.json"
    path.write_text('{"verdict":"pass"}\n', encoding="utf-8")
    return register_artifact(workspace_id, path, role="bluecad_report", source_ref="test")


def test_evidence_records_schema_is_created_and_idempotent() -> None:
    initialize_database()
    initialize_database()
    with open_sqlite_connection() as connection:
        columns = [row["name"] for row in connection.execute("PRAGMA table_info(evidence_records)").fetchall()]
        index = connection.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_evidence_records_workspace_kind'").fetchone()
    assert columns == [
        "id",
        "workspace_id",
        "kind",
        "verdict",
        "metrics_json",
        "source_run_id",
        "candidate_id",
        "attempt_id",
        "report_artifact_id",
        "created_at",
    ]
    assert index is not None


def test_evidence_records_schema_upgrades_preexisting_database() -> None:
    with open_sqlite_connection() as connection:
        connection.execute("CREATE TABLE schema_migrations (migration_id TEXT PRIMARY KEY, name TEXT NOT NULL, applied_at TEXT NOT NULL, checksum TEXT, status TEXT NOT NULL DEFAULT 'applied')")
        connection.execute("INSERT INTO schema_migrations (migration_id, name, applied_at, checksum, status) VALUES ('0007_context_records_fts', 'old', ?, NULL, 'applied')", (utc_now(),))
        connection.commit()
    initialize_database()
    with open_sqlite_connection() as connection:
        row = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'evidence_records'").fetchone()
        migration = connection.execute("SELECT migration_id FROM schema_migrations WHERE migration_id = '0008_evidence_records'").fetchone()
    assert row is not None
    assert migration is not None


def test_record_mesh_quality_evidence_pass_and_empty_group_fail(tmp_path: Path) -> None:
    _init()
    report_id = _artifact(tmp_path)
    pass_result = {"verdict": "pass", "attempts": [{"counts": {"elements_total": 12, "nodes_total": 8}}], "errors": []}
    first_id = record_mesh_quality_evidence("main", pass_result, source_run_id=None, report_artifact_id=report_id)
    first = get_evidence_record(first_id)
    assert first is not None
    assert first.kind == "mesh_quality_v0"
    assert first.verdict == "pass"
    assert json.loads(first.metrics_json) == {"elements_total": 12, "nodes_total": 8, "empty_groups": [], "attempts": 1}

    fail_result = {
        "verdict": "fail",
        "attempts": [{"counts": {"elements_total": 1, "nodes_total": 2}}, {"counts": {"elements_total": 3, "nodes_total": 4}}],
        "errors": [{"code": "MESH_GROUP_EMPTY", "detail": {"group": "loads"}}],
    }
    second_id = record_mesh_quality_evidence("main", fail_result, source_run_id=None, report_artifact_id=report_id)
    second = get_evidence_record(second_id)
    assert second is not None
    assert second.verdict == "fail"
    assert json.loads(second.metrics_json) == {"elements_total": 3, "nodes_total": 4, "empty_groups": ["loads"], "attempts": 2}


def test_record_mesh_quality_evidence_error_without_counts(tmp_path: Path) -> None:
    _init()
    report_id = _artifact(tmp_path)
    result = {
        "verdict": "error",
        "attempts": [{}],
        "errors": [{"code": "MESH_TOOL_ERROR", "detail": {"message": "tool failed"}}],
    }

    record_id = record_mesh_quality_evidence("main", result, source_run_id=None, report_artifact_id=report_id)

    record = get_evidence_record(record_id)
    assert record is not None
    assert record.verdict == "error"
    assert json.loads(record.metrics_json) == {
        "elements_total": None,
        "nodes_total": None,
        "empty_groups": [],
        "attempts": 1,
    }


def test_record_mesh_quality_evidence_fail_without_counts(tmp_path: Path) -> None:
    _init()
    report_id = _artifact(tmp_path)
    result = {
        "verdict": "fail",
        "attempts": [{"counts": {}}],
        "errors": [{"code": "GMSH_EXIT_NONZERO", "detail": {"returncode": 1}}],
    }

    record_id = record_mesh_quality_evidence("main", result, source_run_id=None, report_artifact_id=report_id)

    record = get_evidence_record(record_id)
    assert record is not None
    assert record.verdict == "fail"
    assert json.loads(record.metrics_json) == {
        "elements_total": None,
        "nodes_total": None,
        "empty_groups": [],
        "attempts": 1,
    }


def test_record_fem_static_evidence_pass_and_error(tmp_path: Path) -> None:
    _init()
    report_id = _artifact(tmp_path)
    summary = {
        "verdict": "pass",
        "max_displacement": {"node_id": 1, "value": 2.41},
        "max_von_mises": {"element_id": 2, "node_id": 3, "value": 118.3},
        "solver": {"tool_id": "calculix", "version": "x", "returncode": 0},
        "errors": [],
    }
    report = {"verdict": "fail", "checks": [{"status": "pass"}, {"status": "fail"}]}
    record_id = record_fem_static_evidence("main", summary, report, source_run_id=None, report_artifact_id=report_id)
    record = get_evidence_record(record_id)
    assert record is not None
    assert record.kind == "fem_static_v0"
    assert record.verdict == "fail"
    assert json.loads(record.metrics_json) == {
        "max_displacement_value": 2.41,
        "max_von_mises_value": 118.3,
        "solver_error_code": None,
        "t3_checks_total": 2,
        "t3_checks_failed": 1,
    }

    error_summary = {**summary, "verdict": "error", "errors": [{"code": "SOLVE_ERROR"}]}
    error_id = record_fem_static_evidence("main", error_summary, None, source_run_id=None, report_artifact_id=report_id)
    error = get_evidence_record(error_id)
    assert error is not None
    assert json.loads(error.metrics_json)["solver_error_code"] == "SOLVE_ERROR"


def test_record_fem_static_evidence_error_without_extrema(tmp_path: Path) -> None:
    _init()
    report_id = _artifact(tmp_path)
    error_summary = {
        "verdict": "error",
        "solver": {"tool_id": "calculix", "returncode": 124},
        "errors": [{"code": "SOLVER_TIMEOUT", "message": "solver timed out"}],
    }

    record_id = record_fem_static_evidence("main", error_summary, None, source_run_id=None, report_artifact_id=report_id)

    record = get_evidence_record(record_id)
    assert record is not None
    assert record.verdict == "error"
    assert json.loads(record.metrics_json) == {
        "max_displacement_value": None,
        "max_von_mises_value": None,
        "solver_error_code": "SOLVER_TIMEOUT",
        "t3_checks_total": 0,
        "t3_checks_failed": 0,
    }


def test_record_validation_evidence_pass_and_fail(tmp_path: Path) -> None:
    _init()
    report_id = _artifact(tmp_path)
    candidate = create_candidate_record("main", "brief", BluecadLoopConfig())
    attempt = start_attempt(candidate.id, 1, "external:cheap", prompt_version="test")
    report = {"verdict": "fail", "checks": [{"tier": 1, "status": "pass"}, {"tier": 2, "status": "error"}], "errors": [{"code": "E"}]}
    record_id = record_validation_evidence("main", candidate.id, attempt.id, report, report_artifact_id=report_id)
    record = get_evidence_record(record_id)
    assert record is not None
    assert record.kind == "validation_v0"
    assert record.candidate_id == candidate.id
    assert record.attempt_id == attempt.id
    assert json.loads(record.metrics_json) == {"checks_total": 2, "checks_failed": 1, "tier_max": 2, "errors_total": 1}


def test_evidence_writers_reject_malformed_input(tmp_path: Path) -> None:
    _init()
    report_id = _artifact(tmp_path)
    with pytest.raises(ValueError):
        record_mesh_quality_evidence("main", {"verdict": "pass", "attempts": []}, source_run_id=None, report_artifact_id=report_id)
    with pytest.raises(ValueError):
        record_fem_static_evidence("main", {"verdict": "pass"}, None, source_run_id=None, report_artifact_id=report_id)
    with pytest.raises(ValueError):
        record_validation_evidence("main", "candidate", "attempt", {"verdict": "pass", "checks": {}}, report_artifact_id=report_id)
