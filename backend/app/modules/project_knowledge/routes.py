import sqlite3

from fastapi import APIRouter, HTTPException

from app.modules.memory.replacement import ParameterReplacementError
from app.modules.modeling.parameter_lifecycle import ParameterLifecycleError
from app.modules.modeling.project_knowledge_owner import ProjectKnowledgeOwnerError
from app.modules.project_knowledge.apply import ProjectBasisApplyError
from app.modules.project_knowledge.models import (
    ApprovalRead,
    ApprovalRequest,
    DraftCreate,
    DraftRead,
    DraftUpdate,
    ImpactPreview,
    ReconcileRead,
    ReconcileRequest,
    RevalidationRead,
    RevisionStateCommand,
    ScalarAdmissionRequest,
    ScalarResultRead,
    SnapshotRead,
    ValidationRead,
    ValidationRequest,
    WorkingRevisionRead,
)
from app.modules.project_knowledge.revision_lifecycle import change_revision_state
from app.modules.project_knowledge.service import (
    ProjectKnowledgeError,
    admit_scalar_result,
    approve_draft,
    create_draft,
    evaluate_requirement,
    get_draft,
    get_revision,
    get_snapshot,
    list_revisions,
    preview_impact,
    reconcile,
    revalidation_status,
    update_draft,
)

router = APIRouter(prefix="/project-knowledge", tags=["project-knowledge"])


def _http_error(exc: Exception) -> HTTPException:
    code = getattr(exc, "code", "project_knowledge_error")
    message = getattr(exc, "message", str(exc))
    conflict_codes = {
        "draft_stale",
        "owner_stale",
        "preview_stale",
        "approval_idempotency_conflict",
        "reconciliation_idempotency_conflict",
        "validation_predecessor_stale",
        "validation_set_stale",
        "target_digest_stale",
        "target_snapshot_stale",
        "applicability_stale",
        "revision_stale",
        "revision_not_working",
        "supersede_successor_not_direct",
    }
    missing_codes = {
        "workspace_not_found",
        "draft_not_found",
        "revision_not_found",
        "working_revision_missing",
        "snapshot_missing",
        "owner_not_found",
        "parameter_not_found",
        "scalar_run_missing",
    }
    status_code = 409 if code in conflict_codes else 404 if code in missing_codes else 400
    if isinstance(exc, sqlite3.IntegrityError):
        status_code = 409
        code = "persistence_conflict"
        message = "Persistence constraint rejected the requested state transition."
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


_DOMAIN_ERRORS = (
    ProjectKnowledgeError,
    ProjectBasisApplyError,
    ProjectKnowledgeOwnerError,
    ParameterLifecycleError,
    ParameterReplacementError,
    sqlite3.IntegrityError,
)


@router.post("/drafts", response_model=DraftRead, status_code=201)
def create_draft_endpoint(payload: DraftCreate) -> DraftRead:
    try:
        return create_draft(payload)
    except _DOMAIN_ERRORS as exc:
        raise _http_error(exc) from exc


@router.get("/workspaces/{workspace_id}/drafts/{draft_id}", response_model=DraftRead)
def get_draft_endpoint(workspace_id: str, draft_id: str) -> DraftRead:
    try:
        return get_draft(workspace_id, draft_id)
    except _DOMAIN_ERRORS as exc:
        raise _http_error(exc) from exc


@router.put("/drafts/{draft_id}", response_model=DraftRead)
def update_draft_endpoint(draft_id: str, payload: DraftUpdate) -> DraftRead:
    try:
        return update_draft(draft_id, payload)
    except _DOMAIN_ERRORS as exc:
        raise _http_error(exc) from exc


@router.get("/workspaces/{workspace_id}/drafts/{draft_id}/impact", response_model=ImpactPreview)
def preview_impact_endpoint(workspace_id: str, draft_id: str) -> ImpactPreview:
    try:
        return preview_impact(workspace_id, draft_id)
    except _DOMAIN_ERRORS as exc:
        raise _http_error(exc) from exc


@router.post("/approvals", response_model=ApprovalRead)
def approve_draft_endpoint(payload: ApprovalRequest) -> ApprovalRead:
    try:
        return approve_draft(payload)
    except _DOMAIN_ERRORS as exc:
        raise _http_error(exc) from exc


@router.get("/workspaces/{workspace_id}/revisions", response_model=list[WorkingRevisionRead])
def list_revisions_endpoint(workspace_id: str) -> list[WorkingRevisionRead]:
    try:
        return list_revisions(workspace_id)
    except _DOMAIN_ERRORS as exc:
        raise _http_error(exc) from exc


@router.get("/workspaces/{workspace_id}/revisions/{revision_id}", response_model=WorkingRevisionRead)
def get_revision_endpoint(workspace_id: str, revision_id: str) -> WorkingRevisionRead:
    try:
        return get_revision(workspace_id, revision_id)
    except _DOMAIN_ERRORS as exc:
        raise _http_error(exc) from exc


@router.post("/revisions/{revision_id}/state", response_model=WorkingRevisionRead)
def change_revision_state_endpoint(revision_id: str, payload: RevisionStateCommand) -> WorkingRevisionRead:
    try:
        return change_revision_state(revision_id, payload)
    except _DOMAIN_ERRORS as exc:
        raise _http_error(exc) from exc


@router.post("/scalars", response_model=ScalarResultRead, status_code=201)
def admit_scalar_result_endpoint(payload: ScalarAdmissionRequest) -> ScalarResultRead:
    try:
        return admit_scalar_result(payload)
    except _DOMAIN_ERRORS as exc:
        raise _http_error(exc) from exc


@router.post("/validation", response_model=ValidationRead)
def validate_requirement_endpoint(payload: ValidationRequest) -> ValidationRead:
    try:
        return evaluate_requirement(payload)
    except _DOMAIN_ERRORS as exc:
        raise _http_error(exc) from exc


@router.get("/workspaces/{workspace_id}/revisions/{revision_id}/revalidation", response_model=RevalidationRead)
def revalidation_endpoint(workspace_id: str, revision_id: str) -> RevalidationRead:
    try:
        return revalidation_status(workspace_id, revision_id)
    except _DOMAIN_ERRORS as exc:
        raise _http_error(exc) from exc


@router.post("/reconcile", response_model=ReconcileRead)
def reconcile_endpoint(payload: ReconcileRequest) -> ReconcileRead:
    try:
        return reconcile(payload)
    except _DOMAIN_ERRORS as exc:
        raise _http_error(exc) from exc


@router.get("/workspaces/{workspace_id}/snapshots/{snapshot_id}", response_model=SnapshotRead)
def snapshot_endpoint(workspace_id: str, snapshot_id: str) -> SnapshotRead:
    try:
        return get_snapshot(workspace_id, snapshot_id)
    except _DOMAIN_ERRORS as exc:
        raise _http_error(exc) from exc
