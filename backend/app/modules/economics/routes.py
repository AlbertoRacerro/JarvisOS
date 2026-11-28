"""Workspace techno-economic analysis REST routes (spec 393)."""
from __future__ import annotations

from fastapi import APIRouter

from app.core.database import open_sqlite_connection
from app.modules.economics.models import (
    EconomicsEvaluation,
    EconomicsModelInput,
    WorkspaceEconomicsResponse,
)
from app.modules.economics.service import (
    ensure_workspace_exists,
    evaluate_economics,
    get_workspace_economics_response,
    list_workspace_design_components,
    save_workspace_economics,
)
from app.modules.events.service import log_event

router = APIRouter(prefix="/workspaces/{workspace_id}/economics", tags=["economics"])


@router.get("", response_model=WorkspaceEconomicsResponse)
def get_workspace_economics(workspace_id: str) -> WorkspaceEconomicsResponse:
    """Return the workspace's stored economics model and deterministic TEA evaluation."""
    with open_sqlite_connection(read_only=True) as connection:
        return get_workspace_economics_response(connection, workspace_id)


@router.put("", response_model=WorkspaceEconomicsResponse)
def put_workspace_economics(
    workspace_id: str,
    payload: EconomicsModelInput,
) -> WorkspaceEconomicsResponse:
    """Persist the workspace's economics model atomically and return its TEA evaluation."""
    with open_sqlite_connection() as connection:
        ensure_workspace_exists(connection, workspace_id)
        model, updated_at = save_workspace_economics(workspace_id, payload)
        components = list_workspace_design_components(connection, workspace_id)
        evaluation = evaluate_economics(model, design_components=components)
        log_event(
            connection,
            event_type="EconomicsModelUpdated",
            actor="local-user",
            target_type="WorkspaceEconomics",
            target_id=workspace_id,
            workspace_id=workspace_id,
            payload={
                "capex_count": len(model.capex_items),
                "opex_count": len(model.opex_items),
                "total_annualized_cost": evaluation.total_annualized_cost,
                "is_complete": evaluation.is_complete,
            },
        )
        connection.commit()
        return WorkspaceEconomicsResponse(
            workspace_id=workspace_id,
            model=model,
            evaluation=evaluation,
            updated_at=updated_at,
        )


@router.post("/evaluate", response_model=EconomicsEvaluation)
def evaluate_workspace_economics(
    workspace_id: str,
    payload: EconomicsModelInput,
) -> EconomicsEvaluation:
    """Stateless dry-run TEA evaluation against the workspace's Design components."""
    with open_sqlite_connection(read_only=True) as connection:
        ensure_workspace_exists(connection, workspace_id)
        components = list_workspace_design_components(connection, workspace_id)
        return evaluate_economics(payload, design_components=components)
