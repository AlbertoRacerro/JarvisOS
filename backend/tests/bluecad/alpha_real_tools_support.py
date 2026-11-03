from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from app.core.database import open_sqlite_connection
from app.modules.ai.execution import ProviderBinding
from app.modules.bluecad.export import sha256_file


def analysis_spec(*, element_order: int | None = None) -> dict[str, Any]:
    mesh: dict[str, Any] = {"target_size": 25.0}
    if element_order is not None:
        mesh["element_order"] = element_order
    return {
        "schema_version": "bluecad_analysis_spec_v0_1",
        "analysis_id": "alpha-real-tools",
        "analysis_type": "static",
        "material": {"name": "steel", "E": 200000.0, "nu": 0.3, "rho": 7.8e-9, "yield_strength": 250.0},
        "bcs": [{"port_label": "tube1.port_a", "kind": "fixed"}],
        "loads": [{"port_label": "tube1.port_b", "type": "force_total", "force": [100.0, 0.0, 0.0]}],
        "mesh": mesh,
        "pass_criteria": [
            {"metric": "max_displacement", "op": "<=", "value": 100.0},
            {"metric": "max_von_mises", "op": "<=", "value": 1_000_000.0},
        ],
        "timeout_s": 120.0,
    }


def offline_bindings() -> dict[str, ProviderBinding]:
    return {
        route: ProviderBinding(
            route,
            "scaleway",
            "scripted-alpha-proof",
            False,
            4000,
            execution_class="synthetic",
            context_window_tokens=8192,
        )
        for route in ("external:cheap", "external:reasoning")
    }


def _artifact_rows(candidate_id: str) -> list[dict[str, Any]]:
    with open_sqlite_connection() as connection:
        rows = connection.execute(
            "SELECT filename, stored_path, artifact_type, sha256 FROM artifacts WHERE source_ref LIKE ? ORDER BY created_at, id",
            (f"bluecad_candidate:{candidate_id}:%",),
        ).fetchall()
    return [dict(row) for row in rows]


def _evidence_rows(candidate_id: str) -> list[dict[str, Any]]:
    with open_sqlite_connection() as connection:
        rows = connection.execute(
            "SELECT kind, verdict, candidate_id, attempt_id, source_run_id, report_artifact_id FROM evidence_records WHERE candidate_id = ? ORDER BY created_at, id",
            (candidate_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _simulation_row(candidate_id: str) -> dict[str, Any]:
    with open_sqlite_connection() as connection:
        rows = connection.execute(
            "SELECT status, input_payload, output_payload FROM simulation_runs ORDER BY created_at, id"
        ).fetchall()
    for row in rows:
        if json.loads(row["input_payload"] or "{}").get("candidate_id") == candidate_id:
            return dict(row)
    raise AssertionError(f"simulation run missing for candidate {candidate_id}")


def _simulation_reports(artifacts: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        row["filename"]: json.loads(Path(row["stored_path"]).read_text(encoding="utf-8"))
        for row in artifacts
        if row["artifact_type"] == "bluecad_sim_report"
    }


def assert_full_chain(candidate: Any, *, expected_volume_element_type: str | None = None) -> Path:
    assert candidate.status == "valid"
    assert len(candidate.attempts) == 1
    attempt = candidate.attempts[0]
    assert (attempt.proposal_outcome, attempt.build_outcome, attempt.validation_verdict) == ("ok", "ok", "pass")

    artifacts = _artifact_rows(candidate.id)
    reports = _simulation_reports(artifacts)
    evidence = _evidence_rows(candidate.id)
    simulation = _simulation_row(candidate.id)
    actual_evidence = [(row["kind"], row["verdict"]) for row in evidence]
    expected_evidence = [
        ("validation_v0", "pass"),
        ("mesh_quality_v0", "pass"),
        ("fem_static_v0", "pass"),
    ]
    assert actual_evidence == expected_evidence, {
        "candidate_id": candidate.id,
        "evidence": evidence,
        "simulation": simulation,
        "reports": reports,
        "artifacts": artifacts,
    }
    assert all(row["candidate_id"] == candidate.id and row["attempt_id"] == attempt.id for row in evidence)
    assert all(row["source_run_id"] and row["report_artifact_id"] for row in evidence[1:])

    assert simulation["status"] == "completed"
    assert json.loads(simulation["output_payload"]) == {
        "status": "completed",
        "mesh_verdict": "pass",
        "fem_verdict": "pass",
    }

    roles = [row["artifact_type"] for row in artifacts]
    for role, expected in {
        "bluecad_spec": 1,
        "bluecad_report": 1,
        "bluecad_manifest": 1,
        "bluecad_glb": 1,
        "bluecad_sim_report": 2,
    }.items():
        assert roles.count(role) == expected
    for row in artifacts:
        path = Path(row["stored_path"])
        assert path.is_file() and path.stat().st_size > 0
        assert sha256_file(path) == row["sha256"]

    mesh = reports["mesh_result.json"]
    fem = reports["fem_result.json"]["result_summary"]
    assert mesh["attempts"][0]["gmsh_returncode"] == 0
    counts = mesh["attempts"][0]["counts"]
    assert counts["nodes_total"] > 0 and counts["elements_total"] > 0
    assert all(count > 0 for count in counts["physical_groups"].values())
    if expected_volume_element_type is not None:
        assert set(counts["volume_element_types"]) == {expected_volume_element_type}
        assert counts["volume_element_types"][expected_volume_element_type] == counts["elements_total"]
    assert fem["solver"]["returncode"] == 0
    assert not str(fem["solver"]["version"]).lower().startswith("fake")
    for metric in ("max_displacement", "max_von_mises"):
        value = float(fem[metric]["value"])
        assert 0.0 <= value < float("inf")

    return Path(next(row["stored_path"] for row in artifacts if row["artifact_type"] == "bluecad_manifest"))
