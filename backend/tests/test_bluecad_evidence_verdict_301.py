"""Tests for spec 301: shared verdict lights on solid, mesh and static FEM evidence."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.core.paths import build_paths
from app.modules.bluecad.read_model import get_bluecad_candidate_aggregate

NOW = "2026-11-18T01:00:00+00:00"


def _init_fixture() -> None:
    initialize_storage()


def _seed_workspace(connection: sqlite3.Connection, workspace_id: str = "main") -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO workspaces (id, name, slug, description, status, created_at, updated_at)
        VALUES (?, 'main', 'main', 'fixture', 'active', ?, ?)
        """,
        (workspace_id, NOW, NOW),
    )


def _seed_artifact(
    connection: sqlite3.Connection,
    artifact_id: str,
    payload: dict | str | bytes,
    *,
    workspace_id: str = "main",
    filename: str | None = None,
    artifact_type: str = "bluecad_report",
) -> Path:
    fn = filename or f"{artifact_id}.json"
    artifact_path = build_paths().artifacts_dir / workspace_id / "bluecad" / artifact_id / fn
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(payload, bytes):
        artifact_path.write_bytes(payload)
    elif isinstance(payload, str):
        artifact_path.write_text(payload, encoding="utf-8")
    else:
        artifact_path.write_text(json.dumps(payload), encoding="utf-8")

    connection.execute(
        """
        INSERT INTO artifacts (
            id, workspace_id, filename, stored_path, artifact_type, mime_type,
            sha256, source_ref, status, created_at
        ) VALUES (?, ?, ?, ?, ?, 'application/json', ?, NULL, 'registered', ?)
        """,
        (
            artifact_id,
            workspace_id,
            fn,
            str(artifact_path),
            artifact_type,
            "a" * 64,
            NOW,
        ),
    )
    return artifact_path


def _seed_candidate(
    connection: sqlite3.Connection,
    candidate_id: str = "candidate-1",
    *,
    workspace_id: str = "main",
) -> None:
    connection.execute(
        """
        INSERT INTO bluecad_candidates (
            id, workspace_id, brief_text, brief_digest, status, parked_reason,
            spec_artifact_id, glb_artifact_id, report_artifact_id,
            promoted_decision_id, origin, parent_candidate_id, loop_config_json,
            created_at, updated_at, notes
        ) VALUES (?, ?, 'brief', ?, 'valid', NULL, NULL, NULL, NULL,
                  NULL, 'ai', NULL, '{}', ?, ?, NULL)
        """,
        (candidate_id, workspace_id, "sha256:" + "a" * 64, NOW, NOW),
    )


