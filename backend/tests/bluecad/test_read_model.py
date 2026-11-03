from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.core.paths import build_paths
from app.main import create_app
from app.modules.bluecad.read_model import _aggregate_from_connection, get_bluecad_candidate_aggregate

NOW = "2026-08-12T18:00:00+00:00"


def _seed_workspace(connection: sqlite3.Connection, workspace_id: str, slug: str) -> None:
    connection.execute(
        """
        INSERT INTO workspaces (id, name, slug, description, status, created_at, updated_at)
        VALUES (?, ?, ?, 'fixture', 'active', ?, ?)
        """,
        (workspace_id, slug, slug, NOW, NOW),
    )


def _seed_artifact(
    connection: sqlite3.Connection,
    artifact_id: str,
    *,
    workspace_id: str = "main",
    source_ref: str | None = None,
    sha256: str | None = "auto",
    stored_path: str | None = None,
) -> None:
    digest = artifact_id[0] * 64 if sha256 == "auto" else sha256
    filename = f"{artifact_id}.json"
    if stored_path is None:
        artifact_path = build_paths().artifacts_dir / workspace_id / "bluecad" / artifact_id / filename
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("{}", encoding="utf-8")
        stored_path = str(artifact_path)
    connection.execute(
        """
        INSERT INTO artifacts (
            id, workspace_id, filename, stored_path, artifact_type, mime_type,
            sha256, source_ref, status, created_at
        ) VALUES (?, ?, ?, ?, 'bluecad_report', 'application/json', ?, ?, 'registered', ?)
        """,
        (
            artifact_id,
            workspace_id,
            filename,
            stored_path,
            digest,
            source_ref,
            NOW,
        ),
    )


def _seed_decision(connection: sqlite3.Connection, decision_id: str, *, workspace_id: str) -> None:
    connection.execute(
        """
        INSERT INTO decisions (
            id, workspace_id, title, decision_text, status, origin, created_at, updated_at
        ) VALUES (?, ?, 'fixture decision', 'fixture decision', 'accepted', 'user', ?, ?)
        """,
        (decision_id, workspace_id, NOW, NOW),
    )


def _seed_candidate(
    connection: sqlite3.Connection,
    candidate_id: str,
    *,
    workspace_id: str = "main",
    spec_artifact_id: str | None = None,
    glb_artifact_id: str | None = None,
    report_artifact_id: str | None = None,
    promoted_decision_id: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO bluecad_candidates (
            id, workspace_id, brief_text, brief_digest, status, parked_reason,
            spec_artifact_id, glb_artifact_id, report_artifact_id,
            promoted_decision_id, origin, parent_candidate_id, loop_config_json,
            created_at, updated_at, notes
        ) VALUES (?, ?, 'fixture brief', ?, 'valid', NULL, ?, ?, ?, ?,
                  'ai', NULL, '{}', ?, ?, NULL)
        """,
        (
            candidate_id,
            workspace_id,
            "sha256:" + "a" * 64,
            spec_artifact_id,
            glb_artifact_id,
            report_artifact_id,
            promoted_decision_id,
            NOW,
            NOW,
        ),
    )


def _seed_attempt(
    connection: sqlite3.Connection,
    attempt_id: str,
    candidate_id: str,
    attempt_no: int,
    *,
    spec_artifact_id: str | None = None,
    report_artifact_id: str | None = None,
    manifest_artifact_id: str | None = None,
    error_detail_json: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO bluecad_attempts (
            id, candidate_id, attempt_no, route_class, proposal_outcome,
            build_outcome, validation_verdict, spec_artifact_id, report_artifact_id,
            manifest_artifact_id, started_at, finished_at, error_detail_json
        ) VALUES (?, ?, ?, 'auto', 'ok', 'pass', 'pass', ?, ?, ?, ?, ?, ?)
        """,
        (
            attempt_id,
            candidate_id,
            attempt_no,
            spec_artifact_id,
            report_artifact_id,
            manifest_artifact_id,
            NOW,
            NOW,
            error_detail_json,
        ),
    )


def _seed_run(connection: sqlite3.Connection, run_id: str, *, workspace_id: str = "main") -> None:
    connection.execute(
        """
        INSERT INTO simulation_runs (
            id, workspace_id, model_version_id, run_label, status, input_payload,
            parameter_payload, output_payload, created_at
        ) VALUES (?, ?, NULL, ?, 'succeeded', '{}', '{}', '{}', ?)
        """,
        (run_id, workspace_id, run_id, NOW),
    )


