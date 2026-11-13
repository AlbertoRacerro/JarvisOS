import sqlite3

from fastapi import APIRouter, HTTPException

from app.core.database import open_sqlite_connection
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
    SimulationResultBatchRead,
    SimulationResultNotificationRead,
    SimulationResultResolveRequest,
    SimulationResultValueRead,
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
    list_validations,
    preview_impact,
    reconcile,
    revalidation_status,
    update_draft,
)
from app.modules.project_knowledge.sim_results import (
    ingest_validated_calc_v0_run,
    list_simulation_result_batches,
    list_simulation_result_notifications,
    resolve_simulation_result_exception,
)
from app.modules.project_knowledge.value_history import (
    RunInputsManifestRead,
    ValueComparisonRead,
    ValueComparisonRequest,
    ValueHistoryError,
    ValueVersionChainRead,
    compare_value_versions,
    get_parameter_version_chain,
    get_run_inputs_manifest,
    get_sim_result_value_version_chain,
    get_value_version_chain,
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
        "result_batch_not_found",
        "result_value_not_found",
        "runner_simulation_run_not_found",
        "run_not_found",
        "result_value_version_not_found",
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
    ValueHistoryError,
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


@router.get("/workspaces/{workspace_id}/revisions/{revision_id}/validations", response_model=list[ValidationRead])
def list_validations_endpoint(workspace_id: str, revision_id: str) -> list[ValidationRead]:
    try:
        return list_validations(workspace_id, revision_id)
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


@router.get("/workspaces/{workspace_id}/result-batches", response_model=list[SimulationResultBatchRead])
def list_result_batches_endpoint(workspace_id: str) -> list[SimulationResultBatchRead]:
    try:
        return list_simulation_result_batches(workspace_id)
    except _DOMAIN_ERRORS as exc:
        raise _http_error(exc) from exc


@router.post("/workspaces/{workspace_id}/runs/{run_id}/ingest-results", response_model=SimulationResultBatchRead)
def ingest_run_results_endpoint(workspace_id: str, run_id: str) -> SimulationResultBatchRead:
    try:
        batch = ingest_validated_calc_v0_run(workspace_id, run_id)
        if batch is None:
            raise ProjectKnowledgeError(
                "run_not_eligible_for_ingestion",
                "Simulation run is not an eligible succeeded calc_v0 or studio_calc_v1 run.",
            )
        return batch
    except _DOMAIN_ERRORS as exc:
        raise _http_error(exc) from exc


@router.get("/workspaces/{workspace_id}/result-notifications", response_model=list[SimulationResultNotificationRead])
def list_result_notifications_endpoint(workspace_id: str) -> list[SimulationResultNotificationRead]:
    try:
        return list_simulation_result_notifications(workspace_id)
    except _DOMAIN_ERRORS as exc:
        raise _http_error(exc) from exc


@router.post("/result-values/{value_id}/resolve", response_model=SimulationResultValueRead)
def resolve_result_exception_endpoint(value_id: str, payload: SimulationResultResolveRequest) -> SimulationResultValueRead:
    try:
        return resolve_simulation_result_exception(value_id, payload)
    except _DOMAIN_ERRORS as exc:
        raise _http_error(exc) from exc


@router.get(
    "/workspaces/{workspace_id}/runs/{run_id}/manifest",
    response_model=RunInputsManifestRead,
)
@router.get(
    "/workspaces/{workspace_id}/runs/{run_id}/values-used",
    response_model=RunInputsManifestRead,
)
def get_run_inputs_manifest_endpoint(
    workspace_id: str,
    run_id: str,
) -> RunInputsManifestRead:
    with open_sqlite_connection() as connection:
        try:
            return get_run_inputs_manifest(connection, workspace_id, run_id)
        except _DOMAIN_ERRORS as exc:
            raise _http_error(exc) from exc


@router.get(
    "/workspaces/{workspace_id}/values/{value_kind}/{value_id}/history",
    response_model=ValueVersionChainRead,
)
def get_value_history_endpoint(
    workspace_id: str,
    value_kind: str,
    value_id: str,
) -> ValueVersionChainRead:
    with open_sqlite_connection() as connection:
        try:
            return get_value_version_chain(connection, workspace_id, value_kind, value_id)
        except _DOMAIN_ERRORS as exc:
            raise _http_error(exc) from exc


@router.get(
    "/workspaces/{workspace_id}/parameters/{parameter_id}/history",
    response_model=ValueVersionChainRead,
)
def get_parameter_history_endpoint(
    workspace_id: str,
    parameter_id: str,
) -> ValueVersionChainRead:
    with open_sqlite_connection() as connection:
        try:
            return get_parameter_version_chain(connection, workspace_id, parameter_id)
        except _DOMAIN_ERRORS as exc:
            raise _http_error(exc) from exc


@router.get(
    "/workspaces/{workspace_id}/result-values/{value_id}/history",
    response_model=ValueVersionChainRead,
)
@router.get(
    "/result-values/{value_id}/history",
    response_model=ValueVersionChainRead,
)
def get_result_value_history_endpoint(
    value_id: str,
    workspace_id: str = "main",
) -> ValueVersionChainRead:
    with open_sqlite_connection() as connection:
        try:
            return get_sim_result_value_version_chain(connection, workspace_id, value_id)
        except _DOMAIN_ERRORS as exc:
            raise _http_error(exc) from exc


@router.get(
    "/workspaces/{workspace_id}/values/compare",
    response_model=ValueComparisonRead,
)
def compare_values_get_endpoint(
    workspace_id: str,
    value_kind: str | None = None,
    value_id: str | None = None,
    version_a: str | None = None,
    version_b: str | None = None,
    parameter_a_id: str | None = None,
    parameter_b_id: str | None = None,
) -> ValueComparisonRead:
    with open_sqlite_connection() as connection:
        try:
            return compare_value_versions(
                connection,
                workspace_id=workspace_id,
                value_kind=value_kind,
                value_id=value_id,
                version_a=version_a,
                version_b=version_b,
                parameter_a_id=parameter_a_id,
                parameter_b_id=parameter_b_id,
            )
        except _DOMAIN_ERRORS as exc:
            raise _http_error(exc) from exc


@router.post(
    "/workspaces/{workspace_id}/values/compare",
    response_model=ValueComparisonRead,
)
def compare_values_post_endpoint(
    workspace_id: str,
    payload: ValueComparisonRequest,
) -> ValueComparisonRead:
    with open_sqlite_connection() as connection:
        try:
            return compare_value_versions(
                connection,
                workspace_id=workspace_id,
                value_kind=payload.value_kind,
                value_id=payload.value_id,
                version_a=payload.version_a,
                version_b=payload.version_b,
                parameter_a_id=payload.parameter_a_id,
                parameter_b_id=payload.parameter_b_id,
            )
        except _DOMAIN_ERRORS as exc:
            raise _http_error(exc) from exc


@router.get(
    "/workspaces/{workspace_id}/parameters/compare",
    response_model=ValueComparisonRead,
)
def compare_parameters_get_endpoint(
    workspace_id: str,
    version_a: str,
    version_b: str,
) -> ValueComparisonRead:
    with open_sqlite_connection() as connection:
        try:
            return compare_value_versions(
                connection,
                workspace_id=workspace_id,
                value_kind="parameter",
                parameter_a_id=version_a,
                parameter_b_id=version_b,
            )
        except _DOMAIN_ERRORS as exc:
            raise _http_error(exc) from exc


@router.get(
    "/workspaces/{workspace_id}/result-values/{value_id}/compare",
    response_model=ValueComparisonRead,
)
def compare_result_values_get_endpoint(
    workspace_id: str,
    value_id: str,
    version_a: str,
    version_b: str,
) -> ValueComparisonRead:
    with open_sqlite_connection() as connection:
        try:
            return compare_value_versions(
                connection,
                workspace_id=workspace_id,
                value_kind="simulation_result_value",
                value_id=value_id,
                version_a=version_a,
                version_b=version_b,
            )
        except _DOMAIN_ERRORS as exc:
            raise _http_error(exc) from exc

