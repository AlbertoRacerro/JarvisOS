from __future__ import annotations

import hashlib
import json
from pathlib import Path

from app.modules.ai.context_builder import ContextSelectionSpec, build_workspace_context_bundle
from app.modules.bluecad.evidence import EvidenceRecord, evidence_pack_line, record_validation_evidence
from app.modules.bluecad.ledger import create_candidate_record, register_artifact, start_attempt
from app.modules.bluecad.models import BluecadLoopConfig
from app.modules.modeling.models import RequirementCreate
from app.modules.modeling.service import create_requirement


def _init() -> None:
    from app.core.bootstrap import initialize_storage

    initialize_storage(seed_default=True)


def _artifact(tmp_path: Path) -> str:
    path = tmp_path / "report.json"
    path.write_text('{"verdict":"pass"}\n', encoding="utf-8")
    return register_artifact("main", path, role="bluecad_report", source_ref="test")


def _record(kind: str, metrics: dict[str, object]) -> EvidenceRecord:
    return EvidenceRecord(
        id="abcdef123456",
        workspace_id="main",
        kind=kind,  # type: ignore[arg-type]
        verdict="pass",
        metrics_json=json.dumps(metrics, sort_keys=True, separators=(",", ":")),
        source_run_id=None,
        candidate_id=None,
        attempt_id=None,
        report_artifact_id="1234567890",
        created_at="2026-01-01T00:00:00Z",
    )


def test_evidence_pack_lines_are_bounded_for_each_kind() -> None:
    records = [
        _record("mesh_quality_v0", {"elements_total": 10, "nodes_total": 8, "empty_groups": [], "attempts": 1}),
        _record("fem_static_v0", {"max_displacement_value": 2.41, "max_von_mises_value": 118.3, "solver_error_code": None, "t3_checks_total": 4, "t3_checks_failed": 0}),
        _record("validation_v0", {"checks_total": 3, "checks_failed": 0, "tier_max": 2, "errors_total": 0}),
    ]
    for record in records:
        assert len(evidence_pack_line(record)) <= 300


def test_evidence_pack_line_is_deterministic_and_digest_stable() -> None:
    record = _record("validation_v0", {"checks_total": 3, "checks_failed": 1, "tier_max": 2, "errors_total": 0})
    first = evidence_pack_line(record)
    second = evidence_pack_line(record)
    assert first == second
    assert hashlib.sha256((first + "\n" + second).encode()).hexdigest() == hashlib.sha256((first + "\n" + second).encode()).hexdigest()


def test_evidence_pack_line_truncates_oversized_metrics() -> None:
    metrics = {f"extra_{index:02d}": "x" * 40 for index in range(20)}
    record = _record("validation_v0", {"checks_total": 1, "checks_failed": 0, "tier_max": 1, "errors_total": 0, **metrics})
    line = evidence_pack_line(record)
    assert len(line) <= 300
    assert " ... " in line
    assert "id=abcdef12" in line
    assert "verdict=pass" in line
    assert "src=12345678" in line


def test_context_pack_selection_mixes_evidence_with_existing_kind(tmp_path: Path) -> None:
    _init()
    create_requirement("main", RequirementCreate(statement="Keep validation evidence visible", status="active"))
    report_id = _artifact(tmp_path)
    candidate = create_candidate_record("main", "brief", BluecadLoopConfig())
    attempt = start_attempt(candidate.id, 1, "external:cheap", prompt_version="test")
    record_validation_evidence(
        "main",
        candidate.id,
        attempt.id,
        {"verdict": "pass", "checks": [{"tier": 1, "status": "pass"}], "errors": []},
        report_artifact_id=report_id,
    )

    bundle = build_workspace_context_bundle(
        "main",
        budget_chars=1000,
        selection=ContextSelectionSpec(kinds=["requirement", "evidence"], max_items_per_kind=5),
    )

    assert {block["type"] for block in bundle.blocks} == {"requirement", "evidence"}
    assert all(len(block["content"]) <= 300 for block in bundle.blocks if block["type"] == "evidence")
    assert len(json.dumps(bundle.blocks, sort_keys=True, separators=(",", ":"), ensure_ascii=False)) <= 1000
