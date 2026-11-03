"""102 ENGINEERING-EVIDENCE-CONTRACT-1: fidelity/provenance/qualification contracts and 044 bridge.

Regenerate the frozen schema snapshot only for an accepted K-owned contract change:
    cd backend && python -m tests.test_engineering_evidence_contracts --write-snapshot
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.modules.ai.jarvis_context_models import SourceRef
from app.modules.engineering import evidence_contracts as ev
from app.modules.engineering.refs import ValidityEnvelopeRef

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "engineering_evidence_v1.schema.json"
NOW = datetime(2026, 9, 24, 8, 0, tzinfo=UTC)
BENCH = {"authority_owner": "bluecad_evidence", "object_type": "evidence_record", "object_id": "e1",
         "workspace_id": "main", "content_digest": "sha256:" + "c" * 64}
DECISION = {"authority_owner": "project_knowledge", "object_type": "promotion", "object_id": "promo-1",
            "workspace_id": "main", "revision": "1"}
CAL = {"authority_owner": "project_knowledge", "object_type": "record", "object_id": "cal-1",
       "workspace_id": "main", "revision": "3"}


def current_snapshot() -> dict[str, object]:
    return {
        "version": ev.ENGINEERING_EVIDENCE_VERSION,
        "fidelity_order": list(ev.FIDELITY_ORDER),
        "schemas": {
            name: model.model_json_schema()
            for name, model in (
                ("ProvenanceEntry", ev.ProvenanceEntry),
                ("QualificationBasis", ev.QualificationBasis),
                ("ScientificQualificationRecord", ev.ScientificQualificationRecord),
            )
        },
    }


def _validity(status: str, **overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "authority_owner": "evidence", "object_id": "venv-1", "workspace_id": "main", "revision": "1",
        "qualification_status": status,
        "evidence_refs": [BENCH] if status not in {"unqualified", "candidate"} else [],
        "domain": [{"variable": "T", "lower": {"value": 288.15, "unit": "K"}, "upper": {"value": 303.15, "unit": "K"}}],
    }
    fields.update(overrides)
    envelope = ValidityEnvelopeRef.model_validate(fields)
    return fields | {"content_digest": ev.validity_content_digest(envelope)}


def _record(status: str = "candidate", **overrides: object) -> ev.ScientificQualificationRecord:
    fields: dict[str, object] = {
        "record_id": "qual-pbr-1",
        "subject_ref": {"authority_owner": "process_kernel", "object_type": "dynamic_model", "object_id": "pbr-growth",
                        "workspace_id": "main", "revision": "7"},
        "fidelity": "dynamic_detailed",
        "validity": _validity(status),
        "provenance": [{"kind": "literature", "citation": "Author et al. 2020, growth kinetics",
                        "organism": "Nannochloropsis gaditana", "strain": "CCMP526",
                        "conditions": [{"variable": "light", "value": {"value": 200, "unit": "1"}}]}],
        "uncertainty": [{"variable": "mu_max", "relative": 0.15, "confidence_level": 0.95}],
        "recorded_at": NOW,
    }
    fields.update(overrides)
    return ev.ScientificQualificationRecord.model_validate(fields)


def test_frozen_evidence_schema_snapshot_has_not_drifted() -> None:
    assert SNAPSHOT_PATH.exists(), "run: python -m tests.test_engineering_evidence_contracts --write-snapshot"
    frozen = json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert frozen == json.loads(json.dumps(current_snapshot()))


def test_qualification_record_round_trips_with_typed_subject() -> None:
    record = _record()
    assert type(record.subject_ref).__name__ == "DynamicModelRef"
    assert ev.ScientificQualificationRecord.model_validate_json(record.model_dump_json()) == record


def test_subject_must_be_a_qualifiable_engineering_kind() -> None:
    with pytest.raises(ValidationError):
        _record(subject_ref={"authority_owner": "bluecad", "object_type": "mesh_artifact", "object_id": "m",
                             "workspace_id": "main", "revision": "1"})
    with pytest.raises(ValidationError):
        _record(subject_ref={"authority_owner": "process_kernel", "object_type": "dynamic_model",
                             "object_id": "pbr", "workspace_id": "other", "revision": "1"})


def test_validity_status_is_pinned_to_the_envelope_identity() -> None:
    pinned = _validity("benchmarked")
    with pytest.raises(ValidationError):
        _record("benchmarked", validity=pinned | {"qualification_status": "qualified"},
                basis={"benchmark_refs": [BENCH], "decided_by": "operator", "decided_at": NOW, "decision_ref": DECISION})
    with pytest.raises(ValidationError):
        _record("candidate", validity={k: v for k, v in _validity("candidate").items() if k != "content_digest"})


def test_physics_case_and_material_assumptions_are_qualifiable() -> None:
    case = {"authority_owner": "bluecad", "object_type": "physics_case", "object_id": "k-epsilon-case",
            "workspace_id": "main", "revision": "4"}
    assert type(_record(subject_ref=case, fidelity="field_resolved").subject_ref).__name__ == "PhysicsCaseRef"
    material = case | {"object_type": "material_state", "object_id": "broth-25C"}
    assert type(_record(subject_ref=material).subject_ref).__name__ == "MaterialStateRef"


def test_there_is_no_default_status_and_nothing_is_qualified_without_promotion() -> None:
    with pytest.raises(ValidationError):
        _record("qualified", basis={"benchmark_refs": [BENCH]})
    promoted = _record("qualified", basis={"benchmark_refs": [BENCH], "decided_by": "operator", "decided_at": NOW, "decision_ref": DECISION})
    assert promoted.validity.qualification_status == "qualified"
    with pytest.raises(ValidationError):
        _record("qualified", basis={"benchmark_refs": [BENCH], "decided_by": "model", "decided_at": NOW, "decision_ref": DECISION})
    with pytest.raises(ValidationError):
        _record("qualified", basis={"decided_by": "operator", "decided_at": NOW, "decision_ref": DECISION})


@pytest.mark.parametrize(
    "status, basis, ok",
    [
        ("calibrated", {}, False),
        ("calibrated", {"calibration_refs": [CAL]}, True),
        ("benchmarked", {"calibration_refs": [CAL]}, False),
        ("benchmarked", {"benchmark_refs": [BENCH]}, True),
        ("candidate", {"decided_by": "operator", "decided_at": NOW, "decision_ref": DECISION}, False),
    ],
)
def test_status_requires_matching_evidence(status: str, basis: dict[str, object], ok: bool) -> None:
    if ok:
        _record(status, basis=basis)
    else:
        with pytest.raises(ValidationError):
            _record(status, basis=basis)


def test_synthetic_fixtures_never_support_status_above_candidate() -> None:
    synthetic = [{"kind": "synthetic_fixture", "note": "demo data"}]
    _record("candidate", provenance=synthetic)
    with pytest.raises(ValidationError):
        _record("benchmarked", provenance=synthetic, basis={"benchmark_refs": [BENCH]})


def test_provenance_is_traceable() -> None:
    with pytest.raises(ValidationError):
        ev.ProvenanceEntry(kind="literature")
    with pytest.raises(ValidationError):
        ev.ProvenanceEntry(kind="measurement", citation="lab run 4", strain="CCMP526")
    ev.ProvenanceEntry(kind="expert_estimate", note="initial guess")
    with pytest.raises(ValidationError):
        ev.ProvenanceEntry.model_validate({"kind": "measurement", "citation": "x",
                                           "conditions": [{"variable": "T", "value": {"value": 1, "unit": "furlongz"}}]})
    with pytest.raises(ValidationError):
        ev.QualificationBasis(decided_by="operator")
    with pytest.raises(ValidationError):
        ev.QualificationBasis(decided_by="operator", decided_at=NOW)


def test_fidelity_order_is_total_and_047_screening_is_lowest() -> None:
    assert [ev.fidelity_rank(tier) for tier in ev.FIDELITY_ORDER] == list(range(len(ev.FIDELITY_ORDER)))
    assert ev.fidelity_rank("screening") < ev.fidelity_rank("field_resolved")
    with pytest.raises(ValidationError):
        _record(fidelity="M0_static_screening")


def test_evidence_record_ref_pins_the_044_row_and_detects_relink(tmp_path: Path) -> None:
    from app.core.bootstrap import initialize_storage
    from app.core.database import open_sqlite_connection
    from app.modules.bluecad.evidence import EvidenceRecordCreate, create_evidence_record, get_evidence_record
    from app.modules.bluecad.ledger import register_artifact

    initialize_storage(seed_default=True)
    report = tmp_path / "report.json"
    report.write_text('{"verdict":"pass"}\n', encoding="utf-8")
    artifact_id = register_artifact("main", report, role="bluecad_report", source_ref="test")
    record_id = create_evidence_record(
        EvidenceRecordCreate(
            workspace_id="main", kind="validation_v0", verdict="pass", metrics_json='{"checks_total":1}',
            report_artifact_id=artifact_id,
        )
    ).id
    record = get_evidence_record(record_id)
    assert record is not None
    ref = ev.evidence_record_ref(record)
    assert (ref.authority_owner, ref.object_type, ref.object_id, ref.workspace_id) == (
        "bluecad_evidence", "evidence_record", record_id, "main")
    assert isinstance(ref, SourceRef) and ref.to_jarvis_exact_ref().content_digest == ref.content_digest
    assert ev.evidence_record_ref(record) == ref
    with open_sqlite_connection() as connection:
        connection.execute("UPDATE evidence_records SET verdict = 'fail' WHERE id = ?", (record_id,))
        connection.commit()
    relinked = get_evidence_record(record_id)
    assert relinked is not None and ev.evidence_record_ref(relinked).content_digest != ref.content_digest


if __name__ == "__main__" and "--write-snapshot" in sys.argv:
    SNAPSHOT_PATH.write_text(json.dumps(current_snapshot(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {SNAPSHOT_PATH}")