def _seed_evidence(
    connection: sqlite3.Connection,
    evidence_id: str,
    *,
    candidate_id: str | None,
    attempt_id: str | None,
    report_artifact_id: str,
    source_run_id: str | None = None,
    workspace_id: str = "main",
) -> None:
    connection.execute(
        """
        INSERT INTO evidence_records (
            id, workspace_id, kind, verdict, metrics_json, source_run_id,
            candidate_id, attempt_id, report_artifact_id, created_at
        ) VALUES (?, ?, 'validation_v0', 'pass', '{}', ?, ?, ?, ?, ?)
        """,
        (
            evidence_id,
            workspace_id,
            source_run_id,
            candidate_id,
            attempt_id,
            report_artifact_id,
            NOW,
        ),
    )


def _mark_stale(connection: sqlite3.Connection, ref: str, suffix: str) -> None:
    old_id = f"old-{suffix}"
    new_id = f"new-{suffix}"
    for parameter_id in (old_id, new_id):
        connection.execute(
            """
            INSERT INTO parameters (
                id, workspace_id, name, value, unit, value_status, status, origin,
                created_at, updated_at
            ) VALUES (?, 'main', ?, '1', '1', 'explicit', 'accepted', 'user', ?, ?)
            """,
            (parameter_id, parameter_id, NOW, NOW),
        )
    invalidation_id = f"inv-{suffix}"
    connection.execute(
        """
        INSERT INTO freshness_invalidations (
            id, workspace_id, superseded_parameter_id, replacement_parameter_id,
            source_graph_digest, affected_count, unresolved_diagnostic_count,
            cycle_count, created_at
        ) VALUES (?, 'main', ?, ?, ?, 1, 0, 0, ?)
        """,
        (invalidation_id, old_id, new_id, "sha256:" + "b" * 64, NOW),
    )
    kind, record_id = ref.split(":", 1)
    path_json = json.dumps([f"parameter:{old_id}", ref])
    connection.execute(
        """
        INSERT INTO freshness_marks (
            id, workspace_id, invalidation_id, record_ref, record_kind, record_id,
            reason_code, path_json, path_digest, created_at
        ) VALUES (?, 'main', ?, ?, ?, ?, 'upstream_parameter_superseded', ?, ?, ?)
        """,
        (
            f"mark-{suffix}",
            invalidation_id,
            ref,
            kind,
            record_id,
            path_json,
            "sha256:" + "c" * 64,
            NOW,
        ),
    )


def _init_fixture() -> None:
    initialize_storage(seed_default=True)


def test_aggregate_is_deterministic_workspace_scoped_and_lossless() -> None:
    _init_fixture()
    with open_sqlite_connection() as connection:
        _seed_workspace(connection, "other", "other")
        _seed_artifact(connection, "artifact-a")
        _seed_artifact(connection, "artifact-b", workspace_id="other")
        _seed_candidate(
            connection,
            "candidate-a",
            spec_artifact_id="artifact-a",
            glb_artifact_id="artifact-b",
            report_artifact_id="artifact-a",
        )
        _seed_attempt(
            connection,
            "attempt-2",
            "candidate-a",
            2,
            spec_artifact_id="artifact-a",
            error_detail_json="{malformed",
        )
        _seed_attempt(connection, "attempt-1", "candidate-a", 1, report_artifact_id="artifact-a")
        _seed_candidate(connection, "candidate-other", workspace_id="other")
        connection.commit()

    first = get_bluecad_candidate_aggregate("main", "candidate-a")
    second = get_bluecad_candidate_aggregate("main", "candidate-a")
    assert first is not None and second is not None
    assert first.model_dump() == second.model_dump()
    assert [attempt.id for attempt in first.candidate.attempts] == ["attempt-1", "attempt-2"]
    assert first.candidate.attempts[1].error_detail_json == "{malformed"
    assert [artifact.id for artifact in first.artifacts] == ["artifact-a"]
    assert first.artifacts[0].roles == [
        "attempt.report_artifact_id",
        "attempt.spec_artifact_id",
        "candidate.report_artifact_id",
        "candidate.spec_artifact_id",
    ]
    serialized = first.model_dump_json()
    assert "stored_path" not in serialized
    assert "artifact-b.json" not in serialized
    assert any(item.reference == "artifact-b" and item.code == "missing_reference" for item in first.diagnostics)
    assert get_bluecad_candidate_aggregate("main", "candidate-other") is None
    assert get_bluecad_candidate_aggregate("main", "does-not-exist") is None


