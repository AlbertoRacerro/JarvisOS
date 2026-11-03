from __future__ import annotations

import json
from pathlib import Path

from app.core.bootstrap import initialize_storage
from app.core.database import open_sqlite_connection
from app.modules.bluecad.evidence import (
    record_fem_static_evidence,
    record_mesh_quality_evidence,
    record_validation_evidence,
)
from app.modules.bluecad.evidence_sight import render_evidence_sight
from app.modules.bluecad.ledger import (
    create_candidate_record,
    register_artifact,
    start_attempt,
)
from app.modules.bluecad.models import BluecadLoopConfig


def _init() -> None:
    initialize_storage(seed_default=True)


def _artifact(tmp_path: Path, name: str, body: str = "{}\n") -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return register_artifact(
        "main",
        path,
        role="bluecad_report",
        source_ref=f"test:{name}",
    )


def _candidate_attempt(label: str):
    candidate = create_candidate_record("main", label, BluecadLoopConfig())
    attempt = start_attempt(candidate.id, 1, "external:cheap", prompt_version="test")
    return candidate, attempt


def _mesh_result(verdict: str = "pass") -> dict:
    return {
        "verdict": verdict,
        "attempts": [
            {
                "counts": {"elements_total": 12, "nodes_total": 8},
            }
        ],
        "errors": [],
    }


def _fem_summary() -> dict:
    return {
        "verdict": "pass",
        "max_displacement": {"node_id": 1, "value": 0.1},
        "max_von_mises": {"element_id": 1, "node_id": 1, "value": 100.0},
        "solver": {"tool_id": "calculix", "version": "fake", "returncode": 0},
        "errors": [],
    }


def _link(record_ids: list[str], candidate_id: str, attempt_id: str) -> None:
    with open_sqlite_connection() as connection:
        for record_id in record_ids:
            connection.execute(
                "UPDATE evidence_records SET candidate_id = ?, attempt_id = ? WHERE id = ?",
                (candidate_id, attempt_id, record_id),
            )
        connection.commit()


def test_render_is_exactly_scoped_and_orders_validation_mesh_fem(tmp_path: Path) -> None:
    _init()
    candidate, attempt = _candidate_attempt("target")
    other_candidate, other_attempt = _candidate_attempt("other")

    validation_id = record_validation_evidence(
        "main",
        candidate.id,
        attempt.id,
        {"verdict": "pass", "checks": [], "errors": []},
        report_artifact_id=_artifact(tmp_path, "validation.json"),
    )
    mesh_id = record_mesh_quality_evidence(
        "main",
        _mesh_result(),
        source_run_id=None,
        report_artifact_id=_artifact(tmp_path, "mesh.json"),
    )
    fem_id = record_fem_static_evidence(
        "main",
        _fem_summary(),
        {"verdict": "fail", "checks": [{"status": "fail"}], "errors": []},
        source_run_id=None,
        report_artifact_id=_artifact(tmp_path, "fem.json"),
    )
    _link([mesh_id, fem_id], candidate.id, attempt.id)

    leaked_id = record_validation_evidence(
        "main",
        other_candidate.id,
        other_attempt.id,
        {"verdict": "fail", "checks": [], "errors": []},
        report_artifact_id=_artifact(tmp_path, "other.json"),
    )

    sight = render_evidence_sight("main", candidate.id, attempt.id)

    assert sight is not None
    assert sight.record_ids == (validation_id, mesh_id, fem_id)
    assert leaked_id not in sight.record_ids
    lines = sight.text.splitlines()
    assert lines[0] == "EVIDENCE_SIGHT_V0"
    assert "evidence:validation_v0" in lines[1]
    assert "evidence:mesh_quality_v0" in lines[2]
    assert "evidence:fem_static_v0" in lines[3]


def test_render_is_deterministic_with_tied_timestamps(tmp_path: Path) -> None:
    _init()
    candidate, attempt = _candidate_attempt("ties")
    ids = [
        record_validation_evidence(
            "main",
            candidate.id,
            attempt.id,
            {"verdict": "pass", "checks": [], "errors": []},
            report_artifact_id=_artifact(tmp_path, f"v{index}.json"),
        )
        for index in range(2)
    ]
    with open_sqlite_connection() as connection:
        connection.execute(
            "UPDATE evidence_records SET created_at = '2026-01-01T00:00:00Z' WHERE id IN (?, ?)",
            tuple(ids),
        )
        connection.commit()

    first = render_evidence_sight("main", candidate.id, attempt.id)
    second = render_evidence_sight("main", candidate.id, attempt.id)

    assert first == second
    assert first is not None
    assert first.record_ids == tuple(sorted(ids))
    assert first.digest.startswith("sha256:")


def test_render_enforces_line_and_character_bounds_without_artifact_body(tmp_path: Path) -> None:
    _init()
    candidate, attempt = _candidate_attempt("bounded")
    secret = "RAW_SOLVER_OUTPUT_SHOULD_NEVER_APPEAR"
    for index in range(8):
        record_validation_evidence(
            "main",
            candidate.id,
            attempt.id,
            {
                "verdict": "fail",
                "checks": [{"tier": index, "status": "fail"}],
                "errors": [{"code": f"E{index}"}],
            },
            report_artifact_id=_artifact(
                tmp_path,
                f"bounded-{index}.json",
                json.dumps({"raw": secret, "index": index}),
            ),
        )

    sight = render_evidence_sight(
        "main",
        candidate.id,
        attempt.id,
        max_lines=3,
        max_chars=500,
    )

    assert sight is not None
    assert len(sight.text) <= 500
    assert len(sight.text.splitlines()) <= 4
    assert secret not in sight.text
    assert "evidence:omitted" in sight.text


def test_render_returns_none_for_empty_exact_scope() -> None:
    _init()
    candidate, attempt = _candidate_attempt("empty")

    assert render_evidence_sight("main", candidate.id, attempt.id) is None
