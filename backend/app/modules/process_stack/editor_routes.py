"""HTTP routes for the revisioned DWSIM editor."""

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, Body, HTTPException, Query, Request
from fastapi.responses import FileResponse
from starlette.concurrency import run_in_threadpool

from app.modules.process_stack import editor
from app.modules.process_stack.editor_models import (
    CommandResult,
    EditorCaseRead,
    EditorCommand,
    EditorProjectionRead,
    RevisionRead,
)

router = APIRouter(prefix="/workspaces/{workspace_id}/process/dwsim", tags=["dwsim-editor"])


def _error(exc: editor.EditorError) -> HTTPException:
    detail = {"code": exc.code, "message": str(exc)}
    if exc.current_revision is not None:
        detail["current_revision"] = exc.current_revision
    return HTTPException(exc.status, detail=detail)


@router.get("/cases", response_model=list[EditorCaseRead])
def cases(workspace_id: str) -> list[EditorCaseRead]:
    try:
        return editor.list_cases(workspace_id)
    except editor.EditorError as exc:
        raise _error(exc) from exc


@router.post("/cases", response_model=EditorCaseRead)
def create_case(workspace_id: str, name: str = "DWSIM case") -> EditorCaseRead:
    try:
        return editor.create_case(workspace_id, name)
    except editor.EditorError as exc:
        raise _error(exc) from exc


@router.post("/cases/import", response_model=EditorCaseRead)
async def import_case(workspace_id: str, request: Request, filename: str = Query(...)) -> EditorCaseRead:
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > editor.MAX_IMPORT_BYTES:
        raise HTTPException(413, detail={"code": "case_size_invalid", "message": "Uploaded case is larger than 64 MiB"})
    try:
        return await run_in_threadpool(editor.import_case, workspace_id, Path(filename).name, await request.body())
    except editor.EditorError as exc:
        raise _error(exc) from exc


@router.get("/cases/{case_id}", response_model=EditorProjectionRead)
def projection(workspace_id: str, case_id: str) -> EditorProjectionRead:
    try:
        return editor.projection(workspace_id, case_id)
    except editor.EditorError as exc:
        raise _error(exc) from exc


@router.get("/cases/{case_id}/revisions", response_model=list[RevisionRead])
def revisions(workspace_id: str, case_id: str) -> list[RevisionRead]:
    try:
        return editor.list_revisions(workspace_id, case_id)
    except editor.EditorError as exc:
        raise _error(exc) from exc


@router.get("/cases/{case_id}/revisions/{revision}/download")
def download(workspace_id: str, case_id: str, revision: str) -> FileResponse:
    try:
        path = editor.download_revision(workspace_id, case_id, revision)
    except editor.EditorError as exc:
        raise _error(exc) from exc
    return FileResponse(path, filename=f"{case_id}{path.suffix}", media_type="application/octet-stream")


@router.post("/cases/{case_id}/commands", response_model=CommandResult)
def command(
    workspace_id: str, case_id: str, payload: Annotated[EditorCommand, Body(discriminator="kind")]
) -> dict[str, object]:
    try:
        return editor.execute(workspace_id, case_id, payload)
    except editor.EditorError as exc:
        raise _error(exc) from exc


@router.post("/cases/{case_id}/restore", response_model=CommandResult)
def restore(workspace_id: str, case_id: str, expected_revision: str, source_revision: str) -> dict[str, object]:
    try:
        return editor.restore(workspace_id, case_id, expected_revision, source_revision)
    except editor.EditorError as exc:
        raise _error(exc) from exc