def test_explicit_evidence_run_links_and_four_state_freshness() -> None:
    _init_fixture()
    with open_sqlite_connection() as connection:
        _seed_artifact(connection, "artifact-a")
        _seed_candidate(connection, "candidate-a", report_artifact_id="artifact-a")
        _seed_attempt(connection, "attempt-a", "candidate-a", 1, report_artifact_id="artifact-a")
        _seed_run(connection, "run-linked")
        _seed_run(connection, "run-unrelated")
        _seed_evidence(
            connection,
            "evidence-stale",
            candidate_id="candidate-a",
            attempt_id="attempt-a",
            report_artifact_id="artifact-a",
            source_run_id="run-linked",
        )
        _seed_evidence(
            connection,
            "evidence-fresh",
            candidate_id="candidate-a",
            attempt_id=None,
            report_artifact_id="artifact-a",
        )
        _seed_evidence(
            connection,
            "evidence-unrelated",
            candidate_id=None,
            attempt_id=None,
            report_artifact_id="artifact-a",
            source_run_id="run-unrelated",
        )
        _mark_stale(connection, "evidence:evidence-stale", "evidence")
        connection.commit()

    aggregate = get_bluecad_candidate_aggregate("main", "candidate-a")
    assert aggregate is not None
    assert [item.ref for item in aggregate.evidence] == [
        "evidence:evidence-fresh",
        "evidence:evidence-stale",
        "evidence:evidence-stale",
    ]
    stale_items = [item for item in aggregate.evidence if item.ref == "evidence:evidence-stale"]
    assert all(item.stale is True for item in stale_items)
    fresh_item = next(item for item in aggregate.evidence if item.ref == "evidence:evidence-fresh")
    assert fresh_item.stale is False
    assert "evidence:evidence-unrelated" not in {item.ref for item in aggregate.evidence}
    assert {item.ref for item in aggregate.runs} == {"simulation_run:run-linked"}
    assert "simulation_run:run-unrelated" not in {item.ref for item in aggregate.runs}
    assert aggregate.freshness == "mixed"

    with open_sqlite_connection() as connection:
        _seed_candidate(connection, "candidate-empty")
        connection.commit()
    empty = get_bluecad_candidate_aggregate("main", "candidate-empty")
    assert empty is not None
    assert empty.freshness == "unknown"


def test_digestless_artifact_keeps_explicit_run_provenance_without_fabricated_checksum() -> None:
    _init_fixture()
    with open_sqlite_connection() as connection:
        _seed_run(connection, "run-linked")
        _seed_artifact(
            connection,
            "artifact-a",
            source_ref="simulation_run:run-linked",
            sha256=None,
        )
        _seed_candidate(connection, "candidate-a", report_artifact_id="artifact-a")
        connection.commit()

    aggregate = get_bluecad_candidate_aggregate("main", "candidate-a")
    assert aggregate is not None
    assert aggregate.artifacts == []
    assert {item.ref for item in aggregate.runs} == {"simulation_run:run-linked"}
    assert aggregate.freshness == "fresh"
    assert any(
        item.source == "bluecad.artifact.sha256"
        and item.reference == "artifact-a"
        and item.code == "malformed_reference"
        for item in aggregate.diagnostics
    )
    assert '"sha256":"None"' not in aggregate.model_dump_json()


def test_digestless_artifact_keeps_source_reference_diagnostic() -> None:
    _init_fixture()
    with open_sqlite_connection() as connection:
        _seed_artifact(
            connection,
            "artifact-a",
            source_ref="not-a-canonical-reference",
            sha256=None,
        )
        _seed_candidate(connection, "candidate-a", report_artifact_id="artifact-a")
        connection.commit()

    aggregate = get_bluecad_candidate_aggregate("main", "candidate-a")
    assert aggregate is not None
    assert aggregate.artifacts == []
    assert any(
        item.source == "artifacts.source_ref"
        and item.reference == "not-a-canonical-reference"
        and item.code == "unsupported_reference"
        for item in aggregate.diagnostics
    )


def test_inaccessible_promoted_decision_keeps_dangling_reference_diagnostic() -> None:
    _init_fixture()
    with open_sqlite_connection() as connection:
        _seed_workspace(connection, "other", "other")
        _seed_decision(connection, "decision-other", workspace_id="other")
        _seed_candidate(connection, "candidate-a", promoted_decision_id="decision-other")
        connection.commit()

    aggregate = get_bluecad_candidate_aggregate("main", "candidate-a")
    assert aggregate is not None
    assert aggregate.candidate.promoted_decision_id == "decision-other"
    assert any(
        item.source == "bluecad_candidates.promoted_decision_id"
        and item.reference == "decision:decision-other"
        and item.code == "missing_reference"
        for item in aggregate.diagnostics
    )


