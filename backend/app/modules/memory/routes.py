import sqlite3

from fastapi import APIRouter, HTTPException, Query

from app.modules.flowsheet.freshness import FreshnessError
from app.modules.flowsheet.service import FlowsheetError
from app.modules.memory.jarvis_knowledge_actions import (
    KnowledgeActionError,
    KnowledgeActionsService,
    KnowledgeContextPreviewRequest,
    KnowledgeProposalRequest,
    KnowledgeRouteId,
    build_knowledge_preview,
    route_capabilities,
)
from app.modules.memory.models import (
    MemoryProposalCreate,
    MemoryRecordKind,
    MemoryRecordRead,
    MemoryStatus,
    ParameterReplacementRead,
)
from app.modules.memory.replacement import ParameterReplacementError
from app.modules.memory.service import (
    create_proposal,
    list_proposals,
    promote_parameter_replacement,
    promote_record,
    reject_record,
)

router = APIRouter(prefix="/memory", tags=["memory"])


def _domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, ParameterReplacementError):
        public_code = (
            "parameter_replacement_not_found" if exc.code == "parameter_replacement_cross_workspace" else exc.code
        )
        if public_code == "parameter_replacement_not_found":
            status_code = 404
        elif public_code in {
            "parameter_already_replaced",
            "parameter_replacement_state_inconsistent",
            "parameter_replacement_promotion_required",
        }:
            status_code = 409
        else:
            status_code = 400
        return HTTPException(
            status_code=status_code,
            detail={"code": public_code, "message": exc.message},
        )
    if isinstance(exc, (FreshnessError, FlowsheetError)):
        if exc.code in {"flowsheet_workspace_not_found", "flowsheet_node_not_found"}:
            status_code = 404
        elif exc.code in {
            "freshness_lineage_incomplete",
            "freshness_path_limit_exceeded",
            "freshness_mark_limit_exceeded",
            "flowsheet_graph_limit_exceeded",
            "flowsheet_diagnostics_limit_exceeded",
        }:
            status_code = 409
        else:
            status_code = 400
        detail: dict[str, object] = {"code": exc.code, "message": exc.message}
        if exc.bound is not None:
            detail["bound"] = exc.bound
        if exc.observed_count is not None:
            detail["observed_count"] = exc.observed_count
        return HTTPException(status_code=status_code, detail=detail)
    if isinstance(exc, ValueError):
        message = str(exc)
        status_code = 404 if "not found" in message.lower() else 400
        return HTTPException(status_code=status_code, detail=message)
    if isinstance(exc, sqlite3.IntegrityError):
        return HTTPException(status_code=400, detail="Related record was not found.")
    return HTTPException(status_code=500, detail="Unexpected persistence error.")


@router.post("/proposals", response_model=MemoryRecordRead, status_code=201)
def create_memory_proposal_endpoint(payload: MemoryProposalCreate) -> MemoryRecordRead:
    try:
        return create_proposal(payload)
    except (
        ValueError,
        sqlite3.IntegrityError,
        ParameterReplacementError,
    ) as exc:
        raise _domain_error(exc) from exc


@router.get("/proposals", response_model=list[MemoryRecordRead])
def list_memory_proposals_endpoint(
    workspace_id: str = Query(min_length=1),
    status: MemoryStatus | None = None,
) -> list[MemoryRecordRead]:
    try:
        return list_proposals(workspace_id, status)
    except ValueError as exc:
        raise _domain_error(exc) from exc


@router.post("/{record_kind}/{record_id}/promote", response_model=MemoryRecordRead)
def promote_memory_record_endpoint(record_kind: MemoryRecordKind, record_id: str) -> MemoryRecordRead:
    try:
        return promote_record(record_kind, record_id)
    except (ValueError, sqlite3.IntegrityError, ParameterReplacementError) as exc:
        raise _domain_error(exc) from exc


@router.post(
    "/parameter/{record_id}/promote-replacement",
    response_model=ParameterReplacementRead,
)
def promote_parameter_replacement_endpoint(record_id: str) -> ParameterReplacementRead:
    try:
        return promote_parameter_replacement(record_id)
    except (
        ValueError,
        sqlite3.IntegrityError,
        ParameterReplacementError,
        FreshnessError,
        FlowsheetError,
    ) as exc:
        raise _domain_error(exc) from exc


@router.post("/{record_kind}/{record_id}/reject", response_model=MemoryRecordRead)
def reject_memory_record_endpoint(record_kind: MemoryRecordKind, record_id: str) -> MemoryRecordRead:
    try:
        return reject_record(record_kind, record_id)
    except (ValueError, sqlite3.IntegrityError) as exc:
        raise _domain_error(exc) from exc


@router.get("/jarvis/capabilities")
def jarvis_knowledge_capabilities(route_id: KnowledgeRouteId) -> dict[str, object]:
    return {"route_id": route_id, "capabilities": route_capabilities(route_id)}


@router.post("/jarvis/context-preview")
def jarvis_knowledge_context_preview(payload: KnowledgeContextPreviewRequest) -> dict[str, object]:
    try:
        return build_knowledge_preview(payload)
    except KnowledgeActionError as exc:
        return {"state": "refused", "reason": exc.reason}


@router.post("/jarvis/propose")
def jarvis_knowledge_propose(payload: KnowledgeProposalRequest) -> dict[str, object]:
    return KnowledgeActionsService().propose(payload)
