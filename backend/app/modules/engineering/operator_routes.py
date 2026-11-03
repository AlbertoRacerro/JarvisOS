"""HTTP surface for operator-driven engineering studies and evidence."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request

from app.modules.ai.context_builder import canonical_digest
from app.modules.engineering.multifidelity import MultifidelityStudyRun, escalate_study
from app.modules.engineering.operator_models import CapabilityRead, EscalationRequest, EvaluatorRead
from app.modules.engineering.operator_service import (
    DIGEST,
    MAX_STUDY_BUDGET,
    RecordConflictError,
    capability_reads,
    digest_file_name,
    evaluator_reads,
    evaluator_registry,
    read_record,
    record_paths,
    study_dir,
    write_once,
)
from app.modules.engineering.process_cad_handoff import ProcessDesignEnvelope, process_design_envelope
from app.modules.engineering.studies import StudyDefinition, StudyRun, run_study

router = APIRouter(prefix="/workspaces/{workspace_id}/engineering", tags=["engineering"])


def _registry(request: Request) -> dict[str, Any]:
    override = getattr(request.app.state, "engineering_evaluator_registry", None)
    return override if override is not None else evaluator_registry()


def _directory(workspace_id: str, study_id: str):
    try:
        return study_dir(workspace_id, study_id)
    except LookupError as exc:
        raise HTTPException(404, detail={"code": "workspace_not_found"}) from exc
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "study_id_invalid"}) from exc


def _persist(path, model: Any) -> None:
    try:
        write_once(path, model)
    except RecordConflictError as exc:
        raise HTTPException(409, detail={"code": "engineering_record_conflict"}) from exc


@router.get("/evaluators", response_model=list[EvaluatorRead])
def list_evaluators(workspace_id: str, request: Request) -> list[EvaluatorRead]:
    _directory(workspace_id, "validation")
    return evaluator_reads(_registry(request))


@router.get("/capabilities", response_model=list[CapabilityRead])
def list_capabilities(workspace_id: str, request: Request) -> list[CapabilityRead]:
    _directory(workspace_id, "validation")
    return capability_reads(_registry(request), request.app.state)


@router.post("/studies", response_model=StudyRun)
def create_study(workspace_id: str, definition: StudyDefinition, request: Request) -> StudyRun:
    _directory(workspace_id, definition.study_ref.object_id)
    if definition.study_ref.workspace_id != workspace_id or definition.subject_ref.workspace_id != workspace_id:
        raise HTTPException(404, detail={"code": "workspace_scope_mismatch"})
    if definition.budget > MAX_STUDY_BUDGET:
        raise HTTPException(422, detail={"code": "study_budget_exceeded", "maximum": MAX_STUDY_BUDGET})
    evaluator = _registry(request).get(definition.evaluator_id)
    if evaluator is None:
        raise HTTPException(404, detail={"code": "evaluator_not_found"})
    run = run_study(definition, evaluator)
    base = _directory(workspace_id, definition.study_ref.object_id)
    _persist(base / "definitions" / digest_file_name(canonical_digest(definition.model_dump(mode="json"))), definition)
    _persist(base / "runs" / digest_file_name(run.content_digest), run)
    return run


@router.get("/studies")
def list_studies(workspace_id: str) -> list[str]:
    directory = _directory(workspace_id, "validation").parent
    return sorted(path.name for path in directory.iterdir() if path.is_dir()) if directory.is_dir() else []


@router.get("/studies/{study_id}/runs", response_model=list[StudyRun])
def list_runs(workspace_id: str, study_id: str) -> list[StudyRun]:
    directory = _directory(workspace_id, study_id) / "runs"
    return [read_record(path, StudyRun) for path in record_paths(directory, "")]


@router.get("/studies/{study_id}/runs/{digest}", response_model=StudyRun)
def get_run(workspace_id: str, study_id: str, digest: str) -> StudyRun:
    if not DIGEST.fullmatch(digest):
        raise HTTPException(404, detail={"code": "study_run_not_found"})
    result = read_record(_directory(workspace_id, study_id) / "runs" / digest_file_name(digest), StudyRun)
    if result is None:
        raise HTTPException(404, detail={"code": "study_run_not_found"})
    return result


@router.post("/studies/{study_id}/runs/{digest}/envelope", response_model=ProcessDesignEnvelope)
def create_envelope(workspace_id: str, study_id: str, digest: str, point_index: int) -> ProcessDesignEnvelope:
    run = get_run(workspace_id, study_id, digest)
    try:
        envelope = process_design_envelope(run, point_index)
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "envelope_point_invalid", "message": str(exc)}) from exc
    _persist(_directory(workspace_id, study_id) / "envelopes" / digest_file_name(envelope.envelope_digest), envelope)
    return envelope


@router.get("/studies/{study_id}/runs/{digest}/envelopes/{envelope_digest}", response_model=ProcessDesignEnvelope)
def get_envelope(workspace_id: str, study_id: str, digest: str, envelope_digest: str) -> ProcessDesignEnvelope:
    if not DIGEST.fullmatch(envelope_digest):
        raise HTTPException(404, detail={"code": "envelope_not_found"})
    _ = get_run(workspace_id, study_id, digest)
    envelope = read_record(_directory(workspace_id, study_id) / "envelopes" / digest_file_name(envelope_digest), ProcessDesignEnvelope)
    if envelope is None or envelope.content_digest != digest:
        raise HTTPException(404, detail={"code": "envelope_not_found"})
    return envelope


@router.post("/studies/{study_id}/runs/{digest}/escalations", response_model=MultifidelityStudyRun)
def create_escalation(
    workspace_id: str, study_id: str, digest: str, payload: EscalationRequest, request: Request
) -> MultifidelityStudyRun:
    run = get_run(workspace_id, study_id, digest)
    definition = read_record(
        _directory(workspace_id, study_id) / "definitions" / digest_file_name(run.definition_digest), StudyDefinition
    )
    if definition is None:
        raise HTTPException(404, detail={"code": "study_definition_not_found"})
    if definition.study_ref.workspace_id != workspace_id or definition.study_ref.object_id != study_id:
        raise HTTPException(404, detail={"code": "workspace_scope_mismatch"})
    if definition.evaluator_id != run.evaluator_id or definition.study_ref != run.study_ref:
        raise HTTPException(422, detail={"code": "study_definition_mismatch"})
    registry = _registry(request)
    unknown = sorted(item for item in payload.evaluator_ids if item not in registry)
    if unknown:
        raise HTTPException(404, detail={"code": "evaluator_not_found", "evaluator_ids": unknown})
    evaluators = tuple(registry[item] for item in payload.evaluator_ids)
    result = escalate_study(definition, run, evaluators, payload.policy)
    _persist(_directory(workspace_id, study_id) / "escalations" / digest_file_name(result.content_digest), result)
    return result


@router.get("/studies/{study_id}/runs/{digest}/escalations", response_model=list[MultifidelityStudyRun])
def list_escalations(workspace_id: str, study_id: str, digest: str) -> list[MultifidelityStudyRun]:
    _ = get_run(workspace_id, study_id, digest)
    directory = _directory(workspace_id, study_id) / "escalations"
    rows = [read_record(path, MultifidelityStudyRun) for path in record_paths(directory, "")]
    return [row for row in rows if row.base_run_digest == digest]