def test_inaccessible_artifact_content_is_not_advertised() -> None:
    _init_fixture()
    with open_sqlite_connection() as connection:
        _seed_artifact(connection, "artifact-a", stored_path="/outside/data-root/artifact-a.json")
        _seed_candidate(connection, "candidate-a", report_artifact_id="artifact-a")
        connection.commit()

    aggregate = get_bluecad_candidate_aggregate("main", "candidate-a")
    assert aggregate is not None
    assert aggregate.artifacts == []
    assert any(
        item.source == "bluecad.artifact.content"
        and item.reference == "artifact-a"
        and item.code == "inaccessible_reference"
        for item in aggregate.diagnostics
    )
    assert "/bluecad/artifacts/artifact-a/content" not in aggregate.model_dump_json()


def test_aggregate_performs_zero_writes_and_query_count_is_not_per_evidence() -> None:
    _init_fixture()
    with open_sqlite_connection() as connection:
        _seed_artifact(connection, "artifact-a")
        _seed_candidate(connection, "candidate-a", report_artifact_id="artifact-a")
        for index in range(20):
            _seed_evidence(
                connection,
                f"evidence-{index:02d}",
                candidate_id="candidate-a",
                attempt_id=None,
                report_artifact_id="artifact-a",
            )
        connection.commit()

    with open_sqlite_connection() as connection:
        statements: list[str] = []
        connection.set_trace_callback(statements.append)
        before_changes = connection.total_changes
        aggregate = _aggregate_from_connection(connection, "main", "candidate-a")
        after_changes = connection.total_changes
        connection.set_trace_callback(None)
    assert aggregate is not None
    assert after_changes == before_changes
    freshness_selects = [statement for statement in statements if "FROM freshness_marks" in statement]
    assert len(freshness_selects) == 1
    assert len(aggregate.evidence) == 20


def test_route_hides_cross_workspace_candidate_existence() -> None:
    _init_fixture()
    with open_sqlite_connection() as connection:
        _seed_workspace(connection, "other", "other")
        _seed_candidate(connection, "candidate-other", workspace_id="other")
        connection.commit()

    with TestClient(create_app()) as client:
        missing = client.get("/workspaces/main/bluecad/candidates/missing/aggregate")
        cross_workspace = client.get("/workspaces/main/bluecad/candidates/candidate-other/aggregate")
    assert missing.status_code == 404
    assert cross_workspace.status_code == 404
    assert missing.json() == cross_workspace.json()


def test_candidate_artifact_model_version_path_includes_only_traceable_runs() -> None:
    _init_fixture()
    with open_sqlite_connection() as connection:
        _seed_artifact(connection, "artifact-a")
        _seed_artifact(connection, "artifact-other")
        _seed_candidate(connection, "candidate-a", report_artifact_id="artifact-a")
        connection.execute(
            """
            INSERT INTO model_specs (
                id, workspace_id, title, engineering_question, status, maturity_status,
                created_at, updated_at
            ) VALUES
                ('spec-a', 'main', 'spec a', 'fixture', 'active', 'draft', ?, ?),
                ('spec-other', 'main', 'spec other', 'fixture', 'active', 'draft', ?, ?)
            """,
            (NOW, NOW, NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO model_versions (
                id, workspace_id, model_spec_id, version_label, implementation_artifact_id,
                implementation_kind, status, created_at
            ) VALUES
                ('version-a', 'main', 'spec-a', 'v1', 'artifact-a', 'batch_growth_v0', 'active', ?),
                ('version-other', 'main', 'spec-other', 'v1', 'artifact-other', 'batch_growth_v0', 'active', ?)
            """,
            (NOW, NOW),
        )
        connection.execute(
            """
            INSERT INTO simulation_runs (
                id, workspace_id, model_version_id, run_label, status, input_payload,
                parameter_payload, output_payload, created_at
            ) VALUES
                ('run-linked', 'main', 'version-a', 'linked', 'succeeded', '{}', '{}', '{}', ?),
                ('run-unrelated', 'main', 'version-other', 'unrelated', 'succeeded', '{}', '{}', '{}', ?)
            """,
            (NOW, NOW),
        )
        _mark_stale(connection, "simulation_run:run-linked", "model-run")
        connection.commit()

    aggregate = get_bluecad_candidate_aggregate("main", "candidate-a")
    assert aggregate is not None
    assert {item.ref for item in aggregate.runs} == {"simulation_run:run-linked"}
    linked = next(item for item in aggregate.runs if item.ref == "simulation_run:run-linked")
    assert linked.stale is True
    assert aggregate.freshness == "stale"
