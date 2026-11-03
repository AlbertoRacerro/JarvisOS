import sqlite3

from fastapi import APIRouter, HTTPException

from app.core.errors import workspace_not_found_http_error
from app.modules.workspaces.models import WorkspaceCreate, WorkspaceRead, WorkspaceRename
from app.modules.workspaces.service import (
    WorkspaceStatusRefused,
    create_workspace,
    get_workspace,
    list_workspaces,
    rename_workspace,
    set_workspace_archived,
)

router = APIRouter(prefix="/workspaces", tags=["workspaces"])


@router.post("", response_model=WorkspaceRead, status_code=201)
def create_workspace_endpoint(payload: WorkspaceCreate) -> WorkspaceRead:
    try:
        return create_workspace(payload)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(status_code=409, detail="Workspace slug already exists.") from exc


@router.get("", response_model=list[WorkspaceRead])
def list_workspaces_endpoint(include_archived: bool = False) -> list[WorkspaceRead]:
    return list_workspaces(include_archived=include_archived)


@router.post("/{workspace_id}/archive", response_model=WorkspaceRead)
def archive_workspace_endpoint(workspace_id: str) -> WorkspaceRead:
    try:
        return set_workspace_archived(workspace_id, True)
    except WorkspaceStatusRefused as error:
        raise HTTPException(error.status, detail=str(error)) from error


@router.post("/{workspace_id}/rename", response_model=WorkspaceRead)
def rename_workspace_endpoint(workspace_id: str, payload: WorkspaceRename) -> WorkspaceRead:
    try:
        return rename_workspace(workspace_id, payload.name)
    except WorkspaceStatusRefused as error:
        raise HTTPException(error.status, detail=str(error)) from error


@router.post("/{workspace_id}/restore", response_model=WorkspaceRead)
def restore_workspace_endpoint(workspace_id: str) -> WorkspaceRead:
    try:
        return set_workspace_archived(workspace_id, False)
    except WorkspaceStatusRefused as error:
        raise HTTPException(error.status, detail=str(error)) from error


@router.get("/{workspace_id}", response_model=WorkspaceRead)
def get_workspace_endpoint(workspace_id: str) -> WorkspaceRead:
    workspace = get_workspace(workspace_id)
    if workspace is None:
        raise workspace_not_found_http_error()
    return workspace
