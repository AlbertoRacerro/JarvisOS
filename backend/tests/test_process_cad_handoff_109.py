from datetime import UTC, datetime

import pytest

from app.modules.ai.jarvis_context_models import SourceRef
from app.modules.engineering.evaluator_contracts import EvaluationRequestRef, EvaluationResult, NamedQuantity
from app.modules.engineering.process_cad_handoff import (
    PhysicalVerificationResult,
    envelope_to_geometry_spec,
    evaluate_physical_verification,
    process_design_envelope,
)
from app.modules.engineering.refs import (
    DomainBound,
    EvaluationResultRef,
    Quantity,
    StudyRef,
    ValidityEnvelopeRef,
)
from app.modules.engineering.studies import StudyPoint, StudyRun


def _run() -> StudyRun:
    study = StudyRef(authority_owner="main", object_id="pbr-study", workspace_id="pbr", revision="1")
    request_ref = EvaluationRequestRef(authority_owner="main", object_id="point-2", workspace_id="pbr", revision="1")
    result_ref = EvaluationResultRef(authority_owner="main", object_id="result-2", workspace_id="pbr", revision="1")
    validity = ValidityEnvelopeRef(authority_owner="main", object_id="validity", workspace_id="pbr", revision="1",
                                  qualification_status="unqualified")
    result = EvaluationResult(
        result_ref=result_ref, request_ref=request_ref, evaluator_id="photobioreactor.day_night",
        backend_version="test", status="succeeded", fidelity="reduced_order", validity=validity,
        outputs=(), output_artifacts=(SourceRef(authority_owner="test", object_type="artifact", object_id="run",
                                                workspace_id="pbr", revision="1"),),
        completed_at=datetime.now(UTC),
    )
    point = StudyPoint(index=2, inputs=(
        NamedQuantity(name="tube_inner_diameter", value=Quantity(value=0.05, unit="m")),
        NamedQuantity(name="loop_length", value=Quantity(value=100, unit="m")),
    ), request_ref=request_ref, status="succeeded", evaluation=result, feasible=True)
    return StudyRun(study_ref=study, definition_digest="sha256:definition", content_digest="sha256:content",
                    evaluator_id="photobioreactor.day_night", status="succeeded", availability_state="available",
                    points=(point,), feasible_count=1, best_point_index=2, qualification_status="unqualified")


def _bounds() -> tuple[DomainBound, ...]:
    return (
        DomainBound(variable="tube_inner_diameter", lower=Quantity(value=0.04, unit="m"),
                    upper=Quantity(value=0.06, unit="m")),
        DomainBound(variable="loop_length", lower=Quantity(value=90, unit="m"),
                    upper=Quantity(value=110, unit="m")),
    )


def test_envelope_digest_is_reproducible_and_keeps_qualification() -> None:
    first = process_design_envelope(_run(), 2, bounds=_bounds())
    second = process_design_envelope(_run(), 2, bounds=_bounds())
    assert first.envelope_digest == second.envelope_digest
    assert first.qualification_status == "unqualified"
    assert first.validity_ref is not None and first.validity_ref.qualification_status == "unqualified"


def test_geometry_mapping_preserves_process_bore_and_envelope_is_frozen() -> None:
    envelope = process_design_envelope(_run(), 2, bounds=_bounds())
    spec = envelope_to_geometry_spec(envelope)
    params = spec["parts"][0]["params"]
    assert params["outer_d"] - 2 * params["wall_t"] == pytest.approx(50.0)
    assert params["length"] == pytest.approx(100_000)
    with pytest.raises((AttributeError, TypeError, ValueError)):
        envelope.qualification_status = "qualified"


def test_physical_verification_violation_requests_study_reopen_with_evidence() -> None:
    envelope = process_design_envelope(_run(), 2, bounds=_bounds())
    evidence = SourceRef(authority_owner="bluecad", object_type="fem_result", object_id="verification-1",
                        workspace_id="pbr", revision="1")
    tightened = DomainBound(variable="tube_inner_diameter", lower=Quantity(value=0.049, unit="m"),
                            upper=Quantity(value=0.051, unit="m"))
    result = PhysicalVerificationResult(
        result_ref=SourceRef(authority_owner="bluecad", object_type="verification", object_id="v1",
                             workspace_id="pbr", revision="1"),
        status="failed", measured_quantities=(
            ("tube_inner_diameter", Quantity(value=0.061, unit="m")),
            ("loop_length", Quantity(value=100, unit="m")),
        ), evidence_refs=(evidence,), tightened_bounds=(tightened,),
    )
    decision = evaluate_physical_verification(envelope, result)
    assert decision.status == "reopen_requested"
    assert decision.reopen_request is not None
    assert decision.reopen_request.violated_quantities == ("tube_inner_diameter",)
    assert decision.reopen_request.evidence_refs == (evidence,)
    assert decision.reopen_request.tightened_bounds == (tightened,)
    assert decision.reopen_request.proposal_only is True


def test_physical_verification_happy_path_is_accepted() -> None:
    envelope = process_design_envelope(_run(), 2, bounds=_bounds())
    result = PhysicalVerificationResult(
        result_ref=SourceRef(authority_owner="bluecad", object_type="verification", object_id="pass",
                             workspace_id="pbr", revision="1"),
        status="passed", measured_quantities=(
            ("tube_inner_diameter", Quantity(value=0.05, unit="m")),
            ("loop_length", Quantity(value=100, unit="m")),
        ), evidence_refs=(SourceRef(authority_owner="bluecad", object_type="tier_validation", object_id="tier1",
                                    workspace_id="pbr", revision="1"),),
    )
    assert evaluate_physical_verification(envelope, result).status == "accepted"


def test_unbounded_envelope_failed_verification_still_reopens_study() -> None:
    envelope = process_design_envelope(_run(), 2)
    assert envelope.bounds == ()
    evidence = SourceRef(authority_owner="bluecad", object_type="fem_result", object_id="verification-2",
                         workspace_id="pbr", revision="1")
    tightened = DomainBound(variable="loop_length", lower=Quantity(value=50, unit="m"),
                            upper=Quantity(value=80, unit="m"))
    result = PhysicalVerificationResult(
        result_ref=SourceRef(authority_owner="bluecad", object_type="verification", object_id="v2",
                             workspace_id="pbr", revision="1"),
        status="failed", measured_quantities=(
            ("tube_inner_diameter", Quantity(value=0.05, unit="m")),
            ("loop_length", Quantity(value=100, unit="m")),
        ), evidence_refs=(evidence,), tightened_bounds=(tightened,),
    )
    decision = evaluate_physical_verification(envelope, result)
    assert decision.status == "reopen_requested"
    assert decision.reopen_request is not None
    assert decision.reopen_request.tightened_bounds == (tightened,)