def _seed_attempt(
    connection: sqlite3.Connection,
    attempt_id: str = "attempt-1",
    candidate_id: str = "candidate-1",
    attempt_no: int = 1,
    *,
    manifest_artifact_id: str | None = None,
    report_artifact_id: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO bluecad_attempts (
            id, candidate_id, attempt_no, route_class, proposal_outcome,
            build_outcome, validation_verdict, spec_artifact_id,
            report_artifact_id, manifest_artifact_id, started_at, finished_at,
            error_detail_json
        ) VALUES (?, ?, ?, 'auto', 'ok', 'pass', 'pass', NULL, ?, ?, ?, ?, NULL)
        """,
        (attempt_id, candidate_id, attempt_no, report_artifact_id, manifest_artifact_id, NOW, NOW),
    )


def _seed_simulation_run(
    connection: sqlite3.Connection,
    run_id: str,
    analysis_spec: dict,
    *,
    workspace_id: str = "main",
) -> None:
    connection.execute(
        """
        INSERT INTO simulation_runs (
            id, workspace_id, model_version_id, run_label, status,
            input_payload, parameter_payload, output_payload,
            started_at, completed_at, created_at, notes
        ) VALUES (?, ?, NULL, 'sim-run', 'succeeded', '{}', ?, NULL, ?, ?, ?, 'notes')
        """,
        (run_id, workspace_id, json.dumps(analysis_spec), NOW, NOW, NOW),
    )


def _seed_evidence(
    connection: sqlite3.Connection,
    evidence_id: str,
    *,
    kind: str,
    verdict: str,
    report_artifact_id: str,
    candidate_id: str | None = "candidate-1",
    attempt_id: str | None = "attempt-1",
    source_run_id: str | None = None,
    workspace_id: str = "main",
) -> None:
    connection.execute(
        """
        INSERT INTO evidence_records (
            id, workspace_id, kind, verdict, metrics_json, source_run_id,
            candidate_id, attempt_id, report_artifact_id, created_at
        ) VALUES (?, ?, ?, ?, '{}', ?, ?, ?, ?, ?)
        """,
        (
            evidence_id,
            workspace_id,
            kind,
            verdict,
            source_run_id,
            candidate_id,
            attempt_id,
            report_artifact_id,
            NOW,
        ),
    )


def test_solid_validation_evidence_verdict_usable() -> None:
    _init_fixture()
    with open_sqlite_connection() as conn:
        _seed_workspace(conn)
        _seed_candidate(conn, "cand-1")

        manifest = {
            "assembly": {
                "total_volume_mm3": 1000.0,
                "bbox_mm": {"min": [0.0, 0.0, 0.0], "max": [10.0, 10.0, 10.0]},
                "kernel_checks": {
                    "part1": {"brep_valid": True, "manifold": True},
                },
            },
            "parts": {},
        }
        manifest_path = _seed_artifact(conn, "art-manifest-1", manifest)
        # Create dummy step file next to manifest
        (manifest_path.parent / "model.step").write_text("ISO-10303-21;", encoding="utf-8")

        report = {
            "report_version": "bluecad_validation_report_v0_1",
            "spec_id": "sha256:testspec",
            "verdict": "pass",
            "checks": [
                {"id": "T1_BREP_VALID", "status": "pass"},
                {"id": "T1_WATERTIGHT", "status": "pass"},
                {
                    "id": "T1_VOLUME_DECL",
                    "status": "pass",
                    "detail": {"actual": 1000.0, "rel_error": 0.0002, "rel_tol": 0.001},
                },
            ],
            "errors": [],
        }
        _seed_artifact(conn, "art-report-1", report)
        _seed_attempt(conn, "att-1", "cand-1", manifest_artifact_id="art-manifest-1", report_artifact_id="art-report-1")
        _seed_evidence(
            conn,
            "ev-1",
            kind="validation_v0",
            verdict="pass",
            report_artifact_id="art-report-1",
            candidate_id="cand-1",
            attempt_id="att-1",
        )
        conn.commit()

    aggregate = get_bluecad_candidate_aggregate("main", "cand-1")
    assert aggregate is not None
    ev = next(e for e in aggregate.evidence if e.ref == "evidence:ev-1")
    assert ev.status == "pass"  # native status unchanged
    assert ev.shared_verdict == "usable"
    assert len(ev.checks) > 0
    check_names = {c.name for c in ev.checks}
    assert "BREP_VALID" in check_names
    assert "WATERTIGHT" in check_names
    for c in ev.checks:
        assert c.name in {"BREP_VALID", "WATERTIGHT", "VOLUME_CONSERVATION"}
        assert "/" not in c.detail and "\\" not in c.detail
        assert len(c.detail) <= 200


def test_solid_validation_evidence_verdict_not_usable() -> None:
    _init_fixture()
    with open_sqlite_connection() as conn:
        _seed_workspace(conn)
        _seed_candidate(conn, "cand-fail")

        manifest = {
            "assembly": {
                "total_volume_mm3": 1000.0,
                "bbox_mm": {"min": [0.0, 0.0, 0.0], "max": [10.0, 10.0, 10.0]},
                "kernel_checks": {
                    "part1": {"brep_valid": False, "manifold": False},
                },
            },
            "parts": {},
        }
        manifest_path = _seed_artifact(conn, "art-manifest-fail", manifest)
        (manifest_path.parent / "model.step").write_text("ISO-10303-21;", encoding="utf-8")

        report = {
            "report_version": "bluecad_validation_report_v0_1",
            "spec_id": "sha256:testspec",
            "verdict": "fail",
            "checks": [
                {"id": "T1_BREP_VALID", "status": "fail"},
                {"id": "T1_WATERTIGHT", "status": "fail"},
            ],
            "errors": [{"code": "KERNEL_ERROR", "detail": "B-rep check failed"}],
        }
        _seed_artifact(conn, "art-report-fail", report)
        _seed_attempt(conn, "att-fail", "cand-fail", manifest_artifact_id="art-manifest-fail", report_artifact_id="art-report-fail")
        _seed_evidence(
            conn,
            "ev-fail",
            kind="validation_v0",
            verdict="fail",
            report_artifact_id="art-report-fail",
            candidate_id="cand-fail",
            attempt_id="att-fail",
        )
        conn.commit()

    aggregate = get_bluecad_candidate_aggregate("main", "cand-fail")
    assert aggregate is not None
    ev = next(e for e in aggregate.evidence if e.ref == "evidence:ev-fail")
    assert ev.status == "fail"  # native status unchanged
    assert ev.shared_verdict == "not_usable"
    assert ev.verdict_reason is not None


def test_mesh_quality_evidence_verdict_usable_and_unchecked() -> None:
    _init_fixture()
    with open_sqlite_connection() as conn:
        _seed_workspace(conn)
        _seed_candidate(conn, "cand-mesh")
        _seed_attempt(conn, "att-mesh", "cand-mesh")

        # 1. Good mesh with quality
        good_log = """
Info    : Meshing 3D...
Info    : Optimization starts (volume = 1) with worst = 0.803848 / average = 0.869232:
Info    : 0.00 < quality < 0.10 :         0 elements
Info    : 0.80 < quality < 0.90 :        16 elements
"""
        mesh_dir = build_paths().artifacts_dir / "main" / "bluecad" / "mesh_out"
        mesh_dir.mkdir(parents=True, exist_ok=True)
        log_path = mesh_dir / "gmsh.log"
        log_path.write_text(good_log, encoding="utf-8")
        mesh_payload = {
            "schema_version": "bluecad_mesh_result_v0_1",
            "verdict": "pass",
            "errors": [],
            "attempts": [
                {
                    "attempt_no": 1,
                    "target_size": 25.0,
                    "gmsh_returncode": 0,
                    "counts": {"elements_total": 100, "nodes_total": 50, "volume_element_types": {"C3D4": 100}},
                    "warnings": [],
                    "errors": [],
                    "artifacts": {"gmsh_log": {"path": str(log_path), "sha256": "x" * 64, "bytes": len(good_log)}},
                }
            ],
            "artifacts": {"gmsh_log": {"path": str(log_path), "sha256": "x" * 64, "bytes": len(good_log)}},
        }
        _seed_artifact(conn, "art-mesh-good", mesh_payload)
        _seed_simulation_run(conn, "sim-mesh-good", {"analysis_id": "mesh-spec-1"})
        _seed_evidence(
            conn,
            "ev-mesh-good",
            kind="mesh_quality_v0",
            verdict="pass",
            report_artifact_id="art-mesh-good",
            candidate_id="cand-mesh",
            attempt_id="att-mesh",
            source_run_id="sim-mesh-good",
        )

        # 2. Mesh missing quality -> unchecked
        no_qual_log = "Info: Gmsh 4.15.2 exited successfully with 0 errors\n"
        log2_path = mesh_dir / "gmsh_no_qual.log"
        log2_path.write_text(no_qual_log, encoding="utf-8")
        mesh_no_qual = {
            "schema_version": "bluecad_mesh_result_v0_1",
            "verdict": "pass",
            "errors": [],
            "attempts": [
                {
                    "attempt_no": 1,
                    "target_size": 25.0,
                    "gmsh_returncode": 0,
                    "counts": {"elements_total": 50, "nodes_total": 20, "volume_element_types": {"C3D4": 50}},
                    "warnings": [],
                    "errors": [],
                    "artifacts": {"gmsh_log": {"path": str(log2_path), "sha256": "y" * 64, "bytes": len(no_qual_log)}},
                }
            ],
            "artifacts": {"gmsh_log": {"path": str(log2_path), "sha256": "y" * 64, "bytes": len(no_qual_log)}},
        }
        _seed_artifact(conn, "art-mesh-missing-qual", mesh_no_qual)
        _seed_simulation_run(conn, "sim-mesh-missing-qual", {"analysis_id": "mesh-spec-2"})
        _seed_evidence(
            conn,
            "ev-mesh-missing-qual",
            kind="mesh_quality_v0",
            verdict="pass",
            report_artifact_id="art-mesh-missing-qual",
            candidate_id="cand-mesh",
            attempt_id="att-mesh",
            source_run_id="sim-mesh-missing-qual",
        )
        conn.commit()

    aggregate = get_bluecad_candidate_aggregate("main", "cand-mesh")
    assert aggregate is not None

    ev_good = next(e for e in aggregate.evidence if e.ref == "evidence:ev-mesh-good")
    assert ev_good.status == "pass"  # native status unchanged
    assert ev_good.shared_verdict == "usable"
    assert any(c.name == "ELEMENT_QUALITY" and c.status == "pass" for c in ev_good.checks)

    ev_missing = next(e for e in aggregate.evidence if e.ref == "evidence:ev-mesh-missing-qual")
    assert ev_missing.status == "pass"  # native status unchanged
    assert ev_missing.shared_verdict == "unchecked"
    assert any(c.name == "ELEMENT_QUALITY" and c.status == "not_evaluated" for c in ev_missing.checks)


def test_fem_static_evidence_verdict_usable_and_not_usable() -> None:
    _init_fixture()
    with open_sqlite_connection() as conn:
        _seed_workspace(conn)
        _seed_candidate(conn, "cand-fem")
        _seed_attempt(conn, "att-fem", "cand-fem")

        analysis_spec = {
            "analysis_id": "fem-spec",
            "analysis_type": "static",
            "bcs": [{"port_label": "fixed_port", "kind": "fixed"}],
            "loads": [{"port_label": "load_port", "type": "force_total", "force": [100.0, 0.0, 0.0]}],
        }
        _seed_simulation_run(conn, "sim-fem-1", analysis_spec)

        fem_summary_balanced = {
            "schema_version": "bluecad_result_summary_v0_1",
            "verdict": "pass",
            "errors": [],
            "solver": {"tool_id": "calculix", "version": "2.22", "returncode": 0},
            "analysis_type": "static",
            "max_displacement": {"node_id": 10, "value": 0.045},
            "max_von_mises": {"element_id": 1, "node_id": 10, "value": 120.5},
            "reactions": [{"node_id": 1, "force": [-100.0, 0.0, 0.0]}],
            "artifacts": {},
        }
        _seed_artifact(conn, "art-fem-good", {"result_summary": fem_summary_balanced, "report": None})
        _seed_evidence(
            conn,
            "ev-fem-good",
            kind="fem_static_v0",
            verdict="pass",
            report_artifact_id="art-fem-good",
            candidate_id="cand-fem",
            attempt_id="att-fem",
            source_run_id="sim-fem-1",
        )

        # Unbalanced FEM (reaction does not balance load) -> not_usable
        fem_summary_unbalanced = {
            "schema_version": "bluecad_result_summary_v0_1",
            "verdict": "pass",
            "errors": [],
            "solver": {"tool_id": "calculix", "version": "2.22", "returncode": 0},
            "analysis_type": "static",
            "max_displacement": {"node_id": 10, "value": 0.045},
            "max_von_mises": {"element_id": 1, "node_id": 10, "value": 120.5},
            "reactions": [{"node_id": 1, "force": [0.0, 0.0, 0.0]}],
            "artifacts": {},
        }
        _seed_artifact(conn, "art-fem-unbalanced", {"result_summary": fem_summary_unbalanced, "report": None})
        _seed_evidence(
            conn,
            "ev-fem-unbalanced",
            kind="fem_static_v0",
            verdict="pass",
            report_artifact_id="art-fem-unbalanced",
            candidate_id="cand-fem",
            attempt_id="att-fem",
            source_run_id="sim-fem-1",
        )
        conn.commit()

    aggregate = get_bluecad_candidate_aggregate("main", "cand-fem")
    assert aggregate is not None

    ev_good = next(e for e in aggregate.evidence if e.ref == "evidence:ev-fem-good")
    assert ev_good.status == "pass"
    assert ev_good.shared_verdict == "usable"

    ev_unbal = next(e for e in aggregate.evidence if e.ref == "evidence:ev-fem-unbalanced")
    assert ev_unbal.status == "pass"  # native status unchanged
    assert ev_unbal.shared_verdict == "not_usable"
    assert any(c.name == "FORCE_EQUILIBRIUM" and c.status == "fail" for c in ev_unbal.checks)


def test_unreadable_and_missing_artifacts_yield_unchecked() -> None:
    _init_fixture()
    with open_sqlite_connection() as conn:
        _seed_workspace(conn)
        _seed_candidate(conn, "cand-missing")

        # 1. Attempt with missing manifest artifact (manifest_artifact_id=None)
        _seed_attempt(conn, "att-no-manifest", "cand-missing", manifest_artifact_id=None)
        _seed_artifact(conn, "art-report-valid", {"report_version": "v1", "verdict": "pass", "checks": []})
        _seed_evidence(
            conn,
            "ev-no-manifest",
            kind="validation_v0",
            verdict="pass",
            report_artifact_id="art-report-valid",
            candidate_id="cand-missing",
            attempt_id="att-no-manifest",
        )

        # 2. Report artifact content is invalid JSON
        _seed_artifact(conn, "art-manifest-2", {"assembly": {}})
        _seed_attempt(conn, "att-corrupt", "cand-missing", attempt_no=2, manifest_artifact_id="art-manifest-2")
        _seed_artifact(conn, "art-corrupt-json", "{not valid json", filename="corrupt.json")
        _seed_evidence(
            conn,
            "ev-corrupt",
            kind="validation_v0",
            verdict="pass",
            report_artifact_id="art-corrupt-json",
            candidate_id="cand-missing",
            attempt_id="att-corrupt",
        )

        # 3. Mesh evidence with missing simulation run
        _seed_artifact(conn, "art-mesh-empty", {})
        _seed_evidence(
            conn,
            "ev-missing-run",
            kind="mesh_quality_v0",
            verdict="pass",
            report_artifact_id="art-mesh-empty",
            candidate_id="cand-missing",
            attempt_id="att-corrupt",
            source_run_id=None,
        )

        # 4. FEM evidence with missing result_summary in report payload
        _seed_artifact(conn, "art-fem-no-summary", {"not_summary": True})
        _seed_simulation_run(conn, "sim-fem-spec", {"analysis_id": "test"})
        _seed_evidence(
            conn,
            "ev-fem-no-summary",
            kind="fem_static_v0",
            verdict="pass",
            report_artifact_id="art-fem-no-summary",
            candidate_id="cand-missing",
            attempt_id="att-corrupt",
            source_run_id="sim-fem-spec",
        )
        conn.commit()

    aggregate = get_bluecad_candidate_aggregate("main", "cand-missing")
    assert aggregate is not None

    ev_no_man = next(e for e in aggregate.evidence if e.ref == "evidence:ev-no-manifest")
    assert ev_no_man.status == "pass"  # native status unchanged
    assert ev_no_man.shared_verdict == "unchecked"
    assert ev_no_man.verdict_reason is not None

    ev_corrupt = next(e for e in aggregate.evidence if e.ref == "evidence:ev-corrupt")
    assert ev_corrupt.status == "pass"  # native status unchanged
    assert ev_corrupt.shared_verdict == "unchecked"
    assert ev_corrupt.verdict_reason is not None
    assert "/" not in ev_corrupt.verdict_reason and "\\" not in ev_corrupt.verdict_reason

    ev_no_run = next(e for e in aggregate.evidence if e.ref == "evidence:ev-missing-run")
    assert ev_no_run.status == "pass"  # native status unchanged
    assert ev_no_run.shared_verdict == "unchecked"
    assert ev_no_run.verdict_reason is not None

    ev_no_sum = next(e for e in aggregate.evidence if e.ref == "evidence:ev-fem-no-summary")
    assert ev_no_sum.status == "pass"  # native status unchanged
    assert ev_no_sum.shared_verdict == "unchecked"
    assert ev_no_sum.verdict_reason is not None


def test_no_paths_leak_in_evidence_response() -> None:
    _init_fixture()
    with open_sqlite_connection() as conn:
        _seed_workspace(conn)
        _seed_candidate(conn, "cand-path-guard")
        _seed_attempt(conn, "att-pg", "cand-path-guard")
        _seed_simulation_run(conn, "sim-pg", {"analysis_id": "spec-pg"})

        # Report containing raw paths in errors
        report_with_paths = {
            "schema_version": "bluecad_mesh_result_v0_1",
            "verdict": "fail",
            "errors": [
                {
                    "code": "MESH_FAIL",
                    "detail": {"message": "/tmp/sandbox/path/to/mesh.geo failed: /usr/bin/gmsh died"},
                }
            ],
            "attempts": [],
            "artifacts": {},
        }
        _seed_artifact(conn, "art-paths", report_with_paths)
        _seed_evidence(
            conn,
            "ev-paths",
            kind="mesh_quality_v0",
            verdict="fail",
            report_artifact_id="art-paths",
            candidate_id="cand-path-guard",
            attempt_id="att-pg",
            source_run_id="sim-pg",
        )
        conn.commit()

    aggregate = get_bluecad_candidate_aggregate("main", "cand-path-guard")
    assert aggregate is not None
    ev = next(e for e in aggregate.evidence if e.ref == "evidence:ev-paths")
    assert ev.status == "fail"  # native status preserved

    # Check evidence serialization: no filesystem paths should appear
    ev_dict = ev.model_dump()
    assert "/" not in str(ev_dict.get("verdict_reason") or "")
    assert "\\" not in str(ev_dict.get("verdict_reason") or "")
    for check in ev.checks:
        assert "/" not in check.detail
        assert "\\" not in check.detail
        assert "/tmp/" not in check.detail
        assert "/usr/" not in check.detail
