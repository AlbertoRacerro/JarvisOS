"""106 ENGINEERING-EVALUATOR-1: evaluator/adapter boundary, failure taxonomy and availability.

Regenerate the frozen schema snapshot only for an accepted K-owned contract change:
    cd backend && python -m tests.test_engineering_evaluator_contracts --write-snapshot
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.modules.engineering import evaluator_contracts as evc

SNAPSHOT_PATH = Path(__file__).parent / "fixtures" / "engineering_evaluator_v1.schema.json"
APP = Path(__file__).resolve().parents[1] / "app" / "modules"
NOW = datetime(2026, 9, 24, 9, 0, tzinfo=UTC)
LATER = NOW + timedelta(minutes=10)
WS = "main"
REQ_REF = {"authority_owner": "process_kernel", "object_type": "evaluation_request", "object_id": "req-1",
           "workspace_id": WS, "revision": "1"}
RES_REF = {"authority_owner": "process_kernel", "object_type": "evaluation_result", "object_id": "res-1",
           "workspace_id": WS, "revision": "1"}
MODEL = {"authority_owner": "process_kernel", "object_type": "dynamic_model", "object_id": "pbr",
         "workspace_id": WS, "revision": "3"}


def current_snapshot() -> dict[str, object]:
    return {
        "version": evc.ENGINEERING_EVALUATOR_VERSION,
        "schemas": {
            name: model.model_json_schema()
            for name, model in (
                ("EvaluatorDescriptor", evc.EvaluatorDescriptor),
                ("EvaluatorAvailability", evc.EvaluatorAvailability),
                ("EvaluationRequest", evc.EvaluationRequest),
                ("EvaluationResult", evc.EvaluationResult),
            )
        },
    }


def _request(**overrides: object) -> evc.EvaluationRequest:
    fields: dict[str, object] = {
        "request_ref": REQ_REF,
        "evaluator_id": "process_kernel.047",
        "subject_ref": MODEL,
        "inputs": [{"name": "velocity", "value": {"value": 0.4, "unit": "m/s"}}],
        "backend_options": {"max_iterations": 200, "tolerance": 1e-8},
        "requested_at": NOW,
        "deadline_at": LATER,
    }
    fields.update(overrides)
    return evc.EvaluationRequest.model_validate(fields)


def _result(status: str = "succeeded", **overrides: object) -> evc.EvaluationResult:
    fields: dict[str, object] = {
        "result_ref": RES_REF,
        "request_ref": REQ_REF,
        "evaluator_id": "process_kernel.047",
        "backend_version": "profile_047@abc",
        "status": status,
        "fidelity": "screening",
        "completed_at": NOW + timedelta(seconds=3),
    }
    if status == "succeeded":
        fields["outputs"] = [{"name": "reynolds_number", "value": {"value": 12000.0, "unit": "1"}}]
    fields.update(overrides)
    return evc.EvaluationResult.model_validate(fields)


def test_frozen_evaluator_schema_snapshot_has_not_drifted() -> None:
    assert SNAPSHOT_PATH.exists(), "run: python -m tests.test_engineering_evaluator_contracts --write-snapshot"
    assert json.loads(SNAPSHOT_PATH.read_text(encoding="utf-8")) == json.loads(json.dumps(current_snapshot()))


def test_request_and_result_round_trip_and_answer_each_other() -> None:
    request, result = _request(), _result()
    assert evc.EvaluationRequest.model_validate_json(request.model_dump_json()) == request
    assert evc.EvaluationResult.model_validate_json(result.model_dump_json()) == result
    evc.validate_evaluation_result(request, result)
    with pytest.raises(evc.EvaluatorContractError):
        evc.validate_evaluation_result(request, _result(evaluator_id="openfoam.v11"))
    with pytest.raises(evc.EvaluatorContractError):
        evc.validate_evaluation_result(request, _result(request_ref=REQ_REF | {"object_id": "req-2"}))
    with pytest.raises(evc.EvaluatorContractError):
        evc.validate_evaluation_result(request, _result(completed_at=LATER + timedelta(seconds=1)))


def test_lower_fidelity_success_does_not_answer_an_escalation() -> None:
    escalation = _request(requested_fidelity="field_resolved")
    with pytest.raises(evc.EvaluatorContractError):
        evc.validate_evaluation_result(escalation, _result(fidelity="screening"))
    evc.validate_evaluation_result(escalation, _result(fidelity="field_resolved"))
    refused = _result("refused", fidelity="screening",
                      failure={"category": "unsupported_request", "backend_code": "FIDELITY_UNAVAILABLE"})
    evc.validate_evaluation_result(escalation, refused)


def test_case_artifact_and_validity_refs_cannot_cross_projects() -> None:
    foreign = {"authority_owner": "artifacts", "object_type": "artifact", "object_id": "a1",
               "workspace_id": "other", "content_digest": "sha256:" + "d" * 64}
    with pytest.raises(ValidationError):
        _request(backend_case_ref=foreign)
    _request(backend_case_ref={k: v for k, v in foreign.items() if k != "workspace_id"})
    with pytest.raises(ValidationError):
        _result("succeeded", output_artifacts=[foreign])
    with pytest.raises(ValidationError):
        _result("succeeded", evidence_refs=[foreign])
    with pytest.raises(ValidationError):
        _result("succeeded", validity={"authority_owner": "evidence", "object_id": "v", "workspace_id": "other",
                                       "revision": "1", "qualification_status": "candidate"})


def test_request_refs_stay_in_one_project_and_inputs_are_typed() -> None:
    with pytest.raises(ValidationError):
        _request(subject_ref=MODEL | {"workspace_id": "other"})
    with pytest.raises(ValidationError):
        _request(subject_ref=MODEL | {"object_type": "study"})
    with pytest.raises(ValidationError):
        _request(inputs=[{"name": "v", "value": {"value": 1, "unit": "m/s"}}] * 2)
    with pytest.raises(ValidationError):
        _request(inputs=[{"name": "v", "value": {"value": 1, "unit": "furlongz"}}])
    with pytest.raises(ValidationError):
        _request(backend_options={"tolerance": float("nan")})
    with pytest.raises(ValidationError):
        _request(deadline_at=NOW)
    assert _request().is_expired(LATER) and not _request().is_expired(NOW)
    mesh = MODEL | {"object_type": "mesh_artifact", "authority_owner": "bluecad"}
    assert type(_request(subject_ref=mesh).subject_ref).__name__ == "MeshArtifactRef"


@pytest.mark.parametrize(
    "status, category, ok",
    [
        ("failed", "did_not_converge", True),
        ("failed", "solver_crash", True),
        ("failed", "not_available", False),
        ("refused", "not_available", True),
        ("refused", "outside_validity_domain", True),
        ("refused", "solver_crash", False),
        ("cancelled", "cancelled", True),
        ("cancelled", "timeout", False),
        ("deadline_exceeded", "timeout", True),
        ("deadline_exceeded", "cancelled", False),
    ],
)
def test_failure_taxonomy_is_consistent_with_status(status: str, category: str, ok: bool) -> None:
    failure = {"category": category, "backend_code": "X_CODE"}
    if ok:
        _result(status, failure=failure)
    else:
        with pytest.raises(ValidationError):
            _result(status, failure=failure)


def test_success_and_failure_shapes() -> None:
    with pytest.raises(ValidationError):
        _result("failed")
    with pytest.raises(ValidationError):
        _result("succeeded", outputs=[])
    with pytest.raises(ValidationError):
        _result("succeeded", failure={"category": "solver_crash", "backend_code": "SOLVE_ERROR"})
    with pytest.raises(ValidationError):
        _result("failed", failure={"category": "solver_crash", "backend_code": "SOLVE_ERROR"},
                outputs=[{"name": "x", "value": {"value": 1, "unit": "1"}}])
    artifact = {"authority_owner": "artifacts", "object_type": "artifact", "object_id": "a1",
                "workspace_id": WS, "content_digest": "sha256:" + "d" * 64}
    _result("succeeded", outputs=[], output_artifacts=[artifact])


def test_convergence_is_a_diagnostic_not_validation() -> None:
    with pytest.raises(ValidationError):
        _result("succeeded", numerical={"converged": False, "final_residual": 1e-2})
    converged = _result("succeeded", numerical={"converged": True, "final_residual": 1e-9, "iterations": 40})
    assert converged.validity is None and converged.qualification_record_ref is None
    _result("failed", failure={"category": "did_not_converge", "backend_code": "MAX_ITER"},
            numerical={"converged": False, "iterations": 200})


def test_availability_is_truthful_and_separate_from_qualification() -> None:
    evc.EvaluatorAvailability(evaluator_id="openfoam.v11", state="available", checked_at=NOW, backend_version="v11")
    with pytest.raises(ValidationError):
        evc.EvaluatorAvailability(evaluator_id="openfoam.v11", state="not_installed", checked_at=NOW)
    evc.EvaluatorAvailability(evaluator_id="openfoam.v11", state="not_installed", checked_at=NOW,
                              reason_code="TOOL_BINARY_MISSING")
    with pytest.raises(ValidationError):
        evc.EvaluatorAvailability(evaluator_id="x", state="available", checked_at=NOW)
    assert "qualification" not in " ".join(evc.EvaluatorAvailability.model_fields)
    descriptor = evc.EvaluatorDescriptor(evaluator_id="calculix.2_22", backend_kind="fem_solver",
                                         backend_name="CalculiX", backend_version="2.22",
                                         tool_registry_id="calculix", fidelity="field_resolved")
    assert descriptor.qualification_record_ref is None


def test_every_existing_backend_code_maps_to_a_specific_category() -> None:
    kernel_codes: set[str] = set()
    for path in (APP / "process_kernel").glob("*.py"):
        kernel_codes |= set(re.findall(r'ProcessKernelError\(\s*"([a-z_]+)"', path.read_text(encoding="utf-8")))
    assert kernel_codes, "process kernel codes not found"
    assert {code: evc.failure_category_for_code(code) for code in kernel_codes if
            evc.failure_category_for_code(code) == "internal_error"} == {}
    assert evc.failure_category_for_code("correlation_not_qualified") == "outside_validity_domain"
    registry_text = (APP / "bluecad" / "registry.py").read_text(encoding="utf-8")
    for code in ("TOOL_UNKNOWN", "TOOL_DISABLED", "TOOL_BINARY_MISSING", "TOOL_HASH_MISMATCH"):
        assert code in registry_text
        assert evc.failure_category_for_code(code) == "not_available"
    assert evc.failure_category_for_code("TIMEOUT") == "timeout"
    assert evc.failure_category_for_code("PARSE_ERROR") == "result_parse_error"
    adapters_text = "".join((APP / "bluecad" / name).read_text(encoding="utf-8")
                            for name in ("fem_adapter.py", "fem_adapter_base.py", "mesh_adapter.py"))
    for code, category in (("SOLVE_DIVERGED", "did_not_converge"), ("MESH_FAIL", "solver_crash"),
                           ("MESH_GROUP_EMPTY", "invalid_input")):
        assert f'"{code}"' in adapters_text
        assert evc.failure_category_for_code(code) == category
    assert evc.failure_category_for_code("totally_new_backend_code") == "internal_error"


if __name__ == "__main__" and "--write-snapshot" in sys.argv:
    SNAPSHOT_PATH.write_text(json.dumps(current_snapshot(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {SNAPSHOT_PATH}")
