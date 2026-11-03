from fastapi import APIRouter, HTTPException

from app.modules.flowsheet.freshness import (
    FreshnessError,
    get_freshness_invalidation,
    get_node_freshness,
)
from app.modules.flowsheet.models import (
    FlowsheetFreshnessInvalidationDetailRead,
    FlowsheetGraphRead,
    FlowsheetNodeFreshnessRead,
    FlowsheetNodeRead,
)
from app.modules.flowsheet.service import FlowsheetError, get_flowsheet_graph, get_flowsheet_node

router = APIRouter(prefix="/workspaces/{workspace_id}/flowsheet", tags=["flowsheet"])


def _flowsheet_error(exc: FlowsheetError | FreshnessError) -> HTTPException:
    if exc.code in {
        "flowsheet_workspace_not_found",
        "flowsheet_node_not_found",
        "freshness_invalidation_not_found",
    }:
        status_code = 404
    elif exc.code == "flowsheet_ref_invalid":
        status_code = 400
    elif exc.code in {
        "flowsheet_graph_limit_exceeded",
        "flowsheet_diagnostics_limit_exceeded",
        "freshness_lineage_incomplete",
        "freshness_path_limit_exceeded",
        "freshness_mark_limit_exceeded",
    }:
        status_code = 409
    else:
        status_code = 500
    detail: dict[str, object] = {"code": exc.code, "message": exc.message}
    if exc.bound is not None:
        detail["bound"] = exc.bound
    if exc.observed_count is not None:
        detail["observed_count"] = exc.observed_count
    return HTTPException(status_code=status_code, detail=detail)


@router.get("/graph", response_model=FlowsheetGraphRead)
def get_flowsheet_graph_endpoint(workspace_id: str) -> FlowsheetGraphRead:
    try:
        return get_flowsheet_graph(workspace_id)
    except FlowsheetError as exc:
        raise _flowsheet_error(exc) from exc


@router.get("/nodes/{node_ref}", response_model=FlowsheetNodeRead)
def get_flowsheet_node_endpoint(workspace_id: str, node_ref: str) -> FlowsheetNodeRead:
    try:
        return get_flowsheet_node(workspace_id, node_ref)
    except FlowsheetError as exc:
        raise _flowsheet_error(exc) from exc


@router.get("/nodes/{node_ref}/freshness", response_model=FlowsheetNodeFreshnessRead)
def get_flowsheet_node_freshness_endpoint(
    workspace_id: str,
    node_ref: str,
) -> FlowsheetNodeFreshnessRead:
    try:
        return get_node_freshness(workspace_id, node_ref)
    except (FlowsheetError, FreshnessError) as exc:
        raise _flowsheet_error(exc) from exc


@router.get(
    "/invalidations/{invalidation_id}",
    response_model=FlowsheetFreshnessInvalidationDetailRead,
)
def get_flowsheet_invalidation_endpoint(
    workspace_id: str,
    invalidation_id: str,
) -> FlowsheetFreshnessInvalidationDetailRead:
    try:
        return get_freshness_invalidation(workspace_id, invalidation_id)
    except FreshnessError as exc:
        raise _flowsheet_error(exc) from exc
