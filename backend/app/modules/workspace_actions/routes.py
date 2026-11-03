"""HTTP surface for workspace actions."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.modules.runner.origin import require_runner_origin
from app.modules.workspace_actions import service
from app.modules.workspace_actions.models import ActionOutcome, SurfaceBrief, SurfaceRef
from app.modules.workspaces.service import get_workspace

router = APIRouter(prefix="/workspaces/{workspace_id}/actions", tags=["workspace-actions"])


def _workspace(workspace_id: str) -> None:
    if get_workspace(workspace_id) is None:
        raise HTTPException(status_code=404, detail="Workspace was not found.")


def _error(exc: service.ActionError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail={"code": exc.code, "message": str(exc)})


@router.post("/brief", response_model=SurfaceBrief)
def brief(workspace_id: str, payload: SurfaceRef) -> SurfaceBrief:
    _workspace(workspace_id)
    try:
        return service.surface_brief(workspace_id, payload)
    except service.ActionError as exc:
        raise _error(exc) from exc


@router.get("")
def list_actions(
    workspace_id: str, thread_id: str = Query(...), interaction_id: str | None = None, relay_run_id: str | None = None
) -> dict[str, list[ActionOutcome]]:
    _workspace(workspace_id)
    return {
        "actions": service.list_for(
            workspace_id, thread_id=thread_id, interaction_id=interaction_id, relay_run_id=relay_run_id
        )
    }


@router.get("/{action_id}", response_model=ActionOutcome)
def get_action(workspace_id: str, action_id: str) -> ActionOutcome:
    _workspace(workspace_id)
    try:
        return service.get(workspace_id, action_id)
    except service.ActionError as exc:
        raise _error(exc) from exc


# Apply/dismiss/undo take no body, so a cross-site form POST needs no CORS preflight.
@router.post("/{action_id}/{operation}", response_model=ActionOutcome, dependencies=[Depends(require_runner_origin)])
def operate(workspace_id: str, action_id: str, operation: str) -> ActionOutcome:
    _workspace(workspace_id)
    operation_fn = {"apply": service.apply, "dismiss": service.dismiss, "undo": service.undo}.get(operation)
    if operation_fn is None:
        raise HTTPException(status_code=404, detail="Operation was not found.")
    try:
        outcome = operation_fn(workspace_id, action_id)
        if outcome.state == "stale":
            raise HTTPException(
                status_code=409,
                detail={"code": outcome.reason_code or "stale", "message": outcome.reason or outcome.summary},
            )
        return outcome
    except service.ActionError as exc:
        raise _error(exc) from exc
